#!/usr/bin/env python3
"""Local candidate lifecycle. No operation mutates a captured source workspace."""
import argparse
import ast
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

REPO = Path(__file__).resolve().parents[2]
UPSTREAM = 'https://raw.githubusercontent.com/ros2/ros2/humble/ros2.repos'
BOOTSTRAP_PACKAGES = [
    'build-essential', 'cmake', 'git', 'curl', 'ca-certificates', 'pkg-config',
    'locales', 'util-linux', 'python3-dev', 'python3-setuptools', 'python3-wheel',
    'python3-yaml', 'python3-empy', 'python3-numpy', 'python3-pytest',
    'python3-pytest-cov', 'python3-flake8', 'python3-flake8-docstrings',
    'python3-rosdep2', 'vcstool', 'colcon', 'python3-colcon-ros',
    'python3-colcon-cmake', 'python3-colcon-python-setup-py',
    'python3-colcon-parallel-executor', 'python3-colcon-test-result', 'python3-colcon-metadata',
]
REQUIRED_ROS = ['ros2cli', 'demo_nodes_cpp', 'demo_nodes_py',
                'rmw_fastrtps_cpp', 'rmw_cyclonedds_cpp', 'rviz2', 'rqt_gui']


class PipelineError(RuntimeError):
    pass


class CommandError(PipelineError):
    def __init__(self, command, returncode, output=''):
        self.command, self.returncode, self.output = command, returncode, output
        super().__init__(f'Command failed ({returncode}): {shlex.join(command)}\n{output[-4000:]}')


def now():
    return datetime.now(timezone.utc).isoformat()


def unique_id():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    if path.is_symlink():
        raise PipelineError(f'Refusing symlink metadata: {path}')
    tmp = path.with_name(path.name + '.tmp')
    if tmp.is_symlink():
        raise PipelineError(f'Refusing symlink temporary file: {tmp}')
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    tmp.replace(path)


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise PipelineError(f'Cannot read {path}: {e}') from e


def overlap(a, b):
    a, b = Path(a).resolve(), Path(b).resolve()
    return a == b or a in b.parents or b in a.parents


def contained(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def clean_env(home=None):
    return {'HOME': str(home or Path.home()), 'USER': os.environ.get('USER', ''),
            'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
            'TERM': os.environ.get('TERM', 'dumb'), 'PYTHONNOUSERSITE': '1',
            'PYTHONDONTWRITEBYTECODE': '1', 'GIT_OPTIONAL_LOCKS': '0',
            'ROS_OS_OVERRIDE': 'debian:bookworm'}


class Runner:
    def __init__(self, home=None):
        self.env = clean_env(home)
        self.log = None

    def run(self, command, cwd=None, check=True, timeout=None, env=None, show=False):
        command = [str(x) for x in command]
        effective = dict(self.env)
        if env:
            effective.update(env)
        privileged = command[0] == 'sudo'
        if privileged:
            # Authenticate on the caller's terminal before redirecting logs.
            # Keep sudo in this session: tty-scoped credentials cannot be used
            # by a detached process without a controlling terminal.
            auth = subprocess.run(['sudo', '-v'], env=effective)
            if auth.returncode:
                raise CommandError(['sudo', '-v'], auth.returncode,
                                   'Authenticate from an interactive terminal, then retry.')
            command = [command[0], '-n', *command[1:]]
        # Command output goes directly to the stage log while long builds run.
        with (open(self.log, 'a+') if self.log else tempfile.TemporaryFile(mode='w+')) as out:
            out.write(f'\n[{now()}] $ {shlex.join(command)}\n')
            out.flush()
            start = out.tell()
            print(f'>>> {shlex.join(command)}', flush=True)
            proc = subprocess.Popen(command, cwd=cwd, env=effective, stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=not privileged)
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.stop(proc, group=not privileged)
                code = 124
            except BaseException:
                self.stop(proc, group=not privileged)
                raise
            out.seek(start)
            output = out.read()
        if show or code:
            print(output[-12000:], end='' if output.endswith('\n') else '\n', flush=True)
        if check and code:
            raise CommandError(command, code, output)
        return subprocess.CompletedProcess(command, code, output, '')

    @staticmethod
    def stop(proc, group=True):
        if not group:
            # A terminal-attached sudo shares the caller's process group.
            # Never signal that group (which contains the shell and pipeline).
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            return
        try:
            # Terminate the owned session even if its leader exited first.
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=3)
        except ProcessLookupError:
            proc.wait()
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def git(path, *args):
    result = subprocess.run(['git', '-C', str(path), *args], env=clean_env(),
                            text=True, capture_output=True)
    if result.returncode:
        raise CommandError(result.args, result.returncode, result.stderr)
    return result.stdout.strip()


def generated(path):
    p = PurePosixPath(path)
    return '__pycache__' in p.parts or p.suffix in ('.pyc', '.pyo')


def manifest_load(path):
    try:
        import yaml
    except ImportError as e:
        raise PipelineError('Missing python3-yaml; run bootstrap first.') from e

    class StrictLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        result = {}
        for k, v in node.value:
            key = loader.construct_object(k, deep=deep)
            if not isinstance(key, str) or key in result:
                raise PipelineError(f'Duplicate or non-string YAML key: {key!r}')
            result[key] = loader.construct_object(v, deep=deep)
        return result

    StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        doc = yaml.load(Path(path).read_text(), Loader=StrictLoader)
    except (OSError, yaml.YAMLError) as e:
        raise PipelineError(f'Invalid manifest {path}: {e}') from e
    if not isinstance(doc, dict) or set(doc) != {'repositories'}:
        raise PipelineError('Manifest must contain only repositories.')
    repos = doc['repositories']
    if not isinstance(repos, dict) or not repos:
        raise PipelineError('Manifest must contain at least one repository.')
    for name, spec in repos.items():
        p = PurePosixPath(name)
        if (not name or p.is_absolute() or '..' in p.parts or '.' == name or
                '.git' in p.parts or str(p) != name or '\\' in name):
            raise PipelineError(f'Unsafe repository path: {name}')
        if (not isinstance(spec, dict) or set(spec) != {'type', 'url', 'version'} or
                spec['type'] != 'git' or
                not all(isinstance(spec[k], str) and spec[k] and not spec[k].startswith('-')
                        for k in ('url', 'version'))):
            raise PipelineError(f'Invalid git specification: {name}')
    names = list(repos)
    for i, a in enumerate(names):
        if any(PurePosixPath(a) in PurePosixPath(b).parents or
               PurePosixPath(b) in PurePosixPath(a).parents for b in names[i + 1:]):
            raise PipelineError('Nested manifest repositories are not supported.')
    return repos


