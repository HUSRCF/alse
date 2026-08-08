"""Suspension must be invisible in the output.

Gate C's runtime acceptance asks for a latent hash identical to the ASLE
seed's in deterministic mode. A scheduler that perturbs results when it
preempts cannot deliver that however good its decisions are, so the
executor's central property is that an interrupted run and an
uninterrupted one produce the same bytes.

The tests use an adapter whose arithmetic is exact and whose RNG is
explicit, so a divergence is a divergence and not a floating-point
argument. The failure this guards against is specific: saving the latent
without the generator state resumes from the right image and the wrong
noise, and produces a different result from the same seed without
raising anything.
"""

from __future__ import annotations

import random
import sys
import unittest

sys.dont_write_bytecode = True

from burstserve.executor import (
    ExecutorError,
    Phase,
    StepExecutor,
    StepState,
)


class ToyAdapter:
    """Deterministic denoising with a per-step random draw.

    The draw is what makes this a real test. Without it, an executor that
    forgot the generator state would still pass, because the latent alone
    would carry everything.
    """

    def __init__(self, seed: int = 0):
        self.seed = seed
        self.steps_executed = 0

    def initial_state(self, request) -> StepState:
        rng = random.Random(self.seed)
        return StepState(step_index=0, latent=(0.0,), rng_state=rng.getstate())

    def denoise_one(self, state: StepState, *, quota_units: int) -> StepState:
        self.steps_executed += 1
        rng = random.Random()
        rng.setstate(state.rng_state)
        noise = rng.random()
        value = state.latent[0] * 0.5 + noise
        return StepState(
            step_index=state.step_index + 1,
            latent=(value,),
            rng_state=rng.getstate(),
            # Quota is recorded but does not change the result: a step's
            # output must not depend on how much die it ran on, or
            # scheduling would change the image.
            extra={"last_quota": quota_units},
        )

    def decode(self, state: StepState):
        return ("image", round(state.latent[0], 12))


def run_uninterrupted(steps: int, seed: int = 0, quota: int = 32):
    executor = StepExecutor(object(), ToyAdapter(seed), total_steps=steps)
    executor.prepare()
    while executor.run_step(quota_units=quota):
        pass
    executor.finalize()
    return executor


def run_interrupted(steps: int, at: list[int], seed: int = 0,
                    quotas=(32, 16, 8)):
    """Suspend after each step in ``at``, and vary the quota while at it."""
    executor = StepExecutor(object(), ToyAdapter(seed), total_steps=steps)
    executor.prepare()
    index = 0
    while True:
        quota = quotas[index % len(quotas)]
        more = executor.run_step(quota_units=quota)
        index += 1
        if executor.steps_done in at and more:
            state = executor.suspend()
            executor.resume(state)
        if not more:
            break
    executor.finalize()
    return executor


class SuspensionIsInvisible(unittest.TestCase):
    def test_one_suspension_changes_nothing(self):
        plain = run_uninterrupted(8)
        broken = run_interrupted(8, at=[4])
        self.assertEqual(plain.digest(), broken.digest())

    def test_suspending_after_every_step_changes_nothing(self):
        plain = run_uninterrupted(8)
        broken = run_interrupted(8, at=list(range(1, 8)))
        self.assertEqual(plain.digest(), broken.digest())
        self.assertEqual(broken.suspensions, 7)
        self.assertEqual(broken.resumptions, 7)

    def test_the_quota_does_not_change_the_result(self):
        """Scheduling decides speed, not output.

        If a step's result depended on its quota, every fairness or
        throughput decision would also be an image decision, and the
        deterministic-mode hash could not hold.
        """
        wide = run_uninterrupted(8, quota=32)
        narrow = run_uninterrupted(8, quota=4)
        self.assertEqual(wide.digest(), narrow.digest())

    def test_the_test_can_detect_a_lost_generator(self):
        """The guard on the guard.

        An executor that restored the latent and dropped the RNG would
        pass every test above if the toy adapter had no per-step draw.
        This drops it deliberately and asserts the digests diverge, so
        the suite is known to be sensitive to the failure it is written
        for.
        """
        plain = run_uninterrupted(8)

        adapter = ToyAdapter(0)
        executor = StepExecutor(object(), adapter, total_steps=8)
        executor.prepare()
        for _ in range(4):
            executor.run_step(quota_units=32)
        state = executor.suspend()
        # Resume with a fresh generator: the latent is right, the noise
        # sequence is not.
        lost = StepState(step_index=state.step_index, latent=state.latent,
                         rng_state=random.Random(999).getstate())
        executor.resume(lost)
        while executor.run_step(quota_units=32):
            pass
        executor.finalize()
        self.assertNotEqual(plain.digest(), executor.digest())

    def test_every_step_runs_exactly_once_across_suspensions(self):
        """A resume that replays or skips work would still hash
        differently, but the count says which of the two happened."""
        adapter = ToyAdapter(0)
        executor = StepExecutor(object(), adapter, total_steps=8)
        executor.prepare()
        while True:
            more = executor.run_step(quota_units=16)
            if more:
                executor.resume(executor.suspend())
            else:
                break
        executor.finalize()
        self.assertEqual(adapter.steps_executed, 8)
        self.assertEqual(executor.steps_done, 8)


