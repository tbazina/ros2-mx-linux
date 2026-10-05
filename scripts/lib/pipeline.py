#!/usr/bin/env python3
"""Local candidate lifecycle. No operation mutates a captured source workspace."""
import argparse
import ast
import ctypes
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
                'rmw_fastrtps_cpp', 'rmw_cyclonedds_cpp', 'rviz2', 'rqt_gui',
                'action_tutorials_cpp', 'action_tutorials_py', 'tf2_ros', 'tf2_py',
                'ros2bag', 'rosbag2_transport', 'rosbag2_py']
# The last schema-1 implementation. Migration recomputes its complete input
# fingerprint, including this original code hash; it never adopts unknown inputs.
LEGACY_PIPELINES = ['f95426cbba05212767efc00e5ef169c2b7e850071874404d15b023125568910d']
VALIDATION_PROFILE = REPO / 'config/validation.json'
PYTEST_PLUGIN = REPO / 'scripts/lib/ros2_validation_pytest.py'
MIDDLEWARES = ['rmw_fastrtps_cpp', 'rmw_cyclonedds_cpp']


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
            effective = {key: value for key, value in effective.items() if value is not None}
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


def local_ports(protocol):
    """Read Linux socket tables without opening sockets or changing networking."""
    ports = set()
    for suffix in ['', '6']:
        for line in Path(f'/proc/net/{protocol}{suffix}').read_text().splitlines()[1:]:
            ports.add(int(line.split()[1].rsplit(':', 1)[1], 16))
    return ports


@contextmanager
def subreaper():
    # Detached ROS CLI daemons otherwise escape their command's process group.
    # Adopt only descendants of this pipeline, then identify them by attempt token.
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) or libc.prctl(36, 1, 0, 0, 0):
        raise PipelineError('Cannot enable owned-process cleanup on this Linux system.')
    try:
        yield
    finally:
        libc.prctl(36, previous.value, 0, 0, 0)


