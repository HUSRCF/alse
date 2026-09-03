"""A trace-driven simulator whose costs come from measurement, not guesses.

Gate C asks for byte-identical results from the same seed, and for
canonical service accounting to track SM quota within 1%. Both are
properties of the simulator's construction rather than of its tuning, so
they are built in here:

* every quantity that varies is drawn from a seeded PRNG owned by the
  simulator, and the event loop breaks ties by a total order over
  (time, sequence, tenant), so no result depends on dict iteration,
  floating-point summation order, or wall-clock timing;

* step costs come from the Gate B quota tables via
  :class:`QuotaCostModel`, and co-run costs from the measured externality
  table. Where the table has no entry the simulator says so rather than
  interpolating silently -- the measured penalty spans 22.3% to 192.6%
  across four pairs, so an invented value in between could be wrong by a
  factor of eight.

The scheduler itself is deliberately not decided here. This module
provides the world; policies are separate so that baselines and an oracle
can be compared on identical traces.
"""

from __future__ import annotations

import heapq
import hashlib
import random
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

# Measured on gfx1201, 2026-08-03/04. serial is the fraction of solo time
# that does not shrink with quota, taken from the fitted Amdahl parameters.
MEASURED_MODELS: dict[str, dict[str, float]] = {
    "sdxl": {
        # Refitted 2026-08-06 against the per-step curve. 0.391 came from
        # the call-level fit, where the VAE decode contributes a serial
        # term the denoising steps do not have; carried onto the per-step
        # curve it put the 15-unit extrapolation *below* the measured
        # 12-unit cost.
        "serial_fraction": 0.4419,
        # 768x768, 32 units, measured per denoising step with CUDA events
        # on 2026-08-06. The previous value, 0.1521, was 768 at *16* units
        # -- a half-die figure recorded as the full-die one, with a comment
        # claiming a resolution Gate B never used for this model. See
        # docs/gate-c-decision-log.md.
        "step_seconds_at_full": 0.11552,
        "maskable_units": 32,
    },
    "cogvideox-2b": {
        # Refitted with the curve below, same reason as SDXL: 0.276 came
        # from the call-level fit.
        "serial_fraction": 0.2051,
        # 9 frames, 32 units, measured per denoising step with CUDA events
        # on 2026-08-06. The previous 0.5171 was right to 0.3% -- this
        # model's constant was never the problem, only its curve.
        "step_seconds_at_full": 0.51549,
        "maskable_units": 32,
    },
}

# The measured cost at each quota. Preferred over the Amdahl fit wherever
# an entry exists: the fit exists to extrapolate to quotas that were never
# run, and using it where a measurement is available throws away accuracy
# for no reason. Its residual is not neutral either -- the fit
# under-predicts speed at low quota, which under-states exactly the effect
# this work claims.
#
# SDXL is per denoising step, measured directly at 768x768 on 2026-08-06.
# It was previously the Gate B *call* p50s, which include a VAE decode that
# does not scale with quota the way the steps do, so their ratios are
# diluted by a term the per-step ratios do not contain: 1.503 at 16/32
# where the per-step measurement says 1.363. Combined with the wrong
# full-die constant, the shipped table ran 32% to 50% high at every quota.
#
# CogVideoX-2b was rebuilt the same way on the same day. Its error was
# far smaller -- the call table ran 6.5% low at 4 units falling to 0.3%
# high at 32, against SDXL's 32% to 50% -- because its full-die constant
# was correct and only the curve was diluted. Both models are now the
# same quantity, which they were not before.
#
# Video cells are labelled by frame count: CogVideoX runs at its
# pipeline's native resolution and ignores height/width, so calling this
# curve "768x768" would name a parameter that had no effect on it.
MEASURED_QUOTA_SECONDS: dict[str, dict[int, float]] = {
    # per denoising step, seconds, 768x768
    "sdxl": {4: 0.52141, 8: 0.26875, 12: 0.19350, 16: 0.15750,
             20: 0.13867, 24: 0.12493, 28: 0.12056, 32: 0.11552},
    # per denoising step, seconds, 9 frames
    "cogvideox-2b": {4: 3.11634, 8: 1.57411, 12: 1.05900, 16: 0.80519,
                     20: 0.68764, 24: 0.60812, 28: 0.55175, 32: 0.51549},
}

# Cost tables by device. gfx1201 is the R9700's 32 maskable units, the
# only device measured until the second SKU came back on 2026-09-03.
#
# gfx90a (MI250X, 104 units per GCD) was measured on 2026-09-03 through
# the stream path -- ``scripts/measure_quota_curve.py`` -- because Gate B's
# sweep drives its cells with ROC_GLOBAL_CU_MASK and that path silently
# rounds 52 up to 64 there, which would read as a cheap half-die rather
# than as an error. Every mask below was read back and matched exactly;
# worst CV 0.11%. See experiments/probes/gfx90a/.
#
# The quotas are gfx1201's die fractions, not its unit counts: 13/104 =
# 4/32, 26 = 8, 52 = 16, 78 = 24, 91 = 28. The two architectures are
# therefore comparable at equal shares, and the set is closed under
# complement (13+91, 26+78, 52+52), so a two-tenant split has both sides
# measured and never reaches the fit.
#
# ``serial_fraction`` is 0.0 on this device and that is NOT a fit. Amdahl
# does not describe gfx90a: ``fit_quota_latency`` returns a NEGATIVE
# serial term for both models (-0.0020 s for SDXL, -0.2348 s for
# CogVideoX-2b), and the refutation is stronger than a bad fit -- under
# latency = serial + parallel/q the unit-seconds per step, q*t(q), must
# increase in q, and here it falls from 13 to 26 units before rising. The
# die is least efficient at its narrowest mask AND at its widest, with a
# minimum near a quarter of the die. Leave-one-out MAPE against the
# Amdahl form is 24.9% and 28.1% here against 5.6% and 4.6% on gfx1201,
# where Gate B's bar is 10%.
#
# 0.0 is the closest admissible value to the fit, and the error it leaves
# does not even have a consistent SIGN. At 13 units the Amdahl branch
# predicts 0.958 s for SDXL where 0.854 s was measured -- 12% pessimistic
# -- and 3.384 s for CogVideoX-2b where 4.269 s was measured, 21%
# optimistic. Two models, one device, opposite directions, because the
# efficiency optimum sits at a different width for each. So an unmeasured
# quota here cannot even be described as conservative. That is why the
# measured set is closed under complement, and why
# ``tests/test_gfx90a_cost_tables.py`` pins both directions.
MEASURED_MODELS_GFX90A: dict[str, dict[str, float]] = {
    "sdxl": {
        # not a fit -- see above
        "serial_fraction": 0.0,
        # 768x768, 104 units, per denoising step, stream path, 2026-09-03
        "step_seconds_at_full": 0.11971,
        "maskable_units": 104,
    },
    "cogvideox-2b": {
        # not a fit -- see above
        "serial_fraction": 0.0,
        # 9 frames, 480x720, 104 units, per denoising step, 2026-09-03
        "step_seconds_at_full": 0.42295,
        "maskable_units": 104,
    },
}
MEASURED_QUOTA_SECONDS_GFX90A: dict[str, dict[int, float]] = {
    # per denoising step, seconds, 768x768
    "sdxl": {13: 0.85384, 26: 0.31619, 52: 0.18247,
             78: 0.15377, 91: 0.14476, 104: 0.11971},
    # per denoising step, seconds, 9 frames at 480x720
    "cogvideox-2b": {13: 4.26942, 26: 1.45359, 52: 0.79353,
                     78: 0.56741, 91: 0.48813, 104: 0.42295},
}

