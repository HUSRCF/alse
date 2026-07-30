# Gate A safety and acceptance protocol

This protocol governs all libsmctrl experiments on the shared RTX 4090 host.
It is intentionally stricter than upstream's offset-search loop because a bad
offset writes into an opaque CUDA stream object and can hang a context or
trigger a GPU Xid.

## Invariants

- `vendor/libsmctrl` remains at the commit and hashes recorded in
  `vendor/LIBSMCTRL_SOURCE.json`. Adaptations live outside the submodule.
- Unknown CUDA driver API versions fail closed. Process exit zero alone never
  proves that a mask was applied.
- A mask is a TPC-disable mask. On Ada, one TPC normally contains two SMs; the
  accepted SM set comes from the per-GPU `%smid` histogram, not from `smid/2`.
- Gate A does not use MPS. The runner records host MPS daemons and sets
  `CUDA_MPS_PIPE_DIRECTORY` to the empty string, which NVIDIA documents as an
  explicit MPS bypass. Deleting the variable alone is unsafe because it selects
  the default `/tmp/nvidia-mps` pipe. See NVIDIA's
  [MPS considerations](https://docs.nvidia.com/deploy/mps/when-to-use-mps.html).
- No masked test runs on a GPU with more than 1 GiB allocated or with another
  compute process attached.
- The selected physical device is passed to CUDA by GPU UUID and the native
  probe must report the same UUID. Occupancy is checked both before and after
  the potentially slow environment snapshot.
- Every native invocation runs in a child process with a hard timeout. Raw
  stdout, stderr, configuration, GPU UUID, driver/toolkit versions, source
  revision, outcome, and the complete SM histogram are retained.

## Escalation ladder

1. Build the pinned source and native probe without changing the submodule.
2. Run an unmasked baseline; require a broad, valid SM-ID distribution.
3. Run callback-based global and next-launch probes in separate processes.
   CUDA 13.3 requires an explicit experimental opt-in because upstream has not
   registered that version.
4. Do not try a stream offset until global/next semantics pass and the target
   GPU is explicitly reserved for fault-prone testing.
5. Search only a narrow, predeclared set of 8-byte-aligned candidates around
   the last known CUDA 12.8 offset. Start a fresh process for every candidate.
6. After each candidate, check process timeout, CUDA errors, `nvidia-smi`,
   newly observed Xids, and the histogram. Stop immediately on any health
   anomaly; do not continue the loop.
7. Once a candidate passes, repeat it across single-bit masks at TPC bits
   0, 31, 32, and 63. A candidate is not accepted from one lucky mask.
8. Only after the single-stream matrix passes may two-stream non-overlap,
   boundary swaps, 10,000 updates, Nsight overlap, and diffusion correctness
   tests begin.

## Semantic acceptance

An unmasked probe passes only if:

- the binary reports `status=ok`;
- every observed SM ID is within the device's reported SM count;
- the histogram covers the registered minimum fraction of the GPU; and
- the process has no CUDA error or timeout.

A single-TPC masked probe passes only if:

- all unmasked conditions except broad coverage hold;
- exactly one or two SM IDs are observed on Ada;
- repeated launches return the same derived SM set for that GPU UUID/TPC bit;
- different requested TPC bits used for a partition are disjoint; and
- a follow-up unmasked process confirms the GPU remains healthy.

Global, next-launch, and stream masks are distinct cells. Success in one does
not imply support for another.

The checked-in manifest is a code-enforced promotion lock. Experimental CLI
arguments never override a manifest with `experimental_mask_enabled=false`.
When promotion is justified, a reviewed manifest change must name the approved
modes, exact reserved GPU UUID, and predeclared aligned stream-offset
candidates. A single masked run is only a local probe pass; repeated mapping,
disjointness, follow-up health, and leakage checks are accepted only by the
cross-run matrix validator.

## Stop conditions

Stop Gate A immediately and preserve the failed run if any of the following is
observed:

- Xid, GPU disappearance, persistent context creation failure, or required
  host reboot;
- a masked kernel reaches an SM outside its learned TPC set;
- a supposedly masked run has broad unmasked coverage;
- a next-launch mask leaks into the subsequent kernel;
- a stream mask changes an in-flight kernel; or
- the same candidate is nondeterministic across otherwise identical runs.

No MPS, process-level quota, or temporal-only result may be relabeled as a
successful dynamic libsmctrl Gate A result.
