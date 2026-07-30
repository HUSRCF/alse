# Burst 轮转与动态 SM 分区：16 周论文级推进计划

## 执行状态

- Last updated: 2026-07-30
- Current phase: 第 1 周——工程基线与可复现环境；并行进行 Gate A0
  fail-closed 预研，不视为进入第 2 周
- Current gate: Phase 0 baseline reproducibility；Gate A0 仅允许 unmasked
  baseline
- Status: in_progress
- Last accepted evidence: Phase-0 baseline 与 same-mode correctness 均通过；
  专用 `burstserve-phase0` 环境的 25+52 包 relocatable lock exact-match；其
  stock trials 2/3 跨 GPU latent SHA 完全一致且与原栈相同；14 个 run 中
  12 accepted、2 legacy completed。Gate A0 的
  `experiments/aggregates/gate_a0_4090_dd8c927_seed1_partial_20260730.json`
  已在 commit `d967722` 固化：GPU 1/2/3/4/7 各 3 次，共 15/15
  unmasked cells accepted，均为 4096 blocks、128/128 SM coverage；
  aggregate input SHA256 为 `08ef4053…a188`。该报告明确判定完整 Gate A0
  为 `false`，GPU 0/5/6 各缺 3 次 trial。纯 CPU simulator foundation
  已在 commit `82a27c4` 固化：dual-ledger 原子记账、tenant/request
  生命周期、canonical schema codec 与三态 I/O 模型经两套解释器各
  68/68 测试及六轮独立攻击复审通过。确定性 lifecycle trace 的 canonical
  JSONL、整数 PRNG、线性资源上界、自校验 replay evidence 已在 commit
  `827beb8` 固化：两套解释器各 107/107 完整 simulator 测试，三轮独立
  资源放大与伪造攻击终审无阻断项；这些仍只是获准的后续阶段预研，不代表
  Gate C 通过。
- Active blockers: 尚未完成第二 GPU SKU 的外部预约；从锁重建的干净
  conda base 已成功，但数 GB pip wheelhouse 按高带宽机下载约定待回传后
  完成 offline-install 验证；GPU 0/5/6 被现有进程占用；masked CUDA
  13.3 probe 还缺独占 GPU 预约、父进程异常/延迟 Xid 的完整 fail-closed
  lifecycle、跨 trial/bit validator 和经审查的 promotion manifest。
  runner/native 独立审计还发现 repo-local Git config/attributes 可在只读
  status 中触发外部 filter，以及 v2 aggregate 先哈希后按路径重读的
  TOCTOU/canonical-parser 缺口，修复并复审前不得生成 v2 正式证据
- Next three actions:
  1. 关闭 runner/native 的 repo-local Git 派生执行与 aggregate
     hash→parse TOCTOU/canonical JSON 缺口，再完成独立安全复审
  2. 保持 masked promotion lock 关闭；将 native `--eval` pre-guard
     immediate expansion 与 root-owned Python/stdlib TCB 写入明确边界，
     完成 parent-death、Xid lifecycle 和跨 trial/bit validator 后再预约 GPU
  3. 待 GPU 0/5/6 空闲后按卡串行补齐 unmasked baseline；同时等待第二 SKU
     预约证据与 wheelhouse 回传以关闭 Phase 0
- Latest run IDs / commit: commit `827beb8`；Gate A0 clean subset 15 runs，
  代表 run `bs1-f3628358…ba7`（GPU1）至 `bs1-f3679d41…bf2f`
  （GPU7）；sealed rejection `bs1-81ee41a6…b59` /
  `bs1-73dbd73a…27d`

### 当前阶段 requirement alignment（2026-07-30）

| Requirement | Implementation | Evidence | Alignment | Carry-over |
| --- | --- | --- | --- | --- |
| 固定并不可变导入 ASLE | `vendor_import.py`、`vendor/ASLE_SOURCE.json` | archive `5c6cba9d…f97d`；347-file tree `380cbf15…2afb` | exact | 持续禁止直接修改 `vendor/asle` |
| 4090 默认 49-frame、30/8-step、seed 0/1/2 | Phase-0 ASLE runner | `phase0_runs_20260730.json`；三次分别为 18/2、22/2、16/2 urgent/video | exact | 无 |
| 同 seed deterministic correctness | correctness runner | stock/offload same-mode tensor SHA 成对完全一致；专用环境复跑一致 | exact | cross-mode 数值差异继续仅 `report_only` |
| 隔离环境及 exact lock | `runtime_lock.py`、25 conda + 52 pip lock | 当前专用环境 verification `matches=true` | exact（现有环境） | 离线 wheelhouse 从零重建仍 missing，Phase 0 不关闭 |
| 第二 GPU SKU 预约 | 无 | 无 SKU、时间窗或预约证明 | missing | Phase 0 硬门；H100 优先，A100/A800 备选 |
| 所有 8 张 4090 的 A0 unmasked ×3 | native probe、Gate-A runner、独立聚合器 | `gate_a0_4090_dd8c927_seed1_partial_20260730.json` | approximate（5/8，15/15 subset） | GPU 0/5/6 空闲后各补 3 次；不得使用 busy override |
| 未 promotion 的 masked 请求 fail closed | checked-in Gate-A manifest 与 runner | 两次 sealed rejection 均无 `Popen`/native output | exact（安全锁） | 只证明未越权，不证明 mask 可用 |
| 可观测 Xid 且异常时 fail closed | ctypes NVML event monitor 与 runner integration | 纯模拟测试；GPU7 只读注册 smoke supported bits `61852`、Xid bit `8` | approximate | 修复中断孤儿、有界 reap、真实 quiet window 和 post-health 覆盖后才 exact |
| 可逆 simulator foundation | `src/burstserve/sim` 的 schema、dual-ledger、lifecycle、三态 I/O 与 deterministic trace replay 纯函数 | commits `82a27c4`/`827beb8`；Python 3.11/3.13 各 107/107；trace 三轮独立资源/伪造攻击终审 | approximate（获准预研） | 动作枚举/选择、predictor error 与 Gate C 正式证据仍 missing |
| Gate A 动态 SM 功能与性能 | 尚未运行 masked kernel | 无 TPC map、10,000 次重配、更新 p99、overlap 或 correctness 证据 | missing | Gate A 硬门，不能由 A0 或 simulator 替代 |