DEVICE_TABLES: dict[str, tuple[dict, dict]] = {
    "gfx1201": (MEASURED_MODELS, MEASURED_QUOTA_SECONDS),
    "gfx90a": (MEASURED_MODELS_GFX90A, MEASURED_QUOTA_SECONDS_GFX90A),
}


# Measured pairwise externality: (own units, peer units) -> slowdown factor
# applied to this tenant's step time.
#
# (16,16) is the per-step penalty at 768x768, the mean of 24 side
# measurements across 12 in-process trials (+21.1% to +28.7%, mean
# +23.67%) -- the arrangement the runtime uses. The two-process harness's
# fast state gives 1.2362 over 18 sides, agreeing to 0.04%. It is the only entry the scheduler reads, since
# the policy either splits evenly or gives the whole die to one tenant.
#
# Every entry is now in-process at 768x768, replacing call-level figures
# taken at 512x512 with two processes. The replacement was not cosmetic:
# the old (28,4) entry read 1.926 against a measured 1.070, an 80%
# overstatement, and (8,24) and (24,8) were out by 14%.
#
# The old table was also non-monotone in own-quota -- 16+16 appearing to
# cost less than 8+24 -- and that was the stated reason this function
# refuses to interpolate. The measured curve is monotone: 1.338, 1.307,
# 1.237, 1.126, 1.071 as own-quota rises. Interpolation is still refused,
# but now because five points do not establish a shape, not because the
# shape was strange.
#
# Two-process co-runs also produce a slow state -- 20 side measurements,
# +72.9% to +83.4% at 16+16 -- fixed before the first step and not
# distinguished by temperature, power, any clock, GPU utilisation or the
# CU mask. It does not occur in-process.
#
# One entry per split, not per model: CogVideoX-2b cannot be co-run on this
# card at all -- one process peaks at 28.54 GB and two need 57 on 34.2 GB --
# so applying these to it is an extrapolation across models, recorded in
# the decision log rather than hidden here.
# A 16+16 pairing measured with **two processes** falls into two disjoint
# states, drawn per run at roughly 30% for the fast one. Four
# explanations were proposed and retracted before the null was measured;
# see docs/gate-c-decision-log.md.
#
# 2026-08-08: the paragraph that stood here said the bistability belonged
# to the two-process harness rather than to the die, on 17 in-process
# trials that all came out fast. Those were whole-``pipeline(...)`` calls.
# Measured per step, in exactly the arrangement this runtime uses -- one
# process, two disjoint CU masks -- eight processes ran twelve co-run
# episodes each and two of them latched slow:
#
#   steady state fast   6 of 8 processes   n=54   1.176   (1.163-1.202)
#   steady state slow   2 of 8 processes   n=18   1.776   (1.732-1.850)
#
# The die has the bistability. What the call-level harness saw was the
# VAE decode diluting it, the same granularity error that put a call-level
# externality into a per-step decision.
#
# Three properties decide what a scheduler can do about it, and all three
# were measured rather than assumed. The state is drawn per *mask pair*
# and latches: 0 of 8 processes flipped across nine later episodes, and
# every episode built fresh adapters and re-acquired its streams, so
# re-forming a pairing does not redraw. Interposing a different pair does
# not disturb it: sixteen before/after checks, sixteen unchanged. And it
# is visible in a single step -- each episode's first step against its own
# median gives a mean absolute error of 0.00% over 72 episodes.
#
# The first co-run in a process is a surcharge over whichever state was
# drawn, not a third state: fast processes open at 1.52-1.61 over a 1.176
# plateau, slow ones at 2.04-2.09 over 1.776.
PAIRING_STATE_RATE = 0.30           # probability of the fast state
FAST_PAIRING_EXTERNALITY = 1.2367   # 24 in-process sides, +21.1 to +28.7%
SLOW_PAIRING_EXTERNALITY = 1.7866   # 20 side measurements, +72.9 to +83.4%

# The per-step measurements. Deliberately separate from the two constants
# above, which are call-level: ``probing_partitioning`` multiplies
# FAST_PAIRING_EXTERNALITY by its slow_factor to get a threshold of
# 1.608x, and that threshold happens to be well placed for the per-step
# states -- it keeps a fast pairing (1.176) and its first-co-run surcharge
# (1.52-1.61), and catches a slow one (1.776) and its surcharge
# (2.04-2.09). Replacing the constant with the measured 1.176 would move
# the threshold to 1.529 and start discarding fast pairings on their first
# step. The number is load-bearing as a threshold coefficient, so the
# measurements go beside it rather than into it.
# Re-established 2026-08-09 on the device-span instrument, after the
# adapter's deferred reading was found to hold one value for a whole
# episode whenever the measured side's steps are short. Solo and co-run
# both come from that instrument now, so the ratio below does not mix
# two ways of measuring a step.
#
#   fast   12 of 14 processes   1.299   (1.238-1.344)
#   slow    2 of 14 processes   1.945   (1.843-1.970)
#
# The shape the adapter reported survived -- two states, latched per
# process, no process straddling -- and its numbers did not: it gave
# 1.176 and 1.776 for the same two states. Recorded because "the old
# reading was 8% low on one state and 9% low on the other" is a more
# useful fact about that instrument than "it was wrong".
MEASURED_STEP_PAIRING_FAST = 1.299   # 12 of 14 processes, span
MEASURED_STEP_PAIRING_SLOW = 1.945   # 2 of 14 processes, span
MEASURED_STEP_PAIRING_FAST_RATE = 0.857  # 12 of 14; see below
# The rate is the one thing here a design should not lean on. Two slow
# draws in eight is 25% with a wide interval, and ten decorrelation
# processes earlier the same day all drew fast, which at 25% has
# probability 0.056. What a scheduler needs is that the state is
# detectable, which it is; not that it is rare, which is unestablished.

