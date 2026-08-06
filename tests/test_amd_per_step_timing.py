from __future__ import annotations

import ast
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import amd_profile_cell as cell  # noqa: E402


class PerStepTimingUsesDeviceEventsTest(unittest.TestCase):
    """Per-step timing must not come from a host clock.

    Kernel launches are asynchronous, so a host timer between denoising
    callbacks measures how fast the host queues work until the queue fills.
    The first measurement of this reported 51.8 ms for the early steps
    against 201.1 ms for the middle ones, on steps that do identical work,
    and the plan asks for exactly that early/middle/late split.
    """

    def _diffusion_source(self) -> ast.FunctionDef:
        tree = ast.parse((SCRIPTS / "amd_profile_cell.py").read_text("utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "diffusion":
                return node
        self.fail("diffusion() not found")

    def test_the_callback_records_a_cuda_event(self):
        source = ast.unparse(self._diffusion_source())
        self.assertIn("Event(enable_timing=True)", source)
        self.assertIn("elapsed_time", source)

    def test_the_callback_does_not_read_a_host_clock(self):
        """The regression this guards is precisely perf_counter in on_step."""
        for node in ast.walk(self._diffusion_source()):
            if isinstance(node, ast.FunctionDef) and node.name == "on_step":
                source = ast.unparse(node)
                self.assertNotIn("perf_counter", source)
                self.assertNotIn("time.time", source)
                return
        self.fail("on_step() not found inside diffusion()")

    def test_the_run_synchronises_before_reading_the_events(self):
        """elapsed_time on an unrecorded-yet event is undefined."""
        source = ast.unparse(self._diffusion_source())
        self.assertLess(
            source.index("cuda.synchronize"), source.index("elapsed_time")
        )


class PerStepConsistencyTest(unittest.TestCase):
    """The steps are part of the call, so they cannot outlast it.

    This is the check that would have caught the host-clock defect without
    anyone reading the numbers: a per-step total larger than the end-to-end
    p50 is impossible, and one far smaller means the rest of the call is
    unaccounted for.
    """

    def _fraction(self, total, p50):
        return 0.0 < (total / p50) <= 1.0

    def test_steps_totalling_more_than_the_call_are_inconsistent(self):
        self.assertFalse(self._fraction(6.0, 4.918))

    def test_steps_fitting_inside_the_call_are_consistent(self):
        self.assertTrue(self._fraction(3.8, 4.918))

    def test_the_measured_host_clock_case_was_within_bounds_but_wrong(self):
        """Consistency is necessary, not sufficient -- state that plainly.

        The defective run totalled 2.89 s inside a 4.918 s call, so this
        check alone would have passed it. It bounds the error; the CUDA
        events are what remove it.
        """
        self.assertTrue(self._fraction(19 * 0.15206, 4.918))

    def test_a_zero_or_negative_total_is_inconsistent(self):
        self.assertFalse(self._fraction(0.0, 4.918))
        self.assertFalse(self._fraction(-1.0, 4.918))


class PhaseSummaryTest(unittest.TestCase):
    def test_it_splits_into_thirds(self):
        result = cell.phase_summary([1.0] * 3 + [2.0] * 3 + [3.0] * 3)
        self.assertAlmostEqual(result["early_step_mean_s"], 1.0)
        self.assertAlmostEqual(result["middle_step_mean_s"], 2.0)
        self.assertAlmostEqual(result["late_step_mean_s"], 3.0)

    def test_the_tail_goes_to_the_late_phase_when_it_does_not_divide(self):
        result = cell.phase_summary([1.0, 1.0, 2.0, 2.0, 3.0, 3.0, 3.0, 3.0])
        self.assertAlmostEqual(result["late_step_mean_s"], 3.0)

    def test_an_empty_trajectory_yields_nothing_rather_than_zeros(self):
        self.assertEqual(cell.phase_summary([]), {})


class TelemetryMustNotShareTheTimedProcessTest(unittest.TestCase):
    """Sampling power inside a timed run perturbs the run.

    A thread calling rocm-smi every 0.25 s shares the GIL with the loop
    launching kernels, and the interference scales with how slow those
    kernels are -- 552 samples at 4 units against 85 at 32, which
    manufactured a quota-dependent efficiency trend a kernel trace later
    showed did not exist.
    """

    def test_the_profile_cell_does_not_sample_power_while_timing(self):
        source = (SCRIPTS / "amd_profile_cell.py").read_text("utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                body = ast.unparse(node)
                self.assertNotIn(
                    "package_power_watts()", body,
                    "the cell samples power inside the timed run, which "
                    "contends for the GIL with kernel launches",
                )
                return
        self.fail("main() not found")

    def test_the_power_sweep_runs_its_load_in_a_child_process(self):
        source = (SCRIPTS / "run_amd_power_sweep.py").read_text("utf-8")
        self.assertIn("subprocess.Popen", source)
        self.assertIn("PowerSampler", source)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "run_quota":
                body = ast.unparse(node)
                self.assertIn("Popen", body)
                self.assertIn("sampler.start()", body)
                return
        self.fail("run_quota() not found")

    def test_the_power_load_is_not_a_chain(self):
        """A chained matmul spills to memory and never reaches the cap.

        53.95 TFLOPS at 150 W against 122.58 at 300 W for a single matmul,
        so the chain measures the memory system rather than the CUs.
        """
        source = (SCRIPTS / "amd_matmul_load.py").read_text("utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.While):
                body = ast.unparse(node)
                # One mm per iteration, and its result is not fed back in.
                self.assertEqual(body.count("torch.mm"), 1)
                self.assertNotIn("x = torch.mm(x", body)
                return
        self.fail("no timing loop found")



class PowerSamplerRunsTest(unittest.TestCase):
    """Reading the source is not running it.

    The sampler named its stop flag _stop, which shadows the _stop() method
    threading.Thread calls during join(), so every sweep died at teardown
    with "'Event' object is not callable". The source-level tests passed
    throughout -- they check that a sampler exists, not that it works.
    """

    def _sampler_class(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_pwsweep", SCRIPTS / "run_amd_power_sweep.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_sampler_starts_and_halts_without_error(self):
        module = self._sampler_class()
        module.read_power = lambda: 123.0
        sampler = module.PowerSampler(0.01)
        sampler.start()
        time.sleep(0.1)
        sampler.halt()          # this is what raised TypeError
        self.assertFalse(sampler.is_alive())
        self.assertGreater(len(sampler.samples), 0)
        self.assertEqual(set(sampler.samples), {123.0})

    def test_no_instance_attribute_shadows_a_thread_method(self):
        """Named generically, because the collision is version-specific.

        The failure was on Python 3.12, whose Thread has a _stop() method
        that join() calls during teardown. The dev host runs 3.14, where
        that name is gone, so a test naming _stop would pass here while the
        sweep died there. Any instance attribute covering any Thread
        callable is the bug, whatever it is called.
        """
        import threading

        module = self._sampler_class()
        sampler = module.PowerSampler(0.01)
        clashes = [
            name for name in vars(sampler)
            if callable(getattr(threading.Thread, name, None))
        ]
        self.assertEqual(
            clashes, [],
            f"instance attributes shadow Thread methods: {clashes}",
        )

    def test_a_failing_power_read_is_skipped_not_recorded(self):
        module = self._sampler_class()
        module.read_power = lambda: None
        sampler = module.PowerSampler(0.01)
        sampler.start()
        time.sleep(0.05)
        sampler.halt()
        self.assertEqual(sampler.samples, [])

class SingleSampleTest(unittest.TestCase):
    """A one-sample cell must report no CV, not a CV of zero.

    Sizing runs use --samples 1, and the cell crashed on
    statistics.stdev. Returning 0.0 instead would be worse than crashing:
    zero dispersion reads as perfectly stable and satisfies the CV clause,
    so an unmeasured quantity would pass a gate.
    """

    def test_the_cell_computes_no_cv_from_one_sample(self):
        source = (SCRIPTS / "amd_profile_cell.py").read_text("utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "cv_value" for t in node.targets)
            ):
                expression = ast.unparse(node.value)
                self.assertIn("len(samples) > 1", expression)
                self.assertIn("None", expression)
                return
        self.fail("cv_value is never computed")

    def test_a_none_cv_does_not_satisfy_the_threshold(self):
        for cv, target, expected in ((None, 0.05, False), (0.01, 0.05, True),
                                     (0.09, 0.05, False)):
            with self.subTest(cv=cv):
                self.assertEqual(cv is not None and cv <= target, expected)


if __name__ == "__main__":
    unittest.main()
