"""Offline selection, timing, lint subprocesses and candidate reuse regressions."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_pipeline as fixtures

p = fixtures.p


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.profile, _ = p.validation_profile()
        self.options = dict(mode='local', lint_policy='skip')
        self.profile['local_tests'] = {'core': {'patterns': ['^runtime__{rmw}$'], 'middleware': True}}
        self.inventory = {'core': {**{f'runtime__{rmw}': {} for rmw in p.MIDDLEWARES},
                                   'mypy': {'labels': ['mypy']}, 'runtime_mypy_name': {}},
                          'other': {'some_runtime': {}}}

    def test_local_exact_selection_both_middleware_and_omissions(self):
        plan = p.execution_plan(['core', 'other'], self.inventory, self.profile, self.options)
        self.assertEqual(len(plan['invocations']), 2)
        for task in plan['invocations']:
            self.assertEqual(list(task['ctest']), ['runtime__' + task['middleware']])
        self.assertIn('outside selected profile', str(plan['unselected']))
        self.assertNotIn('other', [x['package'] for x in plan['invocations']])

    def test_no_matching_required_test_or_missing_package_fails(self):
        for packages in [['other'], ['core', 'other']]:
            with self.assertRaises(p.PipelineError):
                p.execution_plan(packages, {'core': {}}, self.profile, self.options)

    def test_full_skip_does_not_use_substrings_or_skip_package(self):
        plan = p.execution_plan(['core'], self.inventory, self.profile, dict(mode='full', lint_policy='skip'))
        self.assertIn('runtime_mypy_name', plan['invocations'][0]['ctest'])
        self.assertNotIn('mypy', plan['invocations'][0]['ctest'])
        self.assertEqual(len(plan['invocations']), 1)

    def test_defaults_explicit_overrides_and_no_certification_selectors(self):
        args = p.parser().parse_args(['validate', '--candidate', '/tmp/candidate'])
        self.assertEqual(p.validation_options(args), dict(mode='local', lint_policy='skip',
                                                       test_timeout=1800, package_timeout=300))
        args.validation_mode = 'full'
        self.assertEqual(p.validation_options(args)['lint_policy'], 'warn')
        self.assertEqual(p.validation_options(args)['test_timeout'], 7200)
        args.test_timeout, args.package_timeout = 21, 12
        self.assertEqual(p.validation_options(args)['package_timeout'], 12)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            p.parser().parse_args(['validate', '--candidate', '/tmp/a', '--packages', 'core'])

    def test_diagnostic_filters_are_bounded_and_empty_matches_fail(self):
        options = dict(mode='full', lint_policy='skip')
        diag = dict(packages=['core'], ctest_regex='^runtime_mypy_name$', pytest_expression=None)
        plan = p.execution_plan(['core', 'other'], self.inventory, self.profile, options, diag)
        self.assertEqual(list(plan['invocations'][0]['ctest']), ['runtime_mypy_name'])
        diag['ctest_regex'] = '^no_match$'
        with self.assertRaises(p.PipelineError):
            p.execution_plan(['core'], self.inventory, self.profile, options, diag)

    def test_stock_allowlist_requires_core_transforms_and_bags(self):
        profile, _ = p.validation_profile()
        self.assertTrue({'rcl', 'rclcpp', 'rclpy', 'tf2', 'tf2_ros', 'tf2_py',
                         'rosbag2_cpp', 'rosbag2_py', 'rosbag2_transport'}.issubset(profile['local_tests']))

    def test_cyclone_comparison_cannot_enable_lan_for_other_middleware(self):
        diagnostic = dict(packages=['core'], compare_cyclone=True)
        configured = {'core': {'fast': {'command': ['RMW_IMPLEMENTATION=rmw_fastrtps_cpp']}}}
        with self.assertRaisesRegex(p.PipelineError, 'other DDS'):
            p.execution_plan(['core'], configured, self.profile, dict(mode='full', lint_policy='skip'), diagnostic)
        with self.assertRaisesRegex(p.PipelineError, 'CTest selections'):
            p.execution_plan(['core'], {'core': {}}, self.profile, dict(mode='full', lint_policy='skip'), diagnostic)

    def test_invalid_diagnostic_regex_is_cli_error(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            p.parser().parse_args(['diagnose', '--candidate', '/tmp/a', '--packages', 'core', '--ctest-regex', '['])


class LocalLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.LifecycleTests('test_full_lifecycle_activation_and_status')
        self.fx.setUp()
        self.profile, _ = p.validation_profile()
        policy = self.fx.base / 'policy'
        policy.mkdir()
        (policy / 'cyclonedds-loopback.xml').write_bytes((p.REPO / 'config/cyclonedds-loopback.xml').read_bytes())
        self.profile['local_tests'] = {'fixture_pkg': {'patterns': ['^test_one$'], 'middleware': True}}
        self.fx.args.validation_profile = policy / 'validation.json'
        p.atomic_json(self.fx.args.validation_profile, self.profile)
        self.fx.args.validation_mode = 'local'
        self.fx.args.lint_policy = None
        self.fx.args.test_timeout = None
        self.inventory = {'fixture_pkg': {'test_one': {'command': ['test.xml']}}}
        self.inventory_patch = patch.object(self.fx.pipe, 'ctest_inventory', return_value=self.inventory)
        self.inventory_patch.start()

    def tearDown(self):
        self.inventory_patch.stop()
        self.fx.tearDown()

    def test_reuse_build_two_dds_report_snapshots_and_activation(self):
        fx = self.fx
        candidate = fx.built()
        before = p.read_json(candidate / 'metadata/state.json')['build_fingerprint']
        fx.pipe.validate()
        state = p.read_json(candidate / 'metadata/state.json')
        self.assertEqual(state['build_fingerprint'], before)
        self.assertEqual(state['config']['jobs'], 3)
        self.assertEqual(state['validation']['mode'], 'local')
        self.assertEqual(state['validation']['coverage']['passed'], 2)
        report = p.read_json(candidate / 'metadata/tests.json')
        self.assertEqual(len(report['files']), 4)
        self.assertEqual(len(list(Path(state['validation']['attempt']).glob('results/*/fixture_pkg/test.xml'))), 2)
        fx.pipe.activate()
        self.assertEqual(len([a for a, _, _ in fx.runner.calls if a[0] == 'colcon' and 'build' in a]), 1)
        calls = [x for x in fx.runner.calls if x[0][0] == '/bin/bash' and 'test' in x[0]]
        self.assertEqual({x[2]['RMW_IMPLEMENTATION'] for x in calls}, set(p.MIDDLEWARES))
        self.assertTrue(all(x[2]['CTEST_PARALLEL_LEVEL'] == '1' for x in calls))

    def test_local_fails_fast_retains_summary_and_smoke_precedes_tests(self):
        fx = self.fx
        candidate = fx.built()
        fx.runner.test_failure = True
        order = []
        original = fx.pipe.smoke_checks
        def smoke(*args, **kwargs):
            order.append('smoke')
            return original(*args, **kwargs)
        run = fx.runner.run
        def record(args, **kwargs):
            if str(args[0]) == '/bin/bash' and 'test' in args:
                order.append('test')
            return run(args, **kwargs)
        with patch.object(fx.pipe, 'smoke_checks', side_effect=smoke), patch.object(fx.runner, 'run', side_effect=record):
            with self.assertRaises(p.PipelineError):
                fx.pipe.validate()
        self.assertEqual(order, ['smoke', 'test'])
        report = p.read_json(candidate / 'metadata/tests.json')
        self.assertEqual(len(report['failures']), 1)
        self.assertIn('Invocations not completed', str(report['infrastructure']))
        self.assertTrue((candidate / 'metadata/smoke.json').exists())

    def test_full_conflicting_ports_stop_before_any_tests(self):
        fx = self.fx
        candidate = fx.built()
        fx.args.validation_mode = 'full'
        with patch.object(p, 'native_port_conflicts', return_value=[{'port': 7400, 'owners': [{'pid': 1, 'name': 'fixture'}]}]):
            with self.assertRaisesRegex(p.PipelineError, 'unused DDS domain-0'):
                fx.pipe.validate()
        self.assertFalse(any(a[0] == '/bin/bash' and 'test' in a for a, _, _ in fx.runner.calls))
        self.assertIn('7400', str(p.read_json(candidate / 'metadata/tests.json')))

    def test_domain_range_never_enters_ephemeral_ports(self):
        fx = self.fx
        fx.pipe.root_setup()
        with patch.object(p, 'local_ports', return_value=set()):
            for seed in [0, 80, 81, 179, 9999]:
                with patch.object(p.uuid, 'uuid4') as uuid:
                    uuid.return_value.int, uuid.return_value.hex = seed, 'a' * 32
                    with fx.pipe.domain(fx.base / str(seed)) as env:
                        self.assertTrue(20 <= int(env['ROS_DOMAIN_ID']) <= 100)

    def test_logging_invocation_uses_private_home_and_unsets_ros_paths(self):
        fx = self.fx
        candidate = fx.built()
        fx.args.validation_mode = 'full'
        fx.runner.packages = ['rcl_logging_spdlog']
        self.inventory.clear()
        self.inventory['rcl_logging_spdlog'] = {'test_one': {}}
        fx.pipe.validate()
        env = next(e for a, _, e in fx.runner.calls if a[0] == '/bin/bash' and 'test' in a)
        self.assertIsNone(env['ROS_HOME'])
        self.assertIsNone(env['ROS_LOG_DIR'])
        self.assertTrue(p.contained(env['HOME'], candidate))

    def test_diagnostic_comparison_preserves_readiness_and_inputs(self):
        fx = self.fx
        candidate = fx.built()
        fx.args.packages = ['fixture_pkg']
        fx.args.ctest_regex = '^test_one$'
        fx.args.compare_cyclone = True
        before = (candidate / 'metadata/state.json').read_bytes()
        fx.pipe.diagnose()
        self.assertEqual((candidate / 'metadata/state.json').read_bytes(), before)
        calls = [e for a, _, e in fx.runner.calls if a[0] == '/bin/bash' and 'test' in a]
        self.assertEqual([e['ROS_LOCALHOST_ONLY'] for e in calls], ['1', '0'])
        self.assertTrue(all(e['RMW_IMPLEMENTATION'] == 'rmw_cyclonedds_cpp' for e in calls))
        self.assertIn('CYCLONEDDS_URI', calls[-1])

    def test_schema2_validator_change_does_not_require_rebuild(self):
        fx = self.fx
        candidate = fx.built()
        fx.pipe.validate()
        original = p.file_hash
        with patch.object(p, 'file_hash', side_effect=lambda path: 'new-plugin' if Path(path) == p.PYTEST_PLUGIN else original(path)):
            with self.assertRaisesRegex(p.PipelineError, 'rerun validate'):
                fx.pipe.activate()
            fx.pipe.validate()
            fx.pipe.activate()


class UtilityAndSubprocessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ignored_python_package_does_not_require_report(self):
        base, exceptions, exact = fixtures.ParserAndReportTests.report_setup(self)
        src = self.base / 'src/repo/ignored'
        src.mkdir()
        (src / 'package.xml').write_text('<package><name>ignored</name></package>')
        (src / 'AMENT_IGNORE').touch()
        (src / 'setup.py').touch()
        (src / 'test').mkdir()
        (src / 'test/test_missing.py').touch()
        (base / 'test.xml').write_text('<testsuite><testcase name="runtime"/></testsuite>')
        self.assertEqual(p.summarize_tests(base.parent, exceptions, exact)['infrastructure'], [])

    def test_exact_pytest_skip_nested_process_and_crash_not_waived(self):
        (self.base / 'test_fixture.py').write_text('def test_mypy(): assert False\n'
            'def test_mypy_runtime_regression(): pass\ndef test_runtime(): pass\n')
        control = self.base / 'control.json'
        p.atomic_json(control, {'lint_policy': 'skip', 'lint_tests': ['test_mypy'],
                               'lint_labels': [], 'evidence': str(self.base / 'items')})
        env = dict(p.clean_env(self.base), PYTHONPATH=str(p.PYTEST_PLUGIN.parent),
                   PYTEST_PLUGINS='ros2_validation_pytest', ROS2_VALIDATION_PYTEST_CONTROL=str(control))
        result = subprocess.run(['/usr/bin/python3', '-B', '-m', 'pytest', '-q', str(self.base)],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = p.read_json(next((self.base / 'items').glob('*.json')))
        self.assertEqual(len(report['selected']), 2)
        self.assertEqual(len(report['unselected']), 1)
        self.assertIn('test_mypy_runtime_regression', str(report['selected']))
        (self.base / 'test_fixture.py').write_text('raise RuntimeError("broken test collection")\n')
        result = subprocess.run(['/usr/bin/python3', '-B', '-m', 'pytest', '-q', str(self.base)],
                                env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)

    def test_lint_only_collection_vs_truly_empty_collection(self):
        control = self.base / 'control.json'
        p.atomic_json(control, {'lint_policy': 'skip', 'lint_tests': ['test_mypy'],
                               'lint_labels': [], 'evidence': str(self.base / 'items')})
        env = dict(p.clean_env(self.base), PYTHONPATH=str(p.PYTEST_PLUGIN.parent),
                   PYTEST_PLUGINS='ros2_validation_pytest', ROS2_VALIDATION_PYTEST_CONTROL=str(control))
        test = self.base / 'test_fixture.py'
        for source, expected in [('def test_mypy(): assert False\n', 0), ('', 5)]:
            test.write_text(source)
            result = subprocess.run(['/usr/bin/python3', '-B', '-m', 'pytest', '-q', str(test)],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    def test_timing_and_resource_observations(self):
        log = self.base / 'latest_build'
        log.mkdir()
        (log / 'events.log').write_text("[1.5] (pkg) JobStarted: {}\n[4.0] (pkg) JobEnded: {}\n")
        self.assertEqual(p.package_timings(self.base), [{'package': 'pkg', 'seconds': 2.5}])
        self.assertGreater(p.resource_observation()['memory']['MemTotal_KiB'], 0)

    def test_manifest_and_documented_cli_examples(self):
        self.assertGreater(len(p.manifest_load(p.REPO / 'manifests/humble-bookworm-known-good.repos')), 50)
        count = 0
        for block in re.findall(r'```bash\n(.*?)```', (p.REPO / 'README.md').read_text(), re.S):
            for line in block.replace('\\\n', '').splitlines():
                if not line.startswith('./scripts/'):
                    continue
                tokens = shlex.split(line)
                if '--help' in tokens:
                    continue  # Help intentionally exits argparse successfully.
                prefix = {'./scripts/bootstrap_ros2_humble.sh': ['bootstrap'],
                          './scripts/update_ros2_humble.sh': ['run']}.get(tokens[0], [])
                p.parser().parse_args(prefix + tokens[1:])
                count += 1
        self.assertGreater(count, 20)
        for verb in ['run', 'validate', 'diagnose', 'prepare', 'bootstrap']:
            result = subprocess.run([str(p.REPO / 'scripts/ros2_humble.sh'), verb, '--help'],
                                    env=p.clean_env(), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--compare-cyclone', subprocess.run(
            [str(p.REPO / 'scripts/ros2_humble.sh'), 'diagnose', '--help'],
            env=p.clean_env(), capture_output=True, text=True).stdout)

    def test_runner_unsets_environment_without_affecting_parent(self):
        runner = p.Runner(self.base)
        result = runner.run(['/usr/bin/python3', '-c', 'import os; print("HOME" in os.environ)'], env={'HOME': None})
        self.assertEqual(result.stdout.strip(), 'False')
        self.assertIn('HOME', runner.env)

    def test_unsafe_loopback_override_refused(self):
        config = self.base / 'config.xml'
        config.write_text('<CycloneDDS><NetworkInterface address="192.0.2.1"/></CycloneDDS>')
        with self.assertRaises(p.PipelineError):
            p.verify_loopback_config(config)


class BuildTuningTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.LifecycleTests('test_full_lifecycle_activation_and_status')
        self.fx.setUp()

    def tearDown(self):
        self.fx.tearDown()

    def baseline(self, workers=4, jobs=5):
        fx = self.fx
        baseline = fx.base / 'baseline'
        baseline.mkdir()
        p.manifest_write(baseline / 'exact.repos', {'fixture/repo': {
            'type': 'git', 'url': str(fx.origin), 'version': fx.revision}})
        p.atomic_json(baseline / 'baseline.json', {
            'workspace': str(fx.home / 'original'), 'reproducible': True,
            'exact_sha256': p.file_hash(baseline / 'exact.repos'), 'exclusions': [],
            'repositories': {'fixture/repo': {'revision': fx.revision, 'url': str(fx.origin)}},
            'config': {'build_type': 'Release', 'symlink_install': True, 'packages_ignore': [],
                       'cmake_args': [], 'workers': workers, 'jobs': jobs}})
        fx.args.source, fx.args.baseline, fx.args.manifest = 'baseline', baseline, None

    def test_captured_concurrency_preserved_and_explicit_override(self):
        fx = self.fx
        self.baseline()
        candidate = fx.prepared()
        config = p.read_json(candidate / 'metadata/state.json')['config']
        self.assertEqual((config['workers'], config['jobs']), (4, 5))
        fx.args.jobs, fx.args.workers = 2, 1
        candidate = fx.prepared()
        config = p.read_json(candidate / 'metadata/state.json')['config']
        self.assertEqual((config['workers'], config['jobs']), (1, 2))

    def test_ccache_opt_in_provisioning_and_fingerprints(self):
        fx = self.fx
        fx.pipe.bootstrap()
        self.assertFalse(any('ccache' in a for a, _, _ in fx.runner.calls))
        fx.args.ccache = True
        fx.pipe.bootstrap()
        self.assertTrue(any('ccache' in a and a[0] == 'sudo' for a, _, _ in fx.runner.calls))
        original = p.file_hash
        def hashes(path):
            return 'launcher' if str(path) == '/usr/bin/ccache' else original(path)
        with patch.object(p.shutil, 'which', return_value='/usr/bin/ccache'), patch.object(p, 'file_hash', side_effect=hashes):
            candidate = fx.built()
            state = p.read_json(candidate / 'metadata/state.json')
            self.assertEqual(state['config']['ccache']['launcher_sha256'], 'launcher')
            self.assertIn('-DCMAKE_CXX_COMPILER_LAUNCHER=/usr/bin/ccache', state['config']['cmake_args'])
            env = fx.pipe.colcon_env(candidate, state)
            self.assertEqual(env['CCACHE_BASEDIR'], str(fx.pipe.root / 'candidates'))
            self.assertEqual(env['CCACHE_DIR'], str(fx.pipe.root / 'cache/ccache'))
            self.assertTrue((candidate / 'metadata/build-timing.json').exists())
            state['config']['ccache']['launcher_sha256'] = 'other'
            with self.assertRaisesRegex(p.PipelineError, 'compiler/cache inputs changed'):
                fx.pipe.colcon_env(candidate, state)

    def test_missing_ccache_requires_explicit_bootstrap(self):
        fx = self.fx
        fx.args.ccache = True
        with patch.object(p.shutil, 'which', return_value=None), self.assertRaisesRegex(p.PipelineError, 'bootstrap --ccache'):
            fx.prepared()

    def test_ccache_symlink_escape_refused(self):
        fx = self.fx
        fx.args.ccache = True
        fx.pipe.root_setup()
        (fx.pipe.root / 'cache').symlink_to(fx.home)
        with patch.object(p.shutil, 'which', return_value='/usr/bin/ccache'), self.assertRaisesRegex(p.PipelineError, 'Unsafe ccache'):
            fx.prepared()

    def test_cache_statistics_failure_preserves_build_exit_code(self):
        fx = self.fx
        fx.args.ccache = True
        original_hash, original_run = p.file_hash, fx.runner.run
        with patch.object(p.shutil, 'which', return_value='/usr/bin/ccache'), patch.object(
                p, 'file_hash', side_effect=lambda path: 'launcher' if str(path) == '/usr/bin/ccache' else original_hash(path)):
            candidate = fx.prepared()
            fx.pipe.deps()
            def fail(args, **kwargs):
                if str(args[0]) == 'colcon' and 'build' in args:
                    raise p.CommandError([str(x) for x in args], 7, 'build failed')
                if str(args[0]) == 'ccache' and '--show-stats' in args:
                    raise OSError('statistics unavailable')
                return original_run(args, **kwargs)
            with patch.object(fx.runner, 'run', side_effect=fail), self.assertRaises(p.CommandError) as raised:
                fx.pipe.build()
            self.assertEqual(raised.exception.returncode, 7)
            record = p.read_json(candidate / 'metadata/build-timing.json')
            self.assertIn('statistics unavailable', record['ccache_statistics_error'])


if __name__ == '__main__':
    unittest.main()