# Per-step, per-side, per model pair. Keyed by (model, units,
# peer_model, peer_units) and giving *that side's* factor, because a
# mismatched pairing does not penalise its two tenants equally: at 24+8
# SDXL reads 0.998 while CogVideoX reads 1.007, and at 16+16 they read
# 1.010 and 1.051.
#
# Measured 2026-08-08 on gfx1201 with each model's own step adapter, SDXL
# at 768x768 and CogVideoX-2b at 480x720x9f, taking the settled episodes
# only. Every mismatched pairing measured settles to both tenants running
# at very nearly solo speed, which is why these are all near 1.0 -- the
# self-paired entries are the ones that are not.
#
# **The first two co-run episodes of a mismatched pairing are not
# described by this table and cannot be.** They run serialised: each
# side's step costs about the sum of the two solo steps, which is a
# factor of 1.89 to 26.3 depending on the split. A cost model keyed on
# widths has nowhere to put "and for the first two rounds it is 26x", and
# a scheduler meets that through measurement -- the runtime's drift
# envelope and the policy's probe -- rather than through a lookup.
#
# Deliberately **not** wired into ``externality()`` yet. That function is
# on the path the behavioural lock covers, and repointing it would change
# what every mixed-model trace costs; the change is worth making
# separately, with the lock re-examined, rather than as a side effect of
# recording measurements. See docs/gate-c-decision-log.md.
# Per-step, per-side, per model pair. Keyed by (model, units,
# peer_model, peer_units) and giving *that side's* factor, because a
# mismatched pairing does not penalise its two tenants equally: at 16+16
# SDXL reads 1.016 and CogVideoX 1.047.
#
# **Re-measured 2026-08-10 on the device-span instrument** after the
# adapter's deferred reading was found to hold one stale value for a
# whole episode. Five splits, five processes each, three episodes, solo
# and co-run both by span: 15 measurements per entry, every range within
# 0.005. The suspended adapter-derived values were within 4% of these, so
# the settled figures had been roughly right -- what the old instrument
# invented was the two-episode transient, not the plateau.
#
# **The transient is now definitively an artifact.** Per episode, across
# all five splits, the span instrument reads flat to three decimals:
# 4+28 gives 1.025/1.025/1.024, 28+4 gives 1.001/1.001/1.003. The
# "serialisation" account -- each side costing solo_a + solo_b for two
# episodes, fitted across five splits to within 3-9% -- was fitting a
# stale variable. A good fit to an artifact is still a fit.
#
# What the entries mean for the die: a mismatched pairing is very nearly
# free on both sides at every split, and partitioning Pareto-dominates
# rotation there -- +71.4% and +1.4% at 4+28 through +3.6% and +30.9% at
# 28+4, with the split deciding how the gain divides rather than whether
# there is one.
#
# Deliberately **not** wired into ``externality()``: that function is on
# the path the behavioural lock covers, and repointing it would change
# what every mixed-model trace costs. Worth doing on its own, with the
# lock re-examined.
MEASURED_STEP_PAIR_EXTERNALITY: dict[tuple[str, int, str, int], float] = {
    # SDXL beside CogVideoX-2b. Span instrument, 15 measurements each.
    ("sdxl", 4, "cogvideox-2b", 28): 1.025,
    ("sdxl", 8, "cogvideox-2b", 24): 1.029,
    ("sdxl", 16, "cogvideox-2b", 16): 1.016,
    ("sdxl", 24, "cogvideox-2b", 8): 1.004,
    ("sdxl", 28, "cogvideox-2b", 4): 1.002,
    # CogVideoX-2b beside SDXL. Same pairings, the other side.
    ("cogvideox-2b", 28, "sdxl", 4): 1.063,
    ("cogvideox-2b", 24, "sdxl", 8): 1.052,
    ("cogvideox-2b", 16, "sdxl", 16): 1.047,
    ("cogvideox-2b", 8, "sdxl", 24): 1.006,
    ("cogvideox-2b", 4, "sdxl", 28): 1.010,
    # SDXL against itself. The only pairing measured that is bistable,
    # and the entry a table cannot honestly hold on its own: the die draws
    # one of these per mask pair and latches it, 6 of 8 processes fast.
    # MEASURED_STEP_PAIRING_FAST / _SLOW carry the same two numbers for
    # the simulator's draw.
    ("sdxl", 16, "sdxl", 16): MEASURED_STEP_PAIRING_FAST,  # span
}

# The transient, kept because it is the largest effect in the data and
# leaving it out of the module would make the table above read as the
# whole story. Value is the number of co-run episodes a mismatched
# pairing spends serialised before it settles.
MEASURED_MISMATCHED_SETTLE_EPISODES = 2


def step_pair_externality(model: str, units: int, peer_model: str,
                          peer_units: int) -> float | None:
    """That side's settled per-step factor, or None if unmeasured.

    Returns None rather than raising, and rather than falling back to the
    width-only table: the width-only table is call-level and describes a
    different quantity, so substituting it would be the granularity error
    this project has now made twice.
    """
    return MEASURED_STEP_PAIR_EXTERNALITY.get(
        (model, units, peer_model, peer_units))


MEASURED_EXTERNALITY: dict[tuple[int, int], float] = {
    (4, 28): 1.3383,
    (8, 24): 1.3070,
    (16, 16): 1.2367,
    (24, 8): 1.1259,
    (28, 4): 1.0706,
}


# Per-model overrides where the model's own co-run has been measured.
# Gate B recorded CogVideoX-2b's co-run as impossible -- two processes
# need 57 GB on a 34.2 GB card -- and it is measurable in the runtime's
# arrangement, where one copy of the weights serves both streams: 14.7 GB
# instead of 29.2, and 6 side measurements at 9 frames giving 1.2891.
#
# It is 4.2% above SDXL's 1.2367 at the same split, so the penalty is
# model-dependent and the shared table under-states it for this model.
# Only (16,16) is measured per-model; other splits fall back to the SDXL
# table and are extrapolations across models, recorded as such rather
# than presented as measurements.
MEASURED_EXTERNALITY_BY_MODEL: dict[str, dict[tuple[int, int], float]] = {
    "cogvideox-2b": {(16, 16): 1.2891},
}

# The same quantity on gfx90a, measured 2026-09-04 with the same harness
# that produced the table above -- run_amd_inproc_corun.py, one process,
# one copy of the weights, two masked streams, self-paired SDXL at
# 768x768, only samples inside the overlap window counted. Checked
# against the stored gfx1201 probe rather than assumed, because a table
# half-measured with another harness would be two quantities in one
# column.
#
# The splits are gfx1201's die fractions rather than its unit counts --
# 13/104 = 4/32, 26 = 8, 52 = 16 -- so the two architectures are compared
# at equal shares, and each co-run fills two entries.
# Twelve trials at 52+52 and six at each asymmetric split, across GCD 4
# and GCD 5, worst per-trial cv 0.58%, every mask read back and the two
# sides disjoint.
#
# **It is monotone in own-quota, and it is nearly gfx1201's.** At the same
# die fractions the two agree to within 0.039 absolute and 3.6% relative
# -- gfx90a 1.3% higher at the narrowest slice, 1.5-3.6% lower at every
# other, so its curve is slightly the steeper:
#
#     fraction   1/8     1/4     1/2     3/4     7/8
#     gfx90a   1.3559  1.2770  1.2176  1.1016  1.0317
#     gfx1201  1.3383  1.3070  1.2367  1.1259  1.0706
#
# So the contention travels and the scaling does not. gfx90a's quota
# curve has a different *shape* from gfx1201's -- 1.10, where the Amdahl
# form is refuted outright -- while its co-run penalty is the same curve
# to a few parts in a hundred. Whatever the penalty is, it is not a
# property of how the die scales with width.
#
# Cross-check: the 2026-08-25 overlap probe measured 52+52 at 1.2336 with
# a different harness and a per-step instrument; this is 1.2176, 1.3%
# apart. GCD 4 and GCD 5 differ by 0.4% to 0.6%, the deterministic
# device-to-device effect that probe also resolved.
MEASURED_EXTERNALITY_GFX90A: dict[tuple[int, int], float] = {
    (13, 91): 1.3559,
    (26, 78): 1.2770,
    (52, 52): 1.2176,
    (78, 26): 1.1016,
    (91, 13): 1.0317,
}

