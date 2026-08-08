"""A step measured at one quota is not evidence about another.

The adapters read their step time one step late, from CUDA events, so a
per-step ``synchronize`` does not drain the pipeline into the number
being measured. That read only succeeds when the previous step's events
have already retired, and in a tight loop they usually have not -- so the
adapter often carries a stale ``last_step_seconds`` for several steps.
Harmless while the quota is fixed.

It is not harmless when the quota changes, and the runtime changes it
between rounds. Measured on the die: an adapter re-granted 16 units after
running at 32 reported 107 ms for the whole 16-unit run, which is the
32-unit cost, and the 32-unit run that followed reported 152 ms, which
was the 16-unit one. Both numbers were plausible and both were the other
width's. Every earlier harness built a fresh adapter per run, so the
first step always drained and the defect never surfaced; the runtime
reuses one adapter per model, and charges the ledger from this field.

So the guard is "no measurement yet *at this quota*", not "no measurement
yet" -- the same distinction the policy's ``observed_at_units`` exists
for, one layer down.

These drive the real ``denoise_one`` with a fake torch and a fake
pipeline rather than asserting on source text, because what matters is
when a drain happens, and that is behaviour.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from burstserve.executor import StepState  # noqa: E402


class FakeTensor:
    """Enough arithmetic for the guidance combination, and nothing else."""

    def __init__(self, value: float = 1.0):
        self.value = value

    def chunk(self, n):
        return tuple(FakeTensor(self.value) for _ in range(n))

    def __add__(self, other):
        return FakeTensor(self.value + getattr(other, "value", other))

    def __sub__(self, other):
        return FakeTensor(self.value - getattr(other, "value", other))

    def __rmul__(self, other):
        return FakeTensor(self.value * other)

    __mul__ = __rmul__


class FakeEvent:
    """Records, and reports whether its work has retired.

    ``retired`` is the whole point: leaving it False is what a real device
    does for a step that was only just issued, and it is the condition
    under which the stale reading survives.
    """

    def __init__(self, registry, retired: bool):
        self.registry = registry
        self.retired = retired
        self.synchronised = False

    def record(self, stream=None):
        pass

    def query(self):
        return self.retired

    def synchronize(self):
        self.synchronised = True
        self.registry.append(self)

    def elapsed_time(self, other):
        # Milliseconds, keyed to the quota the step ran at so a stale
        # reading is distinguishable from a fresh one by its value alone.
        return self.registry.elapsed_ms


class EventFactory(list):
    elapsed_ms = 0.0


class FakeCuda:
    def __init__(self, registry):
        self.registry = registry

    def Event(self, enable_timing=False):
        return FakeEvent(self.registry, self.registry.retired)

    def stream(self, s):
        raise AssertionError("no masked stream is set in these tests")


class FakeNoGrad:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeGenerator:
    def __init__(self, *a, **k):
        self._state = b""

    def set_state(self, state):
        self._state = state

    def get_state(self):
        return self._state


class FakeTorch:
    def __init__(self, registry):
        self.cuda = FakeCuda(registry)

    def no_grad(self):
        return FakeNoGrad()

    def cat(self, tensors):
        return FakeTensor(sum(t.value for t in tensors))

    def Generator(self, device=None):
        return FakeGenerator()


class FakeScheduler:
    def __init__(self):
        self.timesteps = [0, 1, 2, 3, 4, 5, 6, 7]
        self._step_index = 0

    def scale_model_input(self, x, t):
        return x

    def step(self, noise, t, latents, generator=None, return_dict=True):
        return (FakeTensor(latents.value + 1),)


class FakeUnet:
    def __call__(self, *a, **k):
        return (FakeTensor(2.0),)


class FakePipeline:
    def __init__(self):
        self.unet = FakeUnet()


def build_adapter(registry):
    """An adapter with its collaborators replaced, __init__ bypassed.

    __init__ loads a diffusion pipeline and encodes a prompt; neither is
    what these tests are about, and both need a GPU.
    """
    import amd_sdxl_adapter

    adapter = object.__new__(amd_sdxl_adapter.SdxlStepAdapter)
    adapter.torch = FakeTorch(registry)
    adapter.pipeline = FakePipeline()
    adapter.scheduler = FakeScheduler()
    adapter.device = "cuda"
    adapter.guidance_scale = 5.0
    adapter.embeds = FakeTensor(1.0)
    adapter.added = {}
    adapter.last_step_seconds = None
    adapter.last_step_units = None
    adapter._pending_events = None
    adapter.stream = None
    adapter._external_streams = {}
    adapter._previous_stream = None
    return adapter


def a_state(index: int = 0) -> StepState:
    return StepState(step_index=index, latent=FakeTensor(1.0),
                     rng_state=b"", extra={"timesteps": [0, 1, 2, 3, 4, 5],
                                           "scheduler_step_index": index})


class TheFirstStepAtAQuotaIsAlwaysMeasured(unittest.TestCase):
    def setUp(self):
        self.registry = EventFactory()
        self.registry.retired = False      # events never retire in time
        self.registry.elapsed_ms = 160.0
        self.adapter = build_adapter(self.registry)

    def test_the_very_first_step_drains(self):
        """Otherwise the runtime charges it from the cost model."""
        self.adapter.denoise_one(a_state(0), quota_units=16)
        self.assertEqual(len(self.registry), 1, "one synchronize")
        self.assertAlmostEqual(self.adapter.last_step_seconds, 0.160)
        self.assertEqual(self.adapter.last_step_units, 16)

    def test_later_steps_at_the_same_quota_do_not_drain(self):
        """The drain this design exists to avoid."""
        state = a_state(0)
        for index in range(5):
            state = self.adapter.denoise_one(state, quota_units=16)
        self.assertEqual(len(self.registry), 1,
                         "only the first step drained")

    def test_a_new_quota_drains_again(self):
        """The defect: without this the 16-unit run reports 32-unit cost."""
        state = self.adapter.denoise_one(a_state(0), quota_units=32)
        self.registry.elapsed_ms = 160.0
        state = self.adapter.denoise_one(state, quota_units=16)
        self.assertEqual(len(self.registry), 2, "the width change drained")
        self.assertEqual(self.adapter.last_step_units, 16)
        self.assertAlmostEqual(self.adapter.last_step_seconds, 0.160)

    def test_without_the_width_the_reading_would_be_the_old_quota(self):
        """Names the wrong answer, so the test fails if the guard weakens.

        With the events never retiring, a guard keyed only on "is there a
        measurement" leaves the 32-unit reading in place for every
        subsequent 16-unit step.
        """
        self.registry.elapsed_ms = 108.0
        state = self.adapter.denoise_one(a_state(0), quota_units=32)
        stale = self.adapter.last_step_seconds
        self.registry.elapsed_ms = 163.0
        for _ in range(4):
            state = self.adapter.denoise_one(state, quota_units=16)
        self.assertNotAlmostEqual(self.adapter.last_step_seconds, stale)
        self.assertAlmostEqual(self.adapter.last_step_seconds, 0.163)

    def test_a_retired_event_is_read_without_draining(self):
        """The deferred read still works when it can."""
        self.registry.retired = True
        state = self.adapter.denoise_one(a_state(0), quota_units=16)
        drains_after_first = len(self.registry)
        state = self.adapter.denoise_one(state, quota_units=16)
        self.assertEqual(len(self.registry), drains_after_first,
                         "the retired event was read, not synchronised")
        self.assertEqual(self.adapter.last_step_units, 16)


class TheCogVideoXAdapterCarriesTheSameGuard(unittest.TestCase):
    """The two adapters are copies of each other; copies drift.

    Asserted on source rather than behaviour because CogVideoX's
    ``denoise_one`` needs a transformer, rotary state and a 5-D latent to
    drive, and none of that changes what this guard does.
    """

    def test_it_records_the_quota_with_the_events(self):
        source = (SCRIPTS / "amd_cogvideox_adapter.py").read_text("utf-8")
        self.assertIn("self._pending_events = (start, end, quota_units)",
                      source)

    def test_it_drains_on_a_quota_change(self):
        source = (SCRIPTS / "amd_cogvideox_adapter.py").read_text("utf-8")
        self.assertIn("or self.last_step_units != quota_units", source)


if __name__ == "__main__":
    unittest.main()
