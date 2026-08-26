# Requirement alignment, weeks 3-4 (2026-08-13 to 08-26): Gate B and Gate B-AMD

Written 2026-08-26, the last day of the stage, before entering weeks 5-6.
Produced under `plan.md` section 五, "阶段切换前的 requirement alignment",
which permits a standalone hashed report in place of a plan.md checkpoint.

Requirements are quoted as they stand in `plan.md` and are **not
rewritten after the fact**. Where a dated decision changed a
requirement's scope, the decision is cited by its own identifier and the
row is judged against the amended text; where no such decision exists,
the row is judged against the original wording even when that is
inconvenient.

## Summary

| | count |
| --- | --- |
| exact | 9 |
| approximate | 4 |
| missing | 4 |
| not_applicable | 1 |
| **total rows** | **18** |

**Gate B-AMD is not accepted.** Both models stand at 9 of 10 clauses,
failing the same one. **Gate B (NVIDIA wording) is untouched and, as the
plan currently reads, still in force.** Neither is claimed as passed
anywhere in this report.

## The governance finding, first because it decides two rows

The 2026-08-09 single-SKU decision was written into **Gate A's** text:
`plan.md` (currently line 338-341; cite the quotation, not the number, since it drifts) says the AMD clauses are thereafter "唯一的 Gate A" and
the NVIDIA-worded originals are kept as historical record and no longer
count as incomplete.

**No equivalent amendment was made to Gate B.** `plan.md` (currently line 384) still reads
"Gate B-AMD（2026-08-02 新增，**补充而非替代上面的 Gate B**）", and
2026-08-02 predates the single-SKU decision by a week. So as the plan
reads today, Gate B's NVIDIA clauses -- four models on a 4090, SM quotas
`{16..128}`, `G={1,4,8,16}`, pinned/pageable, NUMA and PCIe direction --
remain in force and are entirely unmet.

This is not resolved here. Rewriting a requirement to match what was
built is the failure mode this protocol exists to prevent, and the
single-SKU decision was the user's, so extending it to Gate B is theirs
too. Recorded as an open decision, and the two rows it governs are
scored against the text as written.

## 实现 rows