在上述 hard gaps 关闭前，不正式进入第 2 周。只允许并行开展可逆、纯 CPU
预研：冻结数据 schema、canonical-service/tenant accounting 纯函数、
resident/state-only/cold I/O byte truth table、确定性 trace replay 和抽象
`ProfileProvider`/`Executor` protocol；不得生成正式 profile、真实 scheduler
action、masked action 或性能/SLO claim。

## 一、目标与最终验收标准

目标是基于 ASLE 构建一个面向 diffusion burst serving 的完整系统：

> SLO-aware dual-ledger virtual-service scheduler，联合决定 denoising quantum、SM 配额、tile 数量和模型驻留状态。

核心贡献包括：

- 用 Canonical Solo-Equivalent Service 衡量跨模型、跨 SM 配额的公平服务。
- 用资源债务和 profiled marginal externality 选择空间并发动作。
- 用真实 p99 slack 处理 urgent deadline，超前服务仍计入账本并在 burst 后自动偿还。
- 把模型驻留与请求轮转分成两个时间尺度，按真实 PCIe 字节计算切换成本。
- 使用 libsmctrl 对不同 CUDA stream 设置动态 SM mask；不宣称其“融合 kernel”。
- 在 step/kernel 安全边界切换，不实现 mid-kernel preemption。

### 全局成功门槛

系统完成必须同时满足：

- 固定随机种子的 deterministic correctness 测试通过，无 NaN、死锁或状态丢失。
- 连续一小时 burst 压测无 OOM；不可行请求由 admission control 明确拒绝。
- scheduler 决策开销 p99 小于 1 ms；native SM mask 更新 p99 小于 100 μs。
- solo step-time p50 预测 MAPE 不超过 10%，co-run/外部性预测 MAPE 不超过 15%。
- deadline predictor 的 false-safe rate 不超过 5%。
- 非 deadline-overrides 场景中 weighted Jain fairness 不低于 0.98，最大 service lag 不超过两个最大 canonical quanta。
- 无 burst 时，相对最佳 ASLE 基线的吞吐下降不超过 5%。
- 在预注册的至少两个 burst 场景中，相对最强非 oracle 基线达到以下任一 Pareto 改进：
  - urgent SLO miss rate 相对下降至少 20%，video goodput 下降不超过 5%；或
  - 相同 SLO miss rate 下 video goodput 提升至少 10%。
- 第二 GPU SKU 的 12 个关键实验中，至少 9 个与 4090 呈现相同优化方向。
- 所有论文图表均能从带哈希的 raw logs 一键重建。

## 二、系统设计与实现接口

### 1. 代码与产物组织

- `vendor/asle`：从 `ASLE.tar.gz` 按 SHA256 固定导入的原始基线，不直接修改。
- `src/burstserve`：新运行时、scheduler、profile store、SM controller 和模型 adapter。
- `experiments`：版本化 YAML manifest、原始 JSONL、汇总数据和绘图脚本。
- `plan.md`：唯一进度与验收事实来源。

现有 `toy_bench` 和 `related_work` 保留原位，只作为前期证据和参考材料。

### 2. 运行时架构

采用一个服务进程、一个 CUDA context、每个活动模型一个独立 CUDA stream：

- Controller 保存 tenant、request、resident set 和账本。
- CogVideoX、FLUX、SDXL adapter 将完整 pipeline 拆成可恢复的显式 denoising-step 状态机。
- 每个 adapter 暴露 `prepare()`、`run_step()`、`suspend()`、`resume()` 和 `finalize()`。
- 需要空间并发时，由独立 worker thread 向专属 stream 发射 kernel，并以 CUDA event 汇合到 quantum 边界。
- libsmctrl 为 stream 设置互不重叠的 TPC mask，并由每卡 probe 映射到
  实际 SM 集合；Ada 上不宣称任意单-SM 粒度。
- 第一版不依赖 MPS 或多进程；多进程/MPS 只作为非核心扩展实验。

### 3. 核心数据类型

- `WorkloadSignature`：模型及 revision、输入 shape、frame 数、batch、dtype、CFG、scheduler、总步数、attention backend、streaming mode、硬件/软件 profile ID。
- `Request`：request/tenant ID、到达时间、真实 SLO deadline、剩余步数、latent、scheduler/RNG state。
- `TenantState`：weight、累计 canonical service、virtual progress、resource debt、sleep 状态。
- `Action`：request 集合、quantum `Q`、每个 stream 的 SM mask、tile `G`、resident-set transition。
- `QuantumResult`：完成步数、实际时间、CUDA events、资源消耗、PCIe 字节和错误状态。
- `ProfileStore`：
  - `solo_p50(signature, phase)`
  - `remaining_p99(request, action)`
  - `transition_p99(old_residency, action)`
  - `externality(action, state)`
  - `memory_peak(action)`

Profile lookup 使用版本化 SQLite；原始观测保留为不可变 JSONL。

### 4. 公平、deadline 与动作选择

完成 quantum 后：

$$
\Delta S_i=\sum_{k\in completed}
T_{\mathrm{solo,resident,p50}}^{\mathrm{canonical}}(i,k)
$$

$$
S_i\leftarrow S_i+\Delta S_i,\qquad
v_i=S_i/w_i
$$

Canonical 配置固定为独占全卡、模型已驻留，并选择语义等价配置中 solo p50 最小的 $G_\text{ref}$。当前 SM 配额、co-run 干扰和实际 wall time 都不能改变本轮公平收费。

全局虚拟时间和 service lag：

$$
V\leftarrow V+
\frac{\sum_i\Delta S_i}
{\sum_{j\in active}w_j},
\qquad
L_i=w_iV-S_i
$$

新 tenant 从 $L_i=0$ 开始；重新激活的 tenant 最多保留一个最大 canonical quantum 的 sleeper credit。

资源债务对 SM、HBM、PCIe 分别维护：

$$
D_{i,k}\leftarrow
\max\left(
0,
e^{-\Delta t/60s}D_{i,k}
+r_{i,k}
-\frac{w_i}{\sum_jw_j}\Delta t
\right)
$$

VRAM 不折算进这个标量，始终作为硬容量约束。

每个安全边界按以下固定顺序决策：

