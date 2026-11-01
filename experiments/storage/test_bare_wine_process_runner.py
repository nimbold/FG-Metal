"""Offline tests for retaining real Wine child failures through PrefixLease exit."""
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / 'dxvk_macos_step11d3b/wine_process_runner.py'
spec = importlib.util.spec_from_file_location('bare_wine_process_runner_test_target', SOURCE)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class BareWineProcessRunnerTests(unittest.TestCase):
    def test_runner_source_passes_managed_prefix_authorization(self):
        import storage_policy

        records = storage_policy._authorized_runner_records(
            [str(SOURCE)], requires_managed_prefix=True
        )
        self.assertIn(str(SOURCE), records)

    def exercise(self, outcome):
        temp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        prefix = root / 'prefix with spaces'
        dll = prefix / 'drive_c/windows/system32/winevulkan.dll'
        dll.parent.mkdir(parents=True)
        dll.write_bytes(b'synthetic pinned DLL')
        context = {'environment': {'WINEPREFIX': str(prefix)},
                   'prefix': str(prefix), 'command': ['/fake/certified/wine', 'app.exe'],
                   'duration_seconds': 30, 'runtime_log': str(root / 'runtime.log'),
                   'execution_result': str(root / 'execution-result.json'),
                   'experiment_id': 'step11d3b-smoke-20261013-001122-aabbccdd'}
        context['environment']['FG_B_EXPERIMENT_TOKEN'] = 'a' * 32
        lease = mock.Mock()
        def start(*args, **kwargs):
            kwargs['stdout'].write('wine: real synthetic child diagnostic\n')
            return mock.Mock(pid=12345)
        lease.start_wine_process.side_effect = start
        if isinstance(outcome, BaseException):
            lease.wait_process.side_effect = outcome
        else:
            lease.wait_process.return_value = outcome
        return context, lease, root

    def test_success_retains_execution_record(self):
        context, lease, root = self.exercise(0)
        self.assertEqual(runner.run_managed_process(context, lease), 0)
        record = json.loads((root / 'execution-result.json').read_text())
        self.assertEqual(record['process_returncode'], 0)
        self.assertEqual(record['target_unix_pid'], 12345)
        self.assertEqual(record['experiment_id'], context['experiment_id'])
        self.assertEqual(record['experiment_token'], 'a' * 32)
        self.assertGreater(record['target_process_exit_monotonic_ns'], 0)

    def test_nonzero_exit_preserves_child_diagnostic_for_cleanup_receipt(self):
        context, lease, root = self.exercise(1)
        with self.assertRaisesRegex(RuntimeError, 'Wine child exited 1: wine: real synthetic child diagnostic'):
            runner.run_managed_process(context, lease)
        result = json.loads((root / 'execution-result.json').read_text())
        self.assertEqual(result['process_returncode'], 1)
        self.assertFalse(result['timed_out'])
        self.assertGreater(result['target_process_exit_monotonic_ns'], 0)

    def test_timeout_preserves_original_exception_and_execution_record(self):
        timeout = subprocess.TimeoutExpired(['/fake/certified/wine', 'app.exe'], 120)
        context, lease, root = self.exercise(timeout)
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            runner.run_managed_process(context, lease)
        self.assertIs(caught.exception, timeout)
        result = json.loads((root / 'execution-result.json').read_text())
        self.assertTrue(result['timed_out'])
        self.assertEqual(result['process_returncode'], 124)
        self.assertIsNone(result['target_process_exit_monotonic_ns'])

    def test_output_cap_is_passed_to_prefixlease_watcher(self):
        context, lease, _root = self.exercise(0)
        context['output_limit_bytes'] = 4 * 1024 * 1024
        self.assertEqual(runner.run_managed_process(context, lease), 0)
        self.assertEqual(lease.wait_process.call_args.kwargs['output_limit_bytes'], 4 * 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