| Requirement | Implementation | Evidence | Alignment | Carry-over |
| --- | --- | --- | --- | --- |
| Profile CogVideoX-2B、CogVideoX-5B、FLUX.1-dev 和 SDXL | `scripts/run_amd_matrix.py`, per-cell process with `ROC_GLOBAL_CU_MASK` | SDXL and CogVideoX-2b swept in full: `gate_b_sdxl_v3_20260803.jsonl` and `gate_b_cogvideox2b_20260804.jsonl`, 16 cells each (8 canonical + 8 saturation probe, all eight quotas). `gate_b_sdxl_20260803.jsonl` is the superseded first SDXL sweep and is kept, not deleted | **not_applicable** | Scope redefined by the dated decision `scope-is-the-32gb-class-not-four-models` (2026-08-06): CogVideoX-5b (~32 GB inference peak) and FLUX (33.74 GB weights) are outside the 32 GB-class envelope, not coverage gaps. Gate B-AMD's acceptance scope is the two models and says so |
| AMD quota list `{4,8,12,16,20,24,28,32}` maskable units, one process per cell | same | verdict clause `quota_list_and_per_process_mask` PASS on both models; all eight units present in both sweeps | **exact** | none |
| `G={1,4,8,16}` | no G axis was built | Each sweep is 16 cells: 8 `canonical` at batch 1 and 8 `saturation_probe` at batch 2, over all eight quotas. Batch 2 exists to pose a strictly larger problem for the saturation test, **not** as a point on a G curve | **missing** | The G axis was never swept. The two batch values present are two roles, not two group sizes, and reading them as a G curve would be reading a saturation probe as a profile. Nothing measured to date depends on it; must close before any claim about batching, and it enters the weeks 5-6 manifest |
| early / middle / late step phase | per-cell phase means | `early_step_mean_s`, `middle_step_mean_s`, `late_step_mean_s` on every cell | **exact** | none |
| resident / state-only / cold residency | resident sweep; cold-model probes and replay diagnostic | resident: both sweeps. cold: `cold_model_sdxl_perterm_20260804.json`, `cold_model_cogvideox2b_20260806.json`. **state-only: no separate measurement** | **approximate** | Two of three tiers measured. The cold tier is measured but its *model* fails acceptance (below). state-only is unmeasured and unclaimed |
| compute / HBM / PCIe probe co-runner | the probe role is a scaled instance of the same model (`probe_height`, `probe_batch`), used only for the saturation test | 8 `saturation_probe` cells per sweep | **missing** | The three synthetic probe co-runners the requirement names were not built. The saturation judgment that depends on the probe role is sound as run; what is missing is the ability to attribute contention to compute vs HBM vs PCIe |
| 5 warmup、30 samples；尾部 cell ≥100；每 cell 实测并记录 CV | sweep harness with escalation | verdict clause `steady_state_cv_within_threshold` PASS: worst CV **0.00124** (0.12%), `total_escalations` 0 -- no cell needed extra sampling | **exact** | none. The clause's own escape hatch (raise samples if CV > 5%) was never needed |
| p50 canonical / p99 deadline / pairwise externality / 显存峰值 四张表 | per-cell fields plus one standalone externality file | `p50_s`, `p99_s`, `peak_memory_bytes`, `peak_memory_reserved_bytes` on every cell; `externality_table_20260806.json` (4 pairs measured of 4 attempted, externality 0.223 to 1.926) | **approximate** | The four tables exist as queryable fields rather than as four named canonical artifacts. Nothing is missing from the data; what is missing is the published table form the artifact gate will want |
| pinned/pageable、local/remote NUMA、单向/双向 PCIe | none on the AMD line | no evidence | **missing** | R9700 exposes no usable PCIe throughput counter (`rsmi_dev_pci_throughput_get` returns NOT_SUPPORTED, recorded 2026-08-03), so the PCIe-direction part cannot be measured on this card by the means the requirement implies. pinned/pageable and NUMA are measurable and were not measured. Blocks nothing measured to date |

## 验收 rows