1. 枚举 `Q∈{1,2}`、`G∈{1,4,8,16}`、合法 SM mask、串行/co-run 和 residency 动作。
2. 删除显存不可行、非安全边界或缺少必要 profile 的危险 co-run 动作。
3. 用 p99 remaining time、transition time 和 held-out residual margin 计算真实 deadline slack。
4. 若存在 deadline risk，按“预测 miss 数、总 lateness、外部性、切换成本”排序。
5. 若 video 超过 stall budget，强制候选动作包含 video progress；不可行过载必须显式记录。
6. 正常情况下，在 owed-service tenant 中选择 virtual finish 最早者：

   $$
   F_i=v_i+\Delta S_i(Q)/w_i
   $$

7. 在包含该 tenant 的动作中，依次按 dominant resource debt、marginal externality、transition cost、稳定 action ID 排序。
8. urgent 获得的抢跑服务仍加入 $S_i$，burst 结束后自然偿还。

### 5. 性能与 I/O 模型

解释性模型为：

$$
T_{\mathrm{step}}=
T_{\mathrm{launch}}(G)+
\max\left(
\frac{W}{\eta(s,G)C_s},
\frac{B_{\mathrm{HBM}}}{BW_{\mathrm{HBM}}},
\frac{B_{\mathrm{PCIe}}}{BW_{\mathrm{PCIe}}}
\right)
+I(\mathrm{corunner})
$$

实际调度使用离散实测表，不依赖未经验证的解析外推。timestep 分成 early/middle/late 三个 phase。

模型转换成本按真实状态计算：

$$
C_{A\rightarrow B}=
C_{\mathrm{drain}}+C_{\mathrm{control}}+C_{\mathrm{cache}}
+\frac{B_{\mathrm{missing,H2D}}}{BW_{\mathrm{H2D}}}
+\frac{B_{\mathrm{dirty,D2H}}}{BW_{\mathrm{D2H}}}
-C_{\mathrm{hidden}}
$$

规则如下：

- 同模型、权重已驻留的请求轮转：权重 PCIe 字节为零。
- 只读权重保留 pinned host shadow；GPU eviction 直接释放，不重复 D2H。
- 只有未驻留的只读权重才计入 H2D。
- latent、scheduler 和 RNG state 按实测大小计费。
- “20 GB switch”仅用于真实 20 GB cold-model transfer，不作为每个 quantum 的固定成本。
- 当前 4090 起始测量 H2D 约 21.13 GB/s、D2H 约 22.69 GB/s；执行时重新测量，70% PCIe 理论带宽只作为 sanity check。

## 三、16 周阶段与验收计划

### 第 1 周：2026-07-30 至 08-05——工程基线与可复现环境

实现：

- 将本计划写入 `plan.md`，建立状态、风险、决策和验收记录。
- 固定 ASLE archive 哈希并导入不可变 baseline。
- 建立隔离 Python/CUDA 环境和依赖锁。
- 去除运行路径中的硬编码模型目录，模型仍使用 `/data/zhuoxu/models`，禁止在线下载。
- 建立统一 run manifest、run ID、environment snapshot 和日志 schema。
- 在 4090 上运行 CogVideoX-2B + SDXL 默认配置：49 frames、30 video steps、8 urgent steps，seeds 0/1/2。

验收：

- 三次 baseline 均生成完整 raw log、summary 和环境信息。
- 同 seed 的 latent hash 或确定性容差一致。
- 原始 ASLE baseline 未被修改。
- 第二 SKU 按“H100 优先，A100/A800 备选”的顺序完成预约。

### 第 2 周：2026-08-06 至 08-12——libsmctrl 高风险可行性闸门

实现：

- 依次尝试当前 upstream、兼容 CUDA userspace 环境、最小 offset 适配。
- 编写 SM-ID probe kernel，验证 global、per-stream 和 next-launch TPC
  mask，并从单-bit 观测建立每卡 `TPC bit -> SM set` 映射。
- 在同一 context 的两个 stream 上验证互斥 SM partition 和并发执行。
- 测量 mask 更新、event boundary、kernel drain 和 cache 冷启动。
- 固定 seed 比较 masked、unmasked、co-run 输出。

验收 Gate A：

- 所有 8 张 4090 均能检测到合法 SM/TPC 拓扑。
- probe kernel 100% 只落在指定 TPC mask 经该卡实测映射得到的 SM 集合。
- 10,000 次动态重配置无崩溃、越界或错误 mask。
- native 更新 p99 不超过 100 μs。
- deterministic correctness 不因 mask 或发射顺序破坏。
- profiler 中可观察到真实 stream overlap。

若 08-12 未通过，允许使用第 3 周前三天修复；若 08-15 仍未通过，停止动态 SM 核心投稿路线，保留 temporal-only artifact，并明确标记 full-paper Gate A 失败，不使用 MPS 冒充等价结果。

### 第 3–4 周：2026-08-13 至 08-26——Profile 数据与模型

实现：

- Profile CogVideoX-2B、CogVideoX-5B、FLUX.1-dev 和 SDXL。
- 4090 SM quota 使用 `{16,32,48,64,80,96,112,128}`，记录实际 mask。
- 测试 `G={1,4,8,16}`、early/middle/late step phase、resident/state-only/cold residency。
- 加入 compute、HBM 和 PCIe probe co-runner。
- 常规 cell：5 次 warmup、30 次采样；尾部和切换关键 cell：至少 100 次采样。
- 建立 p50 canonical table、p99 deadline table、pairwise externality table 和显存峰值表。
- 测量 pinned/pageable、local/remote NUMA、单向/双向 PCIe。

验收 Gate B：

- 稳态 solo cell 的 CV 不超过 5%。
- held-out solo p50 MAPE 不超过 10%。
- transition prediction MAPE 不超过 10%。
- resident 同模型轮转观测到零权重 PCIe 流量。
- cold-model 预测严格使用缺失字节和实测带宽。
- 每个 profile 都带硬件、驱动、CUDA、Torch、模型 revision 和 schema version。

### 第 5–6 周：2026-08-27 至 09-09——Trace Simulator 与算法冻结

实现：

- 将现有 toy bench 扩展成 trace-driven simulator。
- 加入 tenant service、deadline borrowing、resource debt、residency state、partial preemption 和 predictor error。
- 实现所有基线和 oracle。
- 固定 action 排序、sleep credit、admission 和 overload 行为。
- 写出 bounded service lag、finite-burst repayment 和 video stall bound 的证明条件。
- 验证“单一标量无法同时精确表达进度公平与非线性多资源成本”的反例。