def cleanup_owned_orphans(token):
    path = Path(f'/proc/self/task/{os.getpid()}/children')
    for value in path.read_text().split():
        pid = int(value)
        try:
            env = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
            if ('ROS2_CANDIDATE_VALIDATION_TOKEN=' + token).encode() not in env:
                continue
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                result, _ = os.waitpid(pid, os.WNOHANG)
                if result:
                    break
                time.sleep(.05)
            else:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
        except (FileNotFoundError, ProcessLookupError, ChildProcessError):
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
            state['stages'][name]['duration_seconds'] = max(0, (
                datetime.fromisoformat(state['stages'][name].get('finished', now())) -
                datetime.fromisoformat(state['stages'][name]['started'])).total_seconds())
            atomic_json(p / 'metadata/state.json', state)
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
        for command in ['git', 'curl', 'vcs', 'rosdep', 'colcon', 'cmake', 'ctest', 'gcc']:
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
                optional = ['ccache'] if getattr(self.args, 'ccache', False) else []
                command = ['sudo', 'apt-get', 'install', '--no-remove', *BOOTSTRAP_PACKAGES, *optional]
                self.runner.run([*command[:3], '--simulate', *command[3:]], show=True)
                self.runner.run([*command[:3], '-y', *command[3:]])
                self.capabilities()
                if optional:
                    self.runner.run(['ccache', '--version'])
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
            state = {'schema_version': 2, 'path': str(p), 'created': now(), 'stages': {},
                     'preparation_pipeline_sha256': file_hash(__file__),
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
                                 str(self.args.workers or 2), p / 'src'])
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
                    checked = self.runner.run(['git', '-C', repo_path, 'apply', '--check', dest], check=False)
                    if checked.returncode:
                        # A matching postimage is already patched; any other drift
                        # must be reviewed rather than silently skipping the patch.
                        self.runner.run(['git', '-C', repo_path, 'apply', '--reverse', '--check', dest])
                        patch['status'] = 'already_present'
                    else:
                        self.runner.run(['git', '-C', repo_path, 'apply', dest])
                        patch['status'] = 'applied'
                state['assets'] = assets
                config = dict(baseline[1]['config']) if self.args.source == 'baseline' else {
                    'build_type': 'Release', 'symlink_install': True, 'packages_ignore': [], 'cmake_args': []}
                config['workers'] = self.args.workers or config.get('workers', 2)
                config['jobs'] = self.args.jobs or config.get('jobs', 2 if self.args.source == 'baseline' else 3)
                if getattr(self.args, 'ccache', False):
                    if not shutil.which('ccache', path=self.runner.env['PATH']):
                        raise PipelineError('Missing ccache; run bootstrap --ccache before prepare --ccache.')
                    cache = self.root / 'cache/ccache'
                    self.destination(cache)
                    if (self.root / 'cache').is_symlink() or cache.is_symlink():
                        raise PipelineError('Unsafe ccache directory.')
                    cache.mkdir(parents=True, exist_ok=True)
                    config['ccache'] = {'directory': str(cache), 'base_dir': str(self.root / 'candidates'),
                                        'version': self.runner.run(['ccache', '--version']).stdout,
                                        'compiler': self.runner.run(['gcc', '--version']).stdout,
                                        'compiler_sha256': file_hash('/usr/bin/gcc'),
                                        'cxx_compiler_sha256': file_hash('/usr/bin/g++'),
                                        'launcher_sha256': file_hash('/usr/bin/ccache')}
                    config['cmake_args'] = [*config.get('cmake_args', []),
                                            '-DCMAKE_C_COMPILER_LAUNCHER=/usr/bin/ccache',
                                            '-DCMAKE_CXX_COMPILER_LAUNCHER=/usr/bin/ccache']
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

    def input_fingerprint(self, p, state, legacy_sha=None):
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
        inputs = ['selected.repos', 'exact.repos', 'profile.json',
                  'source-diff.json', 'package-build.meta']
        if legacy_sha:
            inputs.append('test-exceptions.json')
        inputs += [f'mapping-{i}.yaml' for i, _ in enumerate(state['assets']['mappings'])]
        inputs += [f'patch-{i}.diff' for i, _ in enumerate(state['assets']['patches'])]
        metadata = {name: file_hash(p / 'metadata' / name) for name in inputs}
        value = {'source': stable, 'untracked': untracked_hashes, 'metadata': metadata,
                 'config': state['config']}
        if legacy_sha:
            value['pipeline_sha256'] = legacy_sha
        return digest(value)

    def unchanged(self, p, state):
        if state.get('schema_version') == 1:
            matched = next((sha for sha in LEGACY_PIPELINES
                            if self.input_fingerprint(p, state, sha) == state.get('input_fingerprint')), None)
            built = state['stages'].get('build', {}).get('status') == 'passed'
            if not matched or (built and state.get('build_fingerprint') != state['input_fingerprint']):
                raise PipelineError('Unknown or changed legacy provenance; prepare a fresh candidate from exact.repos.')
            state['migration'] = {'from_schema': 1, 'pipeline_sha256': matched,
                                  'old_fingerprint': state['input_fingerprint'], 'verified': now()}
            state['schema_version'] = 2
            state['input_fingerprint'] = self.input_fingerprint(p, state)
            if state.get('build_fingerprint'):
                state['build_fingerprint'] = state['input_fingerprint']
            state.pop('validation', None)
            state['stages'].pop('validate', None)
            # The caller holds the candidate lock when migration is persisted.
        if state.get('schema_version') != 2:
            raise PipelineError('Unsupported candidate metadata schema.')
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
        env = {'COLCON_HOME': str(home), 'COLCON_DEFAULTS_FILE': '/dev/null',
                'MAKEFLAGS': f'-j{state["config"]["jobs"]}',
                'CMAKE_BUILD_PARALLEL_LEVEL': str(state['config']['jobs'])}
        cache = state['config'].get('ccache')
        if cache:
            self.destination(cache['directory'])
            if (Path(cache['directory']).is_symlink() or
                    Path(cache['directory']).parent.is_symlink() or
                    file_hash('/usr/bin/gcc') != cache['compiler_sha256'] or
                    file_hash('/usr/bin/g++') != cache['cxx_compiler_sha256'] or
                    file_hash('/usr/bin/ccache') != cache['launcher_sha256']):
                raise PipelineError('Recorded compiler/cache inputs changed; prepare a fresh candidate.')
            env.update(CCACHE_DIR=cache['directory'], CCACHE_BASEDIR=cache['base_dir'],
                       CCACHE_CONFIGPATH=str(p / 'metadata/ccache.conf'))
            # Explicit config excludes user-wide sloppiness or other unsafe defaults.
            config = p / 'metadata/ccache.conf'
            expected = 'compiler_check = content\nsloppiness =\n'
            if config.exists() and config.read_text() != expected:
                raise PipelineError('Candidate ccache configuration changed.')
            config.write_text(expected)
        return env

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
                resources = {'before': resource_observation(), 'started': now()}
                self.dependency_check(p, state)
                state['dependencies'] = package_versions(self.runner)
                try:
                    self.runner.run(['colcon', '--log-base', p / 'logs/colcon-build', 'build',
                                     *self.colcon_args(p, state), '--symlink-install', '--cmake-args',
                                     *state['config'].get('cmake_args', []),
                                     '-DBUILD_TESTING=ON', '-DPython3_EXECUTABLE=/usr/bin/python3',
                                     '-DPYTHON_EXECUTABLE=/usr/bin/python3',
                                     f'-DCMAKE_BUILD_TYPE={state["config"]["build_type"]}'],
                                    cwd=p, env=self.colcon_env(p, state))
                finally:
                    resources.update(after=resource_observation(), finished=now(),
                                     packages=package_timings(p / 'logs/colcon-build'))
                    if state['config'].get('ccache'):
                        try:
                            stats = self.runner.run(['ccache', '--show-stats'], env=self.colcon_env(p, state),
                                                    check=False)
                            resources['ccache_statistics'] = stats.stdout
                        except (PipelineError, OSError) as e:
                            # Statistics must never replace the build's exit code.
                            resources['ccache_statistics_error'] = str(e)
                    atomic_json(p / 'metadata/build-timing.json', resources)
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

    @contextmanager
    def domain(self, directory):
        leases = self.root / 'domains'
        if leases.is_symlink():
            raise PipelineError('Unsafe domain reservation directory.')
        leases.mkdir(exist_ok=True)
        start = uuid.uuid4().int % 81
        occupied = local_ports('tcp') | local_ports('udp')
        for offset in range(81):
            domain = 20 + (start + offset) % 81
            base_port = 7400 + 250 * domain
            if 11511 + domain in occupied or any(base_port <= port < base_port + 250 for port in occupied):
                continue
            lease = leases / f'{domain}.lock'
            if lease.is_symlink():
                raise PipelineError('Unsafe domain lease.')
            with lease.open('a') as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                token = uuid.uuid4().hex
                private = Path(directory) / ('domain-' + str(domain) + '-' + token[:8])
                private.mkdir(parents=True)
                env = dict(ROS_DOMAIN_ID=str(domain), ROS_LOCALHOST_ONLY='1',
                           ROS_HOME=str(private / 'home'), ROS_LOG_DIR=str(private / 'logs'),
                           ROS2_CANDIDATE_VALIDATION_TOKEN=token, PYTHONUNBUFFERED='1')
                try:
                    yield env
                finally:
                    cleanup_owned_orphans(token)
                return
        raise PipelineError('No unused local ROS domain available; no existing processes were stopped.')

    def ctest_inventory(self, p, env):
        inventory = {}
        for path in sorted((p / 'build').glob('*/CTestTestfile.cmake')):
            cache = path.parent / 'CMakeCache.txt'
            build_type = re.search(r'^CMAKE_BUILD_TYPE:[^=\n]+=(.*)$',
                                   cache.read_text() if cache.exists() else '', re.M)
            command = ['ctest', '--show-only=json-v1']
            if build_type and build_type[1]:
                command += ['-C', build_type[1]]
            result = self.runner.run(command, cwd=path.parent,
                                     env=env, check=False, timeout=15)
            if result.returncode:
                raise PipelineError(f'Cannot inventory configured CTest tests: {path.parent}')
            try:
                tests = json.loads(result.stdout)['tests']
                inventory[path.parent.name] = {}
                for test in tests:
                    properties = {x['name']: x['value'] for x in test.get('properties', [])}
                    inventory[path.parent.name][test['name']] = {
                        'disabled': properties.get('DISABLED') is True,
                        'skip_regex': properties.get('SKIP_REGULAR_EXPRESSION', []),
                        'labels': properties.get('LABELS', []), 'command': test.get('command', [])}
            except (KeyError, TypeError, ValueError) as e:
                raise PipelineError(f'Invalid CTest inventory: {path.parent}') from e
        return inventory

    def validation_settings(self, p):
        profile_path = Path(getattr(self.args, 'validation_profile', VALIDATION_PROFILE)).resolve()
        profile, config = validation_profile(profile_path)
        exception_path = getattr(self.args, 'exceptions', None) or p / 'metadata/test-exceptions.json'
        exception_path = Path(exception_path).resolve()
        validate_exceptions(read_json(exception_path))
        return profile, config, {'profile': str(profile_path), 'exceptions': str(exception_path),
                                 **validation_options(self.args), 'scope': 'local'}

    def validation_fingerprint(self, p, state, settings):
        _, config = validation_profile(settings['profile'])
        return digest({'input_fingerprint': self.input_fingerprint(p, state),
                       'pipeline_sha256': file_hash(__file__), 'settings': settings,
                       'pytest_plugin_sha256': file_hash(PYTEST_PLUGIN),
                       'profile_sha256': file_hash(settings['profile']),
                       'cyclone_sha256': file_hash(config),
                       'exceptions_sha256': file_hash(settings['exceptions'])})

    def test_packages(self, p, state, attempt, env, config, profile=None, options=None, diagnostic=None):
        profile = profile or validation_profile()[0]
        options = options or validation_options(self.args)
        listed = self.runner.run(['colcon', 'list', '--base-paths', p / 'src', '--names-only',
                                 '--ignore-user-meta', '--packages-ignore',
                                 *state['config']['packages_ignore']], cwd=p, env=env).stdout.splitlines()
        if any(not re.fullmatch(r'[A-Za-z0-9_]+', package) for package in listed):
            raise PipelineError('Invalid colcon package name.')
        inventory = self.ctest_inventory(p, env)
        atomic_json(attempt / 'ctest-inventory.json', inventory)
        plan = execution_plan(listed, inventory, profile, options, diagnostic)
        paths = package_source_paths(p / 'src', listed)
        atomic_json(attempt / 'execution-plan.json', plan)
        base = attempt / 'results'
        base.mkdir()
        aggregate = empty_report()
        aggregate['unselected'] = plan['unselected']
        aggregate['mode'] = options['mode']
        events = {'completed': {}, 'errors': [], 'test_failures': []}
        timings = []
        deadline = time.monotonic() + options['test_timeout']
        for task in plan['invocations']:
            package, invocation = task['package'], task['id']
            remaining = min(deadline - time.monotonic(), options['package_timeout'])
            if remaining <= 0:
                events['errors'].append('Full-test timeout; remaining invocations were not executed.')
                break
            print(f'Testing: {invocation} (sequential, localhost)', flush=True)
            directory = attempt / 'invocations' / invocation
            directory.mkdir(parents=True)
            results = base / invocation
            results.mkdir()
            log_base = directory / 'colcon'
            args = self.colcon_args(p, state)
            args[args.index('--parallel-workers') + 1] = '1'
            command = ['colcon', '--log-base', log_base, 'test', *args,
                       '--packages-select', package, '--test-result-base', results,
                       '--return-code-on-test-failure']
            if task['ctest']:
                expression = '^(' + '|'.join(re.escape(n) for n in task['ctest']) + ')$'
                command += ['--ctest-args', ' -R', expression]
            if task['pytest_expression']:
                command += ['--pytest-args', ' -k', task['pytest_expression']]
            control = directory / 'pytest-control.json'
            atomic_json(control, {'lint_policy': options['lint_policy'], 'lint_tests': profile['lint_tests'],
                                  'lint_labels': profile['lint_labels'], 'evidence': str(directory / 'pytest-items')})
            start, started_ns = time.monotonic(), time.time_ns()
            with self.domain(directory / 'runtime') as domain_env:
                package_env = dict(env, **domain_env, CTEST_PARALLEL_LEVEL='1', MAKEFLAGS='-j1',
                                   CMAKE_BUILD_PARALLEL_LEVEL='1', PYTHONPATH=str(PYTEST_PLUGIN.parent),
                                   PYTEST_PLUGINS='ros2_validation_pytest',
                                   ROS2_VALIDATION_PYTEST_CONTROL=str(control))
                if task['middleware']:
                    package_env['RMW_IMPLEMENTATION'] = task['middleware']
                if task['pytest_expression']:
                    # CTest's nested pytest commands do not receive colcon's
                    # --pytest-args; the clean environment supplies the same filter.
                    package_env['PYTEST_ADDOPTS'] = shlex.join(['-k', task['pytest_expression']])
                if package == 'rcl_logging_spdlog':
                    home = directory / 'logging-home'
                    home.mkdir()
                    package_env.update(HOME=str(home), ROS_HOME=None, ROS_LOG_DIR=None)
                if package == 'cyclonedds':
                    package_env['CYCLONEDDS_URI'] = str(config)
                    conflicts = native_port_conflicts()
                    if conflicts:
                        events['errors'].append('Native Cyclone domain-0 ports became occupied: ' + str(conflicts))
                        break
                if diagnostic and diagnostic.get('loopback'):
                    # Diagnostics only. RMW injects its own interface when localhost
                    # is 1; avoid duplicate interface definitions by supplying the
                    # explicit, verified loopback-only XML with that injection off.
                    verify_loopback_config(config)
                    package_env.update(ROS_LOCALHOST_ONLY='0', CYCLONEDDS_URI=str(config),
                                       RMW_IMPLEMENTATION='rmw_cyclonedds_cpp')
                atomic_json(directory / 'execution.json', {'command': [str(x) for x in command],
                                                          'environment': package_env, 'timeout': remaining})
                result = self.sourced_run(p, command, cwd=p, env=package_env, check=False, timeout=remaining)
            try:
                outcome = read_test_events(log_base, [package])
                events['completed'].update({invocation: outcome['completed'][package]}
                                          if package in outcome['completed'] else {})
                events['errors'].extend(outcome['errors'])
                events['test_failures'].extend(outcome['test_failures'])
            except (PipelineError, OSError, ValueError) as e:
                events['errors'].append(str(e))
            if result.returncode == 124:
                events['errors'].append(f'Package/full-test timeout while running {invocation}.')
            elif result.returncode not in (0, 1):
                events['errors'].append(f'{invocation}: unexpected colcon exit {result.returncode}')
            collect_fresh_junit(p / 'build', results, started_ns)
            summary = self.runner.run(['colcon', 'test-result', '--test-result-base', results, '--verbose'],
                                      check=False, env=env, show=True, timeout=60)
            items = [read_json(x) for x in (directory / 'pytest-items').glob('*.json')]
            source = paths.get(package)
            expects_python = bool(task['python'] and ((source and (source / 'setup.py').exists() and
                any((source / name).exists() for name in ['test', 'tests'])) or
                any(x['selected'] for x in items)))
            report = summarize_tests(results, attempt / 'exceptions.json', p / 'metadata/exact.repos',
                                     options['lint_policy'], profile, {package: task['ctest']},
                                     expected_packages=[package], expected_python=[package] if expects_python else [])
            # A package may legitimately have no tests; only the execution plan
            # and collection evidence can explain that, never source-tree guesses.
            for collected in items:
                report['unselected'].extend(dict(x, package=package) for x in collected['unselected'])
            lint_only = (task['python'] and items and all(not x['selected'] for x in items) and
                         any(x['unselected'] for x in items) and result.returncode == 0)
            if lint_only or (not task['ctest'] and not expects_python and result.returncode == 0 and
                             not report['testcases'] and not report['failed'] and
                             not items):
                report['infrastructure'] = [x for x in report['infrastructure']
                                           if x['message'] not in ('No fresh executed testcases were produced.',
                                               'Missing fresh Python test report cannot be waived.')]
            if summary.returncode not in (0, 1):
                report['infrastructure'].append({'message': f'colcon test-result infrastructure exit {summary.returncode}'})
            elif summary.returncode and not any(report[k] for k in ['failures', 'warnings', 'waived', 'infrastructure']):
                report['infrastructure'].append({'message': 'colcon test-result reports unexplained errors.'})
            if package in events['test_failures'] and package not in report['failure_packages']:
                report['infrastructure'].append({'message': 'TestFailure event has missing failure reports.'})
            atomic_json(directory / 'tests.json', report)
            merge_report(aggregate, report, invocation, task['middleware'])
            timings.append({'invocation': invocation, 'package': package,
                            'seconds': time.monotonic() - start, 'returncode': result.returncode,
                            'timed_out': result.returncode == 124, 'jobs': package_timings(log_base)})
            atomic_json(attempt / 'tests.json', aggregate)
            atomic_json(attempt / 'timing.json', {'invocations': timings, 'options': options,
                                                'resources': resource_observation()})
            if options['mode'] == 'local' and (report['failures'] or report['infrastructure'] or events['errors']):
                break
        pending = [x['id'] for x in plan['invocations'] if x['id'] not in events['completed']]
        if pending:
            events['errors'].append(f'Invocations not completed: {pending}')
        aggregate['infrastructure'].extend({'message': x} for x in events['errors'])
        if not aggregate['testcases']:
            aggregate['infrastructure'].append({'message': 'No fresh executed testcases were produced.'})
        atomic_json(attempt / 'timing.json', {'invocations': timings, 'options': options,
                                            'pending': pending, 'resources': resource_observation()})
        atomic_json(attempt / 'events.json', events)
        return base, inventory, events, aggregate

    def validate(self):
        p, state = self.candidate()
        with self.lock(p / '.candidate.lock'), self.lock(self.root / '.validation.lock'), subreaper():
            self.require(state, 'build')
            self.unchanged(p, state)
            if package_versions(self.runner) != state['build_dependencies']:
                raise PipelineError('System packages changed since build; rerun build before validation.')
            profile, config, settings = self.validation_settings(p)
            options = validation_options(self.args)
            with self.stage(p, state, 'validate'):
                self.dependency_check(p, state)
                fingerprint = self.validation_fingerprint(p, state, settings)
                attempt = p / 'logs' / f'validation-{unique_id()}'
                attempt.mkdir()
                atomic_json(attempt / 'profile.json', profile)
                shutil.copyfile(config, attempt / 'cyclonedds.xml')
                shutil.copyfile(settings['exceptions'], attempt / 'exceptions.json')
                env = dict(self.colcon_env(p, state), ROS_LOCALHOST_ONLY='1',
                           ROS_LOG_DIR=str(attempt / 'ros-logs'), ROS_HOME=str(attempt / 'ros-home'))
                smoke = self.smoke_checks(p)
                atomic_json(attempt / 'smoke.json', smoke)
                atomic_json(p / 'metadata/smoke.json', smoke)
                reports = empty_report()
                reports['mode'] = options['mode']
                conflicts = native_port_conflicts() if options['mode'] == 'full' else []
                atomic_json(attempt / 'preflight.json', {'smoke_blockers': smoke_blockers(smoke),
                                                       'native_port_conflicts': conflicts})
                base = attempt / 'results'
                try:
                    if conflicts:
                        raise PipelineError('Native Cyclone tests require unused DDS domain-0 ports: ' +
                                            str(conflicts) + '; stop your own nodes/daemon and retry.')
                    if smoke_blockers(smoke):
                        raise PipelineError('Communication preflight failed: ' + '; '.join(smoke_blockers(smoke)))
                    base, _, _, reports = self.test_packages(p, state, attempt, env, attempt / 'cyclonedds.xml',
                                                            profile, options)
                except (PipelineError, OSError, ValueError, KeyboardInterrupt) as e:
                    if (attempt / 'tests.json').exists():
                        reports = read_json(attempt / 'tests.json')
                    reports['infrastructure'].append({'message': str(e) or type(e).__name__})
                try:
                    self.unchanged(p, state)
                except PipelineError as e:
                    reports['infrastructure'].append({'message': str(e)})
                deps = package_versions(self.runner)
                if deps != state['build_dependencies']:
                    reports['infrastructure'].append({'message': 'System packages changed during validation.'})
                if fingerprint != self.validation_fingerprint(p, state, settings):
                    reports['infrastructure'].append({'message': 'Validation configuration changed during execution.'})
                atomic_json(attempt / 'tests.json', reports)
                atomic_json(p / 'metadata/tests.json', reports)
                print(json.dumps({k: len(reports[k]) if isinstance(reports[k], list) else reports[k]
                                  for k in ['mode', 'passed', 'failed', 'skipped', 'disabled', 'unselected',
                                            'warnings', 'waived', 'failures', 'infrastructure']}, indent=2), flush=True)
                blockers = [x['message'] for x in reports['infrastructure']]
                blockers += [f"{x['package']}: {x['classname']} {x['name']}" for x in reports['failures']]
                blockers += smoke_blockers(smoke)
                if blockers:
                    raise PipelineError(f'Validation blocked ({len(reports["infrastructure"])} infrastructure errors); '
                                        f'inspect {attempt}/tests.json and smoke.json: ' + '; '.join(blockers[:8]))
                state['validation'] = {'finished': now(), 'scope': 'local', 'mode': options['mode'],
                                       'settings': settings, 'fingerprint': fingerprint,
                                       'input_fingerprint': state['input_fingerprint'], 'dependencies': deps,
                                       'reports': str(base), 'attempt': str(attempt), 'warnings': reports['warnings'],
                                       'coverage': {k: reports[k] for k in ['testcases', 'passed', 'failed', 'skipped', 'disabled']},
                                       'unselected': len(reports['unselected']), **options}
            print(f'Validated candidate for LOCAL use ({options["mode"]} profile): {p} '
                  f'({len(reports["warnings"])} warnings). LAN communication is not certified.')

    def smoke_checks(self, p, check_network=False):
        setup = p / 'install/local_setup.bash'
        results = []
        directory = p / 'logs' / f'smoke-{unique_id()}'
        directory.mkdir()
        for local in (['1', '0'] if check_network else ['1']):
            for rmw in ['rmw_fastrtps_cpp', 'rmw_cyclonedds_cpp']:
                checks = [('pubsub', 'demo_nodes_cpp', 'demo_nodes_py'),
                          ('pubsub', 'demo_nodes_py', 'demo_nodes_cpp'),
                          ('service', 'demo_nodes_cpp', 'demo_nodes_py'),
                          ('action', 'action_tutorials_cpp', 'action_tutorials_py')]
                for index, (kind, server, client) in enumerate(checks):
                    output = directory / f'{local}-{rmw}-{index}.log'
                    entry = {'rmw': rmw, 'kind': kind, 'localhost': local, 'log': str(output)}
                    try:
                        with self.domain(directory / f'{local}-{rmw}-{index}') as domain_env:
                            env = dict(self.runner.env, **domain_env, RMW_IMPLEMENTATION=rmw)
                            env['ROS_LOCALHOST_ONLY'] = local
                            if kind == 'pubsub':
                                result = smoke_pair(setup, server, client, '/candidate_' + uuid.uuid4().hex,
                                                    env, output, timeout=30, discovery=True)
                            else:
                                result = smoke_exchange(setup, kind, env, output, timeout=30)
                            entry.update(result)
                    except (PipelineError, OSError, ValueError) as e:
                        entry.update(passed=False, error=str(e))
                    results.append(entry)
        try:
            with self.domain(directory / 'package-check') as domain_env:
                packages = self.sourced_run(p, ['ros2', 'pkg', 'list'], env=domain_env,
                                            timeout=15).stdout.splitlines()
                missing = sorted(set(REQUIRED_ROS) - set(packages))
                if missing:
                    results.append({'kind': 'packages', 'passed': False, 'error': f'Missing required ROS packages: {missing}'})
        except (PipelineError, OSError) as e:
            results.append({'kind': 'packages', 'passed': False, 'error': str(e)})
        return results

    def diagnose(self):
        p, state = self.candidate()
        with self.lock(p / '.candidate.lock'), self.lock(self.root / '.validation.lock'), subreaper():
            self.require(state, 'build')
            self.unchanged(p, state)
            profile, config, settings = self.validation_settings(p)
            packages = getattr(self.args, 'packages', None)
            if not packages and any(getattr(self.args, key, None) for key in
                                    ['ctest_regex', 'pytest_expression', 'compare_cyclone', 'rmw']):
                raise PipelineError('Diagnostic test selectors/middleware require --packages.')
            attempt = p / 'logs' / f'diagnose-{unique_id()}'
            attempt.mkdir()
            old_log, self.runner.log = self.runner.log, attempt / 'commands.log'
            try:
                prior = p / 'metadata/tests.json'
                reports = read_json(prior) if prior.exists() else empty_report()
                atomic_json(attempt / 'saved-tests.json', reports)
                atomic_json(attempt / 'tests.json', reports)
                if packages:
                    options = validation_options(self.args)
                    options['mode'] = 'full'  # Explicit diagnostic selectors; no certification.
                    deadline = time.monotonic() + options['test_timeout']
                    variants = ['localhost', 'loopback'] if getattr(self.args, 'compare_cyclone', False) else ['localhost']
                    for variant in variants:
                        directory = attempt / variant
                        directory.mkdir()
                        shutil.copyfile(settings['exceptions'], directory / 'exceptions.json')
                        shutil.copyfile(config, directory / 'cyclonedds.xml')
                        env = self.colcon_env(p, state)
                        if getattr(self.args, 'rmw', None):
                            env['RMW_IMPLEMENTATION'] = self.args.rmw
                        if len(variants) > 1:
                            env['RMW_IMPLEMENTATION'] = 'rmw_cyclonedds_cpp'
                        diagnostic = {'packages': packages, 'ctest_regex': getattr(self.args, 'ctest_regex', None),
                                      'pytest_expression': getattr(self.args, 'pytest_expression', None),
                                      'compare_cyclone': len(variants) > 1,
                                      'loopback': variant == 'loopback'}
                        try:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise PipelineError('Diagnostic test budget exhausted; comparison not completed.')
                            variant_options = dict(options, test_timeout=remaining)
                            _, _, _, result = self.test_packages(p, state, directory, env,
                                directory / 'cyclonedds.xml', profile, variant_options, diagnostic)
                        except (PipelineError, OSError, ValueError) as e:
                            result = read_json(directory / 'tests.json') if (directory / 'tests.json').exists() else empty_report()
                            result['infrastructure'].append({'message': str(e)})
                        atomic_json(directory / 'tests.json', result)
                smoke = self.smoke_checks(p, check_network=getattr(self.args, 'check_network', False))
                atomic_json(attempt / 'smoke.json', smoke)
                atomic_json(attempt / 'native-port-conflicts.json', native_port_conflicts())
                print(f'Diagnostic reports: {attempt}\nReadiness was not granted. Local checks: ' +
                      str(not smoke_blockers([x for x in smoke if x.get('localhost', '1') == '1'])))
                print('Saved blocking entries:', len(reports.get('failures', [])) + len(reports.get('infrastructure', [])))
            finally:
                self.runner.log = old_log

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
            if validation.get('fingerprint') != self.validation_fingerprint(p, state, validation['settings']):
                raise PipelineError('Validation code or policy changed; rerun validate (no ROS rebuild required).')
            print(f"Certification: LOCAL use ({validation.get('mode', 'legacy-full')} profile); "
                  f"{len(validation.get('warnings', []))} warnings. LAN unverified.")
            print('Coverage:', validation.get('coverage', {}), 'Unselected entries:', validation.get('unselected', 0))
            if validation.get('mode') == 'local':
                print('Passing selected tests does not resolve failures in omitted full-suite tests; prior logs are retained.')
            print('Open a fresh terminal, then run:\n' + activation_command(p) + '\nexport ROS_LOCALHOST_ONLY=1')
            print('Workspace rollback in another fresh terminal:\n' +
                  'source ' + shlex.quote(str(self.legacy / 'install/local_setup.bash')))
            print('No shell settings were changed. Workspace rollback does not undo apt changes.')

    def status(self):
        if self.args.candidate:
            p, state = self.candidate()
            print('Candidate:', p, 'Source:', state['source'])
            total_seconds = 0
            for name, stage in state['stages'].items():
                duration = stage.get('duration_seconds')
                if duration is None and stage.get('finished'):
                    duration = (datetime.fromisoformat(stage['finished']) - datetime.fromisoformat(stage['started'])).total_seconds()
                print(f'{name}: {stage["status"]}; seconds={duration}; log={stage.get("log", "")}')
                total_seconds += duration or 0
                if stage.get('error'):
                    print('  Error:', stage['error'][:500])
            print('Recorded stage time (latest attempts):', round(total_seconds, 2), 'seconds')
            try:
                self.unchanged(p, state)
                validation = state.get('validation', {})
                current = (validation.get('dependencies') == package_versions(self.runner) and
                           bool(validation.get('fingerprint')) and
                           validation['fingerprint'] == self.validation_fingerprint(p, state, validation['settings']))
                print('Certification:', validation.get('scope', 'unvalidated'))
                print('Validation mode:', validation.get('mode', 'unvalidated'))
                print('Coverage:', validation.get('coverage', {}), 'Unselected entries:', validation.get('unselected', 0))
                if validation.get('mode') == 'local':
                    print('Omitted full-suite failures are not resolved by local validation; see prior attempt logs.')
                print('Warnings:', len(validation.get('warnings', [])))
                print('Ready:', bool(current and state['stages'].get('validate', {}).get('status') == 'passed'))
            except PipelineError as e:
                print(f'Ready: False ({e})')
            timing = p / 'metadata/build-timing.json'
            if timing.exists():
                record = read_json(timing)
                print('Slowest build packages:', record.get('packages', [])[:5])
                if record.get('ccache_statistics'):
                    print('ccache:', record['ccache_statistics'])
            for attempt in sorted((p / 'logs').glob('validation-*'), reverse=True)[:1]:
                timing = attempt / 'timing.json'
                if timing.exists():
                    record = read_json(timing)
                    print('Slowest test invocations:', sorted(record['invocations'], key=lambda x: x['seconds'], reverse=True)[:5])
                    print('Timing report:', timing)
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


