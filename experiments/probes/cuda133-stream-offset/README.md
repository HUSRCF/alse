# CUDA 13.3 stream-mask offset: exploratory probe record

These artifacts are **not formal Gate-A evidence**. They were produced outside
`experiments/runs` by scratchpad drivers, carry no provenance binding, and
authorize nothing. They are kept because they are the raw record behind a
reverse-engineered constant that the project now depends on, including the
runs that failed and the one that faulted the GPU.

Hardware: RTX 4090 (AD102, 128 SM / 64 TPC), physical GPU 0 -- outside the
Gate-A0 fleet {1,2,3,4,7}. Driver 610.43.02, driver API 13030, CUDA 13.3.

## Result

`libsmctrl`'s per-stream SM mask lives at **offset 0x5fc** in the CUDA 13.3
`CUstream` struct, i.e. `MASK_OFF=+280` relative to its `CU_12_2_MASK_OFF`
base of `0x4e4`. Upstream's validated table stops at CUDA 12.8 (`0x4fc`).

This is a reverse-engineered, version-locked constant discovered by writing
into an opaque driver structure. It is not a documented interface and must
never be described as a stable mechanism. A driver update invalidates it.

## Files

| file | what it records |
| --- | --- |
| `phase0_raw.json`, `phase0_verdict.json` | global/next work on 13.3; TPC bit N -> SM {2N, 2N+1} |
| `next_limits.cu` | the three regimes where the thread-local `next` mask fails: CUDA graphs mask 1 of 4 nodes, an interposed launch steals the mask, an unconsumed mask leaks into a later unrelated launch |
| `concurrency_test.cu` | two host threads partition disjointly -- and the unmasked control shows disjointness alone is not evidence |
| `phase1_sweep.json` | the offset sweep: 188 attempts, 1 hit, 141 no-ops, 43 SIGSEGV, 2 timeouts, 1 Xid |
| `phase2_raw.json`, `phase2_verdict.json` | falsification: 3 modes x 4 bits x 3 trials, stream agrees with both callback modes |
| `stream_sweep64.json` | all 64 bits under stream at 0x5fc, union exactly {0..127} |

## What went wrong, on purpose

`MASK_OFF=+492` (`0x6d0`) raised **Xid 31** -- `MMU Fault: ENGINE GRAPHICS
HUBCLIENT_FE faulted @ 0x7f1c_fffff000, FAULT_PDE ACCESS_TYPE_VIRT_WRITE`.
The sweep aborted at that point by rule, so **deltas +496..+512 were never
scanned**; the sweep is truncated, not exhaustive. The fault was contained to
the sealed `memfd:burstserv` child process and GPU 0 returned a full 128/128
SM baseline immediately afterwards.

43 of 188 attempts killed their child with SIGSEGV. Every attempt ran in a
short-lived child for exactly this reason, and an NVML Xid monitor was open
across each child's lifetime rather than only afterwards, because a monitor
registered after a fault would not see it.