验收 Gate C：

- 同 seed 仿真结果逐字节可复现。
- canonical service 对 SM quota 的记账差异小于 1%。
- backlogged workload 的 Jain index 不低于 0.98。
- 无 deadline override 时 service lag 不超过两个最大 quantum。
- 构造的可行 deadline trace 中不存在可避免 miss。
- predictor error `{±5%,±10%,±20%}` 下能够安全降级。
- 算法、公式和 action 顺序在本阶段结束后冻结；之后修改必须写 decision log。

### 第 7–8 周：2026-09-10 至 09-23——ASLE Temporal Runtime

实现：

- 将 callback 内同步 `serve_pending()` 拆成显式 denoising-step executors。
- 加入 tenant/request 队列、FCFS/EDF tenant 内调度和完整 checkpoint。
- 实现 full-GPU fixed rotation、wall-time CFS、step-count、SM-time 和 canonical-service 调度。
- 实现模型 residency epoch 和同模型 burst batching。
- 用 CUDA event 替代不必要的全局 synchronize。
- 写入每个决策前后的 ledger、slack、resident set、PCIe 字节和实测结果。

验收：

- 对原 ASLE seed 的最终 latent 在确定性模式下 hash 一致。
- 正常性能模式无 NaN/Inf，误差不超过 baseline 自身重复波动。
- 同模型 burst 建立 residency 后不再传输模型权重。
- scheduler p99 开销小于 1 ms。
- 无 burst 吞吐下降不超过 5%。
- 一小时 temporal-only 压测无泄漏、死锁或 OOM。
- video stall 不超过配置预算加一个不可抢占 step/transition。

### 第 9–10 周：2026-09-24 至 10-07——动态空间分区集成

实现：

- 将每个 model executor 绑定到独立 CUDA stream 和 SM mask。
- 支持静态 mask、边界动态 mask 和 full-GPU serial fallback。
- 实现 `Q × SM × G × co-run` action executor。
- 加入 stream-local event、mask 生效验证和 action watchdog。
- 在真实 diffusion kernel 上补齐 quota scaling 和 pairwise interference profile。

验收：

- 每个 action 的实测 SM 集合与 manifest 完全一致。
- 动态重配只影响后续 kernel，不破坏 in-flight kernel。
- Nsight 时间线确认预期 overlap。
- co-run step prediction MAPE 不超过 15%。
- 10,000 次 action 切换和一小时并发压测无错误。
- 当 profile 缺失或 drift 超过 15% 时自动回退到保守 serial action，并明确记录原因。

### 第 11–12 周：2026-10-08 至 10-21——完整 Dual-ledger Scheduler

实现：

- 接入 p99 deadline guard、canonical service、resource debt、externality 和 transition model。
- 实现基于 EDF commitments 的 admission control。
- 增加 video stall guard、urgent borrowing/repayment 和 sleeper-credit cap。
- 资源不可行时记录 offered/admitted SLO，不允许静默丢请求。
- 在 4090 上完成内部参数冻结。
- 第 12 周结束前在第二 SKU 上完成环境、模型和 SM mask smoke test。

验收 Gate D：

- false-safe deadline prediction 不超过 5%。
- 所有选中动作满足显存约束，并保留 `max(1 GiB, 5% VRAM)` 安全余量。
- Jain index 不低于 0.98，service lag 满足冻结界限。
- 至少两个内部 burst cell 达到全局 Pareto 改进门槛。
- 第二 SKU 能完成相同 run manifest；否则跨 SKU 主张判定失败。
- 若 Gate D 未通过，停止增加模型或基线，优先修正模型/算法；不得通过挑选 workload 进入论文评测。

### 第 13–14 周：2026-10-22 至 11-04——论文主实验与跨 SKU 验证

实现：

- 冻结代码、manifest 和 primary metrics。
- 在 6 张 4090 上运行矩阵，1 张用于开发复查，1 张保留 clean control。
- 在第二 SKU 运行预注册的 12-cell 精简矩阵。
- 对失败、OOM、拒绝和超时全部保留，不删除异常点。
- 汇总 paired-seed bootstrap 95% CI。

验收：

- 主矩阵完成率至少 95%，缺失 cell 全部重跑或注明确定性失败。
- 每个主 cell 至少 5 seeds；第二 SKU 和昂贵扩展至少 3 seeds。
- 每个 tail latency 点跨 seeds 累计至少 200 个 urgent 请求。
- 所有方法使用完全相同的 arrival trace、模型输入和显存预算。
- 第二 SKU 至少 9/12 cell 与 4090 方向一致。
- 所有主张均能追溯到 run ID 和原始日志。

### 第 15 周：2026-11-05 至 11-11——鲁棒性、消融与 Artifact

实现：

- 扫描 predictor error、profile drift、warm/cold residency、不同 PCIe/NUMA 情况。
- 消融 canonical ledger、resource debt、deadline guard、externality、SM partition、G 和 I/O model。
- 验证 overload admission、sleep/wakeup gaming、tenant request splitting 和长期 burst。
- 整理一键 smoke、profile、主实验、聚合和绘图入口。
- 完成定理、局限和 threat-to-validity 文稿。

验收：

- ±10% predictor error 不造成安全失败；±20% 时可保守降级。
- 所有消融与主方法来自同一代码路径。
- 干净环境可以一键完成小型 artifact smoke test。
- 任何性能 claim 均有对应消融或机制证据。
- 预留本周后半段作为唯一总缓冲；不再增加新功能。

### 第 16 周：2026-11-12 至 11-18——冻结、独立复现与投稿包

实现：

- 冻结代码、profile schema、实验 manifest 和论文数据。
- 从干净环境独立执行安装、smoke、一个主 cell 和全部绘图。
- 建立 claim-to-evidence 表。
- 固定源码、模型 revision、环境、raw logs 和图表哈希。
- 完成 artifact README、已知限制、失败模式和复现时间预算。

最终验收：

- 所有核心结果可从 raw logs 重建。
- 不存在未记录的手工数据修正。
- 不存在把 20 GB 当成任意轮转固定开销、把 virtual deadline 当应用 deadline、或把 libsmctrl 描述成 kernel fusion 的错误表述。
- Gate A–D 全部通过后才按完整系统会议标准投稿；任一核心 Gate 失败都必须降低相应 claim。

## 四、实验与测试矩阵