def validation_profile(path=VALIDATION_PROFILE):
    doc = read_json(path)
    fields = {
            'schema_version', 'scope', 'lint_tests', 'lint_labels',
            'network_only_tests', 'cyclone_config'}
    if (not isinstance(doc, dict) or doc.get('schema_version') not in (1, 2) or
            set(doc) != fields | ({'local_tests'} if doc.get('schema_version') == 2 else set()) or
            doc['scope'] != 'local'):
        raise PipelineError('Invalid local validation profile.')
    if doc['schema_version'] == 2:
        if not isinstance(doc['local_tests'], dict) or not doc['local_tests']:
            raise PipelineError('Local validation needs a nonempty package allowlist.')
        for package, spec in doc['local_tests'].items():
            if (not re.fullmatch(r'[A-Za-z0-9_]+', package) or not isinstance(spec, dict) or
                    set(spec) != {'patterns', 'middleware'} or type(spec['middleware']) is not bool or
                    not isinstance(spec['patterns'], list) or not spec['patterns']):
                raise PipelineError('Invalid local test selector.')
            for pattern in spec['patterns']:
                if not isinstance(pattern, str) or not pattern.startswith('^') or not pattern.endswith('$'):
                    raise PipelineError('Local CTest selectors must be anchored.')
                try:
                    re.compile(pattern.replace('{rmw}', 'rmw_fastrtps_cpp'))
                except re.error as e:
                    raise PipelineError('Invalid local CTest expression.') from e
    for key in ['lint_tests', 'lint_labels']:
        if not isinstance(doc[key], list) or not all(
                isinstance(x, str) and re.fullmatch(r'[a-z][a-z0-9_]*', x) for x in doc[key]):
            raise PipelineError(f'Invalid validation profile {key}.')
    if not isinstance(doc['network_only_tests'], list):
        raise PipelineError('network_only_tests must be a list.')
    for test in doc['network_only_tests']:
        if (not isinstance(test, dict) or set(test) != {
                'package', 'classname', 'name', 'revision', 'message_prefix', 'reason'} or
                not all(isinstance(x, str) and x.strip() for x in test.values()) or
                not re.fullmatch(r'[0-9a-f]{40}', test['revision'])):
            raise PipelineError('Network-only tests require exact identity, revision, failure prefix and reason.')
    if not isinstance(doc['cyclone_config'], str) or not doc['cyclone_config']:
        raise PipelineError('Cyclone configuration must be a profile-relative filename.')
    config = Path(path).parent / doc['cyclone_config']
    if not contained(config, Path(path).parent) or not config.is_file():
        raise PipelineError('Unsafe or missing Cyclone validation configuration.')
    return doc, config