def manifest_write(path, repos):
    import yaml
    Path(path).write_text(yaml.safe_dump({'repositories': repos}, sort_keys=True))


def source_inventory(src, repos):
    result = {}
    for name, spec in repos.items():
        p = src / name
        if not contained(p, src) or p.is_symlink() or not (p / '.git').is_dir():
            raise PipelineError(f'Missing or unsafe repository: {p}')
        branch = subprocess.run(['git', '-C', str(p), 'symbolic-ref', '--short', '-q', 'HEAD'],
                                env=clean_env(), text=True, capture_output=True).stdout.strip()
        tracked = git(p, 'diff', '--binary', 'HEAD', '--')
        untracked = git(p, 'ls-files', '--others', '--exclude-standard', '-z').split('\0')
        result[name] = {'url': git(p, 'remote', 'get-url', 'origin'),
                        'revision': git(p, 'rev-parse', 'HEAD'), 'branch': branch or 'DETACHED',
                        'tracked_diff': tracked,
                        'untracked': [x for x in untracked if x and not generated(x)],
                        'generated': [x for x in untracked if x and generated(x)]}
        if result[name]['url'] != spec['url']:
            raise PipelineError(f'Unexpected origin: {name}')
    actual = {str(p.parent.relative_to(src)) for p in src.rglob('.git')}
    if actual != set(repos):
        raise PipelineError(f'Source repository inventory differs: {sorted(actual ^ set(repos))}')
    return result


def package_versions(runner):
    out = runner.run(['dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${db:Status-Abbrev}\n'])
    return {parts[0]: parts[1] for line in out.stdout.splitlines()
            if len(parts := line.split('\t')) == 3 and parts[2].startswith('ii')}


def system_evidence(runner):
    files = {}
    for p in [Path('/etc/os-release'), Path('/etc/debian_version'), Path('/etc/mx-version')]:
        if p.is_file():
            files[str(p)] = p.read_text(errors='replace')
    versions = {}
    for command in [['gcc', '--version'], ['cmake', '--version'], ['/usr/bin/python3', '--version'],
                    ['vcs', '--version'], ['rosdep', '--version']]:
        if shutil.which(command[0]):
            versions[command[0]] = runner.run(command, check=False).stdout.strip()
    for package in ['python3-empy', 'colcon', 'vcstool', 'python3-rosdep2', 'python3-yaml']:
        versions[package] = runner.run(['dpkg-query', '-W', '-f=${Version}', package],
                                      check=False).stdout.strip()
    evidence = {}
    paths = list(Path('/etc/ros/rosdep').glob('sources.list.d/*'))
    paths += list((Path(runner.env['HOME']) / '.ros/rosdep').rglob('*'))
    for p in paths:
        if p.is_file():
            evidence[str(p)] = {'sha256': file_hash(p), 'size': p.stat().st_size}
    return {'files': files, 'kernel': platform.release(), 'architecture': platform.machine(),
            'versions': versions, 'rosdep_files': evidence}


def profile_load(path):
    doc = read_json(path)
    if (not isinstance(doc, dict) or doc.get('schema_version') != 1 or
            set(doc) != {'schema_version', 'skip_keys', 'local_rosdep_mappings',
                         'packages_ignore', 'patches'} or
            not isinstance(doc['skip_keys'], dict)):
        raise PipelineError('Invalid compatibility profile.')
    if not isinstance(doc['local_rosdep_mappings'], list) or not isinstance(doc['patches'], list):
        raise PipelineError('Mappings and patches must be lists.')
    if not all(isinstance(k, str) and re.fullmatch(r'[A-Za-z0-9_.+-]+', k) and
               isinstance(v, str) and v.strip() for k, v in doc['skip_keys'].items()):
        raise PipelineError('Every skip key needs a justification.')
    if not isinstance(doc['packages_ignore'], list) or not all(
            isinstance(x, str) and re.fullmatch(r'[A-Za-z0-9_]+', x)
            for x in doc['packages_ignore']):
        raise PipelineError('Invalid package exclusions.')
    assets = {'mappings': [], 'patches': []}
    for name in doc['local_rosdep_mappings']:
        if not isinstance(name, str):
            raise PipelineError('Mappings must be profile-relative YAML filenames.')
        p = Path(path).parent / name
        if not contained(p, Path(path).parent) or not p.is_file():
            raise PipelineError(f'Unsafe or missing mapping: {name}')
        assets['mappings'].append({'path': str(p.resolve()), 'sha256': file_hash(p)})
    for patch in doc['patches']:
        if (not isinstance(patch, dict) or set(patch) != {'repository', 'file', 'reason'} or
                not all(isinstance(x, str) and x for x in patch.values())):
            raise PipelineError('Patches require repository, file, and reason.')
        p = Path(path).parent / patch['file']
        if not contained(p, Path(path).parent) or not p.is_file():
            raise PipelineError(f'Unsafe or missing patch: {p}')
        assets['patches'].append(dict(patch, path=str(p.resolve()), sha256=file_hash(p)))
    return doc, assets