| Requirement | Implementation | Evidence | Alignment | Carry-over |
| --- | --- | --- | --- | --- |
| Gate B (NVIDIA wording): 4090 SM quotas, four models, MAPE and PCIe clauses | none on the NVIDIA line beyond Gate A0 | `gate_a0_4090_v2_seed1_fleet5_20260731.json` is Gate **A0**, not Gate B. No 4090 profile sweep exists | **missing** | See the governance finding. Either the 2026-08-09 single-SKU decision is extended to Gate B's text by the user, or Gate B stays open. Not resolved by this report and not rewritten by it |
| Gate B-AMD, SDXL | `gate_b_amd_verdict_sdxl_final.json` | **9 of 10 PASS**, `accepted: false`. Sole failure `cold_model_predicts_transfer_and_framework_separately`: transfer 4.08% PASS, total 7.36% PASS, **framework 88.5% FAIL** against a 10% threshold | **approximate** | The 2026-08-04 tightening is doing exactly what it was written for: the total error (7.36%) would have passed the original single-number clause while the independently calibrated framework term is off by a factor of nine. Deferred to the runtime stage by decision; must be `exact` before any cold-start claim |
| Gate B-AMD, CogVideoX-2b | `gate_b_amd_verdict_cogvideox2b_20260808.json` | **9 of 10 PASS**, `accepted: false`. Sole failure is the same clause and it fails harder: transfer **83.9%**, total **67.3%**, framework **never calibrated (null)** | **approximate** | Worse than SDXL and differently: for SDXL the hardware term is right and the implementation term is wrong; here the transfer term itself is wrong, so the 6.94 GB / 2643-tensor reasoning does not carry to a 13.77 GB model. Same carry-over |
| Gate B-AMD co-run must prove real temporal overlap; stream form requires mask-penetration evidence | `run_amd_inproc_corun.py --share-weights` | clause `corun_is_two_disjointly_masked_workers` PASS, form `streams`, externality a +27.2% / b +31.4%, overlap 111.1 s = 92.5% of the window; prerequisite `stream_mask_penetration_20260807.json` ratios 0.9900 / 0.9887 / 0.9847 / 0.9739 at 4/8/16/32 units | **exact** | The clause was amended 2026-08-08 to admit the stream form **with** penetration evidence, after the two-process form was shown impossible on this card (57 GB needed, 34.2 GB available). Evidence without penetration data is NOT_MEASURED, and that gate was verified with a forged leaking-penetration record |
| resident 同模型轮转零权重传输 (AMD forensics via `rocprofv3 --memory-copy-trace`) | rotation probe | clause PASS: 0.0 copies per rotation, upper bound 10.0 MB per rotation against 15.63 GB of weights and a 156 MB tolerance | **exact** | none |
| 每 profile 带 hardware/driver/ROCm/Torch/model revision/schema version；mask 记录请求值与 `hipExtStreamGetCUMask` 读回值 | per-cell provenance and mask attestation | clause `profiles_carry_full_provenance` PASS (16 cells); every cell carries `amdgpu_driver`, `rocm`, `torch`, `torch_hip`, `gcn_arch`, `firmware`, `model_revision`, `repo`, `schema_version`, and `cu_mask_attestation.readback_matches_request` | **exact** | none |
| 每 cell 记录饱和区状态；未标注者不得入 canonical table | `saturating_regime` / `saturation_basis` per cell | clause `every_cell_records_its_regime` PASS; all eight quotas saturating, `not_saturating` empty; `canonical_eligible` gates entry | **exact** | none |
| held-out solo p50 MAPE ≤10%；transition MAPE ≤10% | canonical fit and transition probe | held-out PASS (e.g. 3.90% at quota 12, `any_extrapolated: false`); transition PASS at **0.209%** over 10 steps, dwell 2 | **exact** | none |
| source tree 未在扫描中移动 | revision binding before and after | clause PASS, `11410e82` before and after | **exact** | none |

## What this permits and forbids for weeks 5-6

Weeks 5-6 is "Trace Simulator 与算法冻结". Under the protocol,
`approximate` alignment permits reversible, low-risk work in the next
stage but forbids crossing a hard gate and forbids marking anything as
accepted on the strength of it.

Permitted, because nothing here depends on the four open rows: simulator
and algorithm work on the cost tables that **did** pass -- the p50
canonical fit, the transition model, the externality table and the
resident rotation result.

Forbidden until the corresponding row is `exact`:

* any cold-start claim, for either model. The cold model fails for both
  and fails differently, and for CogVideoX-2b the transfer term itself
  is wrong.
* any claim about batching. `G` was swept to 2, not 16.
* any claim attributing contention to compute, HBM or PCIe
  specifically. The probe co-runners that would separate them were not
  built.
* any statement that "Gate B passes" or "Gate B-AMD passes". Neither
  does. Both models sit at 9 of 10 with `accepted: false`.

## A note on the calendar

The plan's calendar and the work have diverged, and the alignment table
should say so rather than let the stage boundary imply otherwise. Gate C
(simulation) is already `exact` and the algorithm is frozen -- weeks 5-6
material, done early. Meanwhile hardware runtime experiments belonging
to weeks 9-12 have been running for two weeks and produced the 405-cell
grid and Experiment A. What has *not* moved is Gate A, still `missing`,
and Gate B, whose NVIDIA text nobody has amended.

So entering weeks 5-6 tomorrow is a calendar event, not a transition:
the work it names is largely finished and the work actually in flight
belongs to a later stage. This is recorded so that a later reader does
not read the stage label as a claim about what was gated when.