def validation_options(args):
    mode = getattr(args, 'validation_mode', 'local')
    return {'mode': mode, 'lint_policy': getattr(args, 'lint_policy', None) or
            ('skip' if mode == 'local' else 'warn'),
            'test_timeout': getattr(args, 'test_timeout', None) or (1800 if mode == 'local' else 7200),
            'package_timeout': getattr(args, 'package_timeout', None) or (300 if mode == 'local' else 900)}


def empty_report():
    return dict(testcases=0, passed=0, failed=0, skipped=0, disabled=0, files=[],
                failures=[], warnings=[], waived=[], infrastructure=[], failure_packages=[], unselected=[])


def package_source_paths(source, discovered):
    paths = {}
    for path in Path(source).rglob('package.xml'):
        try:
            name = ET.parse(path).getroot().findtext('name')
        except ET.ParseError:
            continue
        if name in discovered and not any((parent / marker).exists() for parent in
                [path.parent, *path.parent.parents] if contained(parent, source)
                for marker in ['AMENT_IGNORE', 'COLCON_IGNORE']):
            paths[name] = path.parent
    return paths


def merge_report(target, report, invocation, middleware):
    for key in ['testcases', 'passed', 'failed', 'skipped', 'disabled']:
        target[key] += report[key]
    for key in ['files', 'failure_packages']:
        target[key] = sorted(set(target[key] + report[key]))
    for key in ['failures', 'warnings', 'waived', 'infrastructure', 'unselected']:
        target[key].extend(dict(x, invocation=invocation, middleware=middleware) for x in report[key])


