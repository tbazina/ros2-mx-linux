"""Offline validation policies, migration, isolation, and compatibility regressions."""
import contextlib
import io
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_pipeline as fixtures

p = fixtures.p


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.results, self.exceptions, self.exact = fixtures.ParserAndReportTests.report_setup(self)

    def tearDown(self):
        self.tmp.cleanup()

    def junit(self, name='test_one', message='failure', kind='failure', file='example.xml'):
        self.results.joinpath(file).write_text(
            f'<testsuite tests="1" failures="1"><testcase classname="fixture" name="{name}">'
            f'<{kind} message="{message}"/></testcase></testsuite>')

    def ctest(self, name, status, completion, filename='Test.xml', extra=''):
        self.results.joinpath(filename).write_text(
            f'<Site><Testing><Test Status="{status}"><Name>{name}</Name>{extra}<Results>'
            f'<NamedMeasurement name="Completion Status"><Value>{completion}</Value>'
            '</NamedMeasurement></Results></Test></Testing></Site>')

    def summary(self, **kwargs):
        return p.summarize_tests(self.results.parent, self.exceptions, self.exact, **kwargs)

    def test_explicit_skips_disabled_and_unknown_notrun(self):
        self.ctest('benchmark_log', 'notrun', 'SKIP_RETURN_CODE=0')
        self.ctest('disabled_test', 'notrun', 'Disabled', 'Disabled.xml')
        self.ctest('executed', 'passed', 'Completed', 'Executed.xml')
        report = self.summary(ctest_metadata={'fixture_pkg': {'disabled_test': {'disabled': True}}})
        self.assertEqual((report['passed'], report['skipped'], report['disabled']), (1, 1, 1))
        self.assertEqual(report['infrastructure'], [])
        self.assertTrue(self.summary()['infrastructure'])  # Unconfigured disabled test.
        self.ctest('aborted', 'notrun', 'Failed to start', 'Aborted.xml')
        self.assertIn('aborted', str(self.summary()['infrastructure']))

    def test_skips_never_provide_executed_coverage(self):
        self.ctest('benchmark', 'notrun', 'SKIP_RETURN_CODE=0')
        self.assertIn('No fresh', str(self.summary()['infrastructure']))

    def test_exact_lint_names_and_strict_mode(self):
        self.junit('test_mypy')
        self.assertEqual(len(self.summary()['warnings']), 1)
        self.assertEqual(len(self.summary(lint_policy='strict')['failures']), 1)
        self.junit('test_mypy_runtime_regression')
        self.assertEqual(len(self.summary()['failures']), 1)

    def test_lint_package_does_not_hide_runtime_or_tool_crash(self):
        self.junit('test_runtime')
        self.assertEqual(len(self.summary()['failures']), 1)
        for kind, message in [('error', 'ImportError'), ('failure', 'Segmentation fault')]:
            self.junit('test_mypy', message, kind)
            self.assertEqual(len(self.summary()['infrastructure']), 1)
            self.assertEqual(self.summary()['warnings'], [])

    def test_ctest_wrapper_failure_is_not_duplicated(self):
        self.junit('test_mypy', file='example.junit.xml')
        self.ctest('example', 'failed', 'Completed')
        report = self.summary()
        self.assertEqual((report['testcases'], report['failed']), (1, 1))
        self.assertEqual(len(report['warnings']), 1)
        self.assertEqual(report['failures'], [])

    def test_configured_ctest_labels_classify_exact_linked_report(self):
        self.junit('custom_static_check', file='example.junit.xml')
        self.ctest('example', 'failed', 'Completed')
        metadata = {'fixture_pkg': {'example': {'labels': ['mypy'], 'command': ['example.junit.xml']}}}
        self.assertEqual(len(self.summary(ctest_metadata=metadata)['warnings']), 1)
        metadata['fixture_pkg']['example']['labels'] = ['pytest']
        self.assertEqual(len(self.summary(ctest_metadata=metadata)['failures']), 1)

    def test_runtime_lint_and_malformed_results_are_all_reported(self):
        self.junit('test_mypy', file='lint.xml')
        self.junit('test_runtime', file='runtime.xml')
        self.results.joinpath('broken.xml').write_text('<broken')
        report = self.summary()
        self.assertEqual(len(report['warnings']), 1)
        self.assertEqual(len(report['failures']), 1)
        self.assertEqual(len(report['infrastructure']), 1)

    def test_missing_configured_outcomes_block(self):
        self.ctest('executed', 'passed', 'Completed')
        report = self.summary(ctest_metadata={'fixture_pkg': {'executed': {}, 'missing': {}}})
        self.assertIn('missing', str(report['infrastructure']))

    def test_gtest_disabled_results_do_not_provide_coverage(self):
        self.results.joinpath('gtest.xml').write_text('<testsuite tests="1" disabled="1">'
            '<testcase name="disabled" status="notrun" result="suppressed"/></testsuite>')
        report = self.summary()
        self.assertEqual(report['testcases'], 0)
        self.assertEqual(report['disabled'], 1)
        self.assertTrue(report['infrastructure'])

    def test_negative_report_counters_block(self):
        self.results.joinpath('invalid.xml').write_text('<testsuite tests="1" failures="-1">'
                                                      '<testcase name="example"/></testsuite>')
        self.assertIn('Invalid test suite counters', str(self.summary()['infrastructure']))

    def test_quoted_timeout_is_not_infrastructure(self):
        self.junit('test_one', 'Expected negative timeout example')
        report = self.summary()
        self.assertEqual(report['infrastructure'], [])
        self.assertEqual(len(report['failures']), 1)

    def test_missing_python_results_are_detected_with_other_passing_tests(self):
        package = self.base / 'src/repo'
        (package / 'setup.py').write_text('# pytest package')
        (package / 'test').mkdir()
        (package / 'test/test_runtime.py').write_text('def test_runtime(): pass')
        self.results.rmdir()
        other = self.results.parent / 'other'
        other.mkdir()
        (other / 'Test.xml').write_text('<Site><Testing><Test Status="passed"><Name>executed</Name>'
                                      '</Test></Testing></Site>')
        self.assertIn('Missing fresh Python', str(self.summary()['infrastructure']))

    def test_execution_timeout_cannot_be_waived(self):
        self.ctest('example', 'failed', 'Timeout')
        p.atomic_json(self.exceptions, {'schema_version': 1, 'exceptions': [{
            'package': 'fixture_pkg', 'classname': 'CTest', 'name': 'example',
            'revision': 'a' * 40, 'reason': 'must not waive timeout'}]})
        self.assertEqual(self.summary()['waived'], [])
        self.assertIn('infrastructure', str(self.summary()['infrastructure']))

    def test_network_warning_matches_identity_revision_and_failure(self):
        profile = p.validation_profile()[0]
        entry = profile['network_only_tests'][0]
        package = self.base / 'src/repo/package.xml'
        package.write_text('<package><name>ros2multicast</name></package>')
        p.manifest_write(self.exact, {'repo': {'type': 'git', 'url': 'fixture', 'version': entry['revision']}})
        new = self.results.with_name('ros2multicast')
        self.results.rename(new)
        self.results = new
        self.results.joinpath('pytest.xml').write_text(
            f'<testsuite tests="1" failures="1"><testcase classname="{entry["classname"]}" name="test_api">'
            f'<failure message="{entry["message_prefix"]}"/></testcase></testsuite>')
        self.assertEqual(self.summary()['warnings'][0]['category'], 'network')
        p.manifest_write(self.exact, {'repo': {'type': 'git', 'url': 'fixture', 'version': 'b' * 40}})
        self.assertEqual(len(self.summary()['failures']), 1)
        self.assertEqual(self.summary()['warnings'], [])


class LifecycleValidationTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.LifecycleTests()
        self.fx.setUp()

    def tearDown(self):
        self.fx.tearDown()

    def test_sequential_domains_and_private_environments(self):
        fx = self.fx
        candidate = fx.built()
        fx.runner.packages.append('other_pkg')
        fx.pipe.validate()
        calls = [x for x in fx.runner.calls if x[0][0] == '/bin/bash' and 'test' in x[0]]
        self.assertEqual(len(calls), 2)
        for args, _, env in calls:
            self.assertEqual(args[args.index('--parallel-workers') + 1], '1')
            self.assertEqual(env['CTEST_PARALLEL_LEVEL'], '1')
            self.assertEqual(env['ROS_LOCALHOST_ONLY'], '1')
            self.assertTrue(p.contained(env['ROS_HOME'], candidate))
            self.assertTrue(p.contained(env['ROS_LOG_DIR'], candidate))
            self.assertIn('ROS_DOMAIN_ID', env)
        self.assertNotEqual(calls[0][2]['ROS_HOME'], calls[1][2]['ROS_HOME'])
        self.assertEqual(p.read_json(candidate / 'metadata/state.json')['config']['workers'], 2)

    def test_failed_validation_saves_reports_and_runs_smokes(self):
        fx = self.fx
        candidate = fx.built()
        fx.runner.test_failure = True
        with patch.object(fx.pipe, 'smoke_checks', wraps=fx.pipe.smoke_checks) as smoke:
            with self.assertRaises(p.PipelineError):
                fx.pipe.validate()
        smoke.assert_called_once()
        self.assertTrue(p.read_json(candidate / 'metadata/tests.json')['failures'])
        self.assertTrue((candidate / 'metadata/smoke.json').exists())
        self.assertNotIn('validation', p.read_json(candidate / 'metadata/state.json'))

    def test_timeout_keeps_partial_reports_and_runs_smokes(self):
        fx = self.fx
        candidate = fx.built()
        original = fx.runner.run
        def timeout(args, **kwargs):
            result = original(args, **kwargs)
            if str(args[0]) == '/bin/bash' and 'test' in args:
                result.returncode = 124
            return result
        fx.runner.run = timeout
        with self.assertRaises(p.PipelineError):
            fx.pipe.validate()
        self.assertIn('timeout while running', str(p.read_json(candidate / 'metadata/tests.json')['infrastructure']))
        self.assertEqual(len(p.read_json(candidate / 'metadata/smoke.json')), 8)

    def test_incomplete_smoke_matrix_blocks_activation(self):
        fx = self.fx
        fx.built()
        with patch.object(fx.pipe, 'smoke_checks', return_value=[]):
            with self.assertRaisesRegex(p.PipelineError, 'Missing mandatory'):
                fx.pipe.validate()
        with self.assertRaises(p.PipelineError):
            fx.pipe.activate()

    def test_lint_warning_allows_local_activation_strict_blocks(self):
        fx = self.fx
        candidate = fx.built()
        fx.runner.test_name = 'test_mypy'
        fx.runner.test_failure = True
        fx.pipe.validate()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fx.pipe.activate()
        self.assertIn('LOCAL use', out.getvalue())
        self.assertIn('export ROS_LOCALHOST_ONLY=1', out.getvalue())
        self.assertIn('1 warnings', out.getvalue())
        fx.args.lint_policy = 'strict'
        with self.assertRaises(p.PipelineError):
            fx.pipe.validate()
        with self.assertRaises(p.PipelineError):
            fx.pipe.activate()
        self.assertEqual(p.read_json(candidate / 'metadata/tests.json')['warnings'], [])

    def test_validator_changes_require_only_revalidation(self):
        fx = self.fx
        candidate = fx.built()
        fx.pipe.validate()
        original = p.file_hash
        def changed(path):
            return 'changed-code' if Path(path).resolve() == Path(p.__file__).resolve() else original(path)
        with patch.object(p, 'file_hash', side_effect=changed):
            with self.assertRaisesRegex(p.PipelineError, 'rerun validate'):
                fx.pipe.activate()
            fx.pipe.validate()
            fx.pipe.activate()
        builds = [a for a, _, _ in fx.runner.calls if a[0] == 'colcon' and 'build' in a]
        self.assertEqual(len(builds), 1)

    def test_policy_and_exceptions_invalidate_validation_not_build(self):
        fx = self.fx
        candidate = fx.built()
        policy = fx.base / 'validation'
        policy.mkdir()
        profile, config = p.validation_profile()
        (policy / config.name).write_bytes(config.read_bytes())
        p.atomic_json(policy / 'validation.json', profile)
        fx.args.validation_profile = policy / 'validation.json'
        fx.pipe.validate()
        profile['lint_tests'].append('test_new_linter')
        p.atomic_json(fx.args.validation_profile, profile)
        with self.assertRaisesRegex(p.PipelineError, 'rerun validate'):
            fx.pipe.activate()
        fx.pipe.validate()
        fx.pipe.activate()
        state = p.read_json(candidate / 'metadata/state.json')
        self.assertEqual(state['build_fingerprint'], fx.pipe.input_fingerprint(candidate, state))

    def legacy(self):
        fx = self.fx
        candidate = fx.built()
        state = p.read_json(candidate / 'metadata/state.json')
        state['schema_version'] = 1
        old = fx.pipe.input_fingerprint(candidate, state, p.LEGACY_PIPELINES[0])
        state['input_fingerprint'] = state['build_fingerprint'] = old
        p.atomic_json(candidate / 'metadata/state.json', state)
        return candidate, state

    def test_verified_legacy_migration_reuses_build(self):
        fx = self.fx
        candidate, old = self.legacy()
        fx.pipe.validate()
        state = p.read_json(candidate / 'metadata/state.json')
        self.assertEqual(state['schema_version'], 2)
        self.assertEqual(state['migration']['old_fingerprint'], old['input_fingerprint'])
        self.assertEqual(state['stages']['build'], old['stages']['build'])
        fx.pipe.activate()

    def test_unknown_or_changed_legacy_provenance_refused(self):
        candidate, state = self.legacy()
        state['input_fingerprint'] = state['build_fingerprint'] = 'unknown'
        p.atomic_json(candidate / 'metadata/state.json', state)
        with self.assertRaisesRegex(p.PipelineError, 'legacy provenance'):
            self.fx.pipe.validate()
        self.assertEqual(p.read_json(candidate / 'metadata/state.json')['schema_version'], 1)

    def test_root_validation_lock_serializes_candidates(self):
        fx = self.fx
        fx.built()
        with fx.pipe.lock(fx.pipe.root / '.validation.lock'):
            with self.assertRaisesRegex(p.PipelineError, 'Another operation'):
                fx.pipe.validate()

    def test_diagnostics_never_grant_or_replace_readiness(self):
        fx = self.fx
        candidate = fx.built()
        fx.pipe.validate()
        state = (candidate / 'metadata/state.json').read_bytes()
        fx.args.check_network = True
        fx.pipe.diagnose()
        self.assertEqual(state, (candidate / 'metadata/state.json').read_bytes())
        self.assertTrue(list((candidate / 'logs').glob('diagnose-*/tests.json')))

    def test_domain_lease_and_preexisting_ports_are_respected(self):
        fx = self.fx
        fx.pipe.root_setup()
        leases = fx.pipe.root / 'domains'
        leases.mkdir()
        with patch.object(p, 'local_ports', return_value=set()):
            with patch.object(p.uuid, 'uuid4', return_value=Mock(int=0, hex='a' * 32)):
                with fx.pipe.lock(leases / '20.lock'):
                    with fx.pipe.domain(fx.base / 'test-runtime') as env:
                        self.assertEqual(env['ROS_DOMAIN_ID'], '21')
        with patch.object(p, 'local_ports', return_value={11511 + x for x in range(20, 200)}):
            with self.assertRaisesRegex(p.PipelineError, 'No unused'):
                with fx.pipe.domain(fx.base / 'never-created'):
                    self.fail('No domain should be reserved')

    def test_source_patch_only_applied_to_fresh_candidate(self):
        fx = self.fx
        marker = fx.origin / 'launch_testing/launch_testing/markers.py'
        marker.parent.mkdir(parents=True)
        marker.write_bytes((p.REPO / 'tests/fixtures/markers.py').read_bytes())
        fixtures.command('git', 'add', '.', cwd=fx.origin)
        fixtures.command('git', 'commit', '-m', 'retry fixture', cwd=fx.origin)
        p.manifest_write(fx.manifest, {'ros2/launch': {'type': 'git', 'url': str(fx.origin), 'version': 'humble'}})
        fx.args.profile = p.REPO / 'config/bookworm.json'
        before = marker.read_bytes()
        original_run = fx.runner.run
        actual = p.Runner(fx.home)
        def apply_patch(args, **kwargs):
            if str(args[0]) == 'git' and 'apply' in args:
                actual.log = fx.runner.log
                return actual.run(args, **kwargs)
            return original_run(args, **kwargs)
        fx.runner.run = apply_patch
        candidate = fx.prepared()
        self.assertEqual(marker.read_bytes(), before)
        patched = candidate / 'src/ros2/launch/launch_testing/launch_testing/markers.py'
        self.assertIn('previous_success', patched.read_text())
        state = p.read_json(candidate / 'metadata/state.json')
        self.assertEqual(state['assets']['patches'][0]['status'], 'applied')
        # An upstream postimage is recorded instead of being patched twice.
        marker.write_bytes(patched.read_bytes())
        fixtures.command('git', 'add', '.', cwd=fx.origin)
        fixtures.command('git', 'commit', '-m', 'upstream fixed', cwd=fx.origin)
        candidate = fx.prepared()
        self.assertEqual(p.read_json(candidate / 'metadata/state.json')['assets']['patches'][0]['status'], 'already_present')


class SmokeAndCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_service_action_success_failure_timeout_cleanup(self):
        for kind in ['service', 'action']:
            for mode in ['success', 'failure', 'timeout']:
                processes = []
                def spawn(args, **kwargs):
                    process = Mock()
                    process.poll.return_value = None
                    process.wait.return_value = 1 if mode == 'failure' else 0
                    if mode == 'timeout':
                        process.wait.side_effect = subprocess.TimeoutExpired(args, 30)
                    if mode == 'success' and len(processes) == 1:
                        kwargs['stdout'].write('Result of add_two_ints: 5\n' if kind == 'service' else
                                              "Result: array('i', [0, 1, 1, 2, 3, 5, 8, 13, 21, 34, 55])\n")
                        kwargs['stdout'].flush()
                    processes.append(process)
                    return process
                with patch.object(p.Runner, 'stop') as stop:
                    args = [self.base / 'setup', kind, p.clean_env(), self.base / 'smoke.log']
                    if mode == 'success':
                        self.assertTrue(p.smoke_exchange(*args, popen=spawn)['passed'])
                    else:
                        with self.assertRaises(p.PipelineError):
                            p.smoke_exchange(*args, popen=spawn)
                    self.assertEqual(stop.call_count, 2)

    def test_pubsub_success_requires_daemon_free_discovery(self):
        processes = []
        def spawn(args, **kwargs):
            proc = Mock()
            proc.poll.return_value = None
            proc.wait.return_value = 0
            text = 'Publishing\nI heard\n' if 'run' in args else '/candidate_talker\n/candidate_listener\n'
            kwargs['stdout'].write(text)
            kwargs['stdout'].flush()
            processes.append(args)
            return proc
        with patch.object(p.Runner, 'stop') as stop:
            result = p.smoke_pair(self.base / 'setup', 'demo_nodes_cpp', 'demo_nodes_py', '/fixture',
                                  {'RMW_IMPLEMENTATION': 'fixture'}, self.base / 'smoke.log',
                                  discovery=True, popen=spawn)
        self.assertTrue(result['passed'])
        self.assertIn('--no-daemon', processes[-1])
        self.assertEqual(stop.call_count, 3)

    def test_orphan_cleanup_only_signals_owned_token(self):
        token = 'owned'
        def content(path):
            return b'ROS2_CANDIDATE_VALIDATION_TOKEN=owned\0' if str(path).endswith('/101/environ') else b'OTHER=1\0'
        with patch.object(Path, 'read_text', return_value='101 102'):
            with patch.object(Path, 'read_bytes', autospec=True, side_effect=content):
                with patch.object(p.os, 'kill') as kill:
                    with patch.object(p.os, 'waitpid', return_value=(101, 0)):
                        p.cleanup_owned_orphans(token)
        kill.assert_called_once_with(101, signal.SIGTERM)

    def patched_marker(self):
        source = self.base / 'launch_testing/launch_testing/markers.py'
        source.parent.mkdir(parents=True)
        source.write_bytes((p.REPO / 'tests/fixtures/markers.py').read_bytes())
        subprocess.run(['git', 'apply', str(p.REPO / 'config/patches/launch-testing-python311-retry.patch')],
                       cwd=self.base, check=True, capture_output=True)
        return source

    def test_python311_retry_preserves_previous_failures_and_final_failure(self):
        source = self.patched_marker()
        namespace = {}
        exec(compile(source.read_text(), str(source), 'exec'), namespace)
        retry = namespace['retry_on_failure']
        class Case(unittest.TestCase):
            @retry(times=2)
            def test_retry(self):
                self.count = getattr(self, 'count', 0) + 1
                self.assertGreater(self.count, 1)
            @retry(times=2)
            def test_subtest_retry(self):
                self.count = getattr(self, 'count', 0) + 1
                with self.subTest(attempt=self.count):
                    self.assertGreater(self.count, 1)
            @retry(times=2)
            def test_fail(self):
                self.fail('real failure')
        result = unittest.TestResult()
        previous = (object(), 'keep prior failure')
        result.failures.append(previous)
        Case('test_retry').run(result)
        self.assertEqual(result.failures, [previous])
        self.assertEqual(result.errors, [])
        Case('test_subtest_retry').run(result)
        self.assertEqual(result.failures, [previous])
        Case('test_fail').run(result)
        self.assertEqual(len(result.failures), 2)
        self.assertEqual(result.errors, [])

    def test_retry_runs_under_pytest_with_system_python(self):
        source = self.patched_marker()
        test = self.base / 'test_retry.py'
        test.write_text('import importlib.util, unittest\n'
                        f's = importlib.util.spec_from_file_location("markers", {str(source)!r})\n'
                        'm = importlib.util.module_from_spec(s); s.loader.exec_module(m)\n'
                        'class TestRetry(unittest.TestCase):\n'
                        ' @m.retry_on_failure(times=2)\n'
                        ' def test_retry(self):\n'
                        '  self.calls = getattr(self, "calls", 0) + 1\n'
                        '  self.assertGreater(self.calls, 1)\n')
        result = subprocess.run(['/usr/bin/python3', '-B', '-m', 'pytest', '-q', str(test)],
                                cwd=self.base, env=dict(p.clean_env(self.base), PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'),
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