# The same-model penalty as a function of how many ways the die is split,
# measured 2026-09-04 on gfx90a with scripts/run_amd_nway_corun.py.
# Denoising only -- output_type="latent" -- because the shared VAE is
# upcast by whichever slice reaches the decode first, and because the
# burst arithmetic is denominated in denoising steps.
#
# **The penalty depends on how many peers there are, not only on how much
# of the die is busy.** A slice of width w at N ways has N-1 peers filling
# maskable-w units; the pairwise entry (w, maskable-w) has ONE peer
# filling exactly those units. Against it:
#
#     ways  slice   N-way  pairwise  ratio
#        2     52  1.2146    1.2176  0.998   <- same arrangement, a check
#        4     26  1.4625    1.2770  1.145
#        8     13  2.0933    1.3559  1.544
#
# So MEASURED_EXTERNALITY cannot express this and no policy here reads
# this table: it is carried as data for the burst arithmetic, which had
# been using the pairwise number as a stand-in and was flattered by it.
#
# The sweep ran 1, 2, 4, 8 in order on a warming die, which inflates the
# later solo baselines and therefore DEFLATES their externality. The
# growth is if anything understated. The eight-way cell is the weakest --
# 5 to 7 samples inside the overlap window, sd 0.12, and a third trial
# whose solo sat 13.6% above the first's -- and a longer-window rerun is
# kept beside it rather than replacing it.
MEASURED_NWAY_PENALTY_GFX90A: dict[int, float] = {
    1: 1.0001,     # control: a solo, and it must read 1.000
    2: 1.2146,
    4: 1.4625,
    8: 2.0933,
}

EXTERNALITY_TABLES: dict[str, dict[tuple[int, int], float]] = {
    "gfx1201": MEASURED_EXTERNALITY,
    "gfx90a": MEASURED_EXTERNALITY_GFX90A,
}


class UnmeasuredPairing(LookupError):
    """Raised when a co-run pairing has no measured externality.

    Deliberately not interpolated. Across the measured pairs the penalty
    ranges from 1.22x to 1.93x and is not monotone in either quota, so a
    value invented between two entries could be wrong by a large factor in
    an unknown direction.
    """


@dataclass(frozen=True)
class QuotaCostModel:
    """Step time as a function of quota, from a measured quota table."""

    serial_fraction: float
    step_seconds_at_full: float
    maskable_units: int = 32
    measured_curve: tuple[tuple[int, float], ...] = ()

    def __post_init__(self) -> None:
        if not 0.0 <= self.serial_fraction < 1.0:
            raise ValueError("serial_fraction must be in [0, 1)")
        if self.step_seconds_at_full <= 0:
            raise ValueError("step_seconds_at_full must be positive")

    def step_seconds(self, units: int) -> float:
        """Measured where measured, Amdahl only where it is not."""
        if not 1 <= units <= self.maskable_units:
            raise ValueError(
                f"quota {units} outside 1..{self.maskable_units}"
            )
        curve = dict(self.measured_curve)
        if units in curve:
            # Scale the measured p50 to a per-step time using the full-die
            # cell as the reference, so the ratios are exactly the measured
            # ones rather than the fit's approximation of them.
            return self.step_seconds_at_full * (
                curve[units] / curve[self.maskable_units]
            )
        share = units / self.maskable_units
        serial = self.serial_fraction
        return self.step_seconds_at_full * (serial + (1.0 - serial) / share)

    def is_measured(self, units: int) -> bool:
        """Whether this quota came from a measurement or from the fit."""
        return units in dict(self.measured_curve)

    @classmethod
    def for_model(cls, name: str,
                  device: str = "gfx1201") -> "QuotaCostModel":
        """The measured cost table for this model on this device.

        ``device`` defaults to ``gfx1201``, which every table in this
        module was measured on and every campaign before 2026-09-03 ran
        on, so every existing caller is unchanged.

        A device with no measured table raises rather than falling back.
        The fallback would be worse than an error: gfx90a has 104 units
        against gfx1201's 32, so a gfx1201 curve read there would put
        ``step_seconds(52)`` outside the die it was measured on and answer
        with an Amdahl extrapolation of the wrong hardware. 3.7 and 3.8
        both turned on a cost the scheduler believed and did not have.
        """
        tables = DEVICE_TABLES.get(device)
        if tables is None:
            raise KeyError(
                f"no measured quota tables for device {device!r}; "
                f"have {sorted(DEVICE_TABLES)}"
            )
        models, quotas = tables
        if name not in models:
            raise KeyError(
                f"no measured quota table for {name!r} on {device}; "
                f"have {sorted(models)}"
            )
        curve = quotas.get(name, {})
        return cls(**models[name],
                   measured_curve=tuple(sorted(curve.items())))


# Every cost in this module -- step times and co-run penalties alike --
# was measured at one workpoint per model: SDXL at 768x768, CogVideoX-2b
# at 9 frames. That is not a formality. Co-runs at identical step-time
# ratios measured 2026-08-06 put the per-step penalty at +22.4%/+24.5%
# for SDXL at 768 and +76.6%/+78.9% at 832, and the sign of the
# partitioning result flips with it: +16.6% at 768, -21.9% at 832.
#
# The simulator has no workpoint dimension, so it cannot represent that.
# Results computed here are statements about these workpoints, and a
# reader who carries them to another resolution is carrying a number the
# measurements contradict.
MEASURED_WORKPOINT: dict[str, str] = {
    "sdxl": "768x768",
    "cogvideox-2b": "9 frames",
}


