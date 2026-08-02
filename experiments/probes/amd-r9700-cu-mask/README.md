# AMD R9700 CU masking: exploratory probe record

Not formal Gate-A evidence. Produced on `husrcf@X570` outside any provenance
binding. Kept because it is the raw record behind the cross-platform claim,
including the two measurements that corrected a wrong reading.

Hardware: AMD Radeon AI PRO R9700, `gfx1201` (RDNA4), 32 GB, ROCm 7.2.0.

## Result

Two independent masking mechanisms, both first-class:

| mechanism | interface |
| --- | --- |
| `stream_cu_mask` | `hipExtStreamCreateWithCUMask`, documented and exported since HIP 4.2 |
| `global_cu_mask` | `ROC_GLOBAL_CU_MASK`, ROCm process-wide environment variable |

`hipExtStreamGetCUMask` reads the applied mask back. NVIDIA has no counterpart:
there the mask is written blind into an opaque struct and is unobservable
afterwards, so a no-op write and a working one differ only in kernel behaviour.

A 2 x 4 x 3 matrix over both mechanisms passes `validate_masked_tpc_matrix`
**unmodified** -- the same function the NVIDIA line uses.

## What the sweeps corrected

`hipDeviceProp_t::multiProcessorCount` reports 32 while `rocminfo` calls the
part 64 compute units, so the mask width is genuinely ambiguous from outside.
Under `ROC_GLOBAL_CU_MASK` the reported count becomes `popcount(mask)/2` --
zero for a single bit -- which suggested a 64-wide vector. The 64-bit sweep
refuted that: bits 0..31 each confine the kernel to exactly one unit, while
bits 32..63 leave all 32 reachable **and** make the readback disagree with the
request. The documented "extra elements are ignored" behaviour, caught by the
readback rather than inferred.

That same reporting quirk is a trap for the probe itself: it cannot ask a
masked device how wide its own mask is, so the width is declared by the caller
and cross-checked against the device only when nothing is masking it.

Mask bit index is not the hardware unit index: bit 15 lands on unit 29 and bit
16 on unit 2 in the baseline's sorted identifier order.

## Files

| file | what it records |
| --- | --- |
| `sweep32.json` | first 32 bits, one unit each, no overlap, readback always agrees |
| `sweep64.json` | all 64 bits: 32 effective, 32 ignored and detected as ignored |
| `amd_matrix_verdict.json` | the 24-cell two-mechanism matrix and its dense index map |
| `amd_sweep.py`, `amd_sweep64.py`, `amd_matrix.py` | the harnesses |