### 主 workload

- Primary：CogVideoX-2B + SDXL，ASLE 默认 49 frames、30/8 denoising steps。
- Scale：CogVideoX-5B + SDXL。
- Generality：FLUX.1-dev 512×512 + SDXL。
- 扩展 shape：CogVideoX 81/129 frames，仅在 profile 和显存 Gate 通过后运行。
- 显存：24 GB 原生、20 GB 和 16 GB emulated budget；第二 SKU 使用等价 residency regime，而非机械使用相同 GB。

### 到达与 SLO

- Poisson offered load：独占服务能力的 `{0.3,0.6,0.85,1.05}`。
- Bursty：burst size `{2,4,8}`，inter-burst rate 调整为相同平均 offered load。
- Urgent deadline：isolated p99 的 `{1.25×,1.5×,2.0×}`。
- Video stall budget：`2 × isolated video-step p99 + transition p99`。
- Primary claim 使用预先冻结的 0.6/0.85 load、burst 4/8、1.5× deadline；其他组合用于趋势和过载评估。

### 基线

主比较：

- ASLE drain-all/StepSwap。
- FCFS。
- Fixed quantum rotation。
- EDF/least-laxity。
- Static SM partition + round-robin。
- Canonical-service only。
- 完整 dual-ledger scheduler。
- Oracle profile/action upper bound。

货币与机制消融：

- Wall-time CFS。
- Step-count CFS。
- SM-time CFS。
- FLOP-equivalent。
- DRF-inspired resource accounting。
- 无 residency model、无 externality、无 deadline borrowing、无 dynamic SM。

### 必须覆盖的测试

- 同一工作在不同 quota 下的 canonical accounting invariance。
- urgent deadline borrowing 后的公平偿还。
- 新 tenant 与长期 sleeping tenant 无无限信用。
- request splitting 不能获得额外 tenant 份额。
- resident/state-only/cold 三种 I/O 路径字节核对。
- 缺失 profile、过期 profile 和严重误差时的保守 fallback。
- SM mask 正确性、mask churn、stream overlap 和输出确定性。
- OOM 前 admission、workspace 峰值和 suspended-state 上限。
- 多 urgent EDF、burst 长于视频 stall budget、不可行 overload。
- 无 burst、轻载、饱和和过载四种状态。
- 4090 与第二 SKU 的行为一致性。

## 五、`plan.md` 与 Compaction 恢复协议

`plan.md` 顶部始终维护：

- `Last updated`
- `Current phase`
- `Current gate`
- `Status: not_started | in_progress | blocked | accepted`
- `Last accepted evidence`
- `Active blockers`
- `Next three actions`
- `Latest run IDs / commit`
- `Decision log`

每个阶段只有在验收证据路径、run ID 和结果摘要写入后才能标记完成。

每次 context compaction 或新执行会话后的第一组动作固定为：

1. 完整读取 `plan.md`。
2. 检查当前 phase、最后已验收 Gate 和 next actions。
3. 检查工作树、最新 run manifest 和未完成实验。
4. 不重复已经有验收证据的工作。
5. 在继续实现前更新 compaction checkpoint。
6. 若用户最新明确指令与计划冲突，以用户指令为准，并先将变更和理由写入 decision log。

每个工作日结束或每个重大实验批次完成后更新一次状态。验收标准不得因结果不理想而静默降低；任何变更必须记录日期、原因、旧值和新值。

### 阶段切换前的 requirement alignment

进入任何下一阶段前，必须先在 `plan.md` checkpoint 或独立、带哈希的
验收报告中列出：

| 项目 | 必填内容 |
| --- | --- |
| Requirement | 本阶段原始实现/验收要求，不事后改写 |
| Implementation | 对应代码、配置或机制路径 |
| Evidence | commit、run ID、raw log、测试或报告路径 |
| Alignment | `exact`、`approximate`、`missing` 或 `not_applicable` |
| Carry-over | 若非 exact，下一阶段的限制、责任人和最晚关闭时间 |

允许 `approximate` alignment 后并行开展低风险、可逆的下一阶段预研，但需
同时满足：

- 不能跨越安全、正确性、可复现性或论文核心 claim 的硬 Gate；
- 不能把预研结果标记为前一 Gate accepted；
- 所有缺口必须进入 Active blockers 和下一阶段 manifest；
- 在产生依赖该缺口的正式数据前必须补成 `exact`，否则回退。

Gate A、Gate D 和最终 artifact gate 只能在硬要求均为 `exact` 时通过。
本协议落实用户提出的“进入下一阶段前先近似对齐要求”，其中“近似”用于
允许受控预研，不用于降低正式验收门槛。

## 六、固定假设与边界

- 排期按一名主力研究者、8 张 RTX 4090 和自动化实验队列制定。
- 第二 SKU 必须在第 12 周前可重复使用，优先 H100，其次 A100/A800。
- 核心系统是单节点、单 GPU serving；不实现分布式集群调度。
- libsmctrl 是核心贡献依赖，因此有独立的早期硬 Gate。
- 模型和输入均离线，使用当前本地模型 revision。
- 显存始终是硬约束，不与 SM/HBM/PCIe 简单相加成一种货币。
- marginal externality 只用于当前动作排序，不永久计入公平服务。
- 论文表述是“deadline-aware virtual-service scheduling for malleable diffusion workloads”，不是“把 CFS 搬到 GPU”。

## 七、Decision Log

- 2026-07-30：锁定论文级系统目标、ASLE-first、16 周周期、libsmctrl 动态 SM 分区为核心贡献。
- 2026-07-30：锁定一名主力研究者、8×RTX 4090 主线，并要求第 12 周前获得第二 NVIDIA SKU。
- 2026-07-30：采用 dual-ledger；canonical service 负责公平，资源债务和 marginal externality 负责动作选择，真实 SLO 与显存保持独立。
- 2026-07-30：I/O 模型改为 resident/state-only/cold 三态；20 GB 不再作为任意轮转的固定 PCIe 开销。
- 2026-07-30：批准本计划并开始 Phase 0。
- 2026-07-30：Phase 0 smoke 的语义验收必须同时满足
  `runnable=true`、`n_urgent>=1`、`n_video>=1`；原 driver 的进程 exit code
  和 `runnable` 不足以证明两类请求都执行。