class Pipeline:
    def __init__(self, args, runner=None):
        self.args = args
        self.root = args.root.expanduser().resolve()
        self.runner = runner or Runner()
        self.legacy = Path(self.runner.env['HOME']) / 'ros2_humble'
        self.protected = [self.legacy.resolve()]
        for ancestor in [self.root, *self.root.parents]:
            if (ancestor / 'metadata/state.json').is_file():
                raise PipelineError(f'Runtime root overlaps an existing candidate: {ancestor}')
        for p in (self.root / 'baselines').glob('*/baseline.json'):
            self.protected.append(Path(read_json(p)['workspace']).resolve())
        self.destination(self.root)

    def destination(self, path, extra=()):
        path = Path(path).resolve()
        for protected in [*self.protected, *extra]:
            if overlap(path, protected):
                raise PipelineError(f'Destination overlaps protected workspace: {path} / {protected}')
        return path

    @contextmanager
    def lock(self, path):
        self.destination(path)
        path = Path(path)
        if path.is_symlink():
            raise PipelineError(f'Refusing symlink lock: {path}')
        with path.open('a') as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise PipelineError(f'Another operation holds {path}') from e
            yield

    def root_setup(self):
        self.destination(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ['baselines', 'candidates', 'logs']:
            p = self.root / name
            if p.is_symlink():
                raise PipelineError(f'Refusing symlink runtime directory: {p}')
            p.mkdir(exist_ok=True)

    def candidate(self):
        if not self.args.candidate:
            raise PipelineError('--candidate is required.')
        p = self.args.candidate.expanduser().resolve()
        self.destination(p)
        if p.parent != self.root / 'candidates':
            raise PipelineError('Candidate must be a direct child of --root/candidates.')
        for name in ['src', 'build', 'install', 'logs', 'metadata']:
            q = p / name
            if q.is_symlink() or not contained(q, p):
                raise PipelineError(f'Unsafe candidate directory: {q}')
        state = read_json(p / 'metadata/state.json')
        if state.get('path') != str(p):
            raise PipelineError('Candidate moved: build/install prefixes must stay at their original path.')
        for q in p.rglob('*'):
            if q.is_symlink() and not contained(q, p):
                raise PipelineError(f'Candidate symlink leaves its workspace: {q}')
        return p, state

    @contextmanager
    def stage(self, p, state, name):
        old_log = self.runner.log
        self.runner.log = p / 'logs' / f'{name}-{unique_id()}.log'
        print(f'Log: {self.runner.log}', flush=True)
        state.pop('validation', None)
        state['stages'][name] = {'status': 'running', 'started': now(),
                                 'log': str(self.runner.log)}
        atomic_json(p / 'metadata/state.json', state)
        try:
            yield
        except BaseException as e:
            state['stages'][name].update(status='failed', finished=now(), error=str(e))
            atomic_json(p / 'metadata/state.json', state)
            raise
        else:
            state['stages'][name].update(status='passed', finished=now())
            atomic_json(p / 'metadata/state.json', state)
        finally:
            self.runner.log = old_log

    def require(self, state, stage):
        if state['stages'].get(stage, {}).get('status') != 'passed':
            raise PipelineError(f'{stage} must pass first.')

    def latest_baseline(self, optional=False):
        if self.args.baseline:
            p = self.args.baseline.expanduser().resolve()
        else:
            paths = sorted((self.root / 'baselines').glob('*/baseline.json'))
            paths = [x for x in paths if read_json(x).get('reproducible')]
            if not paths:
                if optional:
                    return None
                raise PipelineError('No reproducible baseline: run baseline --workspace PATH or select --source upstream.')
            p = paths[-1].parent
        info = read_json(p / 'baseline.json')
        if not info.get('reproducible'):
            raise PipelineError('Baseline contains substantive changes and cannot be reproduced.')
        if file_hash(p / 'exact.repos') != info['exact_sha256']:
            raise PipelineError('Baseline exact manifest has changed.')
        self.protected.append(Path(info['workspace']).resolve())
        self.destination(self.root)
        return p, info

    def baseline(self):
        ws = self.args.workspace.expanduser().resolve()
        self.destination(self.root, extra=[ws])
        if not (ws / 'install/local_setup.bash').is_file():
            raise PipelineError('Baseline needs an existing install/local_setup.bash.')
        repos = manifest_load(ws / 'ros2.repos')
        inventory = source_inventory(ws / 'src', repos)
        self.root_setup()
        with self.lock(self.root / '.pipeline.lock'):
            p = self.root / 'baselines' / unique_id()
            p.mkdir()
            shutil.copyfile(ws / 'ros2.repos', p / 'original.repos')
            exact = {n: dict(type='git', url=v['url'], version=v['revision'])
                     for n, v in inventory.items()}
            manifest_write(p / 'exact.repos', exact)
            dirty = any(v['tracked_diff'] or v['untracked'] for v in inventory.values())
            caches = {}
            for f in (ws / 'build').glob('*/CMakeCache.txt'):
                caches[f.parent.name] = [line for line in f.read_text(errors='replace').splitlines()
                                        if line.startswith(('CMAKE_BUILD_TYPE:', 'BUILD_TESTING:',
                                                            'Python3_EXECUTABLE:', 'PYTHON_EXECUTABLE:',
                                                            'CMAKE_INSTALL_PREFIX:'))]
            records = {}
            log = ws / 'log/latest_build/logger_all.log'
            if log.is_file():
                records['logger_all_head'] = log.read_text(errors='replace').splitlines()[:10]
                records['build_log'] = str(log.resolve())
            package_types = {name: line.split('=', 1)[1] for name, lines in caches.items()
                             for line in lines if line.startswith('CMAKE_BUILD_TYPE:')}
            types = set(package_types.values())
            config = {'build_type': next(iter(types)) if len(types) == 1 else '',
                      'package_build_types': package_types, 'symlink_install': True,
                      'packages_ignore': [], 'cmake_args': [], 'workers': 2, 'jobs': 2}
            namespace = next((line for line in records.get('logger_all_head', [])
                              if 'Parsed command line arguments:' in line), '')
            for key in ['packages_ignore', 'cmake_args']:
                match = re.search(r'\b' + key + r'=(None|\[.*?\])(?:,|\))', namespace)
                if match:
                    config[key] = ast.literal_eval(match[1]) or []
            unsupported = ['merge_install', 'packages_skip', 'packages_select', 'packages_up_to',
                           'packages_skip_regex', 'packages_select_regex', 'packages_start', 'packages_end']
            for key in unsupported:
                match = re.search(r'\b' + key + r'=(None|False|True|\[.*?\]|[^,]+)(?:,|\))', namespace)
                if match and match[1] not in ('None', 'False', '[]'):
                    dirty = True
                    records['configuration_error'] = f'Unsupported baseline selection/layout: {key}={match[1]}'
            # Reproduction is deliberately limited to settings evidenced by this installation.
            if not records or not any("'--symlink-install'" in x for x in records.get('logger_all_head', [])):
                dirty = True
                records['configuration_error'] = 'Cannot verify symlink-install from the available build log.'
            if any('merge_install=True' in x for x in records.get('logger_all_head', [])):
                dirty = True
                records['configuration_error'] = 'Merged installations need an explicit compatibility configuration.'
            info = {'schema_version': 1, 'captured': now(), 'workspace': str(ws),
                    'label': 'observed user-reported working baseline; not independently validated',
                    'reproducible': not dirty, 'repositories': inventory,
                    'original_sha256': file_hash(p / 'original.repos'),
                    'exact_sha256': file_hash(p / 'exact.repos'),
                    'system': system_evidence(self.runner),
                    'debian_packages': package_versions(self.runner),
                    'build_records': records, 'cmake_settings': caches, 'config': config,
                    'installed_packages': sorted(x.name for x in (ws / 'install').iterdir() if x.is_dir()),
                    'exclusions': sorted(str(x.relative_to(ws / 'src'))
                                         for x in (ws / 'src').rglob('COLCON_IGNORE'))}
            atomic_json(p / 'baseline.json', info)
            print(f'Baseline: {p}')
            if dirty:
                reasons = [records['configuration_error']] if 'configuration_error' in records else []
                reasons += [f'{name}: tracked edits or untracked source files'
                            for name, item in inventory.items() if item['tracked_diff'] or item['untracked']]
                raise PipelineError('Diagnostic snapshot retained; cannot reproduce: ' + '; '.join(reasons))
        return p

    def platform_check(self):
        values = {}
        for line in Path('/etc/os-release').read_text().splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                values[k] = v.strip('"')
        if (values.get('VERSION_CODENAME') != 'bookworm' or
                not ('debian' in (values.get('ID', '') + ' ' + values.get('ID_LIKE', '')).split()) or
                platform.machine() != 'x86_64'):
            raise PipelineError('This profile requires Debian 12 Bookworm/MX 23.x on amd64.')
        mx = Path('/etc/mx-version')
        if mx.exists() and not re.search(r'\b23(?:\.|\b)', mx.read_text()):
            raise PipelineError('This profile supports MX 23.x only.')

    def capabilities(self):
        for command in ['git', 'curl', 'vcs', 'rosdep', 'colcon', 'cmake', 'gcc']:
            if not shutil.which(command, path=self.runner.env['PATH']):
                raise PipelineError(f'Missing {command}; run bootstrap.')
        manifest_load(REPO / 'manifests/humble-bookworm-known-good.repos')
        for verb in ['build', 'test', 'test-result', 'list']:
            text = self.runner.run(['colcon', verb, '--help']).stdout
            if verb == 'build' and '--metas' not in text:
                raise PipelineError('Missing colcon metadata extension; run bootstrap.')
            if verb == 'test' and '--return-code-on-test-failure' not in text:
                raise PipelineError('colcon test extension lacks test-failure exit support.')
        self.runner.run(['/usr/bin/python3', '-c',
                         'import em; assert em.__version__.startswith("3.3."), em.__version__'])

    def bootstrap(self):
        self.platform_check()
        self.root_setup()
        with self.lock(self.root / '.pipeline.lock'):
            stamp = unique_id()
            old_log = self.runner.log
            self.runner.log = self.root / 'logs' / f'bootstrap-{stamp}.log'
            before = package_versions(self.runner)
            record_path = self.root / 'logs' / f'bootstrap-packages-{stamp}.json'
            record = {'status': 'running', 'started': now(), 'before': before,
                      'log': str(self.runner.log)}
            atomic_json(record_path, record)
            try:
                self.runner.run(['sudo', 'apt-get', 'update'])
                command = ['sudo', 'apt-get', 'install', '--no-remove', *BOOTSTRAP_PACKAGES]
                self.runner.run([*command[:3], '--simulate', *command[3:]], show=True)
                self.runner.run([*command[:3], '-y', *command[3:]])
                self.capabilities()
                if not Path('/etc/ros/rosdep/sources.list.d/20-default.list').exists():
                    self.runner.run(['sudo', 'rosdep', 'init'])
                record.update(status='passed', system=system_evidence(self.runner))
            except BaseException as e:
                record.update(status='failed', error=str(e))
                raise
            finally:
                record.update(after=package_versions(self.runner), finished=now())
                atomic_json(record_path, record)
                self.runner.log = old_log

    def prepare(self):
        self.platform_check()
        self.capabilities()
        baseline = self.latest_baseline(optional=self.args.source != 'baseline')
        profile, assets = profile_load(self.args.profile)
        exceptions = read_json(self.args.exceptions)
        validate_exceptions(exceptions)
        if self.args.source == 'manifest' and not self.args.manifest:
            raise PipelineError('--source manifest requires --manifest PATH.')
        if self.args.source != 'manifest' and self.args.manifest:
            raise PipelineError('--manifest is only used with --source manifest.')
        self.root_setup()
        with self.lock(self.root / '.pipeline.lock'):
            p = self.root / 'candidates' / unique_id()
            p.mkdir()
            for name in ['src', 'build', 'install', 'logs', 'metadata']:
                (p / name).mkdir()
            state = {'schema_version': 1, 'path': str(p), 'created': now(), 'stages': {},
                     'source': self.args.source, 'baseline': str(baseline[0]) if baseline else None}
            with self.stage(p, state, 'prepare'):
                selected = p / 'metadata/selected.repos'
                if self.args.source == 'baseline':
                    shutil.copyfile(baseline[0] / 'exact.repos', selected)
                elif self.args.source == 'manifest':
                    shutil.copyfile(self.args.manifest, selected)
                else:
                    temp = p / 'metadata/download.part'
                    self.runner.run(['curl', '--fail', '--location', '--retry', '3',
                                     '--connect-timeout', '20', '--max-time', '180',
                                     '--output', temp, UPSTREAM])
                    manifest_load(temp)
                    temp.replace(selected)
                repos = manifest_load(selected)
                self.runner.run(['vcs', 'import', '--input', selected, '--workers',
                                 str(self.args.workers), p / 'src'])
                inventory = source_inventory(p / 'src', repos)
                for name, spec in repos.items():
                    expected = git(p / 'src' / name, 'rev-parse', f'{spec["version"]}^{{commit}}')
                    if inventory[name]['revision'] != expected:
                        raise PipelineError(f'Import differs from requested revision: {name}')
                if any(v['tracked_diff'] or v['untracked'] for v in inventory.values()):
                    raise PipelineError('Fresh imported sources have substantive changes.')
                exact = {n: dict(type='git', url=v['url'], version=v['revision'])
                         for n, v in inventory.items()}
                manifest_write(p / 'metadata/exact.repos', exact)
                state['exact_sha256'] = file_hash(p / 'metadata/exact.repos')
                state['selected_sha256'] = file_hash(selected)
                atomic_json(p / 'metadata/profile.json', profile)
                atomic_json(p / 'metadata/test-exceptions.json', exceptions)
                for i, asset in enumerate(assets['mappings']):
                    shutil.copyfile(asset['path'], p / 'metadata' / f'mapping-{i}.yaml')
                for i, patch in enumerate(assets['patches']):
                    if patch['repository'] not in repos:
                        raise PipelineError(f'Patch repository missing: {patch["repository"]}')
                    dest = p / 'metadata' / f'patch-{i}.diff'
                    shutil.copyfile(patch['path'], dest)
                    repo_path = p / 'src' / patch['repository']
                    self.runner.run(['git', '-C', repo_path, 'apply', '--check', dest])
                    self.runner.run(['git', '-C', repo_path, 'apply', dest])
                state['assets'] = assets
                config = dict(baseline[1]['config']) if self.args.source == 'baseline' else {
                    'build_type': 'Release', 'symlink_install': True, 'packages_ignore': [], 'cmake_args': []}
                config.update(workers=self.args.workers, jobs=self.args.jobs)
                config['packages_ignore'] = sorted(set(config['packages_ignore'] + profile['packages_ignore']))
                state['config'] = config
                atomic_json(p / 'metadata/package-build.meta', {'names': {
                    name: {'cmake-args': [f'-DCMAKE_BUILD_TYPE={value}']}
                    for name, value in config.get('package_build_types', {}).items()}})
                state['source_inventory'] = source_inventory(p / 'src', exact)
                # A baseline's untracked COLCON_IGNORE markers must be represented, not silently lost.
                if self.args.source == 'baseline':
                    actual = sorted(str(x.relative_to(p / 'src')) for x in (p / 'src').rglob('COLCON_IGNORE'))
                    if actual != baseline[1]['exclusions']:
                        raise PipelineError('Baseline exclusions differ; encode them in the compatibility profile.')
                before = baseline[1]['repositories'] if baseline else {}
                diff = {'added': sorted(set(exact) - set(before)), 'removed': sorted(set(before) - set(exact)),
                        'changed': {n: {'before': before[n]['revision'], 'after': v['version'],
                                        'url_before': before[n]['url'], 'url_after': v['url']}
                                    for n, v in exact.items() if n in before and
                                    (before[n]['revision'] != v['version'] or before[n]['url'] != v['url'])}}
                atomic_json(p / 'metadata/source-diff.json', diff)
                print(json.dumps(diff, indent=2))
                state['input_fingerprint'] = self.input_fingerprint(p, state)
            print(f'Candidate: {p}')
            return p

    def input_fingerprint(self, p, state):
        exact = manifest_load(p / 'metadata/exact.repos')
        if file_hash(p / 'metadata/exact.repos') != state['exact_sha256']:
            raise PipelineError('Exact manifest changed; create a new candidate.')
        inventory = source_inventory(p / 'src', exact)
        for name, spec in exact.items():
            if inventory[name]['revision'] != spec['version']:
                raise PipelineError(f'Checkout differs from exact manifest: {name}')
        # Ignore runtime bytecode while hashing all substantive tracked/untracked changes.
        stable = {n: {k: v for k, v in info.items() if k != 'generated'}
                  for n, info in inventory.items()}
        untracked_hashes = {}
        for n, info in stable.items():
            for name in info['untracked']:
                f = p / 'src' / n / name
                if f.is_file():
                    untracked_hashes[f'{n}/{name}'] = file_hash(f)
        inputs = ['selected.repos', 'exact.repos', 'profile.json', 'test-exceptions.json',
                  'source-diff.json', 'package-build.meta']
        inputs += [f'mapping-{i}.yaml' for i, _ in enumerate(state['assets']['mappings'])]
        inputs += [f'patch-{i}.diff' for i, _ in enumerate(state['assets']['patches'])]
        metadata = {name: file_hash(p / 'metadata' / name) for name in inputs}
        return digest({'source': stable, 'untracked': untracked_hashes, 'metadata': metadata,
                       'config': state['config'], 'pipeline_sha256': file_hash(__file__)})

    def unchanged(self, p, state):
        if self.input_fingerprint(p, state) != state['input_fingerprint']:
            raise PipelineError('Candidate inputs changed; create a fresh candidate.')

    def rosdep_env(self, p):
        env = {'ROS_OS_OVERRIDE': 'debian:bookworm', 'ROS_HOME': str(p / 'metadata/ros-home')}
        mappings = sorted((p / 'metadata').glob('mapping-*.yaml'))
        if mappings:
            sources = p / 'metadata/rosdep-sources'
            sources.mkdir(exist_ok=True)
            lines = [f'yaml {x.as_uri()}\n' for x in mappings]
            (sources / '00-local.list').write_text(''.join(lines))
            for q in Path('/etc/ros/rosdep/sources.list.d').glob('*.list'):
                shutil.copyfile(q, sources / q.name)
            env['ROSDEP_SOURCE_PATH'] = str(sources)
        return env

    def rosdep_args(self, p):
        profile = read_json(p / 'metadata/profile.json')
        args = ['--from-paths', str(p / 'src'), '--ignore-src', '--rosdistro', 'humble',
                '--os=debian:bookworm', '--skip-keys', ' '.join(profile['skip_keys'])]
        return args

    def deps(self):
        self.platform_check()
        self.capabilities()
        p, state = self.candidate()
        with self.lock(p / '.candidate.lock'):
            self.require(state, 'prepare')
            self.unchanged(p, state)
            with self.stage(p, state, 'deps'):
                env = self.rosdep_env(p)
                self.runner.run(['rosdep', 'update', '--rosdistro', 'humble'], env=env)
                before = package_versions(self.runner)
                atomic_json(p / 'metadata/packages-before.json', before)
                try:
                    plan = self.runner.run(['rosdep', 'install', *self.rosdep_args(p),
                                            '-y', '--simulate'], env=env, show=True)
                    packages = apt_packages(plan.stdout)
                    if packages:
                        self.runner.run(['sudo', 'apt-get', 'install', '--simulate',
                                         '--no-remove', *packages], show=True)
                        self.runner.run(['sudo', 'apt-get', 'install', '-y', '--no-remove', *packages])
                    self.runner.run(['rosdep', 'check', *self.rosdep_args(p)], env=env, show=True)
                finally:
                    atomic_json(p / 'metadata/packages-after.json', package_versions(self.runner))
                # A complete installed-package snapshot is conservative: even an unrelated
                # package change requests revalidation instead of guessing transitive impact.
                state['dependencies'] = package_versions(self.runner)
                atomic_json(p / 'metadata/dependencies.json', state['dependencies'])
                atomic_json(p / 'metadata/system.json', system_evidence(self.runner))
                state['rosdep_cache'] = {str(x.relative_to(p)): file_hash(x)
                                         for x in (p / 'metadata/ros-home').rglob('*') if x.is_file()}
                # Dependency cache evidence and generated local rosdep configuration are inputs.
                state['input_fingerprint'] = self.input_fingerprint(p, state)
                state['stages'].pop('build', None)
                state['stages'].pop('validate', None)

    def dependency_check(self, p, state):
        env = self.rosdep_env(p)
        cache = {str(x.relative_to(p)): file_hash(x)
                 for x in (p / 'metadata/ros-home').rglob('*') if x.is_file()}
        if cache != state['rosdep_cache']:
            raise PipelineError('Candidate rosdep cache changed; rerun deps before building.')
        self.runner.run(['rosdep', 'check', *self.rosdep_args(p)], env=env)

    def colcon_env(self, p, state):
        # COLCON_HOME prevents user defaults/mixins from changing the recorded build.
        home = p / 'metadata/colcon-home'
        home.mkdir(exist_ok=True)
        if (p / 'colcon_defaults.yaml').exists() or (p / 'colcon.meta').exists():
            raise PipelineError('Unrecorded workspace defaults/meta are not allowed in a managed candidate.')
        return {'COLCON_HOME': str(home), 'COLCON_DEFAULTS_FILE': '/dev/null',
                'MAKEFLAGS': f'-j{state["config"]["jobs"]}',
                'CMAKE_BUILD_PARALLEL_LEVEL': str(state['config']['jobs'])}

    def colcon_args(self, p, state):
        args = ['--base-paths', str(p / 'src'), '--build-base', str(p / 'build'),
                '--install-base', str(p / 'install'), '--parallel-workers', str(state['config']['workers']),
                '--event-handlers', 'console_cohesion+', '--ignore-user-meta',
                '--metas', str(p / 'metadata/package-build.meta')]
        if state['config']['packages_ignore']:
            args += ['--packages-ignore', *state['config']['packages_ignore']]
        return args

    def build(self):
        p, state = self.candidate()
        with self.lock(p / '.candidate.lock'):
            self.require(state, 'deps')
            self.unchanged(p, state)
            with self.stage(p, state, 'build'):
                self.dependency_check(p, state)
                state['dependencies'] = package_versions(self.runner)
                self.runner.run(['colcon', '--log-base', p / 'logs/colcon-build', 'build',
                                 *self.colcon_args(p, state), '--symlink-install', '--cmake-args',
                                 *state['config'].get('cmake_args', []),
                                 '-DBUILD_TESTING=ON', '-DPython3_EXECUTABLE=/usr/bin/python3',
                                 '-DPYTHON_EXECUTABLE=/usr/bin/python3',
                                 f'-DCMAKE_BUILD_TYPE={state["config"]["build_type"]}'],
                                cwd=p, env=self.colcon_env(p, state))
                self.unchanged(p, state)
                state['build_fingerprint'] = state['input_fingerprint']
                state['build_dependencies'] = state['dependencies']
                state['stages'].pop('validate', None)

    def sourced_run(self, p, command, **kwargs):
        setup = p / 'install/local_setup.bash'
        if not setup.is_file():
            raise PipelineError('Candidate setup script is missing.')
        return self.runner.run(['/bin/bash', '--noprofile', '--norc', '-c',
                                'set -e; source "$1"; shift; exec "$@"', 'candidate', setup, *command],
                               **kwargs)

    def validate(self):
        p, state = self.candidate()
        with self.lock(p / '.candidate.lock'):
            self.require(state, 'build')
            self.unchanged(p, state)
            if package_versions(self.runner) != state['build_dependencies']:
                raise PipelineError('System packages changed since build; rerun build before validation.')
            with self.stage(p, state, 'validate'):
                self.dependency_check(p, state)
                env = self.colcon_env(p, state)
                listed = self.runner.run(['colcon', 'list', '--base-paths', p / 'src', '--names-only',
                                          '--ignore-user-meta', '--packages-ignore',
                                          *state['config']['packages_ignore']], cwd=p, env=env).stdout.splitlines()
                test_base = p / 'logs' / f'test-results-{unique_id()}'
                log_base = p / 'logs' / f'colcon-test-{unique_id()}'
                started_ns = time.time_ns()
                result = self.sourced_run(p, ['colcon', '--log-base', log_base, 'test',
                                              *self.colcon_args(p, state), '--test-result-base', test_base,
                                              '--return-code-on-test-failure'], cwd=p, env=env,
                                          check=False, timeout=self.args.test_timeout)
                collect_fresh_junit(p / 'build', test_base, started_ns)
                summary = self.runner.run(['colcon', 'test-result', '--test-result-base', test_base,
                                           '--verbose'], check=False, env=env, show=True)
                reports = summarize_tests(test_base, p / 'metadata/test-exceptions.json',
                                          p / 'metadata/exact.repos')
                atomic_json(p / 'metadata/tests.json', reports)
                # colcon task return values distinguish infrastructure errors from test
                # failures: completed packages must all have an ordinary successful task.
                events = read_test_events(log_base, listed)
                if result.returncode == 124 or events['errors']:
                    raise PipelineError(f'Test timeout/infrastructure failure: {events}')
                if result.returncode not in (0, 1) or summary.returncode not in (0, 1):
                    raise PipelineError('Unexpected colcon test/test-result error.')
                if result.returncode and not reports['waived']:
                    raise PipelineError('colcon test failed without matching test exceptions.')
                if reports['failures']:
                    raise PipelineError(f'Unwaived test failures: {reports["failures"]}')
                if summary.returncode and not reports['waived']:
                    raise PipelineError('colcon test-result reports errors.')
                if not set(events['test_failures']).issubset(set(reports['failure_packages'])):
                    raise PipelineError('TestFailure events have missing failure reports.')
                packages = self.sourced_run(p, ['ros2', 'pkg', 'list']).stdout.splitlines()
                missing = sorted(set(REQUIRED_ROS) - set(packages))
                if missing:
                    raise PipelineError(f'Required installed ROS packages missing: {missing}')
                smoke = self.smoke_checks(p)
                atomic_json(p / 'metadata/smoke.json', smoke)
                self.unchanged(p, state)
                deps = package_versions(self.runner)
                if deps != state['build_dependencies']:
                    raise PipelineError('System packages changed during validation.')
                state['validation'] = {'finished': now(), 'input_fingerprint': state['input_fingerprint'],
                                       'dependencies': deps, 'reports': str(test_base),
                                       'test_timeout': self.args.test_timeout}
            print(f'Validated candidate: {p}\nRun activate --candidate {shlex.quote(str(p))} for manual instructions.')

    def smoke_checks(self, p):
        setup = p / 'install/local_setup.bash'
        results = []
        for rmw in ['rmw_fastrtps_cpp', 'rmw_cyclonedds_cpp']:
            for talker, listener in [('demo_nodes_cpp', 'demo_nodes_py'), ('demo_nodes_py', 'demo_nodes_cpp')]:
                name = f'{rmw}-{talker}'
                output = p / 'logs' / f'smoke-{name}-{unique_id()}.log'
                topic = '/candidate_' + uuid.uuid4().hex
                env = dict(self.runner.env, RMW_IMPLEMENTATION=rmw, ROS_DOMAIN_ID=str(20 + uuid.uuid4().int % 180),
                           ROS_LOCALHOST_ONLY='1', PYTHONUNBUFFERED='1')
                # ros2 run/pkg use package lookup, not NodeStrategy/daemon commands.
                results.append(smoke_pair(setup, talker, listener, topic, env, output, timeout=30))
        return results

    def activate(self):
        p, state = self.candidate()
        with self.lock(p / '.candidate.lock'):
            self.require(state, 'validate')
            self.unchanged(p, state)
            validation = state.get('validation')
            if not validation or validation['input_fingerprint'] != state['input_fingerprint']:
                raise PipelineError('Validation is missing or stale.')
            if package_versions(self.runner) != validation['dependencies']:
                raise PipelineError('System packages changed; rebuild and revalidate before activation.')
            if not (p / 'install/local_setup.bash').is_file():
                raise PipelineError('Candidate setup script is missing.')
            print('Open a fresh terminal, then run:\n' + activation_command(p))
            print('Workspace rollback in another fresh terminal:\n' +
                  'source ' + shlex.quote(str(self.legacy / 'install/local_setup.bash')))
            print('No shell settings were changed. Workspace rollback does not undo apt changes.')

    def status(self):
        if self.args.candidate:
            p, state = self.candidate()
            print(json.dumps(state, indent=2, sort_keys=True))
            try:
                self.unchanged(p, state)
                current = state.get('validation', {}).get('dependencies') == package_versions(self.runner)
                print('Ready:', bool(current and state['stages'].get('validate', {}).get('status') == 'passed'))
            except PipelineError as e:
                print(f'Ready: False ({e})')
        else:
            for category, filename in [('baselines', 'baseline.json'), ('candidates', 'metadata/state.json')]:
                for p in sorted((self.root / category).glob('*')):
                    if (p / filename).is_file():
                        info = read_json(p / filename)
                        print(category, p, info.get('stages', info.get('reproducible')))

    def run(self):
        # Resolve requested inputs before any package installation on the host.
        if self.args.source == 'baseline':
            self.latest_baseline()
        if self.args.source == 'manifest':
            if not self.args.manifest:
                raise PipelineError('--source manifest requires --manifest.')
            manifest_load(self.args.manifest)
        elif self.args.manifest:
            raise PipelineError('--manifest is only used with --source manifest.')
        profile_load(self.args.profile)
        validate_exceptions(read_json(self.args.exceptions))
        self.bootstrap()
        self.args.candidate = self.prepare()
        self.deps()
        self.build()
        self.validate()


def validate_exceptions(doc):
    if not isinstance(doc, dict) or set(doc) != {'schema_version', 'exceptions'} or doc['schema_version'] != 1:
        raise PipelineError('Invalid test-exception file.')
    if not isinstance(doc['exceptions'], list):
        raise PipelineError('exceptions must be a list.')
    keys = set()
    for e in doc['exceptions']:
        fields = {'package', 'classname', 'name', 'revision', 'reason'}
        if (not isinstance(e, dict) or set(e) != fields or
                not all(isinstance(v, str) and v.strip() for v in e.values()) or
                not re.fullmatch(r'[0-9a-f]{40}', e['revision'])):
            raise PipelineError('Each exception needs package, classname, name, exact revision, and reason.')
        key = tuple(e[k] for k in ['package', 'classname', 'name', 'revision'])
        if key in keys:
            raise PipelineError('Duplicate test exception.')
        keys.add(key)


def apt_packages(output):
    """Accept only the apt installer commands emitted by rosdep simulation."""
    packages = set()
    for line in output.splitlines():
        text = line.strip()
        if text.startswith('#['):
            if not text.startswith('#[apt]'):
                raise PipelineError(f'Non-Debian installer requires a reviewed mapping: {text}')
        elif text.startswith(('sudo ', 'apt-get ', "'sudo ", "'apt-get ")):
            # Alternatives need review; picking one implicitly is not reproducible.
            if text.startswith("'"):
                raise PipelineError('Alternative rosdep packages require an explicit local mapping.')
            tokens = shlex.split(text)
            if tokens[:2] == ['sudo', '-H']:
                tokens = tokens[2:]
            if tokens[:2] != ['apt-get', 'install']:
                raise PipelineError(f'Unsupported dependency command: {text}')
            for package in tokens[2:]:
                if package in ('-y', '-qq'):
                    continue
                if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*(?::[a-z0-9]+)?(?:=[A-Za-z0-9.+:~_-]+)?', package):
                    raise PipelineError(f'Unsafe apt package or option: {package}')
                packages.add(package)
    return sorted(packages)


def collect_fresh_junit(build, destination, started_ns):
    """ament writes detailed JUnit below build; CTest's summary is copied by colcon."""
    for path in Path(build).glob('*/test_results/**/*.xml'):
        if path.stat().st_mtime_ns < started_ns:
            continue
        target = Path(destination) / path.relative_to(build)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def summarize_tests(base, exception_file, exact_file):
    doc = read_json(exception_file)
    validate_exceptions(doc)
    exact = manifest_load(exact_file)
    # A package can reside inside any manifest repository; package mapping is
    # supplied separately through the report's package and resolved below.
    repo_by_package = {}
    candidate = Path(exact_file).parent.parent
    for name in exact:
        for p in (candidate / 'src' / name).rglob('package.xml'):
            try:
                pkg = ET.parse(p).getroot().findtext('name')
                if pkg:
                    repo_by_package[pkg] = name
            except ET.ParseError as e:
                raise PipelineError(f'Malformed package.xml: {p}') from e
    failures, waived, count, files, ctests = [], [], 0, [], []
    failure_packages = set()
    per_file = {}
    for path in sorted(Path(base).rglob('*.xml')):
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as e:
            raise PipelineError(f'Malformed test result: {path}') from e
        cases = list(root.iter('testcase'))
        package = path.relative_to(base).parts[0]
        if root.tag == 'Site':
            if root.find('Testing') is None:
                raise PipelineError(f'Invalid CTest report: {path}')
            files.append(str(path))
            for case in root.findall('Testing/Test'):
                status = case.get('Status')
                if status not in ('passed', 'failed', 'notrun'):
                    raise PipelineError(f'Invalid CTest status: {path}')
                if status == 'notrun':
                    raise PipelineError(f'CTest did not execute: {case.findtext("Name")}')
                count += 1
                if status == 'failed':
                    failure_packages.add(package)
                    completion = case.findtext('Results/NamedMeasurement[@name="Completion Status"]/Value', '')
                    if completion not in ('Completed', 'Failed'):
                        raise PipelineError(f'CTest infrastructure failure: {path}: {completion}')
                    ctests.append((package, case.findtext('Name', ''), path))
            continue
        if root.tag not in ('testsuite', 'testsuites'):
            continue  # CTest metadata XML is not a JUnit report.
        files.append(str(path))
        count += sum(c.find('skipped') is None for c in cases)
        declared_failures = sum(int(s.get('failures', '0')) + int(s.get('errors', '0'))
                                for s in ([root] if root.tag == 'testsuite' else root.findall('testsuite')))
        detailed_failures = sum(any(x.tag in ('failure', 'error') for x in c) for c in cases)
        if declared_failures > detailed_failures:
            raise PipelineError(f'Test suite failure has no identifiable testcase: {path}')
        file_failures = []
        for case in cases:
            if not any(x.tag in ('failure', 'error') for x in case):
                continue
            repo = repo_by_package.get(package)
            revision = exact[repo]['version'] if repo else ''
            failure = {'package': package, 'classname': case.get('classname', ''),
                       'name': case.get('name', ''), 'revision': revision, 'file': str(path)}
            if 'missing_result' in failure['name'] or any(
                    'without generating a result' in (x.get('message', '') + (x.text or ''))
                    for x in case if x.tag in ('failure', 'error')):
                raise PipelineError(f'Missing test result cannot be waived: {path}')
            if any(re.search(r'(?i)\b(timeout|timed out)\b', x.get('message', '') + (x.text or ''))
                   for x in case if x.tag in ('failure', 'error')):
                raise PipelineError(f'Test timeout cannot be waived: {path}')
            failure_packages.add(package)
            match = next((e for e in doc['exceptions'] if all(e[k] == failure[k]
                          for k in ['package', 'classname', 'name', 'revision'])), None)
            (waived if match else failures).append(dict(failure, reason=match['reason']) if match else failure)
            file_failures.append(bool(match))
        per_file[(package, path.name)] = file_failures
    for package, name, path in ctests:
        # Ament's CTest wrapper fails when its detailed JUnit fails. Only waive
        # that wrapper when every corresponding detailed failure was matched.
        matches = [values for (pkg, filename), values in per_file.items()
                   if pkg == package and (filename == name + '.xml' or filename.startswith(name + '.'))]
        if matches and any(matches) and all(all(values) for values in matches):
            continue
        repo = repo_by_package.get(package)
        failure = {'package': package, 'classname': 'CTest', 'name': name,
                   'revision': exact[repo]['version'] if repo else '', 'file': str(path)}
        match = next((e for e in doc['exceptions'] if all(e[k] == failure[k]
                      for k in ['package', 'classname', 'name', 'revision'])), None)
        (waived if match else failures).append(dict(failure, reason=match['reason']) if match else failure)
    if not files or count == 0:
        raise PipelineError('No fresh executed testcases were produced.')
    return {'testcases': count, 'files': files, 'failures': failures, 'waived': waived,
            'failure_packages': sorted(failure_packages)}


def read_test_events(log_base, packages):
    path = Path(log_base) / 'latest_test/events.log'
    if not path.exists():
        candidates = sorted(Path(log_base).glob('test_*/events.log'))
        if len(candidates) != 1:
            raise PipelineError('Missing or ambiguous colcon task events.')
        path = candidates[0]
    text = path.read_text(errors='replace')
    completed = {}
    aborted, test_failures = [], []
    for line in text.splitlines():
        if 'JobEnded:' in line:
            try:
                payload = ast.literal_eval(line.split('JobEnded:', 1)[1].strip())
            except (ValueError, SyntaxError) as e:
                raise PipelineError('Malformed colcon task event.') from e
            completed[payload['identifier']] = payload['rc']
        if 'JobAborted:' in line:
            aborted.append(line)
        if 'TestFailure:' in line:
            payload = ast.literal_eval(line.split('TestFailure:', 1)[1].strip())
            test_failures.append(payload['identifier'])
    errors = [f'{name}: task rc={completed.get(name, "missing")}' for name in packages
              if completed.get(name) != 0] + aborted
    return {'completed': completed, 'errors': errors, 'test_failures': test_failures}


def smoke_pair(setup, talker, listener, topic, env, output, timeout=30, popen=subprocess.Popen):
    processes = []
    start = time.monotonic()
    try:
        with Path(output).open('w+') as log:
            for package, executable in [(listener, 'listener'), (talker, 'talker')]:
                command = ['/bin/bash', '--noprofile', '--norc', '-c',
                           'set -e; source "$1"; shift; exec "$@"', 'smoke', str(setup),
                           'ros2', 'run', package, executable, '--ros-args', '-r', f'chatter:={topic}']
                processes.append(popen(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True))
            while time.monotonic() - start < timeout:
                log.flush()
                text = Path(output).read_text(errors='replace')
                if all(p.poll() is None for p in processes) and 'Publishing' in text and 'I heard' in text:
                    return {'rmw': env['RMW_IMPLEMENTATION'], 'talker': talker, 'listener': listener,
                            'passed': True, 'log': str(output), 'seconds': time.monotonic() - start}
                if any(p.poll() is not None for p in processes):
                    raise PipelineError(f'Smoke process exited before communication: {output}')
                time.sleep(0.1)
        raise PipelineError(f'Smoke timeout ({timeout}s): {output}')
    finally:
        for proc in processes:
            Runner.stop(proc)


def activation_command(candidate):
    return 'source ' + shlex.quote(str(Path(candidate) / 'install/local_setup.bash'))


def positive(value):
    try:
        parsed = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError('Must be a positive integer.') from e
    if parsed < 1:
        raise argparse.ArgumentTypeError('Must be a positive integer.')
    return parsed


def parser():
    p = argparse.ArgumentParser(description='Prepare and validate isolated ROS 2 Humble candidates; activation is manual.')
    sub = p.add_subparsers(dest='command', required=True)
    for verb in ['baseline', 'bootstrap', 'prepare', 'deps', 'build', 'validate', 'activate', 'run', 'status']:
        q = sub.add_parser(verb)
        q.set_defaults(candidate=None, baseline=None, manifest=None)
        q.add_argument('--root', type=Path, default=Path.home() / 'ros2_humble_candidates',
                       help='Runtime root (default: ~/ros2_humble_candidates).')
        if verb in ['deps', 'build', 'validate', 'activate', 'status']:
            q.add_argument('--candidate', type=Path, required=verb != 'status',
                           help='Existing candidate under ROOT/candidates.')
        if verb == 'baseline':
            q.add_argument('--workspace', type=Path, default=Path.home() / 'ros2_humble',
                           help='Read-only workspace for baseline capture.')
        if verb in ['prepare', 'run']:
            q.add_argument('--source', choices=['baseline', 'upstream', 'manifest'], default='baseline')
            q.add_argument('--baseline', type=Path,
                           help='Baseline snapshot directory; default: latest reproducible snapshot.')
            q.add_argument('--manifest', type=Path, help='Local manifest for --source manifest.')
            q.add_argument('--profile', type=Path, default=REPO / 'config/bookworm.json')
            q.add_argument('--exceptions', type=Path, default=REPO / 'config/test-exceptions.json')
            q.add_argument('--workers', type=positive, default=2)
            q.add_argument('--jobs', type=positive, default=2)
        if verb in ['validate', 'run']:
            q.add_argument('--test-timeout', type=positive, default=7200, help='Full-test timeout in seconds.')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if os.geteuid() == 0:
        print('Run as a normal user; only package installation uses sudo.', file=sys.stderr)
        return 2
    try:
        pipe = Pipeline(args)
        getattr(pipe, args.command)()
        return 0
    except CommandError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return e.returncode if 0 < e.returncode < 126 else 1
    except (PipelineError, OSError, ValueError, KeyboardInterrupt) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