def verify_loopback_config(path):
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        raise PipelineError('Malformed diagnostic loopback configuration.') from e
    interfaces = root.findall('.//NetworkInterface')
    if not interfaces or any(x.get('address') != '127.0.0.1' or x.get('autodetermine') == 'true'
                             for x in interfaces) or root.find('.//Peers') is not None:
        raise PipelineError('Explicit-loopback diagnostics require loopback-only interfaces and no remote peers.')


def is_lint_test(name, metadata, profile):
    return (name in profile['lint_tests'] or
            bool(set(metadata.get('labels', [])) & set(profile['lint_labels'])))


def execution_plan(packages, inventory, profile, options, diagnostic=None):
    """Resolve required selectors before starting any tests; never broaden silently."""
    tasks, unselected = [], []
    mode, lint = options['mode'], options['lint_policy']
    requested = diagnostic.get('packages') if diagnostic else None
    selected_packages = requested or (list(profile.get('local_tests', {})) if mode == 'local' else packages)
    if mode == 'local' and not selected_packages:
        raise PipelineError('Local mode needs a schema-2 profile with required test selectors.')
    missing = set(selected_packages) - set(packages)
    if missing:
        raise PipelineError(f'Required packages were not discovered by colcon: {sorted(missing)}')
    for package in packages:
        configured = inventory.get(package, {})
        if package not in selected_packages:
            unselected.append({'package': package, 'reason': 'outside selected profile',
                               'tests': sorted(configured)})
            continue
        spec = profile.get('local_tests', {}).get(package, {}) if mode == 'local' else {}
        implementations = MIDDLEWARES if spec.get('middleware') else [None]
        for rmw in implementations:
            selected = set()
            for pattern in spec.get('patterns', []):
                matches = {name for name in configured if re.fullmatch(
                    pattern.replace('{rmw}', re.escape(rmw or '')), name)}
                if not matches:
                    raise PipelineError(f'{package}: required selector matched no configured tests: {pattern} ({rmw})')
                selected.update(matches)
            if not spec:
                selected = set(configured)
            elif lint != 'skip':
                selected.update(name for name, meta in configured.items() if is_lint_test(name, meta, profile))
            if diagnostic and diagnostic.get('ctest_regex'):
                selected = {name for name in selected if re.search(diagnostic['ctest_regex'], name)}
                if configured and not selected:
                    raise PipelineError(f'{package}: diagnostic CTest selector matched no tests.')
            if diagnostic and diagnostic.get('compare_cyclone'):
                if not configured:
                    raise PipelineError('Cyclone comparison requires CTest selections; diagnose Python packages separately with --rmw.')
                for name in selected:
                    command = ' '.join(configured[name].get('command', []))
                    if any(other in name or f'RMW_IMPLEMENTATION={other}' in command
                           for other in ['rmw_fastrtps_cpp', 'rmw_fastrtps_dynamic_cpp', 'rmw_connextdds']):
                        raise PipelineError('Cyclone comparison cannot include other DDS implementations; restrict --ctest-regex.')
            excluded = set()
            if lint == 'skip':
                excluded = {name for name in selected if is_lint_test(name, configured[name], profile)}
                selected -= excluded
            omitted = sorted(set(configured) - selected)
            if omitted:
                unselected.append({'package': package, 'middleware': rmw, 'tests': omitted,
                                   'lint_tests': sorted(excluded), 'reason': 'test selection/lint policy'})
            if configured and not selected:
                if mode == 'local':
                    raise PipelineError(f'{package}: no required runtime tests remain.')
                continue  # Full-profile lint-only package was intentionally excluded.
            tasks.append({'id': f'{package}-{rmw or "default"}', 'package': package,
                          'middleware': rmw, 'ctest': {n: configured[n] for n in sorted(selected)},
                          'python': not bool(configured),
                          'pytest_expression': diagnostic.get('pytest_expression') if diagnostic else None})
    if not tasks:
        raise PipelineError('No test invocations selected.')
    return {'invocations': tasks, 'unselected': unselected}