- 2026-07-30：ASLE 原 driver 将 arm setup 计入 arrival horizon；Phase 0
  保留其行为用于 baseline provenance，但任何 steady-state 性能比较必须将
  setup/warmup 与 service window 分离。
- 2026-07-30：同模式 correctness 使用 exact latent SHA 作为 Phase-0
  硬验收；跨模式只报告 fp64 数值差异，未预注册阈值前不得事后把差异解释为
  pass/fail。
- 2026-07-30：RTX 4090 上 libsmctrl 的控制粒度按 TPC（通常 2 SM）建模，
  不再使用“任意指定 SM”表述；每张卡必须用 SM-ID histogram 建立实际映射。
- 2026-07-30：本机 `cuDriverGetVersion=13030`；早期 GitHub fork 的
  stream offset 仅覆盖到 CUDA 12.2，作者 upstream 与 BulletServe 内嵌
  版本覆盖到 12.8；未知版本必须 fail closed，禁止把未生效 mask 误判为
  成功。
- 2026-07-30：以作者维护的
  `http://rtsrv.cs.unc.edu/cgit/cgit.cgi/libsmctrl.git` 为 Gate A upstream，
  固定 commit `c250928…fcb7` 为 immutable Git submodule；该版本的 x86
  stream switch 最高为 CUDA 12.8 (`12080`)，对本机 `13030` 仍不支持。
- 2026-07-30：阶段切换前新增 requirement-to-evidence alignment；允许
  `approximate` 只用于低风险并行预研，任何硬 Gate 和正式 claim 仍要求
  `exact`。
- 2026-07-30：Gate A runner 使用 GPU UUID 选择并反向核对设备；因主机
  存在多个 MPS control daemon，不能用“删除变量”证明隔离，改为 NVIDIA
  文档指定的空 `CUDA_MPS_PIPE_DIRECTORY` bypass，并记录 daemon。
- 2026-07-30：checked-in Gate-A manifest 是代码强制的 promotion lock；
  CLI experimental flags 不能单独启动 CUDA 13.3 masked probe。单次
  masked histogram 最多记为 `local_probe_passed`，只有跨 trial/bit、
  follow-up health 和 leakage validator 才能接收 cell/Gate。
- 2026-07-30：native probe 加入 parent-death guard 或其他安全语义后必须
  升级 schema 并形成新的 binary/build/source identity。`d967722` 的 15 个
  A0 cells 继续作为其旧 identity 的有效历史证据，但不得与新 identity 下
  GPU 0/5/6 的 cells 拼接成完整 8-GPU Gate；安全版本提交后应在同一精确
  identity 下重跑全部 8 卡 × 3 trials。
- 2026-07-30：任何 native parent-death 负向测试必须在
  `CUDA_VISIBLE_DEVICES=""` 且 MPS bypass 下执行，并且不得携带 unsupported
  driver override；README 不提供可绕过 promotion manifest、Xid monitor
  与独占检查的裸 masked 命令。
- 2026-07-30：纯 CPU simulator foundation 以 commit `82a27c4` 固化；
  canonical verifier 必须把 quantum-start state、resource-only
  intermediate 和完成 evidence 串成同一不可漂移语义链，已绑定的
  resource-decay policy 禁止在中间态更换。该提交不包含 trace scheduler，
  也不能替代 Gate A 或 Gate C 的真实验收。
- 2026-07-30：deterministic lifecycle trace foundation 以 commit
  `827beb8` 固化；canonical JSONL 解码/生成和 replay 均保持线性资源上界，
  `TraceReplayResult` 必须绑定原 `TraceDocument` 并在构造时复验完整
  event → delta → final-state 链。该提交尚不包含 action selector、
  predictor error 或 Gate C 正式实验。

## 八、Compaction Checkpoints

- 2026-07-30 / second post-`82a27c4` recovery：按协议分段完整重读当前
  753 行 `plan.md`，核对工作树、最近 8 个提交、最新实验产物、GPU
  占用、wheelhouse 与第二 SKU 预约线索。HEAD 仍为 `82a27c4`，工作树
  包含尚未提交的 trace、native safety、runner/NVML/v2 validator 候选
  实现；没有新增 raw run、aggregate 或真实 masked kernel。最新正式 GPU
  证据仍是旧 v1 identity 的 5 卡 15/15 unmasked subset 与两次
  `Popen` 前 sealed rejection，不能和未来 v2 identity 混合。GPU 0/6
  仍由 VLLM 占用，GPU 5 仍由 `.kvsim/bin/python` 占用；GPU
  1/2/3/4/7 虽空闲，但源码 dirty，禁止产生正式 GPU 证据。trace 的四项
  资源放大缺口已有候选修复，双解释器各 105/105，正在进行最终独立攻击
  复审；native 独立复审新发现 `/usr/bin/python3 -I` 仍会加载 system
  site/`.pth` 的高风险缺口，`-I -S` 修复与 fresh gate 已形成候选，尚待
  独立复核；runner 正在关闭 ambient executable/environment trust、
  formal environment capture 触碰目标 GPU、name-based `libcuda` 加载和
  final launcher-FD 绑定窗口，并同步采用 `-I -S`。三条线均须独立复审
  清零后分离提交；promotion lock 继续关闭。offline wheelhouse 仍只有
  `wheelhouse-cp312`，项目所需 wheelhouse 与第二 GPU SKU 预约证据仍
  missing，Phase 0 不关闭。

- 2026-07-30 / first post-`82a27c4` recovery：按协议分段完整重读当前
  736 行 `plan.md`，核对工作树、最近 8 个提交、最新实验产物和 GPU
  占用。HEAD 为 `82a27c4`；该提交仅固化已复审的纯 CPU dual-ledger
  simulator foundation。最新正式 GPU 证据仍是旧 v1 identity 的 5 卡
  15/15 unmasked subset 与两次 `Popen` 前 sealed rejection；没有新增
  raw run、aggregate 或真实 masked kernel。GPU 0/6 仍由 VLLM 占用，
  GPU 5 仍由 `.kvsim/bin/python` 占用；GPU 1/2/3/4/7 虽空闲，但 native、
  runner/NVML/v2 validator 和 deterministic trace replay 候选实现仍在
  dirty worktree 中，禁止据此产生正式证据。当前继续限定为纯 CPU 加固：
  trace 必须关闭 canonical byte-budget 与超长整数解析缺口；native 必须
  关闭 shell 初始化注入并保留真实 gate 完成 sentinel；runner 必须完成
  sealed post-build attestation 的 strict canonical 解析与全 lifecycle
  独立复审。各线只有在双解释器回归和独立攻击复审无阻断项后才可提交。
  checked-in promotion manifest 继续关闭，未来 v2 identity 不得与旧 v1
  evidence 混合。offline wheelhouse 与第二 GPU SKU 预约仍为 Phase 0
  外部 blocker。