class IllegalTransitionsAreRefused(unittest.TestCase):
    """A scheduler bug should fail at the call, not corrupt a ledger."""

    def test_run_step_before_prepare(self):
        executor = StepExecutor(object(), ToyAdapter(), total_steps=4)
        with self.assertRaises(ExecutorError):
            executor.run_step(quota_units=32)

    def test_run_step_past_the_end(self):
        executor = run_uninterrupted(4)
        with self.assertRaises(ExecutorError):
            executor.run_step(quota_units=32)

    def test_finalize_before_the_last_step(self):
        executor = StepExecutor(object(), ToyAdapter(), total_steps=4)
        executor.prepare()
        executor.run_step(quota_units=32)
        with self.assertRaises(ExecutorError):
            executor.finalize()

    def test_resume_without_suspending(self):
        executor = StepExecutor(object(), ToyAdapter(), total_steps=4)
        executor.prepare()
        with self.assertRaises(ExecutorError):
            executor.resume()

    def test_resume_at_the_wrong_step_is_refused(self):
        """Silently accepting it would replay or skip work, and the only
        symptom would be a wrong image."""
        executor = StepExecutor(object(), ToyAdapter(), total_steps=8)
        executor.prepare()
        for _ in range(3):
            executor.run_step(quota_units=32)
        state = executor.suspend()
        stale = StepState(step_index=1, latent=state.latent,
                          rng_state=state.rng_state)
        with self.assertRaises(ExecutorError):
            executor.resume(stale)

    def test_digest_before_finalize(self):
        executor = StepExecutor(object(), ToyAdapter(), total_steps=4)
        executor.prepare()
        with self.assertRaises(ExecutorError):
            executor.digest()

    def test_a_zero_step_request_is_refused_at_construction(self):
        with self.assertRaises(ValueError):
            StepExecutor(object(), ToyAdapter(), total_steps=0)

    def test_a_step_needs_a_positive_quota(self):
        executor = StepExecutor(object(), ToyAdapter(), total_steps=4)
        executor.prepare()
        with self.assertRaises(ValueError):
            executor.run_step(quota_units=0)


class AnAdapterThatDoesNotAdvanceIsCaught(unittest.TestCase):
    """The one thing the executor cannot verify by construction.

    An adapter returning a state whose index did not move would make
    every resume replay the same step forever, and the executor's own
    counter would still look correct.
    """

    def test_a_stuck_index_raises(self):
        class Stuck(ToyAdapter):
            def denoise_one(self, state, *, quota_units):
                return StepState(step_index=state.step_index,
                                 latent=state.latent,
                                 rng_state=state.rng_state)

        executor = StepExecutor(object(), Stuck(), total_steps=4)
        executor.prepare()
        with self.assertRaises(ExecutorError):
            executor.run_step(quota_units=32)


class SchedulerFacingState(unittest.TestCase):
    def test_runnable_tracks_the_lifecycle(self):
        executor = StepExecutor(object(), ToyAdapter(), total_steps=2)
        self.assertFalse(executor.runnable)
        executor.prepare()
        self.assertTrue(executor.runnable)
        executor.run_step(quota_units=32)
        self.assertTrue(executor.runnable)
        executor.run_step(quota_units=32)
        self.assertFalse(executor.runnable)   # complete
        executor.finalize()
        self.assertIs(executor.phase, Phase.FINISHED)

    def test_a_failed_request_is_distinguishable_from_an_unstarted_one(self):
        executor = StepExecutor(object(), ToyAdapter(), total_steps=4)
        executor.prepare()
        executor.run_step(quota_units=32)
        executor.fail("out of memory")
        self.assertIs(executor.phase, Phase.FAILED)
        self.assertFalse(executor.runnable)
        self.assertEqual(executor.steps_done, 1)

    def test_the_quota_history_records_what_each_step_ran_on(self):
        """The ledger reconciles against this, so it is kept where the
        step happened rather than reconstructed afterwards."""
        executor = run_interrupted(6, at=[2, 4])
        self.assertEqual([q for _, q in executor.quota_history],
                         [32, 16, 8, 32, 16, 8])


if __name__ == "__main__":
    unittest.main()
