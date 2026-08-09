# 2026-08-08 raw runs, kept whole

Everything the day's campaigns wrote on the card, including the runs that
turned out to be measuring the harness. Nothing here is a summary; the
analysed forms live in `../decorr/`, `../splits/`, `../queue-pressure/`,
`../latch/` and `../mismatched/`, and the argument they support is in
`docs/gate-c-decision-log.md`.

Two of these are kept specifically because they were wrong, and both are
cited in the log:

* `mismatched_t1_20260808.json` -- the first mismatched trial. Its solo
  numbers are inverted (16 units reading faster than 32) because the
  adapter was reused across widths and reported the previous width's step
  time. That inversion is what found the defect; deleting it would delete
  the evidence for the fix.
* `sp2.log` -- the split campaign whose first attempt died at 12+20 with
  `UnmeasuredPairing`. The refusal was correct behaviour: the cost model
  declines to invent an externality for a pair it has never measured.

`thermal_*.csv` are 1 Hz samples of junction temperature, clocks and
package power, epoch-stamped so a run can be aligned against the die
rather than guessed at. The decorrelation campaign needed them to show
that a 30 C rise moves solo by 5% and the externality ratio not at all.