def externality(own_units: int, peer_units: int | None,
                model: str | None = None,
                device: str = "gfx1201") -> float:
    """Slowdown factor for a tenant sharing the die with one peer.

    Uses the model's own measurement where one exists and the shared
    table otherwise. The penalty is model-dependent -- CogVideoX-2b is
    4.2% above SDXL at 16+16 -- so a fallback is an extrapolation across
    models, not a lookup.

    Keyed by quota, model and device, not by workpoint. The penalty
    depends strongly on that too -- see MEASURED_WORKPOINT -- and this
    function silently assumes the one the table was built at, because a
    trace does not carry a resolution to key on.

    ``device`` defaults to gfx1201, which every table here was measured
    on and every campaign before 2026-09-03 ran on, so every existing
    caller is unchanged. A device with no measured table raises rather
    than falling back: 52+52 does not exist on a 32-unit die, and the
    two architectures' quota curves have different *shapes* and not only
    different constants (1.10), so borrowing one for the other is an
    extrapolation of the wrong hardware.

    The per-model table is gfx1201's only. It is not consulted on another
    device, because a model-specific penalty measured on one architecture
    is not a correction to apply on a different one.
    """
    if peer_units is None:
        return 1.0
    key = (own_units, peer_units)
    if device == "gfx1201" and model is not None:
        own_table = MEASURED_EXTERNALITY_BY_MODEL.get(model)
        if own_table and key in own_table:
            return own_table[key]
    table = EXTERNALITY_TABLES.get(device)
    if table is None:
        raise KeyError(
            f"no measured externality table for device {device!r}; "
            f"have {sorted(EXTERNALITY_TABLES)}"
        )
    if key not in table:
        raise UnmeasuredPairing(
            f"no measured externality for {own_units}+{peer_units} on "
            f"{device}; measured pairs are {sorted(table)}"
        )
    return table[key]


@dataclass(frozen=True)
class Request:
    """One inference request. Immutable so a trace cannot drift."""

    request_id: int
    tenant: str
    model: str
    arrival_s: float
    steps: int
    deadline_s: float | None = None

    def __post_init__(self) -> None:
        if self.steps <= 0:
            raise ValueError("a request needs at least one step")
        if self.arrival_s < 0:
            raise ValueError("arrival must not be negative")


@dataclass
class RequestState:
    request: Request
    steps_done: int = 0
    started_s: float | None = None
    finished_s: float | None = None
    service_seconds: float = 0.0
    quota_seconds: float = 0.0     # units x seconds, the accounting currency
    # What the scheduler was told, refreshed before each decision. Never
    # used to charge or to advance time -- only to choose.
    predicted_step_seconds: dict[int, float] = field(default_factory=dict)
    # This tenant's running charge, refreshed before each decision. A policy
    # that has to keep tenants even needs to see how far apart they already
    # are, and deriving it from steps_done would be wrong: a step is worth a
    # different amount of die-time in every model and at every quota.
    tenant_quota_seconds: float = 0.0
    # The last step's measured cost. None before the first step. A policy
    # comparing this against predicted_step_seconds sees whether the
    # pairing it formed landed in the fast or slow state -- the two are
    # 46% apart, so the comparison needs no threshold tuning.
    observed_step_seconds: float | None = None
    # The quota that observation was taken at. Without it a step measured
    # alone, or a first step carrying kernel compilation, reads as
    # evidence about a pairing -- and since that verdict stops the request
    # being paired, no newer observation ever arrives.
    observed_at_units: int | None = None

    @property
    def complete(self) -> bool:
        return self.steps_done >= self.request.steps


@dataclass(order=True)
class _Event:
    """Ordered by (time, sequence) so ties never depend on insertion luck."""

    time_s: float
    sequence: int
    kind: str = field(compare=False)
    payload: object = field(compare=False, default=None)


class Trace:
    """A reproducible request stream.

    Generated from a seed rather than from wall-clock timing, and sorted
    into a total order, so the same seed yields the same trace on any host.
    """

    def __init__(self, requests: Sequence[Request]):
        self.requests = tuple(
            sorted(requests, key=lambda r: (r.arrival_s, r.request_id))
        )

    def __len__(self) -> int:
        return len(self.requests)

    def __iter__(self):
        return iter(self.requests)

    @classmethod
    def poisson(
        cls,
        *,
        seed: int,
        tenants: Sequence[tuple[str, str]],
        rate_per_s: float,
        horizon_s: float,
        steps: int,
        deadline_slack: float | None = None,
    ) -> "Trace":
        """A Poisson arrival stream, round-robin across tenants.

        Uses its own Random instance seeded explicitly: the global random
        module is process state, and a simulation that reads it is not
        reproducible in a process that used random for anything else.
        """
        if rate_per_s <= 0:
            raise ValueError("rate must be positive")
        rng = random.Random(seed)
        requests: list[Request] = []
        now = 0.0
        index = 0
        while True:
            now += rng.expovariate(rate_per_s)
            if now > horizon_s:
                break
            tenant, model = tenants[index % len(tenants)]
            deadline = None
            if deadline_slack is not None:
                nominal = QuotaCostModel.for_model(model).step_seconds(
                    MEASURED_MODELS[model]["maskable_units"]
                ) * steps
                deadline = now + nominal * deadline_slack
            requests.append(Request(
                request_id=index, tenant=tenant, model=model,
                arrival_s=now, steps=steps, deadline_s=deadline,
            ))
            index += 1
        return cls(requests)


# A policy sees the runnable requests and the die, and returns a quota per
# request. Returning fewer entries than requests leaves the rest unserved.
Policy = Callable[[Sequence[RequestState], int, float], dict[int, int]]


class Predictor:
    """What the scheduler believes a step costs, which is not what it costs.

    Gate C requires safe degradation at +/-5%, 10% and 20% predictor error.
    Modelling that needs the belief and the truth kept apart: a simulator
    where the policy reads the true cost cannot degrade at all, and would
    report perfect robustness for a scheduler that has none.

    Error is drawn once per (request, quota) and cached, so a policy that
    asks twice gets one answer. A predictor that returned a fresh sample
    each call would let a policy average the noise away by asking
    repeatedly, which no real predictor permits.
    """

    def __init__(self, *, relative_error: float = 0.0, seed: int = 0):
        if relative_error < 0:
            raise ValueError("relative_error is a magnitude; use >= 0")
        self.relative_error = relative_error
        self._rng = random.Random(seed)
        self._cache: dict[tuple[int, int], float] = {}

    def step_seconds(self, request_id: int, model: str, units: int) -> float:
        key = (request_id, units)
        if key not in self._cache:
            true_cost = QuotaCostModel.for_model(model).step_seconds(units)
            if self.relative_error:
                factor = 1.0 + self._rng.uniform(
                    -self.relative_error, self.relative_error
                )
            else:
                factor = 1.0
            self._cache[key] = true_cost * factor
        return self._cache[key]

    def is_exact(self) -> bool:
        return self.relative_error == 0.0


