# Pre-registration: does 1.5 survive on the device it was measured on?

Written 2026-09-06, before any cell of this sweep was run, after the
sweep's comparator set was found not to exist.

## Why this runs

1.5 says SDXL beside CogVideoX-2b costs **1.00-1.06 per side at every
split** on gfx1201, and that both tenants beat their rotation share. It
is one of the project's oldest claims and 1.9's framing leans on it.

Two things happened to it.

* **1.12**, measured on gfx90a with the corrected instrument, reads
  **4.93** for the SDXL side at the even split against a same-model
  control of **1.247**. That is 3.95x, not 1.00-1.06.
* 1.5's own harness, `run_amd_mismatched_corun.py`, reads
  `last_step_seconds` every iteration. That attribute goes stale when the
  CPU runs ahead, so the loop records one reading N times -- 3.10. 1.5's
  published narrative contains the signature: "every SDXL step in the
  first co-run episode takes 988 ms ... with no spread at all".

So 1.5 is in doubt on the device it was measured on, and nobody has
re-measured it there.

## The comparator does not exist yet, which is why this sweep has two halves

gfx1201's five same-model pairs *look* measured -- `nway_steps_sdxl_32u_*`
gives 1.052 / 1.103 / 1.183 / 1.084 / 1.047. They are from the pre-fix
directory and **their allocator control fails**: at eight ways the
post-episode solo reads 2048 ms against a solo_before of 504 ms, and at
`16+16` it reads 195.5 ms against 151.3, which is the co-run value. A
co-run penalty must vanish when the peers stop. Those did not.

The corrected runs on gfx1201 -- `nway_walled/*`, 2026-09-05, the ones
behind 1.11 -- do return: 504 -> 521 at eight ways. But they cover only
the **equal** N-way splits. **No asymmetric pair on gfx1201 has a valid
step-level measurement.**

So the same-model pairs are re-measured here, in the same sweep, on the
same instrument, in the same session as the mismatched ones. The
comparator is therefore fixed by construction rather than chosen: it is
whatever this campaign's same-model run at the identical widths returns.

## Design

`scripts/run_amd_nway_steps.py`, the sync-free instrument, `--diagnose`
on every run so the three controls are in the same file. 6 episodes,
14 steps, 4 warmup dropped, seed 0, SDXL 768x768, CogVideoX-2b 9 frames
at 480x720 -- unchanged from the gfx90a sweep so the two devices stay
commensurable.

    A  same-model SDXL      widths 4,28  8,24  16,16  24,8  28,4
    B  mismatched pair      widths 4,28  8,24  16,16  24,8  28,4
                            models sdxl,cogvideox-2b -- so A's five
                            splits sweep SDXL from 4 units to 28
    C  mismatched 16,16 with the offsets reversed -- 1.12's falsifier,
       which is whether the penalty follows the model or the die position
    D  CogVideoX-2b self-paired at 16,16 -- the video side's own control,
       which 1.12 did not have
    E  CogVideoX-2b alone at 32 units -- the rotation baseline
       (SDXL's is `nway_walled/w1.json`, 108.5 ms, same instrument)

Thirteen runs.

## Verdicts, fixed in advance

Let `same(w)` be this campaign's same-model penalty at width `w`, and
`mm(w)` the mismatched one for the same slice.

1. **1.5 stands, and 1.12 is a CDNA2 phenomenon.** Every split gives both
   sides <= 1.06 **and** each side beats its rotation share. Then 1.5's
   scope narrows to "on gfx1201", 1.12's stays "on gfx90a", and the two
   claims are a real architecture difference rather than a contradiction.
2. **1.5 is withdrawn.** Any split gives `mm(w) >= 2 x same(w)` for the
   SDXL side. Two is the bar because the mismatch, not the width, then
   has to be what costs; gfx90a's ratio was 3.95x. Then the mismatched
   penalty is claimed per split from these numbers, and **1.5b goes with
   it** -- 1.5b is the same defective harness on gfx90a and 1.12 already
   contradicts it at the same widths.
3. **Between.** Penalties above 1.06 but below `2 x same(w)` everywhere.
   Then 1.5's numeric band is withdrawn, the Pareto-against-rotation
   claim is re-derived from these numbers, and whichever way that comes
   out is what gets claimed. The band and the Pareto claim are separable
   and are judged separately.

A side "beats its rotation share" iff `solo(w) x mm(w) < 2 x solo(32)`
for its model, both terms from this instrument.

## Reported whatever the verdict

* Per-slice externality per episode, both sides, every split.
* The three controls per run: solo after the co-run, after
  `empty_cache`, after the peer streams are destroyed. **A run whose
  controls do not come back within 10% of `solo_before` is reported as an
  instrument failure and not as a penalty** -- that is the rule that
  disqualified the comparator set above, and it applies to this campaign's
  own output identically.
* Each solo against `MEASURED_QUOTA_SECONDS` for its width and model.
* The pair's aggregate step rate, against rotation and against the whole
  die -- the cross-arrangement comparison 1.13 established, because
  externality ratios at different widths have different baselines.
* `MEASURED_EXTERNALITY`'s call-level entry beside each step-level one.
  gfx90a's step-level pair table was withdrawn with its N-way table, so
  **whether the call-level table over-charges narrow slices is currently
  an open question on both devices**, and half of this sweep answers it
  on gfx1201. It is reported, not acted on: changing the default table is
  a separate decision needing both devices.

## What would make this campaign invalid

* Any control not returning, per the rule above.
* A solo more than 10% off the measured quota curve for its width, which
  is how a mask that failed to apply shows up.
* The card not exclusive at launch, or a foreign process appearing during
  the sweep.
* Fewer than 6 episodes in any run.
