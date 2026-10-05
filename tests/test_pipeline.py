"""Offline lifecycle and failure tests. No apt, ROS builds, or network requests."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location('pipeline', Path(__file__).resolve().parents[1] / 'scripts/lib/pipeline.py')
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)


def command(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, env=dict(p.clean_env(),
                          GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.test',
                          GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.test'),
                          text=True, capture_output=True, check=True).stdout.strip()


def make_repo(path):
    path.mkdir(parents=True)
    command('git', 'init', '-b', 'humble', str(path))
    (path / 'package.xml').write_text('<package><name>fixture_pkg</name></package>')
    (path / 'source.txt').write_text('baseline\n')
    command('git', 'add', '.', cwd=path)
    command('git', 'commit', '-m', 'fixture', cwd=path)
    command('git', 'tag', 'v1', cwd=path)
    return command('git', 'rev-parse', 'HEAD', cwd=path)


class FakeRunner(p.Runner):
    def __init__(self, home):
        super().__init__(home)
        self.calls = []
        self.fail = None
        self.test_failure = False
        self.test_name = 'test_one'
        self.packages = ['fixture_pkg']
        self.infrastructure_failure = False
        self.missing_results = False
        self.package_version = '1'
        self.dependency_plan = '#[apt] Installation commands:\n  sudo -H apt-get install -y fixture-dev\n'

    def run(self, args, cwd=None, check=True, timeout=None, env=None, show=False):
        args = [str(x) for x in args]
        self.calls.append((args, cwd, env))
        original = args
        if args[0] == '/bin/bash':
            args = args[7:]
        code, text = 0, ''
        if self.fail and self.fail(args):
            code, text = 7, 'injected failure'
        elif args[0] == 'dpkg-query':
            text = f'fixture\t{self.package_version}\tii \n' if '${binary:Package}' in args[2] else '1'
        elif args[:2] == ['vcs', 'import']:
            manifest = p.manifest_load(args[args.index('--input') + 1])
            for name, spec in manifest.items():
                dest = Path(args[-1]) / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                command('git', 'clone', spec['url'], str(dest))
                command('git', 'checkout', spec['version'], cwd=dest)
        elif args[:2] == ['rosdep', 'install']:
            text = self.dependency_plan
        elif args[0] == 'colcon' and 'build' in args:
            install = Path(args[args.index('--install-base') + 1])
            (install / 'local_setup.bash').write_text('# fixture\n')
        elif args[:2] == ['colcon', 'list']:
            text = '\n'.join(self.packages) + '\n'
        elif args[0] == 'colcon' and 'test' in args:
            package = args[args.index('--packages-select') + 1]
            base = Path(args[args.index('--test-result-base') + 1])
            log = Path(args[args.index('--log-base') + 1]) / 'latest_test'
            log.mkdir(parents=True)
            rc = 2 if self.infrastructure_failure else 0
            events = f"[0] ({package}) JobEnded: {{'identifier': '{package}', 'rc': {rc}}}\n"
            if self.test_failure:
                events += f"[0] ({package}) TestFailure: {{'identifier': '{package}'}}\n"
            (log / 'events.log').write_text(events)
            code = 1 if self.test_failure else rc
            if not self.missing_results:
                (base / package).mkdir(parents=True)
                fail = '<failure message="known failure"/>' if self.test_failure else ''
                (base / package / 'test.xml').write_text(
                    f'<testsuite tests="1" failures="{int(self.test_failure)}">'
                    f'<testcase classname="fixture" name="{self.test_name}">{fail}</testcase></testsuite>')
                if '--ctest-args' in args:
                    status = 'failed' if self.test_failure else 'passed'
                    (base / package / 'Test.xml').write_text(
                        f'<Site><Testing><Test Status="{status}"><Name>{self.test_name}</Name>'
                        '<FullCommandLine>test.xml</FullCommandLine><Results>'
                        '<NamedMeasurement name="Completion Status"><Value>Completed</Value>'
                        '</NamedMeasurement></Results></Test></Testing></Site>')
        elif args[:2] == ['colcon', 'test-result']:
            code = int(self.test_failure)
        elif args[:3] == ['ros2', 'pkg', 'list']:
            text = '\n'.join(p.REQUIRED_ROS)
        if check and code:
            raise p.CommandError(original, code, text)
        return subprocess.CompletedProcess(original, code, text, '')


class FixturePipeline(p.Pipeline):
    def platform_check(self):
        pass

    def capabilities(self):
        pass

    def smoke_checks(self, candidate, check_network=False):
        results = []
        for rmw in ['rmw_fastrtps_cpp', 'rmw_cyclonedds_cpp']:
            for talker, listener in [('demo_nodes_cpp', 'demo_nodes_py'), ('demo_nodes_py', 'demo_nodes_cpp')]:
                results.append(dict(rmw=rmw, kind='pubsub', localhost='1', talker=talker, listener=listener, passed=True))
            for kind in ['service', 'action']:
                results.append(dict(rmw=rmw, kind=kind, localhost='1', passed=True))
        return results


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ros2-pipeline-test-')
        self.base = Path(self.tmp.name)
        self.home = self.base / 'home'
        self.home.mkdir()
        self.origin = self.base / 'origin'
        self.revision = make_repo(self.origin)
        self.manifest = self.base / 'input.repos'
        p.manifest_write(self.manifest, {'fixture/repo': {'type': 'git', 'url': str(self.origin), 'version': 'humble'}})
        self.args = p.parser().parse_args(['prepare', '--root', str(self.base / 'runtime'),
                                         '--source', 'manifest', '--manifest', str(self.manifest)])
        self.args.test_timeout = 7200
        # Legacy lifecycle fixtures exercise unrestricted full-mode report gates.
        # Local profile selectors and defaults are covered in test_local_pipeline.
        self.args.validation_mode = 'full'
        self.args.lint_policy = 'warn'
        self.port_guard = patch.object(p, 'native_port_conflicts', return_value=[])
        self.port_guard.start()
        profile = p.read_json(p.REPO / 'config/bookworm.json')
        profile['patches'] = []
        self.args.profile = self.base / 'bookworm.json'
        p.atomic_json(self.args.profile, profile)
        self.runner = FakeRunner(self.home)
        self.pipe = FixturePipeline(self.args, self.runner)
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()

    def tearDown(self):
        self.port_guard.stop()
        self.quiet.__exit__(None, None, None)
        self.tmp.cleanup()

    def prepared(self):
        candidate = self.pipe.prepare()
        self.args.candidate = candidate
        return candidate

    def built(self):
        candidate = self.prepared()
        self.pipe.deps()
        self.pipe.build()
        return candidate

    def test_full_lifecycle_activation_and_status(self):
        candidate = self.built()
        self.pipe.validate()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.pipe.activate()
            self.pipe.status()
        self.assertIn(p.activation_command(candidate), out.getvalue())
        self.assertIn('Ready: True', out.getvalue())
        state = p.read_json(candidate / 'metadata/state.json')
        self.assertTrue(all(x['status'] == 'passed' for x in state['stages'].values()))
        self.assertEqual(len(p.read_json(candidate / 'metadata/smoke.json')), 8)
        self.assertFalse(any('pull' in args for args, _, _ in self.runner.calls))
        self.assertTrue(any('--no-remove' in args for args, _, _ in self.runner.calls))

    def test_revision_resolution_branch_tag_sha(self):
        for version in ['humble', 'v1', self.revision]:
            p.manifest_write(self.manifest, {'fixture/repo': {'type': 'git', 'url': str(self.origin), 'version': version}})
            candidate = self.prepared()
            self.assertEqual(p.manifest_load(candidate / 'metadata/exact.repos')['fixture/repo']['version'], self.revision)

    def test_protected_workspace_and_symlink_escape(self):
        self.args.root = self.home / 'ros2_humble' / 'runtime'
        with self.assertRaisesRegex(p.PipelineError, 'protected'):
            FixturePipeline(self.args, self.runner)
        self.args.root = self.base / 'runtime'
        candidate = self.prepared()
        (candidate / 'build').rmdir()
        (candidate / 'build').symlink_to(self.home / 'ros2_humble')
        with self.assertRaisesRegex(p.PipelineError, 'Unsafe'):
            self.pipe.build()

    def test_concurrent_candidate_operation(self):
        candidate = self.prepared()
        with self.pipe.lock(candidate / '.candidate.lock'):
            with self.assertRaisesRegex(p.PipelineError, 'Another operation'):
                self.pipe.deps()

    def test_unvalidated_activation_refused(self):
        self.prepared()
        with self.assertRaisesRegex(p.PipelineError, 'validate must pass'):
            self.pipe.activate()

    def test_missing_default_baseline_stops_before_apt(self):
        self.args.source = 'baseline'
        self.args.manifest = None
        with self.assertRaisesRegex(p.PipelineError, 'No reproducible baseline'):
            self.pipe.run()
        self.assertFalse(any(a[0] == 'sudo' for a, _, _ in self.runner.calls))

    def test_run_orchestrates_without_activation(self):
        self.pipe.run()
        candidate, state = self.pipe.candidate()
        self.assertEqual(state['stages']['validate']['status'], 'passed')
        self.assertNotIn('activate', state['stages'])
        self.assertTrue((candidate / 'install/local_setup.bash').exists())

    def test_rosdep_cache_change_blocks_build(self):
        candidate = self.prepared()
        self.pipe.deps()
        cache = candidate / 'metadata/ros-home/cache'
        cache.parent.mkdir(parents=True)
        cache.write_text('different dependency rules')
        with self.assertRaisesRegex(p.PipelineError, 'rosdep cache changed'):
            self.pipe.build()

    def test_source_edit_invalidates_readiness(self):
        candidate = self.built()
        self.pipe.validate()
        (candidate / 'src/fixture/repo/source.txt').write_text('changed')
        with self.assertRaisesRegex(p.PipelineError, 'inputs changed'):
            self.pipe.activate()

    def test_profile_edit_invalidates_readiness(self):
        candidate = self.built()
        self.pipe.validate()
        (candidate / 'metadata/profile.json').write_text('{}')
        with self.assertRaisesRegex(p.PipelineError, 'inputs changed'):
            self.pipe.activate()

    def test_dependency_change_invalidates_readiness(self):
        self.built()
        self.pipe.validate()
        self.runner.package_version = '2'
        with self.assertRaisesRegex(p.PipelineError, 'System packages changed'):
            self.pipe.activate()

    def test_build_failure_and_explicit_retry(self):
        candidate = self.prepared()
        self.pipe.deps()
        self.runner.fail = lambda a: a[0] == 'colcon' and 'build' in a
        with self.assertRaises(p.CommandError):
            self.pipe.build()
        self.assertEqual(p.read_json(candidate / 'metadata/state.json')['stages']['build']['status'], 'failed')
        self.runner.fail = None
        self.pipe.build()
        self.pipe.validate()

    def test_dependency_failure_and_retry(self):
        candidate = self.prepared()
        self.runner.fail = lambda a: a[0] == 'sudo' and 'install' in a and '--simulate' not in a
        with self.assertRaises(p.CommandError):
            self.pipe.deps()
        self.assertTrue((candidate / 'metadata/packages-after.json').exists())
        self.runner.fail = None
        self.pipe.deps()

    def test_bootstrap_failure_records_package_changes(self):
        self.runner.fail = lambda a: a[0] == 'sudo' and 'install' in a and '--simulate' not in a
        with self.assertRaises(p.CommandError):
            self.pipe.bootstrap()
        records = list((self.pipe.root / 'logs').glob('bootstrap-packages-*.json'))
        self.assertEqual(p.read_json(records[0])['status'], 'failed')
        self.assertIn('after', p.read_json(records[0]))

    def test_unresolved_dependencies_stop(self):
        self.prepared()
        self.runner.fail = lambda a: a[:2] == ['rosdep', 'install']
        with self.assertRaises(p.CommandError):
            self.pipe.deps()
        self.assertFalse(any(a[0] == 'sudo' for a, _, _ in self.runner.calls))

    def test_empty_skip_keys_preserved(self):
        profile = self.base / 'empty.json'
        doc = p.read_json(p.REPO / 'config/bookworm.json')
        doc['skip_keys'] = {}
        doc['patches'] = []
        p.atomic_json(profile, doc)
        self.args.profile = profile
        self.prepared()
        self.pipe.deps()
        args = next(a for a, _, _ in self.runner.calls if a[:2] == ['rosdep', 'install'])
        self.assertEqual(args[args.index('--skip-keys') + 1], '')

    def test_compatibility_patch_and_local_mapping_are_copied(self):
        changed = self.origin / 'source.txt'
        changed.write_text('patched\n')
        patch_text = command('git', 'diff', cwd=self.origin) + '\n'
        command('git', 'checkout', '--', 'source.txt', cwd=self.origin)
        patch_file = self.base / 'compat.diff'
        patch_file.write_text(patch_text)
        (self.base / 'mapping.yaml').write_text('fixture-dev:\n  debian:\n    bookworm: [fixture-dev]\n')
        profile = p.read_json(p.REPO / 'config/bookworm.json')
        profile['patches'] = [{'repository': 'fixture/repo', 'file': 'compat.diff', 'reason': 'test compatibility patch'}]
        profile['local_rosdep_mappings'] = ['mapping.yaml']
        self.args.profile = self.base / 'profile.json'
        p.atomic_json(self.args.profile, profile)
        original = self.runner.run
        def with_git(args, **kwargs):
            if str(args[0]) == 'git':
                return p.Runner.run(self.runner, args, **kwargs)
            return original(args, **kwargs)
        self.runner.run = with_git
        candidate = self.prepared()
        self.assertEqual((candidate / 'src/fixture/repo/source.txt').read_text(), 'patched\n')
        self.assertEqual((self.origin / 'source.txt').read_text(), 'baseline\n')
        self.assertTrue((candidate / 'metadata/mapping-0.yaml').exists())
        self.pipe.deps()
        self.pipe.build()
        self.pipe.validate()

    def test_external_baseline_protects_its_workspace(self):
        external = self.base / 'external-baseline'
        external.mkdir()
        ws = self.base / 'custom-working-install'
        p.manifest_write(external / 'exact.repos', {'repo': {'type': 'git', 'url': 'fixture', 'version': self.revision}})
        p.atomic_json(external / 'baseline.json', {'workspace': str(ws), 'reproducible': True,
                      'exact_sha256': p.file_hash(external / 'exact.repos')})
        self.args.root = ws / 'runtime'
        self.args.baseline = external
        pipe = FixturePipeline(self.args, self.runner)
        with self.assertRaisesRegex(p.PipelineError, 'protected'):
            pipe.latest_baseline()
        self.assertFalse(ws.exists())

    def test_existing_candidate_cannot_be_runtime_root(self):
        candidate = self.prepared()
        self.args.root = candidate / 'nested-runtime'
        with self.assertRaisesRegex(p.PipelineError, 'existing candidate'):
            FixturePipeline(self.args, self.runner)

    def test_download_failure_retains_failed_candidate(self):
        self.args.source = 'upstream'
        self.args.manifest = None
        self.runner.fail = lambda a: a[0] == 'curl'
        with self.assertRaises(p.CommandError):
            self.pipe.prepare()
        states = list((self.pipe.root / 'candidates').glob('*/metadata/state.json'))
        self.assertEqual(len(states), 1)
        self.assertEqual(p.read_json(states[0])['stages']['prepare']['status'], 'failed')

    def test_missing_prerequisites(self):
        with patch('shutil.which', return_value=None):
            with self.assertRaisesRegex(p.PipelineError, 'Missing git'):
                p.Pipeline.capabilities(self.pipe)

    def test_missing_results_block_validation(self):
        self.built()
        self.runner.missing_results = True
        with self.assertRaisesRegex(p.PipelineError, 'No fresh'):
            self.pipe.validate()

    def test_infrastructure_failure_blocks_validation(self):
        self.built()
        self.runner.infrastructure_failure = True
        with self.assertRaisesRegex(p.PipelineError, 'infrastructure'):
            self.pipe.validate()

    def test_test_failure_and_exact_exception(self):
        exceptions = self.base / 'exceptions.json'
        p.atomic_json(exceptions, {'schema_version': 1, 'exceptions': [{
            'package': 'fixture_pkg', 'classname': 'fixture', 'name': 'test_one',
            'revision': self.revision, 'reason': 'fixture regression exercising an exact waiver'}]})
        self.args.exceptions = exceptions
        self.built()
        self.runner.test_failure = True
        self.pipe.validate()
        self.pipe.activate()

    def test_wrong_revision_exception_does_not_waive(self):
        exceptions = self.base / 'exceptions.json'
        p.atomic_json(exceptions, {'schema_version': 1, 'exceptions': [{
            'package': 'fixture_pkg', 'classname': 'fixture', 'name': 'test_one',
            'revision': 'f' * 40, 'reason': 'wrong revision'}]})
        self.args.exceptions = exceptions
        self.built()
        self.runner.test_failure = True
        with self.assertRaises(p.PipelineError):
            self.pipe.validate()

    def test_baseline_capture_unchanged_and_reproduction(self):
        ws = self.home / 'ros2_humble'
        ws.mkdir()
        (ws / 'src/fixture').mkdir(parents=True)
        command('git', 'clone', str(self.origin), str(ws / 'src/fixture/repo'))
        shutil_copy = __import__('shutil').copyfile
        shutil_copy(self.manifest, ws / 'ros2.repos')
        (ws / 'install').mkdir()
        (ws / 'install/local_setup.bash').write_text('# fixture')
        (ws / 'log/latest_build').mkdir(parents=True)
        (ws / 'log/latest_build/logger_all.log').write_text("Command line arguments: ['colcon', 'build', '--symlink-install']\n")
        (ws / 'build/fixture_pkg').mkdir(parents=True)
        (ws / 'build/fixture_pkg/CMakeCache.txt').write_text('CMAKE_BUILD_TYPE:STRING=\nBUILD_TESTING:BOOL=ON\n')
        (ws / 'build/vendor_pkg').mkdir()
        (ws / 'build/vendor_pkg/CMakeCache.txt').write_text('CMAKE_BUILD_TYPE:STRING=RelWithDebInfo\n')
        before = {str(x.relative_to(ws)): x.read_bytes() for x in ws.rglob('*') if x.is_file()}
        self.args.workspace = ws
        baseline = self.pipe.baseline()
        after = {str(x.relative_to(ws)): x.read_bytes() for x in ws.rglob('*') if x.is_file()}
        self.assertEqual(before, after)
        self.args.source = 'baseline'
        self.args.manifest = None
        self.args.baseline = baseline
        candidate = self.prepared()
        self.assertEqual(p.read_json(candidate / 'metadata/state.json')['config']['build_type'], '')
        self.assertEqual(p.read_json(baseline / 'baseline.json')['config']['package_build_types'],
                         {'fixture_pkg': '', 'vendor_pkg': 'RelWithDebInfo'})
        self.assertEqual(p.read_json(candidate / 'metadata/package-build.meta')['names']['vendor_pkg'],
                         {'cmake-args': ['-DCMAKE_BUILD_TYPE=RelWithDebInfo']})
        self.assertFalse(any(a[0] == 'sudo' for a, _, _ in self.runner.calls))
        (ws / 'src/fixture/repo/source.txt').write_text('modified baseline')
        with self.assertRaisesRegex(p.PipelineError, 'Diagnostic snapshot retained'):
            self.pipe.baseline()

    def test_real_colcon_ctest_fixture(self):
        """A tiny non-ROS CMake fixture exercises actual colcon flags/reports."""
        package = self.origin / 'package.xml'
        package.write_text('<package format="3"><name>fixture_pkg</name><version>0.0.1</version>'
                           '<description>Offline fixture</description><maintainer email="fixture@example.test">Fixture</maintainer>'
                           '<license>MIT</license><export><build_type>cmake</build_type></export></package>')
        (self.origin / 'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.16)\n'
            'project(fixture_pkg NONE)\nenable_testing()\nadd_test(NAME fixture_pass COMMAND /bin/true)\n'
            'install(FILES source.txt DESTINATION share/fixture_pkg)\n')
        command('git', 'add', '.', cwd=self.origin)
        command('git', 'commit', '-m', 'cmake fixture', cwd=self.origin)
        # Reproduce a package-specific setting while the global setting stays
        # empty, as in the existing mixed-default Humble installation.
        baseline = self.base / 'mixed-baseline'
        baseline.mkdir()
        revision = command('git', 'rev-parse', 'HEAD', cwd=self.origin)
        p.manifest_write(baseline / 'exact.repos', {'fixture/repo': {
            'type': 'git', 'url': str(self.origin), 'version': revision}})
        p.atomic_json(baseline / 'baseline.json', {
            'workspace': str(self.base / 'original-install'), 'reproducible': True,
            'exact_sha256': p.file_hash(baseline / 'exact.repos'), 'exclusions': [],
            'repositories': {'fixture/repo': {'revision': revision, 'url': str(self.origin)}},
            'config': {'build_type': '', 'package_build_types': {'fixture_pkg': 'RelWithDebInfo'},
                       'symlink_install': True, 'packages_ignore': [], 'cmake_args': []}})
        self.args.source = 'baseline'
        self.args.baseline = baseline
        self.args.manifest = None
        original = self.runner.run
        real_runner = p.Runner(self.home)
        def hybrid(args, **kwargs):
            tokens = [str(x) for x in args]
            inner = tokens[7:] if tokens[0] == '/bin/bash' else tokens
            if inner[0] in ['colcon', 'ctest']:
                real_runner.log = self.runner.log
                return real_runner.run(args, **kwargs)
            return original(args, **kwargs)
        self.runner.run = hybrid
        candidate = self.built()
        self.assertRegex((candidate / 'build/fixture_pkg/CMakeCache.txt').read_text(),
                         r'CMAKE_BUILD_TYPE:[^=\n]+=RelWithDebInfo')
        self.pipe.validate()
        summary = p.read_json(candidate / 'metadata/tests.json')
        self.assertGreater(summary['testcases'], 0)
        self.assertEqual(summary['failures'], [])

    def test_real_colcon_failure_is_not_marked_ready(self):
        package = self.origin / 'package.xml'
        package.write_text('<package format="3"><name>fixture_pkg</name><version>0.0.1</version>'
                           '<description>Offline fixture</description><maintainer email="fixture@example.test">Fixture</maintainer>'
                           '<license>MIT</license><export><build_type>cmake</build_type></export></package>')
        (self.origin / 'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.16)\n'
            'project(fixture_pkg NONE)\nenable_testing()\nadd_test(NAME fixture_failure COMMAND /bin/false)\n'
            'install(FILES source.txt DESTINATION share/fixture_pkg)\n')
        command('git', 'add', '.', cwd=self.origin)
        command('git', 'commit', '-m', 'failing cmake fixture', cwd=self.origin)
        original = self.runner.run
        real_runner = p.Runner(self.home)
        def hybrid(args, **kwargs):
            tokens = [str(x) for x in args]
            inner = tokens[7:] if tokens[0] == '/bin/bash' else tokens
            if inner[0] in ['colcon', 'ctest']:
                real_runner.log = self.runner.log
                return real_runner.run(args, **kwargs)
            return original(args, **kwargs)
        self.runner.run = hybrid
        candidate = self.built()
        with self.assertRaises(p.PipelineError):
            self.pipe.validate()
        state = p.read_json(candidate / 'metadata/state.json')
        self.assertEqual(state['stages']['validate']['status'], 'failed')
        self.assertNotIn('validation', state)


class ParserAndReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_manifest_validation(self):
        path = self.base / 'manifest.repos'
        for content in ['repositories: {}', 'repositories:\n  ../escape: {}',
                        'repositories: {}\nrepositories: {}',
                        'repositories:\n  a: {type: git, url: x, version: true}',
                        'repositories:\n  a: {type: git, url: x, version: v}\n  a/b: {type: git, url: x, version: v}']:
            path.write_text(content)
            with self.assertRaises(p.PipelineError, msg=content):
                p.manifest_load(path)

    def test_apt_plan_rejects_non_apt_and_options(self):
        self.assertEqual(p.apt_packages('#[apt] Installation commands:\n sudo -H apt-get install -y libfoo-dev\n'), ['libfoo-dev'])
        for text in ['#[pip] Installation commands:', 'sudo -H apt-get install --allow-remove-essential foo',
                     "'sudo -H apt-get install -y foo' (alternative 1/2)"]:
            with self.assertRaises(p.PipelineError):
                p.apt_packages(text)

    def report_setup(self):
        meta = self.base / 'metadata'
        meta.mkdir()
        repo = self.base / 'src/repo'
        repo.mkdir(parents=True)
        (repo / 'package.xml').write_text('<package><name>fixture_pkg</name></package>')
        exact = meta / 'exact.repos'
        p.manifest_write(exact, {'repo': {'type': 'git', 'url': 'fixture', 'version': 'a' * 40}})
        exceptions = meta / 'exceptions.json'
        p.atomic_json(exceptions, {'schema_version': 1, 'exceptions': []})
        base = self.base / 'results/fixture_pkg'
        base.mkdir(parents=True)
        return base, exceptions, exact

    def test_ctest_results_and_missing_results(self):
        base, exceptions, exact = self.report_setup()
        (base / 'Test.xml').write_text('<Site><Testing><Test Status="passed"><Name>example</Name></Test></Testing></Site>')
        self.assertEqual(p.summarize_tests(base.parent, exceptions, exact)['testcases'], 1)
        (base / 'Test.xml').write_text('<Site><Testing><Test Status="notrun"><Name>example</Name></Test></Testing></Site>')
        self.assertTrue(p.summarize_tests(base.parent, exceptions, exact)['infrastructure'])

    def test_missing_pytest_report_cannot_be_waived(self):
        base, exceptions, exact = self.report_setup()
        (base / 'test.xml').write_text('<testsuite tests="1" failures="1"><testcase classname="fixture" name="pytest.missing_result"><failure/></testcase></testsuite>')
        p.atomic_json(exceptions, {'schema_version': 1, 'exceptions': [{
            'package': 'fixture_pkg', 'classname': 'fixture', 'name': 'pytest.missing_result',
            'revision': 'a' * 40, 'reason': 'must not work'}]})
        report = p.summarize_tests(base.parent, exceptions, exact)
        self.assertIn('cannot be waived', report['infrastructure'][0]['message'])
        self.assertEqual(report['waived'], [])

    def test_timeout_report_cannot_be_waived(self):
        base, exceptions, exact = self.report_setup()
        (base / 'test.xml').write_text('<testsuite tests="1" failures="1"><testcase classname="fixture" name="test_timeout"><failure message="Timeout (&gt;10s)"/></testcase></testsuite>')
        p.atomic_json(exceptions, {'schema_version': 1, 'exceptions': [{
            'package': 'fixture_pkg', 'classname': 'fixture', 'name': 'test_timeout',
            'revision': 'a' * 40, 'reason': 'must not work'}]})
        report = p.summarize_tests(base.parent, exceptions, exact)
        self.assertIn('timeout cannot be waived', report['infrastructure'][0]['message'])
        self.assertEqual(report['waived'], [])

    def test_readme_commands_match_cli_and_shell_syntax(self):
        import re
        readme = (p.REPO / 'README.md').read_text()
        commands = []
        for block in re.findall(r'```bash\n(.*?)```', readme, re.S):
            result = subprocess.run(['bash', '-n'], input=block, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for line in block.replace('\\\n', ' ').splitlines():
                tokens = shlex.split(line)
                if tokens and tokens[0] == './scripts/ros2_humble.sh':
                    with contextlib.redirect_stderr(io.StringIO()):
                        if tokens[1:] != ['--help']:
                            p.parser().parse_args(tokens[1:])
                    commands.append(tokens)
        self.assertGreater(len(commands), 10)

    def test_historical_manifest_is_unchanged(self):
        self.assertEqual(p.file_hash(p.REPO / 'manifests/humble-bookworm-known-good.repos'),
                         '07a22f2ab15dfd4b060824870cb5639fc5520da21f0458bf51c2b34e2f6799e6')

    def test_only_fresh_ament_reports_copied(self):
        build = self.base / 'build/pkg/test_results'
        build.mkdir(parents=True)
        old = build / 'old.xml'
        old.write_text('<testsuite/>')
        os.utime(old, (1, 1))
        since = time.time_ns()
        fresh = build / 'new.xml'
        fresh.write_text('<testsuite/>')
        # Explicit timestamp avoids coarse filesystem clock ambiguity.
        os.utime(fresh, ns=(since + 1, since + 1))
        p.collect_fresh_junit(self.base / 'build', self.base / 'result', since)
        self.assertTrue((self.base / 'result/pkg/test_results/new.xml').exists())
        self.assertFalse((self.base / 'result/pkg/test_results/old.xml').exists())

    def test_activation_command_shell_quoting(self):
        candidate = self.base / "candidate with ' quotes"
        self.assertEqual(shlex.split(p.activation_command(candidate)),
                         ['source', str(candidate / 'install/local_setup.bash')])

    def test_runner_preserves_exit_and_timeout(self):
        runner = p.Runner(self.base)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(p.CommandError) as caught:
                runner.run(['/bin/sh', '-c', 'exit 7'])
            self.assertEqual(caught.exception.returncode, 7)
            result = runner.run(['/bin/sleep', '10'], timeout=0.01, check=False)
            self.assertEqual(result.returncode, 124)

    def test_sudo_authentication_keeps_terminal_and_does_not_log_password(self):
        runner = p.Runner(self.base)
        proc = Mock()
        proc.wait.return_value = 0
        with patch.object(p.subprocess, 'run', return_value=subprocess.CompletedProcess(['sudo', '-v'], 0)) as auth:
            with patch.object(p.subprocess, 'Popen', return_value=proc) as spawn:
                with contextlib.redirect_stdout(io.StringIO()):
                    runner.run(['sudo', 'apt-get', 'update'])
        self.assertEqual(auth.call_args.args[0], ['sudo', '-v'])
        self.assertNotIn('start_new_session', auth.call_args.kwargs)
        self.assertNotIn('stdout', auth.call_args.kwargs)
        self.assertEqual(spawn.call_args.args[0], ['sudo', '-n', 'apt-get', 'update'])
        self.assertFalse(spawn.call_args.kwargs['start_new_session'])

    def test_failed_sudo_authentication_does_not_run_apt(self):
        with patch.object(p.subprocess, 'run', return_value=subprocess.CompletedProcess(['sudo', '-v'], 1)):
            with patch.object(p.subprocess, 'Popen') as spawn:
                with self.assertRaises(p.CommandError):
                    p.Runner(self.base).run(['sudo', 'apt-get', 'update'])
        spawn.assert_not_called()

    def test_terminal_attached_cleanup_never_signals_callers_process_group(self):
        proc = Mock()
        proc.poll.return_value = None
        with patch.object(p.os, 'killpg') as killpg:
            p.Runner.stop(proc, group=False)
        proc.terminate.assert_called_once()
        killpg.assert_not_called()

    def test_smoke_success_failure_timeout_and_cleanup(self):
        class Process:
            pid = 999999
            def __init__(self, done=False):
                self.done = done
            def poll(self):
                return 1 if self.done else None
        for mode in ['success', 'failure', 'timeout']:
            processes = []
            def spawn(args, **kwargs):
                proc = Process(mode == 'failure')
                processes.append(proc)
                if mode == 'success':
                    kwargs['stdout'].write('Publishing\nI heard\n')
                    kwargs['stdout'].flush()
                return proc
            with patch.object(p.Runner, 'stop') as stop:
                output = self.base / (mode + '.log')
                if mode == 'success':
                    result = p.smoke_pair(self.base / 'setup', 'demo_nodes_cpp', 'demo_nodes_py', '/fixture',
                                          {'RMW_IMPLEMENTATION': 'fixture'}, output, timeout=0.01, popen=spawn)
                    self.assertTrue(result['passed'])
                else:
                    with self.assertRaises(p.PipelineError):
                        p.smoke_pair(self.base / 'setup', 'demo_nodes_cpp', 'demo_nodes_py', '/fixture',
                                     {'RMW_IMPLEMENTATION': 'fixture'}, output, timeout=0.01, popen=spawn)
                self.assertEqual(stop.call_count, 2)

    def test_both_middleware_directions_are_checked(self):
        args = p.parser().parse_args(['status', '--root', str(self.base / 'runtime')])
        pipe = p.Pipeline(args, p.Runner(self.base))
        pipe.root_setup()
        (self.base / 'logs').mkdir()
        with patch.object(p, 'smoke_pair', return_value={'passed': True}) as smoke:
            with patch.object(p, 'smoke_exchange', return_value={'passed': True}) as exchange:
                with patch.object(pipe, 'sourced_run', return_value=subprocess.CompletedProcess([], 0, '\n'.join(p.REQUIRED_ROS))):
                    results = pipe.smoke_checks(self.base)
        self.assertEqual(len(results), 8)
        self.assertEqual(smoke.call_count, 4)
        self.assertEqual(exchange.call_count, 4)
        self.assertEqual({call.args[4]['RMW_IMPLEMENTATION'] for call in smoke.call_args_list},
                         {'rmw_fastrtps_cpp', 'rmw_cyclonedds_cpp'})
        self.assertTrue(all(call.kwargs['discovery'] for call in smoke.call_args_list))

    def test_wrapper_environment_and_legacy_variable(self):
        script = p.REPO / 'scripts/ros2_humble.sh'
        result = subprocess.run([str(script), '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('baseline', result.stdout)
        result = subprocess.run([str(script), '--help'], env=dict(os.environ, WS='unsafe'), capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('WS is retired', result.stderr)


if __name__ == '__main__':
    unittest.main()