class PairingStates:
    """Which state each formed pairing landed in.

    Measured behaviour, not a modelling choice -- but measured of the
    two-process harness, and off by default because the runtime this
    project designs does not use it. Two streams in one process take the
    fast state in 17 of 17 trials.

    Enabled, the draw reproduces the two-process behaviour: a pairing
    lands fast or slow, the two are 46% apart with no overlap, and a
    simulator using the fast figure there would report a gain the
    hardware supplies 30% of the time. That configuration is what
    test_pairing_probe.py exercises.

    A pairing is identified by the set of request ids sharing the die, so
    re-forming a pairing -- dropping it and building it again -- draws
    again. **This is an assumption, and it is the one this model is most
    exposed on.** What the hardware has shown is that relaunching the two
    processes draws afresh. The runtime being designed is one process
    with a stream per model, where re-forming means changing a stream's
    CU mask rather than relaunching; whether that redraws, or whether the
    bistability exists there at all, is unmeasured. See
    docs/gate-c-decision-log.md.
    """

    def __init__(self, *, seed: int = 0, rate: float = PAIRING_STATE_RATE,
                 enabled: bool = False, latched: bool = False,
                 fast: float | None = None, slow: float | None = None,
                 settle_after: int = 0):
        self._rng = random.Random(seed)
        self.rate = rate
        self.enabled = enabled
        # ``latched`` models what the die was measured to do rather than
        # what the two-process harness suggested. Under it the draw is
        # keyed by the pair of granted widths -- the mask pair -- and
        # ``forget`` does nothing, because eight processes re-formed their
        # pairings nine times each with fresh adapters and fresh streams
        # and not one redrew. A policy that drops a slow pairing hoping the
        # next round is a new coin is, under this mode, simply paying for a
        # round it already knows the cost of.
        self.latched = latched
        self.fast = (MEASURED_STEP_PAIRING_FAST if fast is None
                     else fast) if latched else (
            FAST_PAIRING_EXTERNALITY if fast is None else fast)
        self.slow = (MEASURED_STEP_PAIRING_SLOW if slow is None
                     else slow) if latched else (
            SLOW_PAIRING_EXTERNALITY if slow is None else slow)
        # Rounds of real co-run a pairing spends slow before it settles.
        # Measured on the mismatched pair: SDXL against CogVideoX-2b ran
        # at 6.29x and 6.15x for its first two episodes and then 1.01x for
        # every episode after, identically in four processes. A model
        # without this cannot represent the case where giving up early
        # forfeits the gain, which is the case that decides whether a
        # sticky verdict may be permanent.
        self.settle_after = settle_after
        self._state: dict[frozenset[int], bool] = {}
        self._latched: dict[tuple[int, ...], bool] = {}
        self._rounds_seen: dict[tuple[int, ...], set] = {}

    def factor_for(self, members: frozenset[int],
                   quotas: Sequence[int] | None = None,
                   at: float | None = None) -> float:
        """Externality multiplier for the pairing.

        Without ``latched`` the draw is per member set and a re-formed
        pairing draws again. With it, the draw is per mask pair and
        survives re-forming, which is what the hardware does; ``quotas``
        supplies that key and falls back to the member set when a caller
        has not been updated to pass it.
        """
        if not self.enabled or len(members) < 2:
            return self.fast
        if self.latched:
            key = tuple(sorted(quotas)) if quotas else tuple(sorted(members))
            if self.settle_after:
                # Counted in distinct rounds rather than calls, because a
                # round asks once per member and a pairing that settles
                # after two rounds is not one that settles after four
                # calls.
                seen = self._rounds_seen.setdefault(key, set())
                seen.add(at)
                if len(seen) <= self.settle_after:
                    return self.slow
                return self.fast
            if key not in self._latched:
                self._latched[key] = self._rng.random() < self.rate
            return self.fast if self._latched[key] else self.slow
        if members not in self._state:
            self._state[members] = self._rng.random() < self.rate
        return self.fast if self._state[members] else self.slow

    def is_fast(self, members: frozenset[int]) -> bool | None:
        return self._state.get(members)

    def is_fast_for_quotas(self, quotas: Sequence[int]) -> bool | None:
        return self._latched.get(tuple(sorted(quotas)))

    def forget(self, members: frozenset[int]) -> None:
        """Re-form the pairing: the next draw is independent.

        A no-op under ``latched``. Re-forming was measured not to redraw,
        and a simulator that let it would report a recovery the hardware
        does not offer.
        """
        if self.latched:
            return
        self._state.pop(members, None)