- 2026-07-30 / fifth post-`d967722` recovery：按协议完整重读当前
  708 行 `plan.md`，核对工作树、最近 8 个提交、最新实验产物和 GPU
  占用。HEAD 仍为 `d967722`；最新正式证据仍是旧 v1 identity 的 5 卡
  15/15 unmasked subset 与两次 `Popen` 前 sealed rejection，没有新增
  raw run、aggregate 或真实 masked kernel。GPU 0/6 仍由 VLLM 占用，
  GPU 5 仍由 `.kvsim/bin/python` 占用；GPU 1/2/3/4/7 空闲但当前源码
  dirty，禁止据此产生正式证据。独立复审确认 simulator 只剩一个提交阻断
  项：已绑定的 resource-decay policy 可在 canonical intermediate 中漂移；
  native 仍需 fail-closed 拒绝 make dry-run/touch/ignore/question 模式并将
  parent-guard Python 测试纳入有限源码闭包；runner/NVML 仍需关闭
  pre-/post-spawn、信号、quarantine、v2 cross-binding、sealed NVML
  snapshot 和真实 supervisor 测试等复审项。上述三线继续只做纯 CPU
  修复、双解释器回归和独立复审；checked-in promotion manifest 保持关闭，
  新 v2 identity 只能在 clean commit 与 fresh attestation 后产生，且不得
  与旧 v1 evidence 混合。新出现的未跟踪零字节文件 `x` 暂不删除，先由
  native 测试作者确认归属。offline wheelhouse 与第二 GPU SKU 预约仍为
  Phase 0 外部 blocker。

- 2026-07-30 / fourth post-`d967722` recovery：按协议完整读取当前
  `plan.md`，核对工作树、最近 5 个提交和 `experiments` 中最新 run /
  aggregate。HEAD 仍为 `d967722`；最新正式证据仍是旧 v1 identity 的
  5 卡 15/15 unmasked subset，以及两次在 native `Popen` 前发生的
  sealed rejection，没有新增正式 run，也没有运行真实 masked kernel。
  当前未提交候选实现仍分为 simulator dual-ledger/lifecycle/schema、
  sealed-memfd native launcher/build attestation、runner 的 build/GPU
  lease、NVML/Xid quarantine 与有界 reap 三条线；本次恢复将继续完成
  主代理逐文件审查、独立复审和无 GPU 回归，发现项清零后才提交并生成
  新 v2 identity。checked-in promotion manifest 继续关闭，旧 v1
  evidence 不与未来 v2 evidence 混合。offline wheelhouse、第二 GPU SKU
  预约和 GPU 0/5/6 空闲窗口仍是 Phase 0 外部 blocker。

- 2026-07-30 / third post-`d967722` recovery：按协议完整读取当前 682 行
  `plan.md`，并核对工作树、最近 5 个提交、最新 raw runs 与 aggregate。
  HEAD 仍为 `d967722`；最新正式证据仍是旧 v1 identity 的 5 卡
  15/15 unmasked subset，以及两次在 `Popen` 前发生的 sealed rejection，
  本次恢复前后均未运行真实 masked kernel。当前未提交工作分为三条纯
  CPU/构建安全线：simulator dual-ledger 原子记账与生命周期语义、
  sealed-memfd native launcher/build attestation、runner 的 build/GPU
  锁、NVML/Xid quarantine 与安全 reap。三条线必须先完成逐文件主审、
  双解释器测试和独立复审，再形成新提交/新 identity；checked-in
  promotion manifest 保持关闭，旧 v1 A0 证据不得和未来 v2 证据混合。
  Phase 0 的 wheelhouse 离线重建、第二 GPU SKU、GPU 0/5/6 空闲预约仍是
  外部 blocker，不影响继续进行上述可逆 CPU 实现，但仍阻止阶段验收。

- 2026-07-30 / second post-`d967722` recovery：按协议重新完整读取当前
  668 行 `plan.md`，核对工作树、最近 8 个提交和最新 30 个实验产物。
  HEAD 仍为 `d967722`，最新正式证据仍是旧 v1 identity 的 15/15
  unmasked subset 与两次 `Popen` 前 sealed rejection；没有新的 masked
  kernel 运行。工作树中的 native launcher/parent guard、runner/NVML v2
  和纯 CPU simulator 仍未提交、未构成 Gate 晋级。两项独立只读复审已经
  确认 simulator 的局部公平公式与 PCIe 算术成立，但指出 backlog 生命周期、
  co-run 归因守恒、全体 tenant 债务更新、sub-ms decay remainder、continuation
  最后副本和 schema codec 六类集成缺口；runner 复审尚在完成中，已指出
  fake-Popen 测试可能误杀真实 PGID、formal source binding 未覆盖新 launcher
  构建链以及缺少 per-GPU lock。接下来先修复并双解释器验收这些阻塞项，
  再将 native launcher/attestation 接入 runner；promotion lock 继续关闭，
  不运行真实 mask，也不把旧 v1 A0 cells 与新安全 identity 混合。

- 2026-07-30 / post-`d967722` recovery：按恢复协议完整重读 649 行
  `plan.md`，核对 `git status`、最近 12 个 commit、最新 raw-run 目录和
  Gate A0 报告。HEAD 仍为 `d967722`，没有新的正式 masked 运行；最新两次
  masked 请求仍是 promotion-lock 在 `Popen` 前拒绝的
  `bs1-81ee41a6…b59` / `bs1-73dbd73a…27d`。GPU 0/5/6 的 A0 空缺、
  第二 SKU 预约和 offline wheelhouse 仍是硬 blocker。当前工作树中的
  native parent-death guard、NVML Xid/lifecycle 加固和纯 CPU simulator
  foundation 均是尚待主代理逐文件审查与完整测试的实现，不构成 Gate A
  晋级；继续保持 checked-in promotion lock 关闭，禁止实际 masked kernel。