def package_timings(log_base):
    rows = []
    latest = Path(log_base) / 'latest_build/events.log'
    if not latest.is_file():
        latest = Path(log_base) / 'latest_test/events.log'
    if not latest.is_file():
        return rows
    starts = {}
    for line in latest.read_text(errors='replace').splitlines():
        match = re.match(r'\[([\d.]+)\] \(([^)]+)\) (JobStarted|JobEnded):', line)
        if match:
            stamp, package, event = match.groups()
            if event == 'JobStarted':
                starts[package] = float(stamp)
            elif package in starts:
                rows.append({'package': package, 'seconds': float(stamp) - starts[package]})
    return sorted(rows, key=lambda x: x['seconds'], reverse=True)


def resource_observation():
    memory = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        if key in ('MemAvailable', 'MemTotal', 'SwapTotal', 'SwapFree'):
            memory[key + '_KiB'] = int(value.split()[0])
    return {'time': now(), 'logical_cpus': os.cpu_count(), 'load_average': list(os.getloadavg()),
            'memory': memory}


def native_port_conflicts():
    """Best-effort socket ownership, without signals or network changes."""
    sockets = {}
    for suffix in ('', '6'):
        for line in Path('/proc/net/udp' + suffix).read_text().splitlines()[1:]:
            fields = line.split()
            port = int(fields[1].rsplit(':', 1)[1], 16)
            if 7400 <= port < 7650:
                sockets[fields[9]] = {'port': port, 'owners': []}
    for process in Path('/proc').glob('[0-9]*'):
        try:
            for fd in (process / 'fd').iterdir():
                target = os.readlink(fd)
                if target.startswith('socket:[') and target[8:-1] in sockets:
                    owner = {'pid': int(process.name), 'name': (process / 'comm').read_text().strip()}
                    if owner not in sockets[target[8:-1]]['owners']:
                        sockets[target[8:-1]]['owners'].append(owner)
        except (OSError, ValueError):
            continue
    return sorted(sockets.values(), key=lambda x: x['port'])