@dataclass
class SimulationResult:
    """Everything a Gate C criterion is judged on, and nothing derived."""

    completed: list[RequestState]
    unfinished: list[RequestState]
    horizon_s: float
    quota_seconds_by_tenant: dict[str, float]
    service_seconds_by_tenant: dict[str, float]
    steps_executed: int
    unmeasured_pairings: list[tuple[int, int]]
    predictor_relative_error: float = 0.0
    granted_unit_seconds: float = 0.0
    quantum_s: float = 0.25
    peak_lag_unit_seconds: float = 0.0
    deadline_override_rounds: int = 0
    exclusive_rounds: int = 0

    def canonical_bytes(self) -> bytes:
        """Byte-exact serialisation of everything the gate is judged on.

        Gate C asks for identical results from an identical seed, and
        "identical" has to mean bytes rather than a rounded summary --
        a scheduler whose decisions drift in the last place would pass a
        comparison at four decimals and be non-deterministic anyway.

        Floats go out as ``float.hex()``, which is exact and round-trips;
        ``repr`` round-trips too but its shortest-representation rules are
        a promise about Python's formatter, not about the value. Dicts are
        emitted in sorted key order because insertion order here follows
        arrival order, which is a property of the trace and not of the
        result.
        """
        def num(value: float) -> str:
            return float(value).hex()

        lines = [
            b"burstserve.trace_sim.SimulationResult\x00v1",
            f"horizon_s={num(self.horizon_s)}".encode(),
            f"quantum_s={num(self.quantum_s)}".encode(),
            f"steps_executed={self.steps_executed}".encode(),
            f"predictor_relative_error={num(self.predictor_relative_error)}"
            .encode(),
            f"granted_unit_seconds={num(self.granted_unit_seconds)}".encode(),
            f"peak_lag_unit_seconds={num(self.peak_lag_unit_seconds)}"
            .encode(),
            f"deadline_override_rounds={self.deadline_override_rounds}"
            .encode(),
            f"exclusive_rounds={self.exclusive_rounds}".encode(),
        ]
        for tenant in sorted(self.quota_seconds_by_tenant):
            lines.append(
                f"quota:{tenant}="
                f"{num(self.quota_seconds_by_tenant[tenant])}".encode()
            )
        for tenant in sorted(self.service_seconds_by_tenant):
            lines.append(
                f"service:{tenant}="
                f"{num(self.service_seconds_by_tenant[tenant])}".encode()
            )
        for label, states in (("done", self.completed),
                              ("open", self.unfinished)):
            for state in sorted(states, key=lambda s: s.request.request_id):
                finished = (
                    num(state.finished_s)
                    if state.finished_s is not None else "-"
                )
                started = (
                    num(state.started_s)
                    if state.started_s is not None else "-"
                )
                lines.append(
                    f"{label}:{state.request.request_id}"
                    f":{state.request.tenant}"
                    f":{state.request.model}"
                    f":steps={state.steps_done}/{state.request.steps}"
                    f":start={started}:end={finished}"
                    f":quota={num(state.quota_seconds)}"
                    f":service={num(state.service_seconds)}".encode()
                )
        for left, right in sorted(self.unmeasured_pairings):
            lines.append(f"unmeasured:{left}+{right}".encode())
        return b"\n".join(lines)

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def jain_index(self) -> float:
        """Fairness over the accounting currency, not over wall time.

        Wall time charges a tenant for being slowed by a peer; quota-seconds
        charge it for capacity it was given. The 2026-08-06 externality
        table makes the difference concrete -- at an 8+24 split the larger
        tenant is slowed 128% by a peer it did not choose.
        """
        values = [v for v in self.quota_seconds_by_tenant.values()]
        if not values:
            return 1.0
        total = sum(values)
        if total <= 0:
            return 1.0
        return total ** 2 / (len(values) * sum(v ** 2 for v in values))

    def full_die_equivalent_seconds(self) -> float:
        """Work completed, expressed as time it would take on the full die.

        This is the utilisation numerator. Counting wall time served, or
        quota-seconds, would both credit a policy for holding the die
        rather than for finishing work -- and a partitioned policy holds
        the die exactly as much as an exclusive one does.
        """
        total = 0.0
        for state in self.completed + self.unfinished:
            model = MEASURED_MODELS[state.request.model]
            full_step = model["step_seconds_at_full"]
            total += state.steps_done * full_step
        return total

    def utilisation(self) -> float:
        """Full-die-equivalent work per second of horizon.

        Can exceed 1.0, and that is the finding rather than a bug: two
        tenants overlap their serial phases, so the die completes more
        full-die-equivalent work per second than one tenant at full width.
        """
        if self.horizon_s <= 0:
            return 0.0
        return self.full_die_equivalent_seconds() / self.horizon_s

    def accounting_error(self) -> float:
        """Relative gap between what was charged and what was handed out.

        Gate C bounds this at 1%. It is a self-consistency check, not a
        performance metric: if the sum of every tenant's quota-seconds does
        not match the units the scheduler actually granted, some tenant is
        being charged for capacity it did not get, or is holding capacity
        nobody is charged for -- and the fairness index cannot see either.
        """
        charged = sum(self.quota_seconds_by_tenant.values())
        if self.granted_unit_seconds <= 0:
            return 0.0 if charged == 0 else float("inf")
        return abs(charged - self.granted_unit_seconds) / self.granted_unit_seconds

    def service_lag_quanta(self, backlogged: Sequence[str] | None = None) -> float:
        """Worst deviation from an equal share, in quanta, among backlogged
        tenants.

        Gate C bounds this at two quanta absent deadline overrides, and the
        definition matters. The gap between the largest and smallest total
        is not lag: two tenants submitting different amounts of work should
        receive different amounts of service, and charging that as unfairness
        would make every heterogeneous trace look broken. Lag is the
        distance from the share a tenant was owed.

        A tenant is owed an equal share only while it is backlogged. One
        that had nothing to run cannot be behind, so the caller names which
        tenants were continuously demanding; with no such list the measure
        covers every tenant and is an upper bound.
        """
        names = (
            [t for t in self.quota_seconds_by_tenant if t in set(backlogged)]
            if backlogged is not None
            else list(self.quota_seconds_by_tenant)
        )
        if len(names) < 2 or self.quantum_s <= 0:
            return 0.0
        values = [self.quota_seconds_by_tenant[n] for n in names]
        fair_share = sum(values) / len(values)
        full_die_quantum = 32 * self.quantum_s
        return max(abs(v - fair_share) for v in values) / full_die_quantum

    def peak_service_lag_quanta(self) -> float:
        """The bound Gate C actually states: worst lag at any instant.

        Sampled every round over the tenants that were backlogged at that
        moment. The end-of-run figure cannot serve: a policy that runs one
        tenant to completion and then the other finishes exactly even,
        having been maximally unfair the whole way.
        """
        if self.quantum_s <= 0:
            return 0.0
        return self.peak_lag_unit_seconds / (32 * self.quantum_s)

    def deadline_misses(self) -> list[RequestState]:
        missed = []
        for state in self.completed:
            deadline = state.request.deadline_s
            if deadline is not None and state.finished_s is not None:
                if state.finished_s > deadline:
                    missed.append(state)
        # An unfinished request with a deadline in the past has missed it.
        for state in self.unfinished:
            if state.request.deadline_s is not None:
                if self.horizon_s > state.request.deadline_s:
                    missed.append(state)
        return missed


