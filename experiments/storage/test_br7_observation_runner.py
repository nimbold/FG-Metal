"""BR7 observation admission and qualitative capture invariants."""
import inspect
import unittest
from experiments.dxvk_macos_step11d3b import runner_b

class BR7ObservationRunnerTests(unittest.TestCase):
    def test_six_samples_and_seventh_reproduction_are_bounded(self):
        for index in range(1,8):
            self.assertEqual(runner_b.phase_command('sample',index),(60,f'br7-sample-{index:02d}'))
        for index in (0,8):
            with self.assertRaises(ValueError): runner_b.phase_command('sample',index)
    def test_confirmation_is_300_seconds(self):
        self.assertEqual(runner_b.phase_command('confirm',1),(300,'br7-confirm-01'))
        with self.assertRaises(ValueError): runner_b.phase_command('confirm',2)
    def test_capture_does_not_activate_and_has_pid_guard(self):
        source=inspect.getsource(runner_b.capture_window)
        self.assertNotIn('activate(',source)
        self.assertNotIn('activateIgnoring',source)
        self.assertIn('kCGWindowOwnerPID',source)
        self.assertIn('monotonic_capture_started_ns',source)
        self.assertIn('monotonic_capture_finished_ns',source)
    def test_samples_require_authenticated_smoke(self):
        with self.assertRaisesRegex(RuntimeError,'authenticated prior smoke'):
            runner_b.run_one('sample',1)
        with self.assertRaisesRegex(RuntimeError,'authenticated prior smoke'):
            runner_b.run_one('confirm',1)
