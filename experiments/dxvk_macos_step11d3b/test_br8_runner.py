import unittest
from experiments.dxvk_macos_step11d3b import runner_b
class BR8AdmissionTests(unittest.TestCase):
    def test_independent_eighty_second_conditions(self):
        self.assertEqual(len({runner_b.phase_command('state', i)[1] for i in range(1,9)}),8)
        for i in range(1,9): self.assertEqual(runner_b.phase_command('state',i)[0],80)
    def test_out_of_range_condition_is_rejected(self):
        for i in (None,0,9):
            with self.assertRaises(ValueError): runner_b.phase_command('state',i)
    def test_state_requires_authenticated_smoke_before_admission(self):
        with self.assertRaisesRegex(RuntimeError,'authenticated prior smoke'):
            runner_b.run_one('state',1)
if __name__=='__main__': unittest.main()