def simulate(
    trace: Trace,
    policy: Policy,
    *,
    horizon_s: float,
    maskable_units: int = 32,
    quantum_s: float = 0.25,
    seed: int = 0,
    predictor: "Predictor | None" = None,
    pairing_states: "PairingStates | None" = None,
) -> SimulationResult:
    """Run a trace under a policy.

    Time advances in fixed quanta rather than to the next step boundary.
    A step-boundary loop would let a tenant with short steps be rescheduled
    more often than one with long steps, which silently favours it; a fixed
    quantum gives every tenant the same decision points. The quantum is
    recorded in the result because Gate C bounds service lag in units of it.
    """
    if horizon_s <= 0:
        raise ValueError("horizon must be positive")
    if quantum_s <= 0:
        raise ValueError("quantum must be positive")

    # The scheduler's beliefs. Execution always uses the true cost below,
    # so an inaccurate predictor degrades the decisions without corrupting
    # the measurement of what those decisions cost.
    if predictor is None:
        predictor = Predictor(relative_error=0.0, seed=seed)
    if pairing_states is None:
        pairing_states = PairingStates(seed=seed)

    costs = {name: QuotaCostModel.for_model(name) for name in MEASURED_MODELS}
    pending = list(trace.requests)
    states: dict[int, RequestState] = {}
    runnable: list[RequestState] = []
    completed: list[RequestState] = []
    # Seeded with every tenant in the trace, at zero. A tenant that is
    # never served would otherwise be absent from the dict, and a fairness
    # index computed over the survivors cannot see starvation at all -- it
    # reports 1.0 for a policy that fed one tenant and ignored the other.
    tenants_in_trace = {r.tenant for r in trace.requests}
    quota_seconds: dict[str, float] = {t: 0.0 for t in tenants_in_trace}
    service_seconds: dict[str, float] = {t: 0.0 for t in tenants_in_trace}
    unmeasured: list[tuple[int, int]] = []
    steps_executed = 0
    # Units actually handed out, times the seconds they were held. The
    # accounting must reconcile against this: a tenant charged less than it
    # held is being subsidised by the others, and the fairness index would
    # not see it.
    granted_unit_seconds = 0.0
    # Peak lag, sampled every round. The final lag is not the bound: a
    # policy that serves one tenant to completion and then the other ends
    # perfectly even while having been maximally unfair throughout.
    peak_lag_unit_seconds = 0.0
    deadline_override_rounds = 0
    exclusive_rounds = 0

    now = 0.0
    while now < horizon_s:
        # Admit everything that has arrived, in trace order.
        while pending and pending[0].arrival_s <= now:
            request = pending.pop(0)
            state = RequestState(request=request)
            states[request.request_id] = state
            runnable.append(state)

        for state in runnable:
            state.tenant_quota_seconds = quota_seconds.get(
                state.request.tenant, 0.0
            )
        if not runnable:
            if not pending:
                break
            now = max(now + quantum_s, pending[0].arrival_s)
            continue

        # Sorted before the policy sees it: a policy must not be able to
        # depend on the order requests happened to be appended in.
        ordered = sorted(
            runnable, key=lambda s: (s.request.arrival_s, s.request.request_id)
        )
        for state in ordered:
            state.predicted_step_seconds = {
                units: predictor.step_seconds(
                    state.request.request_id, state.request.model, units
                )
                for units in (4, 8, 12, 16, 20, 24, 28, 32)
            }
        assignment = policy(ordered, maskable_units, now)
        granted = {
            rid: units for rid, units in sorted(assignment.items())
            if units > 0
        }
        if sum(granted.values()) > maskable_units:
            raise ValueError(
                f"policy assigned {sum(granted.values())} of "
                f"{maskable_units} units"
            )

        if not granted:
            now += quantum_s
            continue

        round_seconds = 0.0
        round_spent: dict[int, float] = {}

        # Externality needs each tenant's peer. Only pairs are measured, so
        # a triple is reported rather than approximated.
        active = sorted(granted.items())
        members = frozenset(rid for rid, _ in active)
        # Pairings that are no longer formed are forgotten, so re-forming
        # one draws a fresh state. That is the whole basis of probing: the
        # hardware draws again when the processes are relaunched.
        for stale in [m for m in list(pairing_states._state) if m != members]:
            pairing_states.forget(stale)
        for rid, units in active:
            peer = None
            if len(active) == 2:
                peer = next(u for r, u in active if r != rid)
            elif len(active) > 2:
                unmeasured.append((units, -len(active)))
                peer = None
            state = states[rid]
            try:
                factor = externality(units, peer, state.request.model)
                if peer is not None:
                    # The table entry is the fast state. Which state this
                    # pairing is actually in is drawn per pairing -- or,
                    # under ``latched``, per mask pair, which is why the
                    # granted widths are passed rather than only the
                    # members.
                    factor = pairing_states.factor_for(
                        members, quotas=[u for _, u in active], at=now)
            except UnmeasuredPairing:
                unmeasured.append((units, peer if peer is not None else -1))
                factor = 1.0
            step_cost = costs[state.request.model].step_seconds(units) * factor
            # Whole steps only: a partially executed denoising step is not
            # a state the runtime can checkpoint. At least one step runs
            # even when it outlasts the quantum -- truncating instead
            # deadlocks any configuration whose step exceeds the quantum,
            # which a 16+16 split does at 0.299 s against 0.25.
            #
            # This is not the runtime's rule, and the difference went
            # unstated until 2026-08-25. The runtime advances every
            # active request by exactly one step per round; this advances
            # by however many whole steps fit the quantum. At 0.25 s the
            # two agree everywhere except SDXL at 24 and 32 units, where
            # this gives two steps and the runtime gives one -- so the
            # simulator has been slightly more generous to a fast tenant
            # holding a wide quota. Gate C froze a policy against this
            # loop and the runtime executes it under the other rule.
            # Recorded rather than reconciled: changing the simulator
            # would move a frozen comparison, and the runtime's rule is
            # the one now under measurement.
            affordable = max(1, int(quantum_s // step_cost))
            remaining = state.request.steps - state.steps_done
            taken = min(affordable, remaining)
            if taken == 0:
                continue
            if state.started_s is None:
                state.started_s = now
            state.steps_done += taken
            # What the scheduler can observe after the fact, which is how a
            # probe works: it does not read the state, it reads the step it
            # just paid for.
            state.observed_step_seconds = step_cost
            state.observed_at_units = units
            spent = taken * step_cost
            state.service_seconds += spent
            state.quota_seconds += units * spent
            steps_executed += taken
            quota_seconds[state.request.tenant] = (
                quota_seconds.get(state.request.tenant, 0.0) + units * spent
            )
            service_seconds[state.request.tenant] = (
                service_seconds.get(state.request.tenant, 0.0) + spent
            )
            if state.complete:
                state.finished_s = now + spent
            round_seconds = max(round_seconds, spent)
            round_spent[rid] = spent

        # Advance by the work actually done, not by a fixed quantum. A
        # fixed advance charges the horizon for time nobody used: at 0.152 s
        # per step against a 0.25 s quantum the die would idle 39% of every
        # round, and utilisation would read 0.61 for a policy that never
        # left it idle.
        advance = round_seconds if round_seconds > 0 else quantum_s
        # Everyone holding units holds them for the whole round, including
        # a tenant whose own step finished early. Charging only the time a
        # tenant was computing would let a fast tenant occupy the die for
        # free while a slower peer sets the round length.
        # Classify the round independently of what the policy says it did.
        # Gate C exempts deadline overrides from the lag bound, so a policy
        # that self-reported its overrides could exempt itself from the
        # bound by relabelling. The conditions are recomputed here from the
        # same predictions the policy saw.
        if len(runnable) > 1 and len(granted) == 1:
            exclusive_rounds += 1
            (chosen_id,) = granted
            chosen = next(
                s for s in runnable if s.request.request_id == chosen_id
            )
            half = units // 2
            deadline = chosen.request.deadline_s
            if deadline is not None:
                left = deadline - now
                shared_cost = chosen.predicted_step_seconds.get(half)
                whole_cost = chosen.predicted_step_seconds.get(units)
                todo = chosen.request.steps - chosen.steps_done
                if (shared_cost is not None and whole_cost is not None
                        and todo * shared_cost > left
                        and todo * whole_cost <= left):
                    deadline_override_rounds += 1

        granted_unit_seconds += sum(granted.values()) * advance
        for rid, units in granted.items():
            tenant = states[rid].request.tenant
            held_over = advance - round_spent.get(rid, 0.0)
            if held_over > 0:
                quota_seconds[tenant] = (
                    quota_seconds.get(tenant, 0.0) + units * held_over
                )
        now += advance
        backlogged_now = {s.request.tenant for s in runnable}
        if len(backlogged_now) > 1:
            charged = [quota_seconds.get(t, 0.0) for t in sorted(backlogged_now)]
            fair = sum(charged) / len(charged)
            peak_lag_unit_seconds = max(
                peak_lag_unit_seconds, max(abs(c - fair) for c in charged)
            )
        newly_done = [s for s in runnable if s.complete]
        for state in newly_done:
            runnable.remove(state)
            completed.append(state)

    return SimulationResult(
        completed=sorted(completed, key=lambda s: s.request.request_id),
        unfinished=sorted(runnable, key=lambda s: s.request.request_id),
        horizon_s=min(now, horizon_s),
        quota_seconds_by_tenant=dict(sorted(quota_seconds.items())),
        service_seconds_by_tenant=dict(sorted(service_seconds.items())),
        steps_executed=steps_executed,
        unmeasured_pairings=sorted(unmeasured),
        predictor_relative_error=predictor.relative_error,
        granted_unit_seconds=granted_unit_seconds,
        quantum_s=quantum_s,
        peak_lag_unit_seconds=peak_lag_unit_seconds,
        deadline_override_rounds=deadline_override_rounds,
        exclusive_rounds=exclusive_rounds,
    )