def summarize_tests(base, exception_file, exact_file, lint_policy='warn', profile=None,
                    ctest_metadata=None, expected_packages=None, expected_python=None):
    """Collect every outcome. Malformed/missing reports are data, never waivers."""
    doc = read_json(exception_file)
    validate_exceptions(doc)
    profile = profile or validation_profile()[0]
    ctest_metadata = ctest_metadata or {}
    exact = manifest_load(exact_file)
    repo_by_package, python_reports = {}, set()
    candidate = Path(exact_file).parent.parent
    report = empty_report()
    for name in exact:
        for path in (candidate / 'src' / name).rglob('package.xml'):
            try:
                package = ET.parse(path).getroot().findtext('name')
                if package:
                    repo_by_package[package] = name
                    ignored = any((parent / marker).exists() for parent in
                                  [path.parent, *path.parent.parents]
                                  if contained(parent, candidate / 'src')
                                  for marker in ['AMENT_IGNORE', 'COLCON_IGNORE'])
                    if (not ignored and (expected_packages is None or package in expected_packages) and
                            (path.parent / 'setup.py').exists() and any(
                            list((path.parent / directory).rglob('test_*.py')) for directory in ['test', 'tests'])):
                        python_reports.add(package)
            except ET.ParseError:
                report['infrastructure'].append({'message': f'Malformed package.xml: {path}'})
    if expected_python is not None:
        python_reports = set(expected_python)

    def identity(package, classname, name, path):
        repo = repo_by_package.get(package)
        return dict(package=package, classname=classname, name=name,
                    revision=exact[repo]['version'] if repo else '', file=str(path))

    def fail(item, message, infrastructure=False, labels=()):
        item = dict(item, message=message)
        if item.get('package'):
            report['failure_packages'].append(item['package'])
        if infrastructure:
            report['infrastructure'].append(item)
            return
        match = next((e for e in doc['exceptions'] if all(e[k] == item[k]
                      for k in ['package', 'classname', 'name', 'revision'])), None)
        if match:
            report['waived'].append(dict(item, reason=match['reason']))
        elif lint_policy == 'warn' and (item['name'] in profile['lint_tests'] or
                                       set(labels).intersection(profile['lint_labels'])):
            report['warnings'].append(dict(item, category='lint', reason='Lint policy: warn'))
        else:
            network = next((e for e in profile['network_only_tests'] if
                            all(e[k] == item[k] for k in ['package', 'classname', 'name', 'revision']) and
                            message.startswith(e['message_prefix'])), None)
            if network:
                report['warnings'].append(dict(item, category='network', reason=network['reason']))
            else:
                report['failures'].append(item)

    wrappers, detailed = [], {}
    for path in sorted(Path(base).rglob('*.xml')):
        package = path.relative_to(base).parts[0]
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError) as e:
            fail(identity(package, '', '', path), f'Malformed test result: {e}', True)
            continue
        if root.tag == 'Site':
            report['files'].append(str(path))
            if root.find('Testing') is None:
                fail(identity(package, 'CTest', '', path), 'Invalid CTest report', True)
            wrappers.extend((package, case, path) for case in root.findall('Testing/Test'))
            continue
        if root.tag not in ('testsuite', 'testsuites'):
            continue
        report['files'].append(str(path))
        cases = list(root.iter('testcase'))
        suites = [root] if root.tag == 'testsuite' else root.findall('testsuite')
        try:
            counters = [int(suite.get(key, '0')) for suite in suites
                        for key in ['tests', 'failures', 'errors', 'skipped']]
            if any(value < 0 for value in counters):
                raise ValueError('Negative test suite counter')
            declared = sum(int(x.get('failures', '0')) + int(x.get('errors', '0')) for x in suites)
            if declared > sum(any(x.tag in ('failure', 'error') for x in c) for c in cases):
                fail(identity(package, '', '', path), 'Test suite failure has no identifiable testcase', True)
        except ValueError:
            fail(identity(package, '', '', path), 'Invalid test suite counters', True)
        records = []
        for case in cases:
            item = identity(package, case.get('classname', ''), case.get('name', ''), path)
            issues = [x for x in case if x.tag in ('failure', 'error')]
            suppressed = case.get('status') == 'notrun' and case.get('result') == 'suppressed'
            if case.get('status') == 'notrun' and not suppressed:
                fail(item, 'JUnit testcase did not execute for an unknown reason', True)
                continue
            if case.find('skipped') is not None or case.get('result') == 'skipped' or suppressed:
                if issues:
                    fail(item, 'Test is simultaneously skipped and failed', True)
                report['disabled' if suppressed else 'skipped'] += 1
                continue
            report['testcases'] += 1
            if not issues:
                report['passed'] += 1
                continue
            report['failed'] += 1
            records.append(item)
            message = '\n'.join(x.get('message', '') + '\n' + (x.text or '') for x in issues).strip()
            missing = ('missing_result' in item['name'] or
                       any(text in message for text in ['without generating a result',
                                                       'The test did not generate a result file.']))
            # Execution markers/types are authoritative. A traceback that merely
            # quotes a timeout assertion is an ordinary reported test failure.
            timeout = any(x.get('type', '').rsplit('.', 1)[-1] in
                          ('TimeoutError', 'TimeoutExpired') or
                          re.fullmatch(r'Timeout(?: \(>[^)]+\))?', x.get('message', ''))
                          for x in issues)
            timeout = timeout or bool(re.fullmatch(
                r'AssertionError: (?:Waiting for output timed out|Timed out waiting for process .+)',
                message.splitlines()[-1] if message else ''))
            crash = any(x.get('message', '').startswith(('Segmentation fault', 'Process crashed',
                                                        'Aborted (core dumped)')) for x in issues)
            infra = missing or timeout or crash or any(x.tag == 'error' for x in issues)
            if missing:
                message = 'Missing test result cannot be waived: ' + message
            elif timeout:
                message = 'Test timeout cannot be waived: ' + message
            labels = [label for metadata in ctest_metadata.get(package, {}).values()
                      if path.name in ' '.join(metadata.get('command', []))
                      for label in metadata.get('labels', [])]
            fail(item, message, infra, labels)
        detailed[(package, path.name)] = records

    for package, case, path in wrappers:
        name, status = case.findtext('Name', ''), case.get('Status')
        item = identity(package, 'CTest', name, path)
        completion = case.findtext('Results/NamedMeasurement[@name="Completion Status"]/Value', '')
        properties = ctest_metadata.get(package, {}).get(name, {})
        if status == 'notrun':
            if re.fullmatch(r'SKIP_RETURN_CODE=-?\d+', completion):
                report['skipped'] += 1
            elif completion == 'Disabled' and properties.get('disabled') is True:
                report['disabled'] += 1
            elif completion == 'SKIP_REGULAR_EXPRESSION_MATCHED' and properties.get('skip_regex'):
                report['skipped'] += 1
            else:
                fail(item, f'CTest did not execute: {name} ({completion or "unknown reason"})', True)
            continue
        if status not in ('passed', 'failed'):
            fail(item, f'Invalid CTest status: {status}', True)
            continue
        # Match the actual detailed result named in the ament command first.
        command = case.findtext('FullCommandLine', '') + ' ' + case.findtext(
            'Results/NamedMeasurement[@name="Command Line"]/Value', '')
        linked = [key for key in detailed if key[0] == package and
                  (key[1] in command or key[1] == name + '.xml' or key[1].startswith(name + '.'))]
        if not linked:
            report['testcases'] += 1
            report['passed' if status == 'passed' else 'failed'] += 1
        if status == 'failed':
            if completion not in ('Completed', 'Failed'):
                fail(item, f'CTest infrastructure failure: {completion}', True)
            elif not any(detailed[key] for key in linked):
                fail(item, case.findtext('Results/Measurement/Value', '') or 'CTest failed',
                     labels=properties.get('labels', []))
        elif any(detailed[key] for key in linked):
            fail(item, 'CTest passed but its detailed report failed', True)
    for package in sorted(python_reports):
        if not any(pkg == package for pkg, _ in detailed):
            report['infrastructure'].append({'package': package,
                                             'message': 'Missing fresh Python test report cannot be waived.'})
    for package, configured in ctest_metadata.items():
        observed = {case.findtext('Name', '') for pkg, case, _ in wrappers if pkg == package}
        missing = sorted(set(configured) - observed)
        if missing:
            report['infrastructure'].append({'package': package,
                                             'message': f'Configured CTest tests have no fresh outcome: {missing}'})
    report['failure_packages'] = sorted(set(report['failure_packages']))
    if not report['files'] or report['testcases'] == 0:
        report['infrastructure'].append({'message': 'No fresh executed testcases were produced.'})
    return report


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


def smoke_blockers(results):
    blockers = [x.get('error', 'Communication check failed') for x in results if not x['passed']]
    for rmw in ['rmw_fastrtps_cpp', 'rmw_cyclonedds_cpp']:
        local = [x for x in results if x.get('rmw') == rmw and x.get('localhost') == '1']
        directions = {(x.get('talker'), x.get('listener')) for x in local if x.get('kind') == 'pubsub'}
        if not {('demo_nodes_cpp', 'demo_nodes_py'), ('demo_nodes_py', 'demo_nodes_cpp')}.issubset(directions):
            blockers.append(f'Missing mandatory pub/sub checks: {rmw}')
        for kind in ['service', 'action']:
            if not any(x.get('kind') == kind for x in local):
                blockers.append(f'Missing mandatory {kind} check: {rmw}')
    return blockers


