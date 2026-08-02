# The AMD contract is a reduction, and its scope is exactly one setup

The AMD line drops guards the CUDA line requires. This document exists so that
the reduction can never be quoted as a precedent, and so that a future reader
can tell in one pass whether the conditions it depends on still hold.

**The reduction applies to `gfx1201` on `husrcf@X570`, single card,
single operator, masking through documented HIP interfaces. It applies to
nothing else. It is not a revised standard, not a simplification the CUDA line
may adopt, and not a default for future AMD hardware.**

## Why the reduction is possible at all

Most of what the CUDA runner does is not about evidence quality. It is about
surviving a blind write into an opaque driver structure on a shared machine.
On this AMD setup neither condition holds:

* the mask is programmed through `hipExtStreamCreateWithCUMask` and
  `ROC_GLOBAL_CU_MASK`, both documented interfaces, so nothing is written
  blind and nothing can land on a field that moved;
* `hipExtStreamGetCUMask` reads the applied mask back, so a request that was
  not honoured is reported rather than inferred -- it is what caught mask bits
  32..63 being silently ignored;
* the card is not shared, so no other process can appear mid-matrix and no
  reservation has to be asserted against anyone.

## Ledger

Each row: what is dropped, the CUDA-side hazard it addresses, why that hazard
is absent here, and the condition that brings it back.

| Dropped | Hazard on CUDA | Absent here because | Reinstate when |
| --- | --- | --- | --- |
| NVML Xid monitor, drain, and acceptance gate | A wrong struct offset faults the GPU. Observed: `Xid 31`, `MMU Fault ... ACCESS_TYPE_VIRT_WRITE`, during the offset sweep | No blind write exists on this path | AMD masking ever needs an undocumented write, or any probe writes device state directly |
| GPU lease quarantine and armed-poison payload | A masked run that faulted must not be followed by another until a human looks | Same: the fault mode it guards does not exist | Same as above |
| Exclusive reservation evidence with a time window | A colleague can take the card mid-matrix, splitting evidence across conditions | Single-operator machine | A second user, a scheduler, or any shared-tenancy arrangement |
| Busy-GPU preflight against foreign compute processes | Same | Same | Same |
| Sealed-memfd launcher, `F_SEAL_EXEC`, `execveat` | The probe binary must not change between attestation and exec on a shared box | Single operator; the binary is attested and the tree is bound | Shared tenancy, or any path where another account can write the build directory |
| Parent-death guard (`PR_SET_PDEATHSIG`) | A masked child outliving its supervisor keeps a mask applied to a card someone else then uses | No other user; the mask dies with the process or the stream | Shared tenancy |
| MPS bypass pinning | MPS silently changes which SMs a kernel can reach | ROCm has no MPS equivalent in this configuration | An equivalent multi-process server is introduced |
| Multi-GPU fleet A0 (N cards x 3 trials) | Per-card silicon variation must not be mistaken for an effect | One card, so there is no cross-card claim to make | A second AMD card, or any claim comparing cards |

## What is NOT reduced

These are evidence properties, not hazard mitigations, and they are kept in
full:

* content-addressed run IDs via `derive_run_id`, with no timestamp or host in
  the identity;
* Git provenance binding of the source tree, refusing a dirty or ambiguous
  worktree;
* a build attestation over the probe binary, pinned in the manifest;
* a manifest that declares the matrix shape before any cell is run;
* per-cell contract checks, and `validate_masked_tpc_matrix` **unmodified** --
  the same function the CUDA line uses, including its two-mechanism
  requirement, its determinism and disjointness checks, and its demand that
  masked sets be a proper subset of the same card's unmasked baseline;
* the readback check, which the CUDA line cannot perform and which is
  therefore an addition rather than a reduction.

## How the scope is enforced, not merely stated

Documentation does not stop a reduction from leaking. Three mechanisms do:

1. **No shared code is weakened.** The AMD runner is a separate module. It
   imports provenance and validation primitives; it does not modify
   `smctrl_runner`, so no CUDA gate can be relaxed by an AMD change.
2. **AMD cells carry their own schema version.** They cannot be counted in a
   CUDA aggregate, which pins the CUDA cell schema exactly.
3. **A test asserts both.** See `tests/test_amd_cu_runner.py`: the CUDA gate
   still refuses every masked mode on an unpromoted manifest, and an AMD cell
   is rejected by the CUDA cell validator.