- 2026-07-30 / post-`dd8c927` recovery：按恢复协议完整重读 613 行
  `plan.md`，检查 clean worktree、最近 commits 与 raw runs。确认正确的
  Gate A0 seed-1 批次采用“不同 GPU 可并行、同一 GPU 的 trial 0/1/2
  串行”，GPU 1/2/3/4/7 共 15/15 单次 accepted，均报告 4096 blocks、
  128/128 SM coverage、clean source/build/binary provenance 与 post-health
  通过。更早 seed-0 批次误把同卡 trials 并发，全部保留但必须由矩阵报告
  排除；GPU 0/5/6 因已有进程占用尚未运行。两次 masked 请求均在 native
  launch 前被 promotion manifest fail-closed 拒绝。当前只对已注册
  5-GPU subset 近似对齐，完整 8-GPU A0 与 masked Gate A 均未通过。

- 2026-07-30 / initial：计划首次落盘；Phase 0 开始，尚未产生 baseline run。
- 2026-07-30 / phase0-foundation：导入并验证 ASLE archive
  `5c6cba9d…f97d`，347 文件 tree hash `380cbf15…2afb`；选定临时
  `aris-vllm` 环境（Python 3.11.15、Torch 2.11.0+cu130、Diffusers
  0.38.0）；环境快照位于
  `experiments/environment/phase0_4090_20260730.json`；28 项测试通过。
- 2026-07-30 / first-gpu-smokes：`stepswap`
  `bs1-185ab7f…ab857` 通过（1 urgent/1 video，urgent 1.152 s，峰值
  7.6128 GB）；`offload_tiled` `bs1-7d5de5f…659b4` 虽进程成功但为
  1 urgent/0 video。根因是 arm 初始化计入 1 秒 horizon，触发 final-drain-only。
  默认 trace 改为 seed 1、horizon 10 s、lambda 0.1，仍确定只有一次 arrival；
  runner 新增语义验收，防止再次伪通过。
- 2026-07-30 / phase0-default-baselines：修正后 tiny runs：
  `stepswap bs1-7043686…c55a`（1 urgent/6 video）与
  `offload_tiled bs1-b279da9…3d44`（1 urgent/1 video）均通过。默认
  49-frame、30/8-step、120 s Poisson runs：
  seed 0 为 18 urgent/2 video、seed 1 为 22/2、seed 2 为 16/2，全部
  runnable 且峰值 7.6286 GB。seed 1 在 GPU2/GPU4 重复的 elapsed 为
  149.2/149.1 s，urgent p50 为 3.281/3.242 s，p99 为 6.186/6.306 s。
  聚合证据为 `experiments/aggregates/phase0_runs_20260730.json`；latent
  correctness 尚待专用 runner 验收，不能以 timing repeat 替代。
- 2026-07-30 / phase0-correctness：stock trials
  `bs1-23397e1…66b8`/`bs1-9f6d425…6840` 的 tensor SHA 均为
  `364b6fa3…a9d`；`offload_tiled` trials
  `bs1-67ddcaf…cfab3`/`bs1-17ac978…b69d` 的 tensor SHA 均为
  `18621be8…05a`，两个 same-mode comparison 均 `verdict=pass`。stock
  对 offload 的 fp64 `max_abs=0.021484375`、`mean_abs=0.0012742501`，
  按冻结规则为 `report_only`。刷新后的聚合包含 12 个 run：10 accepted、
  2 legacy completed、0 failed/incomplete。Phase 0 尚不能标记 accepted：
  最小 runtime lock 的落盘验证和第二 SKU 外部预约证据仍缺失。
- 2026-07-30 / phase0-runtime-lock：从已验证栈建立项目专用
  `/data/zhuoxu/miniconda3/envs/burstserve-phase0`，锁位于
  `environments/phase0/runtime-lock.json`；包含 25 个 exact conda URL
  和 8 个根包解析出的 52 个 pip distribution，文本锁 SHA 分别为
  `a4782192…1567`、`1f81112e…1a80`。专用环境 exact verification
  `matches=true`，系统/专用解释器均为 45 tests passed。使用 conda 锁从零
  创建 `burstserve-phase0-locked` base 已成功；pip 下载因当前链路仅约
  2–3 MB/s 中止，按既定方式由高带宽机生成 wheelhouse 后再做 offline
  reconstruction。Phase 0 仍因这项独立重建验证和第二 SKU 预约而
  `in_progress`。
- 2026-07-30 / phase0-dedicated-smoke：在 commit `e94474e` 的干净源码
  与专用 `burstserve-phase0` 环境上运行 stock trials 2/3：
  `bs1-26ed3d2…3fb3`（GPU3）和 `bs1-2289516…f334`（GPU4）。两者 tensor
  SHA 均为 `364b6fa3…a9d`，与原 `aris-vllm` stock correctness SHA
  相同，same-mode comparison `verdict=pass`。刷新聚合为 14 runs：
  12 accepted、2 legacy completed、0 failed/incomplete。
- 2026-07-30 / gate-a0-source-pin：论文作者 upstream 已固定为 submodule
  `vendor/libsmctrl@c250928…fcb7`，关键源码 SHA 与兼容性事实记录于
  `vendor/LIBSMCTRL_SOURCE.json`。本机 driver API `13030`，而 upstream
  stream cases 最高 `12080`；因此任何 masked probe 默认 fail closed。
  当前仅推进无 mask baseline 与隔离的 callback 语义探针，不直接猜测或写入
  CUDA 13.3 stream offset。
- 2026-07-30 / gate-a0-probe-implementation：实现 native `%smid` probe
  和 provenance-complete Python runner；native JSON 反向报告 GPU UUID，
  baseline 要求至少 75% SM coverage、计数总和等于 blocks、requested
  iterations 和硬件 manifest 一致。runner 用 UUID 设置
  `CUDA_VISIBLE_DEVICES`，以空 MPS pipe 明确 bypass，环境采集后再次检查
  GPU 占用，并在结束后检查 GPU 可访问性。Gate-A manifest 当前
  `experimental_mask_enabled=false`、无 approved mode/reserved UUID/
  stream candidate，因此所有 masked 模式仍被代码封死。native clean
  build 成功，系统和 `burstserve-phase0` 解释器各 64 tests passed；
  pre-commit GPU7 baseline 为 128/128 SM，只能算探索性证据。尚未运行
  post-commit formal baseline，也未运行任何 masked probe。