def smoke_pair(setup, talker, listener, topic, env, output, timeout=30, popen=subprocess.Popen,
               discovery=False):
    processes = []
    start = time.monotonic()
    try:
        with Path(output).open('w+') as log:
            for package, executable in [(listener, 'listener'), (talker, 'talker')]:
                command = ['/bin/bash', '--noprofile', '--norc', '-c',
                           'set -e; source "$1"; shift; exec "$@"', 'smoke', str(setup),
                           'ros2', 'run', package, executable, '--ros-args', '-r', f'chatter:={topic}']
                if discovery:
                    command += ['-r', f'__node:=candidate_{executable}']
                processes.append(popen(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True))
            while time.monotonic() - start < timeout:
                log.flush()
                text = Path(output).read_text(errors='replace')
                if all(p.poll() is None for p in processes) and 'Publishing' in text and 'I heard' in text:
                    if discovery:
                        with tempfile.TemporaryFile(mode='w+') as graph_output:
                            command = ['/bin/bash', '--noprofile', '--norc', '-c',
                                       'set -e; source "$1"; shift; exec "$@"', 'smoke', str(setup),
                                       'ros2', 'node', 'list', '--no-daemon', '--spin-time', '1']
                            graph = popen(command, env=env, stdout=graph_output, stderr=subprocess.STDOUT,
                                          start_new_session=True)
                            processes.append(graph)
                            try:
                                code = graph.wait(timeout=max(.01, timeout - (time.monotonic() - start)))
                            except subprocess.TimeoutExpired as e:
                                raise PipelineError(f'Smoke discovery timeout: {output}') from e
                            graph_output.seek(0)
                            names = graph_output.read()
                            log.write('\n[daemon-free discovery]\n' + names)
                            log.flush()
                            if code or not {'/candidate_talker', '/candidate_listener'}.issubset(names.splitlines()):
                                raise PipelineError(f'Smoke discovery failed: {output}')
                    return {'rmw': env['RMW_IMPLEMENTATION'], 'talker': talker, 'listener': listener,
                            'passed': True, 'log': str(output), 'seconds': time.monotonic() - start}
                if any(p.poll() is not None for p in processes):
                    raise PipelineError(f'Smoke process exited before communication: {output}')
                time.sleep(0.1)
        raise PipelineError(f'Smoke timeout ({timeout}s): {output}')
    finally:
        for proc in processes:
            Runner.stop(proc)


def smoke_exchange(setup, kind, env, output, timeout=30, popen=subprocess.Popen):
    service = kind == 'service'
    package = 'demo_nodes' if service else 'action_tutorials'
    executable = 'add_two_ints' if service else 'fibonacci_action'
    resource = 'add_two_ints' if service else 'fibonacci'
    remap = resource + ':=/candidate_' + uuid.uuid4().hex
    processes = []
    start = time.monotonic()
    try:
        with Path(output).open('w+') as log:
            for language, role in [('cpp', 'server'), ('py', 'client')]:
                args = ['/bin/bash', '--noprofile', '--norc', '-c',
                        'set -e; source "$1"; shift; exec "$@"', 'smoke', str(setup),
                        'ros2', 'run', package + '_' + language, executable + '_' + role,
                        '--ros-args', '-r', remap]
                processes.append(popen(args, env=env, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True))
            server, client = processes
            try:
                code = client.wait(timeout=max(.01, timeout - (time.monotonic() - start)))
            except subprocess.TimeoutExpired as e:
                raise PipelineError(f'Smoke {kind} timeout ({timeout}s): {output}') from e
            log.flush()
            text = Path(output).read_text(errors='replace')
            if service:
                received = re.search(r'Result of add_two_ints: 5\b', text)
            else:
                match = re.search(r"Result: array\('i', \[([0-9, ]+)\]\)", text)
                received = bool(match and [int(x) for x in match[1].split(',')] ==
                                [0, 1, 1, 2, 3, 5, 8, 13, 21, 34, 55])
            if code or server.poll() is not None or not received:
                raise PipelineError(f'Smoke {kind} failed or returned an unexpected result: {output}')
            return {'passed': True, 'seconds': time.monotonic() - start}
    finally:
        for process in processes:
            Runner.stop(process)


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


def regex_option(value):
    try:
        re.compile(value)
    except re.error as e:
        raise argparse.ArgumentTypeError(f'Invalid regular expression: {e}') from e
    return value


def parser():
    p = argparse.ArgumentParser(description='Prepare and validate isolated ROS 2 Humble candidates; activation is manual.')
    sub = p.add_subparsers(dest='command', required=True)
    for verb in ['baseline', 'bootstrap', 'prepare', 'deps', 'build', 'validate', 'activate', 'run', 'status', 'diagnose']:
        descriptions = {'validate': 'Run selected local tests (default) or full upstream tests; require local DDS checks.',
                        'diagnose': 'Inspect saved results and run quick checks without granting readiness.',
                        'activate': 'Verify current local certification and print commands for a fresh terminal.'}
        q = sub.add_parser(verb, description=descriptions.get(verb), help=descriptions.get(verb))
        q.set_defaults(candidate=None, baseline=None, manifest=None)
        q.add_argument('--root', type=Path, default=Path.home() / 'ros2_humble_candidates',
                       help='Runtime root (default: ~/ros2_humble_candidates).')
        if verb in ['deps', 'build', 'validate', 'activate', 'status', 'diagnose']:
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
            q.add_argument('--workers', type=positive, help='Package workers: captured baseline value, otherwise 2.')
            q.add_argument('--jobs', type=positive, help='Compiler jobs: captured baseline value, otherwise 3.')
        if verb in ['bootstrap', 'prepare', 'run']:
            q.add_argument('--ccache', action='store_true', help='Opt in to compiler caching; bootstrap/run install ccache.')
        if verb == 'diagnose':
            q.add_argument('--check-network', action='store_true', help='Also diagnose default-interface DDS communication; never certify LAN use.')
            q.add_argument('--packages', nargs='+', help='Run bounded diagnostics for these discovered packages.')
            q.add_argument('--ctest-regex', type=regex_option, help='Diagnostic-only CTest name expression.')
            q.add_argument('--pytest-expression', help='Diagnostic-only pytest -k expression.')
            q.add_argument('--rmw', choices=[*MIDDLEWARES, 'rmw_fastrtps_dynamic_cpp'],
                           help='Middleware for diagnostic subprocesses.')
            q.add_argument('--compare-cyclone', action='store_true',
                           help='Compare normal localhost and explicit loopback multicast; requires --packages.')
        if verb in ['validate', 'run', 'diagnose']:
            if verb != 'diagnose':
                q.add_argument('--validation-mode', choices=['local', 'full'], default='local')
            q.add_argument('--lint-policy', choices=['skip', 'warn', 'strict'],
                           help='Default: skip in local mode, warn in full mode. warn still executes lint.')
            q.add_argument('--validation-profile', type=Path, default=VALIDATION_PROFILE,
                           help='Local scope, exact lint/network classifications and Cyclone configuration.')
        if verb == 'validate':
            q.add_argument('--exceptions', type=Path, help='Reviewed exact test exceptions; default: candidate snapshot.')
        if verb in ['validate', 'run', 'diagnose']:
            q.add_argument('--test-timeout', type=positive, help='Test budget: local/diagnostic 1800s; full 7200s.')
            q.add_argument('--package-timeout', type=positive, help='Invocation limit: local/diagnostic 300s; full 900s.')
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
