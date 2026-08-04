# Burst 轮转与动态 SM 分区：16 周论文级推进计划

## 执行状态

- Last updated: 2026-08-03
- Current phase: **第 3–4 周——Gate B profiling（AMD 线）**;Gate A-AMD 已于
  2026-08-02 全条通过。
  **主推进平台自 2026-08-02 起转为 AMD**（`husrcf@X570`，Radeon AI PRO
  R9700 / gfx1201），NVIDIA 线的成果保留但不再是推进重心
- Current gate: **Gate B-AMD**(NVIDIA 侧仍停在 Gate A)。九条的当前状态
  (2026-08-03):
  **已通过 9 条**(证据 `gate_b_amd_verdict_final_20260803.json`,
  source revision `271e4426`,`source_revision_stable=True`):
  quota 列表与逐进程掩码;每 cell 记录饱和状态(8/8 canonical 全部 eligible);
  稳态 CV ≤5%(实测全部 **≤0.18%**,零加采样);held-out solo p50 MAPE
  **1.5–2.6%**;**transition prediction MAPE 7.75%**(87 个 step 样本,
  且 `steady_8`=`steady_32`=`switching` 三者 latent 校验和逐位相同);
  resident 轮转零权重流量(1/3/5 轮转 HtoD 拷贝次数恒为 1199,斜率精确为 0);
  每 profile 带硬件/驱动/ROCm/Torch/固件/模型 revision/schema 与掩码读回值;
  co-run 双进程不相交掩码,512² 与 1024² 两个规模均实测重叠 >99%,
  外部性稳定在 **23–24%**;source tree 未在扫描期间移动。
  **仅剩 1 条判负** — cold-model 预测 MAPE 17.4%(阈值 10%)。原因已定位:
  61% 的时长是框架开销而非传输;加入独立标定的框架项后总误差看似降到 3.8%,
  但逐项校验显示传输项高估 46%、框架项低估 68%,是**抵消出来的假准确**。
  **须先定条文意图再做,不得以调模型迎合阈值**(见 decision log 两条)。
- 历史:**NVIDIA 侧 TPC→SM 映射已取得正式证据并接受**
  （三模式 36 格，`gate_a_masked_all_modes_gpu1_attested_offset_20260802`）；
  **AMD 侧 CU 映射已取得正式证据并接受**（双机制 24 格，
  `amd_r9700_cu_mask_20260802`，sha256 `bfbeb8e3…8362`）。
  但 Gate A 的六条验收里两侧都只完成了「映射」这一条，其余四条未做
- Status: in_progress
- Last accepted evidence: AMD `amd-r9700-gfx1201-x570-20260802`，source
  revision `04bfc25b…`（干净），探针摘要 `d536d473…a6f1`；24 格全接受、
  0 拒绝，unmasked baseline 32 单元，判据为与 NVIDIA 线共用且未修改的
  `validate_masked_tpc_matrix`，15 项全 PASS。NVIDIA 侧：Gate A0 五卡
  15/15（`683c0ca1…8f0e`），masked 三模式 36 格接受，CUDA 13.3 stream
  offset `0x5fc` 经全 64 bit 双射验证
- Active blockers:
  1. **Gate A 六条验收只完成一条**（映射）。未做：10,000 次动态重配置、
     mask 更新 p99 ≤ 100 μs、masked/unmasked/co-run 确定性一致、
     profiler 可见真实 stream overlap、两 stream 互斥分区并发执行
  2. **AMD 映射仅抽样 4/32 bit**，与 NVIDIA 线当初 4/64 的过度外推是同一
     类问题，须扫满 32 bit 才能作全 die 主张
  3. 第二 GPU SKU 预约仍未关闭（Phase 0 硬门）。**已决定：R9700 不顶替该
     要求。** AMD 线是 NVIDIA 线的补充而非替代，因此 NVIDIA 侧的第二 SKU
     （H100 优先，A100/A800 备选）仍须独立覆盖，该硬门不因 AMD 线的进展
     而关闭
  4. offline wheelhouse 从零重建仍未完成（Phase 0 硬门）
  5. **已决定：AMD 线补充而非替代 NVIDIA 线。** 原 Gate A 条文（编队 5 张
     4090、TPC、native 更新）继续对 NVIDIA 侧完整有效且仍欠五条；AMD 侧
     另立平行的 Gate A-AMD 条文，两条线各自独立通过，互不顶替
- Next three actions:
  1. AMD 侧扫满 32 个 mask bit，取得全 die 映射的正式证据
  2. AMD 侧补齐 Gate A-AMD 剩余五条：并发互斥分区、10,000 次重配置、
     mask 更新延迟分布、masked/unmasked 确定性一致、可观测 overlap
  3. NVIDIA 侧第二 SKU 预约（H100 优先）——不因 AMD 线进展而放松
- Latest run IDs / commit: HEAD 见 git log；AMD baseline
  `bs1-5c2669b73ffd90727f08`，AMD 聚合
  `experiments/aggregates/amd_r9700_cu_mask_20260802.json`

### 当前阶段 requirement alignment（2026-07-30）

| Requirement | Implementation | Evidence | Alignment | Carry-over |
| --- | --- | --- | --- | --- |
| 固定并不可变导入 ASLE | `vendor_import.py`、`vendor/ASLE_SOURCE.json` | archive `5c6cba9d…f97d`；347-file tree `380cbf15…2afb` | exact | 持续禁止直接修改 `vendor/asle` |
| 4090 默认 49-frame、30/8-step、seed 0/1/2 | Phase-0 ASLE runner | `phase0_runs_20260730.json`；三次分别为 18/2、22/2、16/2 urgent/video | exact | 无 |
| 同 seed deterministic correctness | correctness runner | stock/offload same-mode tensor SHA 成对完全一致；专用环境复跑一致 | exact | cross-mode 数值差异继续仅 `report_only` |
| 隔离环境及 exact lock | `runtime_lock.py`、25 conda + 52 pip lock | 当前专用环境 verification `matches=true` | exact（现有环境） | 离线 wheelhouse 从零重建仍 missing，Phase 0 不关闭 |
| 第二 GPU SKU 预约 | 无 | 无 SKU、时间窗或预约证明 | missing | Phase 0 硬门；H100 优先，A100/A800 备选 |
| 编队 5 张 4090 的 A0 unmasked ×3 | native probe、Gate-A runner、独立聚合器 | `gate_a0_4090_v2_seed1_fleet5_20260731.json`：15/15 accepted、2 个 sealed rejection 有效、`gate_a0.complete=true`，report `683c0ca1…8f0e`，每格绑定 VBIOS/板厂/NUMA/功耗上限 | **exact** | 编队 2026-07-31 由 8 张缩为 5 张（见 Decision Log）；GPU 0/5/6 若释放可作编队外补充证据，但届时须整批重跑 |
| 未 promotion 的 masked 请求 fail closed | checked-in Gate-A manifest 与 runner | 两次 sealed rejection 均无 `Popen`/native output | exact（安全锁） | 只证明未越权，不证明 mask 可用 |
| 可观测 Xid 且异常时 fail closed | ctypes NVML event monitor 与 runner integration | 2026-08-01 起有真机覆盖：真实 `libnvidia-ml.so.610.43.02` 上 9 个必需符号全部绑定、sealed memfd 快照 sha256 与磁盘一致、supported bits `61852`、Xid bit `8` 注册成功、250 ms quiet window 实测 250.1 ms、0 事件、`safe_for_acceptance=true`；错误 hash/version 双向 fail-closed | approximate | 已修复 `nvmlDeviceGetHandleByUUID_v2` 不存在导致的必然失败；仍需真实 Xid 注入验证（无安全注入手段）与 post-health 覆盖后才 exact |
| 可逆 simulator foundation | `src/burstserve/sim` 的 schema、dual-ledger、lifecycle、三态 I/O 与 deterministic trace replay 纯函数 | commits `82a27c4`/`827beb8`；Python 3.11/3.13 各 107/107；trace 三轮独立资源/伪造攻击终审 | approximate（获准预研） | 动作枚举/选择、predictor error 与 Gate C 正式证据仍 missing |
| Gate A 动态 SM 功能与性能 | 尚未运行 masked kernel；masked 单 cell validator（`validate_masked_cell_contract`）与跨 trial/bit/mode 矩阵 validator（`validate_masked_tpc_matrix`）均已实现，纯函数、26 项对抗测试、端到端串联已验证 | 无 TPC map、10,000 次重配、更新 p99、overlap 或 correctness 证据；两个 validator 尚未接入 `validate_gate_a0`（无 masked 证据可消费） | missing | Gate A 硬门，不能由 A0 或 simulator 替代；仍缺 Xid 真实覆盖与 CUDA 13.3 stream offset 政策 |

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

- 编队内全部 5 张 4090（GPU 1/2/3/4/7）均能检测到合法 SM/TPC 拓扑。
  2026-07-31 由 8 张缩为 5 张，理由与代价见 Decision Log；GPU 0/5/6 长期
  被他人任务占用，不得以 busy override 顶替。
- probe kernel 100% 只落在指定 TPC mask 经该卡实测映射得到的 SM 集合。
- 10,000 次动态重配置无崩溃、越界或错误 mask。
- native 更新 p99 不超过 100 μs。
- deterministic correctness 不因 mask 或发射顺序破坏。
- profiler 中可观察到真实 stream overlap。

验收 Gate A-AMD（2026-08-02 新增，**补充而非替代上面的 Gate A**）：

单卡 `gfx1201`（Radeon AI PRO R9700，32 个可掩码单元），机制为
`hipExtStreamCreateWithCUMask` 与 `ROC_GLOBAL_CU_MASK`。上面的 Gate A 条文
以 NVIDIA 措辞写成，继续对 NVIDIA 侧完整有效；AMD 侧按下列平行条文验收，
两者各自独立通过，**任何一侧的通过都不顶替另一侧**，NVIDIA 的第二 GPU SKU
覆盖要求亦不因本条文而关闭。

- 全部 32 个 mask bit 均建立实测 `mask bit -> 单元` 映射，且跨两种机制一致。
  不得由抽样若干 bit 外推全 die——NVIDIA 侧曾以 4/64 抽样过度外推，已记录。
- probe kernel 100% 只落在该 bit 经实测映射得到的单元集合。
- 两个 CU-masked stream 在同一进程内互斥分区且并发执行。判据必须是
  「落在各自 oracle 预测的集合内」，**不得**用「两臂 SM 集合不相交」——
  NVIDIA 侧实测证明未加 mask 的对照组同样不相交。
- 10,000 次动态重配置无崩溃、越界或错误 mask；因 `hipExtStreamGetCUMask`
  可读回，每次重配置均须读回校验而非仅靠 kernel 行为间接推断。
- 记录 mask 配置延迟分布（p50/p99）。**语义与 NVIDIA 不同须如实写明**：
  HIP 在建流时确定掩码，而非就地更新已有 stream，因此「更新延迟」在两平台
  上不是同一件事，不得直接比较。
- deterministic correctness 不因 mask 或发射顺序破坏。
- 并发 overlap 可观测：以 kernel 内时钟记录各 block 的进入/退出时刻，
  证明两臂执行区间实际重叠。

若 08-12 未通过，允许使用第 3 周前三天修复；若 08-15 仍未通过，停止动态 SM 核心投稿路线，保留 temporal-only artifact，并明确标记 full-paper Gate A 失败，不使用 MPS 冒充等价结果。

### 第 3–4 周：2026-08-13 至 08-26——Profile 数据与模型

实现：

- Profile CogVideoX-2B、CogVideoX-5B、FLUX.1-dev 和 SDXL。
- 4090 SM quota 使用 `{16,32,48,64,80,96,112,128}`，记录实际 mask。
- 测试 `G={1,4,8,16}`、early/middle/late step phase、resident/state-only/cold residency。
- 加入 compute、HBM 和 PCIe probe co-runner。
- 常规 cell：5 次 warmup、30 次采样；尾部和切换关键 cell：至少 100 次采样。
  该数字尚未在本工作负载上验证：2026-08-01 筛查显示 22 请求的
  `urgent_p50` 单卡 CV 达 5.5%，故每个 cell 必须实测并记录达到的 CV，
  不满足 `CV ≤ 5%` 时提高采样量或改用 per-step / 遥测指标。
- 建立 p50 canonical table、p99 deadline table、pairwise externality table 和显存峰值表。
- 测量 pinned/pageable、local/remote NUMA、单向/双向 PCIe。

验收 Gate B：

- 稳态 solo cell 的 CV 不超过 5%。
- held-out solo p50 MAPE 不超过 10%。
- transition prediction MAPE 不超过 10%。
- resident 同模型轮转观测到零权重 PCIe 流量。
- cold-model 预测严格使用缺失字节和实测带宽。
- 每个 profile 都带硬件、驱动、CUDA、Torch、模型 revision 和 schema version。

验收 Gate B-AMD（2026-08-02 新增，**补充而非替代上面的 Gate B**）：

单卡 `gfx1201`（R9700，32 个可掩码单元）。上面的 Gate B 条文以 4090 的 SM
计数写成，继续对 NVIDIA 侧完整有效；AMD 侧按下列平行条文验收。

- quota 列表用 `{4,8,12,16,20,24,28,32}` **个可掩码单元**，不是 SM 计数。
  每个 cell 以独立进程加 `ROC_GLOBAL_CU_MASK` 固定配额——该路径已实测穿透
  PyTorch（2026-08-02），无需改动框架的 stream 管理。
- **每个 cell 必须记录自己是否处于饱和区**。2026-08-02 实测：负载填不满 die
  时 quota→吞吐**非单调**（1024 行时 16 单元达峰，32 单元反降至峰值 69%），
  4096 行以上才恢复接近线性。未标注饱和状态的 quota→latency 条目不得进入
  canonical table，否则调度器的单调假设会与数据冲突而无从判断是模型错还是
  测量错。
- 稳态 solo cell 的 CV 不超过 5%，且**必须记录实际达到的 CV 与加采样次数**，
  不得只声称满足 30 采样的约定。
- held-out solo p50 MAPE 不超过 10%；transition prediction MAPE 不超过 10%。
- resident 同模型轮转观测到零权重传输。**取证手段与 NVIDIA 侧不同**：R9700
  无可用硬件 PCIe 计数器（`rsmi_dev_pci_throughput_get` 返回 NOT_SUPPORTED，
  `--showmetrics` 中 PCIe 带宽项全为 N/A，2026-08-03 实测），故改用
  `rocprofv3 --memory-copy-trace` 按进程统计 HtoD 拷贝。判据不变，仍是
  「权重字节数为零」；copy trace 因归属到进程而**比整卡计数器更严格**。
- **cold-model 预测须分项各自达标，不得只看总误差**（2026-08-04 修订，
  **收紧而非放松**：原条文只要求一个总数落在 10% 内，现要求三项都落在
  10% 内）。用「同一尺寸序列的纯拷贝」(replay) 把观测拆成两半后，三项
  **各自**的 MAPE 都不得超过 10%：
  1. **传输项** 对照 replay 实测。仅用缺失字节与实测带宽，零拟合常数；
     带宽是传输尺寸的函数而非单一标量（实测跨 1400 倍，见 decision log）。
  2. **框架项** 对照 `observed − replay`。须**独立标定**（在与目标模型无关的
     负载上测 μs/张量），不得由 `observed − transfer` 反推——那是用观测预测
     观测的循环论证。
  3. **总和** 对照 `observed`，即调度器实际承担的墙钟时间。
  修订理由：原条文措辞隐含「传输主导」的假设，该假设已被数据证伪——
  diffusion pipeline 的 6.94 GB 摊在 2643 个中位仅 2.6 KB 的张量上，
  **61% 的加载时长是框架开销而非传输**，任何「字节 ÷ 带宽」形式都无法表达它。
  且 2026-08-03 实测证明只看总数会被**抵消误差**骗过：传输项高估 46%、
  框架项低估 68%，总误差却只有 3.8%。
  分项的另一个收益：传输项是**硬件属性**（换软件栈仍成立），框架项是
  **实现属性**（可被预分配 / 批量传输 / CUDA graph 优化掉）。把两者分开
  记录，才能说出「这张卡的 cold start 物理下界是 X，当前实现是 Y」这种
  对调度与优化都有用的结论；揉进一个总数就看不见了。
- 每个 profile 带硬件、驱动、ROCm、Torch、模型 revision 与 schema version；
  **掩码须同时记录请求值与 `hipExtStreamGetCUMask` 读回值**（NVIDIA 侧无此项）。
- co-run cell 用两个各带不相交掩码的进程，而非单进程多 stream。**必须证明
  两进程确有时间重叠**：不重叠的两次运行会给出与「零外部性」完全相同的数字。
  取证方式为 warmup 后 barrier 同步、按固定墙钟时长而非固定样本数采样，且
  只统计完全落在双方共同采样窗口内的样本；跨窗口边界的样本一律丢弃而不按
  比例折算——折算会把外部性稀释向零，即让结果好看的方向。

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
- 在 4 张 4090（GPU 1/2/3/4）上运行矩阵；漂移参照为 GPU 4 上周期重跑的
  固定 reference cell；GPU 7 为纯备用。2026-07-31 由 6+1+1 缩为 4+1，
  2026-08-01 按实测调整角色（见 Decision Log）。
- 任何 arm 之间的比较必须在同一张卡上配对：实测卡间系统偏差最大 3.49%。
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
- 复验跨板厂等价性：技嘉卡（GPU 1/3）热余量比公版少 8.2 °C，长压测下
  可能先热降频；若失效，profile 与主张必须按板厂分组重述。
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

- 排期按一名主力研究者、5 张 RTX 4090（4 张矩阵 + 1 张备用/clean control）
  和自动化实验队列制定；2026-07-31 由 8 张缩为 5 张。
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
- 2026-07-30：隔离 raw-byte Git provenance 以 commit `3820a98`
  固化；formal source 不再允许调用 repo-local clean filter、attributes、
  hook 或 fsmonitor 来证明 clean。完整对象内容、raw worktree、index 与
  gitlink 均受有限资源和 A/B 稳定性约束；allowlist 仍是显式 blind spot，
  formal caller 必须保留 protected roots，并对 in-tree build 产物实施
  独立、精确、有限的 attestation exception。
- 2026-07-30：formal ASLE archive policy 固定要求精确 regular file、
  mode `0644`、size `31479528` 和 SHA256
  `5c6cba9d…f97d`；现场归档从 `0664` 收紧为 `0644`，前后 inode、size
  与内容摘要不变。该权限修正只关闭必然 fail-closed 的本地前置条件，不构成
  fresh build 或正式 evidence。
- 2026-07-30：为外部 agent 连续执行建立英文临时交接文档
  `CLAUDE_HANDOFF.md`；接手者必须先启动 `achieve goal`，按 native →
  Gate → 双解释器/fresh CPU gate → 独立复审/提交/clean v2 identity 的
  固定顺序持续推进，不得因状态汇报、单次测试失败或提交完成而停止。
- 2026-08-01：**跨板厂等价性筛查完成：可当同质用，但 profile 必须按单卡建**。
  设计：2 板厂 × 2 卡 × 4 次重复 = 16 run，按重复次序交错以免机器漂移与
  板厂对齐；固定 seed 1 的同一到达轨迹、49 帧、30/8 步、120 s；全程 1 Hz
  采样 SM 时钟/温度/功耗。结果（`board_equivalence_screen_20260801.json`，
  sha256 `9d5e2b60…765d`）：
  - **端到端 elapsed 技嘉 147.90 s vs 公版 147.90 s，差 0.00%**，16 run
    合并 CV 0.46%、极差 2.0 s。
  - **板厂不是解释变量，单卡个体才是**：GPU 1/2/3 三张卡（跨板厂）稳态
    时钟极差仅 0.19%，而 GPU 4 比这三张低 1.16%；同板厂内最大差
    （GPU 2 vs GPU 4，均公版）+1.22%，反而**大于**跨板厂差 +0.54%；剔除
    GPU 4 后跨板厂差降至 −0.07%。GPU 4 的特征是吃更多电（393.5 W vs
    385–387 W）跑更低时钟，属芯片体质差异。
  - **绑定约束是两批卡完全相同的 450 W 功耗墙**：16 个 run 全部
    `sw_power_cap=Active`、`hw/sw_thermal_slowdown=Not Active`，峰值功耗
    445.7–453.9 W。散热器差异只体现在结温。
  - **温度是唯一稳定的板厂签名**：技嘉 73.22 °C vs 公版 65.04 °C，差
    8.2 °C（目标温度规格 84 °C，当前两批都未降频）。
  结论与约束：
  1. **【2026-08-01 当日更正，见下一条】** 原写「五张编队卡可视为性能等价」
     属越界：该结论只有 GPU 1/2/3/4 的数据，补测 GPU 7 后被推翻。仍然成立
     的是：**profile 表必须按单卡（physical_gpu + GPU UUID）建，不得按板厂
     分组** —— 最大系统性偏差来自个体卡而非板厂。
  2. **任何 arm 之间的比较必须在同一张卡上配对**，或在 arm 之间随机化卡
     分配。1.2% 的单卡系统偏差不会随 seed 平均掉，而论文最小的 claim 阈值
     是 5%（无 burst 吞吐下降）。
  3. 该等价性**仅在功耗墙是绑定约束、且两批卡都未热降频时成立**。技嘉卡
     热余量少 8.2 °C，第 7–8 周的一小时压测与第 11–12 周的长 burst 压测
     必须重新验证；届时若技嘉先热降频，等价性即失效。
  4. 未覆盖：GPU 0/5/6 从未在本负载下测过（被他人长任务占用）；GPU 7 只有
     Gate A0 的 2.3 秒探针，**没有**本负载数据。因此不能声称「8 张卡都
     差不多」，只能声称「已测的 GPU 1/2/3/4 在本负载下等价」。
- 2026-08-01：**Xid 监视器真机覆盖，并修复一个会让 masked Gate A 永远无法
  进行的必然失败**。此前 `tests/test_nvml_events.py` 的**每一个**测试都跑在
  本仓库自己写的 `FakeNvml` 上，唯一的 layout 测试也只比对常量——监视器从未
  接触过真实的 `libnvidia-ml.so.1`。
  - **缺陷**：`_REQUIRED_SYMBOLS` 要求 `nvmlDeviceGetHandleByUUID_v2`，而
    **NVML 从未发布过该符号的 _v2 变体**。真实库（driver 610.43.02）导出的是
    `nvmlDeviceGetHandleByUUID` 与 `nvmlDeviceGetHandleByUUIDV`。其余 8 个符号
    均存在，只有这一个错。后果：监视器在真机上**必然** `NvmlLibraryError`，
    而 masked cell 要求监视器干净 —— 即 **masked Gate A 在修复前不可能通过**。
  - **为什么一直没发现**：plan.md 记录的那次「GPU7 只读注册 smoke，supported
    bits 61852、Xid bit 8」是用**裸 ctypes** 做的，绕过了被测代码；我用真实库
    复现得到同样的 61852 与 bit 8，证实该 smoke 从未经过监视器路径。
  - 更糟的是有一个测试把错误**锁死**了：`test_legacy_uuid_symbol_is_not_an_
    accepted_fallback` 断言普通的 `nvmlDeviceGetHandleByUUID` 必须被拒绝为
    "legacy fallback" —— 但它才是唯一正确的 API。该测试的**意图**（不接受
    静默降级到弱符号）是对的，**前提**是错的。已改为
    `test_only_the_documented_uuid_symbol_is_accepted`，改为拒绝
    `nvmlDeviceGetHandleByUUIDV`（它接收 `nvmlUUID_t` 结构体而非 `char *`，
    绑定它会在 ABI 边界造成类型混淆）与不存在的 `_v2`。
  - **新增真机覆盖**（无 GPU/无库时干净 skip，不破坏干净 checkout）：
    (1) `test_real_library_exports_every_required_symbol` —— 只需库文件即可
    运行，是今天这个 bug 的直接回归测试；(2) 真机注册与 drain：9 个符号全部
    解析为驱动导出的名字、sealed memfd 快照 sha256 与磁盘一致、载入路径为
    `/proc/self/fd/`、supported bits `61852`、Xid bit 注册为 `8`、250 ms quiet
    window 实测 250.1 ms、final zero poll 执行、0 事件、`safe_for_acceptance
    = true`；(3) 错误 hash 与错误 version 双向 fail-closed。
    已验证这两个测试对修复前的代码确实失败（一 failure 一 error）。
  - 附带澄清（非缺陷）：`safe_for_acceptance` 属性要求 `_closed`，故在
    `with` 块内必为 False；runner 读的是 close 后写出的 provenance 记录。
  - 仍未 exact：**没有安全的真实 Xid 注入手段**，因此「观测到 Xid 时 fail
    closed」这条路径在真机上仍只有模拟覆盖；post-health 覆盖亦未完成。
- 2026-08-01：**masked 单 cell validator 落地，与矩阵 validator 串联**。
  `validate_masked_cell_contract` 校验 masked 模式独有的那半个契约，并把通过
  的 cell 转成矩阵 validator 所需的观测记录 —— 即**一个 cell 只能经由单 cell
  检查才能进入矩阵**，两者串联在测试中已端到端验证（36 格 → 正确 TPC→SM 映射）。
  关键约束（均来自 producer 实测，非臆测）：
  - **masked run 在构造上永远不可能被接受**：runner 的
    `accepted = local_probe_passed and mode == "baseline" and not allow_busy_gpu`，
    因此 masked 最多得到 `local_probe_passed=True` + `accepted=False`；
    validator 精确要求这个组合，并要求 `requires_matrix_validation=True`
    不可被豁免。这把 7/30 冻结的「单次 masked histogram 最多记为
    local_probe_passed」从文字变成了代码。
  - **未经 promotion 的 manifest 不能产出 cell**：要求
    `safety.experimental_mask_enabled is True` 且 mode 在 `approved_mask_modes`
    内——否则该 run 是 sealed rejection 而非 cell。这条使 masked cell 与
    sealed rejection 在验证层面互斥，无法混淆。
  - **masked 必须持有覆盖整个运行窗口的预约**：final preflight 的
    `required_horizon_s` 必须为正 float 且 `required_until > captured_at`，
    launch-commit 与 post-health 的 reservation 均须 `required_for_mode=True`
    且 checks 全真（baseline 恰恰相反，是精确的零预约契约）。
  - **parent-death guard 强制**：native `parent_guard` 必须
    `mode=linux_pdeathsig_sigkill`、`status=armed`、两个死亡信号均为 9、
    expected/observed PID 为同一正整数。
  - **Xid 监视器必须干净**：`masked_health_monitor_status="clean"`、记录键精确、
    checks 全真；`xid_observed`/`monitor_failed` 一律拒绝。
  - native 报告的 `requested_enabled_tpc` 必须等于 config 的 bit、`tpc_count`
    等于声明的 die；argv 必须请求同一个 bit；子进程环境必须携带
    `BURSTSERVE_PARENT_PID`，且 `MASK_OFF` **当且仅当** stream 模式存在。
  12 项对抗测试覆盖：未 promotion 的 manifest、未批准的模式、伪称 accepted、
  豁免矩阵校验、六种 parent guard 破坏、五种预约缺失、四种监视器不洁、
  native 与 config 的 bit 不符、argv/环境不匹配（含 MASK_OFF 双向错配）、
  矩阵外的 bit/trial/blocks，以及 36 格端到端串联。
  两个 validator 仍**未接入 `validate_gate_a0`**（无 masked 证据可消费）；
  promotion lock 完全关闭，实测三种 masked 模式 `permitted=False`。
- 2026-08-01：**masked 跨 trial/bit/mode 矩阵 validator 落地（纯 CPU，先于任何
  masked 证据）**。此前 `gate_a_results.py` 只有 baseline 与 sealed rejection
  两个验证器，**没有任何 masked 验证路径** —— 而 decision log 早已规定「单次
  masked histogram 最多记为 `local_probe_passed`，只有跨 trial/bit 与 leakage
  validator 才能接收 cell/Gate」，该 validator 一直缺位。
  核心论点：**单次 masked histogram 不构成证据** —— 它呈现的映射正是由它自己
  观测出来的，无法被自身证伪。Gate A 的主张是「请求某个 TPC bit 会把 kernel
  限制在该 bit 所指的 SM 集合」，其证据必然是横截面的。`validate_masked_tpc_matrix`
  因此检查：
  1. **跨 trial 确定性** —— 同一 bit 每次必须给出完全相同的 SM 集合；
  2. **跨机制一致性** —— global/next/stream 三种掩码机制对同一 bit 必须给出
     相同集合，因为 TPC→SM 映射是硅片属性而非机制属性；
  3. **跨 bit 不相交** —— 不同 bit 的集合必须互不重叠，这是「bit 在做选择而非
     仅仅允许」的唯一可得证据（即 leakage 检查）；
  4. **与硅片一致** —— 每个集合的大小须恰为 `sm_count/expected_tpc_count`
     （本机 128/64 = 2），且所有 SM id 落在设备范围内；
  5. **相对 unmasked baseline 确实收窄** —— 掩码集合必须显著小于同卡 baseline
     的覆盖（128），否则掩码根本没有起作用；
  6. 矩阵完整性（3 模式 × 4 bit × 3 trial = 36 格，无缺无多无重复）、单一 GPU
     身份、block 总数与精确 JSON 类型。
  实现为**纯函数**，消费已验证的单 cell 观测，因此可以在任何 masked kernel
  获准运行之前就完成实现、测试与复审 —— 这正是 promotion lock 关闭期间应该
  做的工作。14 项对抗测试覆盖：非确定性单次漂移、模式间分歧、bit 间重叠、
  掩码覆盖全片、缺失 baseline 参照、矩阵残缺/多余/重复、越界 SM id、
  半个 TPC、混合 GPU 身份、block 总数篡改、五种类型替换、matrix/硅片声明畸形。
  **尚未接入 `validate_gate_a0`**：还没有 masked 证据可消费，且接入前需要先写
  masked 单 cell validator（对应 baseline 的 `_validate_baseline_run_v2`）。
  promotion lock 保持完全关闭。
- 2026-08-01（补测 GPU 7 后更正）：**编队并非同质，5 张卡里有 2 张离群；
  板厂等价结论仍成立**。补测编队最后一张无负载数据的卡（GPU 7，同样 4 次
  重复、同一配置），结果推翻了当日早前基于 4 张卡的「等价」判断。完整
  20-run 报告：`board_equivalence_screen_fleet5_20260801.json`，sha256
  `ff1bcfe6…da7d`（取代仅含 4 卡的 `board_equivalence_screen_20260801.json`）。
  - 稳态 SM 时钟：GPU 1 2659.6 / GPU 2 2664.0 / GPU 3 2664.7（主群，极差
    0.19%）；**GPU 4 2631.9（−1.16%）；GPU 7 2569.9（−3.49%）**。
  - GPU 7 的端到端 elapsed 152.28 s，比主群均值慢 **+2.96%**；全 20 run
    的 CV 由 0.46% 升到 1.37%、极差由 2.0 s 升到 8.0 s。
  - 诊断为**芯片体质而非主机侧瓶颈**：GPU 7 的峰值时钟只有 2610 MHz（其余
    四张 2700–2730），只有 7.5% 的时间超过 2600 MHz（其余 74–92%）；同时
    util 85.9%、峰值功耗 450.8 W 与其他卡一致，排除了「等主机数据」。
  - **板厂等价结论反而更强**：GPU 2 与 GPU 7 的板级身份完全相同（同 vbios、
    同 subsystem、同功耗限制、同最大时钟、同 PCIe），稳态时钟却差 3.7%。
  - 影响评估：2.96% 的端到端差距相对论文最小 claim 阈值（无 burst 吞吐下降
    ≤ 5%）已占 59% 的边距，**不可忽略**；处理方式仍是同卡配对，而非放宽阈值。
- 2026-08-01：**编队角色按实测重新分配**（原「4 矩阵 + 1 备用/clean control」
  中 GPU 7 承担 control 的安排作废）。
  - 旧值：矩阵 GPU 1/2/3/4；GPU 7 兼作备用与 clean control。
  - 新值：矩阵 GPU 1/2/3/4 不变；**漂移参照改为在 GPU 4 上周期性重跑一个
    固定 reference cell**；GPU 7 降为**纯备用**，仅在矩阵卡不可用时顶替。
  - 原因：clean control 的作用是探测机器级漂移，看重的是**稳定性**而非
    绝对速度。GPU 4 是全场最稳（elapsed CV 0.22%、时钟 CV 0.09%），GPU 7
    最不稳（1.28% / 0.20%）且慢 3.49%，用它当参照会把自身噪声当成漂移。
  - 代价（明确接受）：**放弃「一张常年不跑实验的干净卡」**——编队只有 5 张，
    要保 4 张矩阵卡就必然没有多余的空闲卡。漂移参照因此不是「未被触碰的
    卡」，而是「同一张卡上重复的同一个 cell」，该性质必须在论文中如实描述。
  - 若 GPU 7 真被启用顶替，其 −3.49% 偏差由已冻结的「同卡配对」规则兜住，
    但必须在该批次 manifest 中显式标注。
- 2026-08-01：**`urgent_p50_s` 不得用作 profile 或等价性指标（现有采样量下）**。
  实测单卡内部 CV 高达 5.5%（GPU 1 四次：3.216/2.878/2.903/2.894 s），
  **大于任何组间效应**；而同批数据里稳态 SM 时钟的 CV 只有 0.05–0.09%。
  Gate B 要求「稳态 solo cell 的 CV 不超过 5%」，按 22 个请求的 p50 计算
  达不到该要求。第 3–4 周的 profile 采样因此必须：改用 per-step 计时或
  遥测量作为主指标，或把每 cell 的采样量提高到使 p50 的 CV ≤ 5% 为止，
  并在 manifest 中记录实际达到的 CV。原「常规 cell：5 warmup + 30 采样」
  的数字在本工作负载下未经验证，不得直接沿用。
- 2026-07-31：**每次 formal run 增记并绑定 GPU 板级身份**。本机 8 张 4090
  实为两种板卡：GPU 0/1/3 为技嘉（VBIOS `95.02.18.C0.8B`，功耗上限 479 W），
  其余为 NVIDIA 公版（`95.02.3C.40.40`，上限 450 W）；同一颗 AD102，故
  SM/TPC 拓扑一致，A0 测量不受影响（15 格全部 128/128 SM、直方图相同）。
  但持续负载下的散热与 boost 行为可能不同，而矩阵四张卡（1/2/3/4）恰好
  横跨两种板卡。原 `query_gpu` 只采集 index/name/uuid/bus/显存/利用率/驱动，
  **板级差异完全无记录**，一条 `nvidia-smi -pl` 就能静默改变某张卡的持续
  时钟并污染跨卡复用的 profile。现每次 run 记录 VBIOS、板卡 subsystem ID、
  NUMA 节点、当前/默认/最大功耗限制、SM 与显存最高频率、PCIe 上限，聚合器
  以精确键与类型校验并绑定到 `run.preflight`；且要求**当前功耗限制必须等于
  厂商默认值**，被调高或调低即在 preflight fail-closed。附带查明：8 张卡各
  挂一个独立 NUMA 节点、卡间全为 `SYS` 路径、链路空闲降为 Gen1 加载后回到
  Gen4——这些对 I/O 模型的实测 H2D/D2H 带宽有直接影响，已记录，但 NUMA
  绑定测量属 Gate B 范围，本次未做。证据已在新 identity 下整批重采：
  15 cells + 2 sealed rejections，spec `1fcbdd5f…7dbe`、aggregate input
  `bad63883…c4e5`、report `683c0ca1…8f0e`。
- 2026-07-31：**主线 GPU 编队由 8 张 4090 缩为 5 张（范围变更，非降低门槛）**。
  - 旧值：排期与验收按本机全部 8 张 4090；第 13–14 周 6 张跑矩阵 + 1 张
    开发复查 + 1 张 clean control；Gate A/A0 要求 8 张全部通过；
    `REQUIRED_GATE_A0_GPU_COUNT = 8`。
  - 新值：编队为 GPU 1/2/3/4/7 共 5 张——4 张（1/2/3/4）跑矩阵，1 张
    （7）兼作开发复查与 clean control；Gate A/A0 要求这 5 张全部通过；
    v2 程序使用 `REQUIRED_GATE_A0_GPU_COUNT_V2 = 5`。
  - 原因：GPU 0/6 被用户 `yitong` 的 VLLM 双卡任务占用（已 2 天以上），
    GPU 5 被用户 `rlwu` 的 kvsim 占用（已 8 天以上，且处于 T/stopped 状态
    仍持有显存）。三者均为他人长任务、ETA 未知，本项目无权处置；而正式
    证据禁止 busy override。继续把 8 张写进验收，等于把一个我们无法控制
    的外部条件写成硬门。
  - 代价（已接受并如实记录）：第 13–14 周主矩阵可用卡由 6 张降为 4 张，
    该阶段 wall-clock 预计约 1.5–2 倍；论文中的硬件规模表述必须写实际的
    5 张（4+1），不得再写 8×RTX 4090。
  - 不变的部分：验收阈值本身没有放松——编队内每张卡仍需 3 次 trial 全部
    accepted，仍禁止 busy override，仍禁止 v1/v2 identity 混拼。两个 VBIOS
    批次仍都被覆盖（A 批 479W：GPU 1、3；B 批 450W：GPU 2、4、7）。
  - 实现约束：`REQUIRED_GATE_A0_GPU_COUNT = 8` 保持冻结，因为它已写入
    已发布的 v1 报告，该报告仍须逐字节可重建；v2 由独立常量决定，且判据
    由「恰好等于」改为「不少于」，多声明仍合法。
    `experiments/manifests/gate_a_4090.json` 的 `physical_gpu_indices`
    **故意保持 [0..7] 不变**：该字段嵌入每次 run 的 gate manifest 并参与
    formal identity，一旦更改，已接受的 15 个 cell 将无法再与后续 run 拼接。
  - Carry-over：GPU 0/5/6 一旦释放，可作为超出编队的补充证据补测；但因
    HEAD 此后已推进，补测须在届时的统一 identity 下重跑全部 cell。
- 2026-07-30：CLAUDE_HANDOFF 的 "empty compute/MPS lists" 要求按以下方式
  落实并记录偏离：preflight、final launch preflight 与 post-health 的
  **compute 进程列表必须为空**；host MPS daemon 列表按 2026-07-30 已冻结
  的决定继续作为 provenance 记录，只校验记录结构精确（`pid`/`command`/
  `arguments`）并要求 `empty_mps_pipe_bypass_exact`、
  `host_mps_state_recorded_after_probe` 与 `mps_bypass` 精确记录为真，
  **不要求 MPS 列表为空**。理由：dd 常驻十余个
  `nvidia-cuda-mps-control -d`，要求 MPS 列表为空将使任何真实证据永远
  无法通过，并与"不以 daemon 缺席证明隔离、改用 NVIDIA 文档的空
  `CUDA_MPS_PIPE_DIRECTORY` bypass 并记录 daemon"的冻结决定直接冲突。
  该偏离只放宽 provenance 记录，不放宽任何安全判定。
- 2026-07-30：native attestation 的 TCB 文档按实现如实收敛：`/proc`
  的 `cmdline`/`environ`/`stat` 只做有界单次快照读取，README 不再声称
  会检测"变化中的记录"，并显式说明进程可自行改写自己的 argv/environ；
  `make --eval`/`-E` 明确记为在 parse guard 运行之前就已被 GNU make
  展开，属 clean-entry 契约之外，只能由 formal runner 自行生成固定
  argv 关闭，guard 的拒绝只能把已执行的注入转成硬 parse error。

## 八、Compaction Checkpoints

- 2026-07-31 / 编队缩为 5 张，Gate A0 通过（HEAD 见本条提交）：用户在核对
  「是否真的需要 8 张卡」后决定把主线编队由 8 张缩为 5 张。追溯发现 8 这个
  数字并非 Gate A0 的内在要求，而是 2026-07-30 排期假设「按 8×RTX 4090 制定」
  推导出来的：论文主矩阵原定 6+1+1 用满 8 张，故 A0 需覆盖 8 张。GPU 0/6 被
  `yitong` 的 VLLM、GPU 5 被 `rlwu` 的 kvsim（T/stopped 但持有 4.8 GB 显存）
  长期占用，ETA 未知且无权处置，因此把 8 张写进验收等于把不可控外部条件写成
  硬门。按范围变更处理：编队 = GPU 1/2/3/4/7（4 张矩阵 + 1 张备用/clean
  control），论文规模表述同步改为 5 张，代价是第 13–14 周 wall-clock 约
  1.5–2 倍。**这不是降低阈值**：编队内每卡仍需 3 次全 accepted、仍禁 busy
  override、仍禁 v1/v2 拼接；两个 VBIOS 批次（479W 的 GPU 1/3、450W 的
  GPU 2/4/7）仍都覆盖。实现上 `REQUIRED_GATE_A0_GPU_COUNT = 8` 冻结不动
  （它已写入已发布 v1 报告，该报告仍逐字节可重建，`aggregate_input_sha256`
  仍为 `08ef4053…a188`），v2 改用 `REQUIRED_GATE_A0_GPU_COUNT_V2 = 5`，判据
  由「恰好等于」改为「不少于」；`gate_a_4090.json` 的 `physical_gpu_indices`
  故意保留 [0..7]，因为它参与 formal identity，改了就再也无法扩展已接受的
  15 个 cell。结果：`gate_a0_4090_v2_seed1_fleet5_20260731.json` 判定
  **`gate_a0.complete = true`**，5/5 卡、15/15 cells、2 个 sealed rejection
  有效，evidence spec `655ceaa1…8b52`、aggregate input `956488e0…679d`、
  report `683c0ca1…8f0e`。上一版 8 卡口径的 partial 报告
  （`…partial_20260730.json`，report `dfa9a93c…64d8`）保留不删，其对应 spec
  可在 commit `faa8056` 取回。两套解释器各 305 tests 通过。Carry-over：
  GPU 0/5/6 释放后可作编队外补充证据，但 HEAD 此后已推进，届时须整批重跑。

- 2026-07-31 / v2 identity 下首批被接受的 Gate A0 证据（HEAD `faa8056`）：
  按 plan.md 的 next action ① 执行。**前置条件核实**：源码树原本不
  formal-clean——raw Git scanner 刻意不读 `.gitignore`，5 个
  `__pycache__`（约 90 个 stale `.pyc`）与 `CLAUDE_HANDOFF.md` 都算额外
  未跟踪项，`formal_git_build_exception_paths_exact` 因此为 false；清理后
  `formal_source_tree_policy_clean=True`、零失败检查。**GPU 前提只满足
  5/8 且不是「等一会」能解决的**：GPU 0/6 是用户 `yitong` 的 VLLM 双卡任务
  （当时已跑 1 天 19 小时），GPU 5 是用户 `rlwu` 的 kvsim（已跑 8 天 5
  小时），均为他人多日长任务，不得干预、ETA 未知。经用户批准后在空闲的
  GPU 1/2/3/4/7 上执行。
  **真机首跑连续暴露四个只有真实运行才会出现的缺陷，全部已修并加回归**：
  (1) runner 把 GPU lease 目录建在 build lock 目录内部，创建子目录使父
  目录 `nlink` 由 2 变 3，而 build attestation 把 `build_lock.directory_nlink`
  当作被证明的构建身份——**第一次 formal run 就在自己的 preflight 之前
  永久作废了自己的 attestation**，此后每次 verify 必然失败（`f42b538`）。
  (2) source identity 快照里含 pinned verifier 的 `/proc/self/fd/<n>`，
  该编号在 prelaunch 与 preexec 之间必然变化（期间又开了监控与 launcher
  描述符），使 pre-exec 重校验**永远不可能匹配**（`5219113`）。
  (3) manifest 的 `runtime_api_version` 写成 13000，实测为 13030；用独立
  链接 `/usr/local/cuda-13.3` 的 `libcudart` 的程序验证 `cudaRuntimeGetVersion=13030`
  （toolkit cudart 13.3.29），确认是 manifest 数据错误而非环境不符（`cb05d02`）。
  (4) v2 sealed rejection 的期望 argv 未包含 producer 在
  `experimental_allow_unsupported_driver` 为真时追加的 `--allow-unsupported-driver`，
  使最强形式的拒绝证据反被判为不精确（`c8fe949`）。(1)(2) 与前三轮复审
  发现的 B1/B1-residual 属同一族：**把瞬时运行状态烘进要求稳定的身份**，
  或**把期望收得比 producer 真实输出更窄**。
  另外确认一条操作规律：`source_revision` 的 raw 标签覆盖三个允许根之外的
  任何未跟踪文件，因此 **evidence spec 与 aggregate 只能在全部 run 完成之后
  再写**，否则后续 run 的 identity 会与前面的错开（已实际踩到两次）。
  **结果**：GPU 1/2/3/4/7 × trial 0/1/2 共 15 个 cell 全部 accepted，
  每个 4096 blocks / 128 of 128 SM / coverage 1.0 / 31 项语义检查全真 /
  三次 source 重校验全匹配 / post-health 全真 / stderr 空 / 15 个共享同一
  formal identity 与同一 runner schema；另有 2 个 sealed rejection
  （global 与 stream，均显式 opt-in 不受支持的驱动，因而唯一可能拦下它们的
  就是 checked-in promotion manifest），二者均无 `native.json`、事件恰为
  `run.preflight,run.rejected`、stdout 为空、exit 4、`process_exit_code`
  为 null，**未启动任何子进程、未执行任何 masked kernel**。
  evidence spec `ad5f5f5f…e474`、aggregate input `0c2e5ab9…b0de`、
  report `dfa9a93c…64d8`，落盘于
  `experiments/aggregates/gate_a0_4090_v2_seed1_partial_20260730.json`。
  **Gate A0 仍未通过**：报告本身判定 `complete=false`，GPU 0/5/6 各缺 3 次；
  未使用 busy override；旧 15 个 v1 cells 未与本批拼接。两套解释器各
  304 tests 通过；旧 v1 报告的 `aggregate_input_sha256` 仍为
  `08ef4053…a188`。早期四次失败 run 全部保留为原始证据，未被 spec 引用
  （source_revision 不同，自动落选）。

- 2026-07-30 / handoff step 1–4 完成，七批已提交（HEAD `5bcc55f`）：
  在下一条 checkpoint 的实现基础上，完成了三轮独立对抗式复审、修复、
  分批提交与 clean-commit 重建。**三轮共六个复审代理，发现并关闭 6 个
  BLOCKING**，其中五个是「同类问题第一次没修干净」：
  (1) R1 native：`/proc/<pid>/stat` 整体按 strict ASCII 解码，而内核把
  `comm` 按原始字节输出，导致进程名含非 ASCII 字节的**存活**组成员被当作
  「已退出」跳过——这是证明「组已空」的那个函数本身 fail-open。
  (2) R2 native：同类残留——线程组 leader 线程先退出时 `/proc/<tgid>/stat`
  报 `Z`，但其余线程仍在运行，`state != "Z"` 的跳过同样把存活组报告为空。
  (3) R1 Gate：`_validate_sealed_rejection_v2` 要求 `manifest_policy` 的
  false 检查**恰好**两项，但对着仓库里真实的 closed manifest 实测产生 20 项，
  且 `selected_is_v2` 会在缺少合法 v2 rejection 时强制 `rejections_valid=false`
  ——即**任何真实 v2 矩阵都永远无法完成 Gate A0**。
  (4) R2 Gate：同类残留——放宽后的 `masked_` 前缀族漏掉 stream 模式的四个
  offset 授权门与 `runner_masked_health_monitor_is_implemented`，而现有两个
  真实 sealed rejection 恰好是 global + stream。
  (5) R1 Gate：v2 基线路径**从不校验** `command.environment_overrides`
  ——`_validate_baseline_command`（含 `CUDA_MPS_PIPE_DIRECTORY == ""`、
  `CUDA_VISIBLE_DEVICES == uuid`、`MASK_OFF is None`）只在 v1 被调用，
  v2 仅查键名且该字段不在必需键内。这直接掏空了 MPS 偏离的补偿控制。
  (6) R1 Gate：`_validate_source_revalidation` 等多处仍用宽松 `!=`，
  `1` 可冒充 `True`。
  两个 Gate 阻断之所以被测试漏掉，根因同为 **fixture 被弯折以迎合校验器**：
  rejection 的 `manifest_policy` 被硬编码成校验器想要的两键形状，baseline
  的 `command.json` 只写了 producer 13 个键中的 6 个。两个 fixture 已按
  producer 真实输出重写（新增 `_child_environment`、`_closed_manifest_policy`，
  rejection 也写全 13 键并按 mode 参数化）。R3 两个领域均**零阻断**：Gate
  侧以 345,600 状态扫描确认授权族在「太窄」方向已完整、两个真实 rejection
  全部落在族内；native 侧确认剩余唯一遗漏模式需要「扫描期间并发 clone」，
  而扫描前强制的组 SIGKILL 使其不可达。R3 的非阻断项也已修：授权族排除
  `masked_threads_are_native_canonical`（它是 manifest 正确性检查而非授权门）、
  libcuda identity 绝对类型钉死、`native_build.found` 精确校验、
  `_thread_group_has_live_thread` 改为要求两次连续全死快照，并把 docstring 与
  README 的说法收敛为「先 SIGKILL 再扫描」这一真实保证，不再声称扫描本身
  自洽。双解释器各 301 tests 通过（provenance 14 / git_provenance 18 /
  nvml_events 24 / environment 8 / smctrl_runner 139（21 项既有 legacy
  fake-Popen skip）/ gate_a_results 46 / native_parent_guard 52）。
  旧 v1 报告在全部改动后仍逐字节相同（`f35164ab…22bf`，input
  `08ef4053…a188`），并已加回归测试锁定。fresh CPU-only
  `gate-required-check` 在提交后重跑通过，inventory 精确为 9 个文件 +
  3 个 `0700` 目录。clean commit 上已填入 fresh v2 artifact pin，但
  promotion lock 保持完全关闭，三种 masked 模式实测 `permitted=False`。
  全程未运行任何 GPU kernel，未运行 masked 模式，未产生新的正式 GPU 证据。

- 2026-07-30 / handoff step 1–3 完成（post-`3820a98`，仍未提交）：按
  `CLAUDE_HANDOFF.md` 逐项推进。开工前逐个核对交接文档的 8 个哈希，全部
  一致（`smctrl_runner.py` `5fa395b0…18a4`、`environment.py`
  `41ed0687…05c7e`、`Makefile` `869473d5…a9ba5`、`build_attestation.py`
  `bfbeeb12…fb7bd`、`test_native_parent_guard.py` `0543a375…98cca`、
  `gate_a_results.py` `577616fd…1ef87`、`test_gate_a_results.py`
  `fe568438…97f1c`、native `README.md` `85d46ae4…3bbda9`），HEAD 仍为
  `3820a98`。**Native 五项**已全部关闭：(1) `run_command()` 现在在直接
  子进程正常退出的路径上也 `killpg` 整个 session/进程组并有界验证组内无
  可执行成员（僵尸不计），新增"关闭全部管道后仍存活的孙进程"正向测试与
  killpg 被拦截时的有界 fail-closed 负向测试；(2) `verify` 改为 canonical
  精确字节 + 递归精确 JSON 类型双重比对（`exact_json_equal`），并加入
  `true→1` 端到端替换拒绝测试；(3) JSON node 上界移到解析前的字符串/
  转义感知词法预算，>100k 扁平数组在建图前即被拒（以 patch `json.loads`
  证明），且 object key 不计入节点数，不会误拒合法文档；(4) README 写明
  `make --eval/-E` 在 guard 之前展开、属 clean-entry 契约之外，须由 runner
  生成固定 argv；(5) 删除 `/proc` "stability-checked" 的不实声明。
  **Gate v2 七项**已全部关闭：evidence-spec ↔ baseline cell ↔ sealed
  rejection ↔ report 四者 schema 双向配对，rejection schema 纳入 report
  版本选择，交接文档指定的完整攻击（8 个 valid v1 cells + v1 spec + 完全
  合法的 v2 rejection）已验证仅被新配对检查阻断（该 rejection 的唯一错误
  即 schema pairing，8 cells 全 valid，`gate_a0.complete=false`）；v2
  config/outcome/native/sealed-rejection 全部改为精确 JSON 类型比较，
  bool/int/float 互换一律拒绝；`run.started.final_launch_preflight`
  已与 final-preflight 事件和 outcome 三方 canonical 绑定；baseline
  final-preflight 与 preflight/post-health 的完整契约（精确键、canonical
  timestamp、float `0.0` horizon、`required_until==captured_at`、精确 GPU
  记录、空 compute 列表、null error、精确 check 名集合与全真、
  reservation 记录）全部校验；事件顺序仍以 sequence 为逻辑序，未引入
  wall-clock 推断。旧 v1 报告保持逐字节不变，重算 SHA256 仍为
  `f35164ab…22bf`、aggregate input 仍为 `08ef4053…a188`，并已加入回归
  测试锁定。两套解释器各 293 tests 通过（provenance 14 / git_provenance 18
  / nvml_events 24 / environment 8 / smctrl_runner 139（21 项既有 legacy
  fake-Popen skip）/ gate_a_results 42 / native_parent_guard 48）。
  fresh CPU-only `CUDA_VISIBLE_DEVICES='' make gate-required-check` 通过，
  内层 48/48，末行 sentinel 正确；最终 inventory 精确为 9 个文件
  （0400/0500，全部 euid 所有）+ 3 个 0700 目录（`build`、
  `build/smctrl_probe`、`build/smctrl_probe/tmp`），无 object、archive、
  legacy lock、stale tmp leaf、pyc 或任何多余文件，私有锁为 0700 目录 +
  0600 文件。未运行任何 GPU kernel，未运行 masked 模式。checked-in
  promotion manifest 仍完全关闭（`experimental_mask_enabled=false`、
  approved modes/reserved UUIDs 为空、reservation 为 null、四个 approval
  pin 仍为全零占位），本次 fresh build 的 identity 未写入 pin。GPU 0/6
  仍由 VLLM 各占 20,650 MiB，GPU 5 仍由 `.kvsim/bin/python` 占 4,838 MiB。
  下一步：独立攻击复审 → 分批提交 → clean commit 上重建并生成 v2
  attestation/pins。当前批次未复审、未提交，不得据此生成正式证据。

- 2026-07-30 / fourth post-`3a256e4`, first post-`3820a98` recovery：
  按恢复协议重新逐段完整读取当前 861 行 `plan.md`，并核对 dirty
  worktree、最近 8 个提交、最新 run / aggregate、GPU 占用、wheelhouse、
  第二 SKU 线索和三个并行任务。HEAD 为 `3820a98`；隔离 raw-byte Git
  provenance 已在该提交验收，系统与专用解释器各 18/18，并经独立攻击
  复审判定无模块级提交阻断项。其 runner/environment/native caller 接入、
  exact build exception、ASLE archive binding 和严格 source-metadata
  contract 仍属于当前 dirty 候选，尚未冻结或形成正式 identity。没有新增
  raw run、aggregate、正式 GPU 证据或 masked kernel；最新正式 GPU 证据
  仍为旧 v1 identity 的 GPU 1/2/3/4/7 各 3 次、15/15 unmasked subset，
  aggregate input SHA256 仍为 `08ef4053…a188`，最近两次运行仍是
  `Popen` 前 sealed rejection。GPU 0/6 各由 VLLM 占用 20,650 MiB，
  GPU 5 由 `.kvsim/bin/python` 占用 4,838 MiB；GPU 1/2/3/4/7 虽仅
  2 MiB，但源码 dirty，禁止生成正式证据。项目 wheelhouse 仍未回传，
  仅发现外部 67 MiB
  `/data/zhuoxu/INA-opt/downloads/wheelhouse-cp312`；第二 GPU SKU
  仍无预约文件、SKU、时间窗或外部证明。runner 候选已接入 safe Git、
  `-I -S -B`、ASLE 精确 pin、canonical build inventory 与目录身份约束，
  但完整 `LIBSMCTRL_SOURCE.json` 类型/摘要/commit 绑定、policy
  adversarial table、native error-histogram 语义、dead legacy 清理和双
  解释器全回归仍未关闭；native I/O 子任务正在对 build attestation 实施
  有界 `O_NOFOLLOW|O_NONBLOCK` 读取与有界 subprocess，尚未冻结。
  Gate v2 frozen-FD/canonical-parser 候选此前两套解释器各 34/34，
  但仍未纳入 runner 新增的
  `launch_commit_reservation_revalidation`，因此仅为 provisional，须在
  runner 冻结后同步 schema、重跑历史 v1 哈希和独立复审。恢复后的
  read-only 复审还确认 raw `source_revision()` 会因精确 build exception
  与 pinned `ASLE.tar.gz` 仍出现在 untracked 集中而标成 `+dirty-`，旧
  `source_tree_is_clean_commit` 检查会错误拒绝所有真实 formal launch；
  当前修复必须保留 raw 标签，同时只以完整 raw Git、零 tracked drift 和
  精确 ASLE/build exception 决定 formal eligibility，并加入 extra-untracked
  负向测试。promotion
  manifest 继续关闭，不运行 GPU kernel/mask，不使用 busy override，
  旧 v1 与未来 v2 identity 继续严格隔离。

- 2026-07-30 / third post-`3a256e4` recovery：按恢复协议逐段完整重读当前
  `plan.md`，并重新核对 dirty worktree、最近 8 个提交、最新 run /
  aggregate、GPU 占用、wheelhouse、第二 SKU 预约线索和并行任务。HEAD
  仍为 `3a256e4`；没有新增 raw run、aggregate、正式 GPU 证据或真实
  masked kernel，最新正式 GPU 证据仍是旧 v1 identity 的 5 卡 15/15
  unmasked subset，aggregate input SHA256 仍为 `08ef4053…a188`，且
  两个最新 run 仍是 `Popen` 前 sealed rejection。GPU 0/6 分别由
  VLLM 占用 20,650 MiB，GPU 5 由 `.kvsim/bin/python` 占用
  4,838 MiB；GPU 1/2/3/4/7 仅有 2 MiB，但源码 dirty，禁止生成正式
  证据。项目 wheelhouse 仍未回传，只发现外部 67 MiB
  `/data/zhuoxu/INA-opt/downloads/wheelhouse-cp312`；第二 GPU SKU
  仍无预约文件、SKU、时间窗或外部证明。Gate v2 frozen-FD /
  canonical-parser 候选在冻结版本上已由两套解释器各 34/34 通过，独立
  复审当时无提交阻断项，但 runner 随后新增
  `launch_commit_reservation_revalidation`，因此 Gate exact schema 必须
  在 runner 最终冻结后重新对齐、双解释器回归和独立复审，当前只算
  provisional。隔离 Git provenance 首轮 12/12 结论已被独立攻击复审
  推翻：loose-ref fallback、FIFO metadata hang、缺失对象未验证、派生
  prefix 无界、annotated-tag HEAD、ref grammar、allowlist 隐藏可执行
  内容和 policy 输入无界共 8 类缺口正在修复；未经再次独立复审不得提交。
  runner/native 候选已覆盖 durable masked poison、launch-commit
  reservation horizon、signal terminal downgrade、目录 fsync 故障和
  有界 Xid drain，但 strict canonical parsers、安全 Git 调用方接入、
  native 有界 `O_NONBLOCK|O_NOFOLLOW` 读取、TCB 文档及 runner↔Gate
  schema 同步仍未关闭。三个子任务继续分别处理 Git provenance、
  runner/native 实现和只读 contract 复审；promotion manifest 保持关闭，
  不运行 GPU kernel/mask，旧 v1 与未来 v2 identity 继续严格隔离。

- 2026-07-30 / second post-`3a256e4` recovery：按协议分段完整重读当前
  `plan.md`，并重新核对工作树、最近 8 个提交、最新 run/aggregate、GPU
  占用、wheelhouse、第二 SKU 预约线索与并行实现状态。HEAD 仍为
  `3a256e4`；没有新增 raw run、aggregate、正式 GPU 证据或真实 masked
  kernel，最新正式证据仍是旧 v1 identity 的 5 卡 15/15 unmasked subset
  与两次 `Popen` 前 sealed rejection。GPU 0/6 仍由 VLLM 占用，GPU 5
  仍由 `.kvsim/bin/python` 占用；GPU 1/2/3/4/7 虽空闲，但源码 dirty，
  禁止生成正式证据。项目 wheelhouse 仍缺失，仅有外部 67 MiB
  `wheelhouse-cp312`；第二 GPU SKU 仍无预约证明。Gate v2 frozen-FD /
  canonical parser 候选曾达双解释器各 28/28，但独立复审新发现 oversized
  sparse file 在状态登记顺序及 v1 legacy reopen 路径仍可绕过读取上限，
  且 public `validate_gate_a0` 未复用严格 spec normalization，因此尚未
  验收。runner 已有 baseline 无预约 preflight、NVML 最终零时轮询、
  2 秒总 drain 上限和 first-Xid fail-fast 候选修复；完整 reservation
  lifecycle、terminal/signal 原子降级及 masked 子进程异常退出后的 durable
  poison 仍须关闭。隔离 Git provenance 模块首轮 12/12 CPU 测试通过，
  正在收紧 ignored-root allowlist，尚未接入 runner、environment 和 native
  attestation。所有候选必须完成资源上限回归、双解释器测试和独立复审后才
  可提交；promotion manifest 继续关闭，旧 v1 与未来 v2 identity 不混合。

- 2026-07-30 / first post-`3a256e4` recovery：按协议分段完整重读当前
  783 行 `plan.md`，核对工作树、最近 8 个提交、最新实验产物、GPU
  占用、wheelhouse、第二 SKU 预约线索与并行任务。HEAD 为 `3a256e4`；
  `827beb8` 的 deterministic trace 已验收并提交，当前 dirty worktree
  仅包含尚未验收的 native parent/sealed-exec/build-attestation 与
  runner/NVML/v2 aggregate 候选。没有新增 raw run、aggregate 或真实
  masked kernel；最新正式 GPU 证据仍是旧 v1 identity 的 5 卡 15/15
  unmasked subset 与两次 `Popen` 前 sealed rejection。GPU 0/6 仍由
  VLLM 占用，GPU 5 仍由 `.kvsim/bin/python` 占用，GPU 1/2/3/4/7
  空闲但源码 dirty，禁止生成正式 GPU 证据。项目 wheelhouse 仍缺失，
  仅发现外部 67 MiB `wheelhouse-cp312`；第二 GPU SKU 仍无预约证明。
  当前三条并行工作分别负责 runner/native 实现、repo-local Git
  provenance 设计与 Gate v2 单次快照/canonical parser 复核；必须关闭
  Git clean-filter 派生执行、aggregate hash→path 重读 TOCTOU，并记录
  `make --eval` immediate expansion 及 root-owned Python/stdlib/loader/
  libc TCB 边界，独立复审清零后方可提交。promotion manifest 继续关闭，
  不运行 GPU kernel/mask，也不把旧 v1 evidence 与未来 v2 identity 混合。

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
- 2026-08-01 / cuda133-mask-mechanism-probe：在 GPU 0（非编队卡、探测时
  无他人进程）上执行 stream-offset 探测方案的 Phase 0，结论是**整个
  stream offset 探测不必要**，Phase 1 的盲写扫描取消。
  事实一：`global` 与 `next` 两种 masking 在本机 CUDA 13.3 / driver API
  `13030` 上**可用**。它们不写不透明结构体，而是经 `cuGetExportTable`
  订阅 QMD `PRE_UPLOAD` 调试回调改写 launch descriptor，所以不受 upstream
  stream offset 表（最高 `12080`）的版本限制；`driver_is_validated_by_upstream`
  对 `13030` 无条目一事只反映 upstream 未在此版本验证过，不代表机制失效。
  事实二：oracle 矩阵 `{global,next}` × TPC bit `{0,31,32,63}` × 3 trials
  共 24 格全部成功，`validate_masked_tpc_matrix` 14 项检查全 PASS、
  `accepted=true`，映射为 bit N → SM `{2N, 2N+1}`（64 TPC × 2 SM = 128 SM，
  与 AD102 拓扑一致），unmasked baseline 同卡 128/128 SM。
  事实三：`g_next_sm_mask` 是 `__thread` 线程局部变量，两个 host 线程各设
  自己的 next-mask 并发提交，实测得到并发的、不相交的 SM 分区：urgent 臂
  bits 0..7 → SM 0..15，video 臂 bits 32..39 → SM 64..79，时间窗完全重叠
  150 ms，3 次重复完全一致。即 `next` 已能提供 per-stream masking 所要的
  并发空间分区能力，无需任何盲写。
  对照与自我纠正：**不加 mask 的对照组两臂同样不相交**（各 64 个 SM，分别
  占据全部偶数 / 奇数 SM），因为块调度器把两个并发 kernel 平分到整块 die。
  所以"SM 集合不相交"本身不是 masking 生效的证据；判别证据是"被约束到
  oracle 预测的那一组 SM"。测试的判据已相应改为与 oracle 逐一比对，对照组
  在该判据下正确判为 FAILED。
  已知边界：`global`/`next` 的 mask 是 `uint64`，只能覆盖 TPC bit 0..63。
  RTX 4090 恰为 64 TPC 故全覆盖；若将来上 >64 TPC 的卡（如 132 SM / 66 TPC
  的 H100），bit 64+ 只能经 `uint128` 的 stream ext API 触及，届时 stream
  offset 问题会重新出现。
  边界与状态：本次全程未做任何盲写、未出现 Xid、未修改 promotion lock、
  未产生任何正式证据。所有运行在 scratchpad 的隔离子进程中进行，未写入
  `experiments/runs`。promotion lock 仍完全关闭，masked 正式证据仍未授权。
  原始数据：scratchpad `streamoff/{phase0_raw.json,phase0_verdict.json,
  concurrency_test.cu}`。
- 2026-08-01 / masked-promotion-manifest：为 masked Gate A 准备晋级 manifest
  与配套门禁修正，**未运行任何 masked kernel**。
  分离式晋级：新建 `experiments/manifests/gate_a_4090_masked_global_next.json`
  （`manifest_id` = `gate-a-4090-cuda133-20260801-promoted-global-next-gpu1`），
  原 `gate_a_4090.json` 一字未动。理由是 manifest 内容参与形式化身份派生，
  就地改会改变未来 run 的 run_id；而已接受的 A0 证据把 manifest 内容逐 run
  内嵌、SHA256 对内嵌副本校验，故分离后两边都保持可验证，两条 checked-in
  sealed rejection 仍绑定在未晋级 manifest 上继续有效。
  卡的选择：GPU 1（UUID `GPU-4cc58bdd…abfb`）。它在 A0 已接受的编队
  {1,2,3,4,7} 内、当前空闲、且不是漂移参照卡 GPU 4；masked 矩阵测的是 SM
  身份而非性能，per-card 硅片差异与之无关。
  Xid 监视器身份实测固化：在 GPU 0 上真机运行 `NvmlXidMonitor`，
  `libnvidia-ml.so.610.43.02`、SHA256 `2dc828b3…c172`、NVML 版本
  `13.610.43.02`、method `nvmlEventSetWait_v2_exact_xid`、Xid bit（8）
  受支持且注册成功、drain 干净 `safe_for_acceptance=true`、observed quiet
  1001 ms。四个 source approval pin 与当前构建产物逐一比对完全一致。
  stream 永久封死：`approved_mask_modes` 仅 `["global","next"]`，
  `stream_offset_search_enabled` 与 `global_next_matrix_accepted` 均保持
  false。实测该 manifest 下 stream 被 5 条独立检查拒绝
  （`masked_mode_approved`、`masked_mode_is_registered_in_single_tpc_matrix`、
  `stream_offset_is_declared`、`stream_offset_search_promoted`、
  `stream_prerequisites_accepted`），global/next `permitted=True`。
  代码修正（收紧而非放宽）：`single_tpc_matrix_modes_are_canonical` 原先
  硬编码要求矩阵模式恰为 `["global","next","stream"]`，导致合法的
  driver-specific 子集连同 baseline 一起被拒。改为「必须是
  `CANONICAL_MASKED_MODE_ORDER` 的规范序子集」，并新增
  `single_tpc_matrix_modes_cover_approved_modes` 要求
  `approved_mask_modes ⊆ matrix modes`——否则某模式可被批准放行而矩阵里
  根本没有它的 cell，跨模式一致性这条证伪依据会静默消失。乱序、重复、
  未知名、空列表仍全部拒绝。新测试对修改前的代码为 2 failures + 3 errors，
  修改后通过；全量 474 tests OK（21 skipped）。未晋级 manifest 行为逐模式
  复核不变：baseline 放行、三种 masked 分别 20/20/24 项检查失败。
  仍未满足的前置条件：`exclusive_reservation_evidence` 的时间窗
  （`2026-08-01T15:42:52Z` → `2026-08-02T00:00:00Z`）必须在正式运行前刷新并
  以干净提交状态存在——`load_gate_manifest_record` 要求 manifest 已提交且
  head/index/worktree 三者一致，且预约时窗参与形式化身份，因此 24 格矩阵
  必须落在同一个预约窗内完成，否则各 cell 形式化身份不一致。
- 2026-08-01 / next-mask-refuted-for-serving：**撤回 2026-08-01
  `cuda133-mask-mechanism-probe` 条目里的结论 (D)「stream offset 探测不必要」。
  该结论是错的。** 独立复审提出三条失效路径，我用自己重写的测试
  （scratchpad `streamoff/next_limits.cu`，未复用复审的探针以免共用同一个 bug）
  在 GPU 0 上全部复现：
  (1) **CUDA graph 击穿 `next`**：把 4 个 kernel capture 成 graph、设一次
  next-mask 再 `cudaGraphLaunch`，只有 1 个节点被 mask，而且是**节点 3**
  （执行序最后一个），无法预测落在哪个节点。vLLM / TensorRT-LLM 的 decode
  路径都是 graph capture 的，因此 `next` 在真实服务系统里不可用。
  (2) **mask 被窃取**：设好 mask 后，中间插入的任何一次 launch 会把它吃掉；
  实测中间那个 kernel 拿到 SM {6,7}，本来的目标 kernel 全裸跑 32 SM。真实
  引擎里 cuBLAS/cuDNN/NCCL 内部发射就坐在这个缝里。
  (3) **陈旧 mask 泄漏**：设了但未被消费的 mask 会静默作用到该线程之后一次
  **无关**的 launch（实测拿到 SM {40,41}）。这是 ambient authority，
  per-stream mask 没有这个失效模式。
  修正后的准确表述：`global`/`next` 对**我们自己完全掌控每一次 launch 的
  受控 microbenchmark**（即 masked Gate A 的 TPC→SM 映射矩阵）是充分的；
  对**真实 serving 系统**不充分。因此 masked Gate A 可以按 global/next 推进，
  而 BurstServe 服务路径的空间分区仍然需要 per-stream masking——CUDA 13.3
  stream offset 问题重新打开，先前「Phase 1 取消」的决定作废。
  同时纠正两处我的过度表述：(a) 4 个 bit 不足以支撑「bit N → SM {2N,2N+1}」
  这个全 die 结论，复审补跑了全部 64 个 bit（128/128 格、并集恰为 {0..127}）
  才使该结论成立；(b) 我说「validate_masked_tpc_matrix 全 PASS」时暗示了
  该 validator 认可这个映射，**它不认可**——它检查确定性、跨模式一致、
  两两不相交、基数与 in-range，但从不检验映射本身，喂给它编造的
  bit0→{100,101} 同样会 accepted。`phase0_verdict.json` 里的
  `tpc_sm_mapping` 是输入的回显，不是发现。
  另记两个尚未处理的隐患：libsmctrl 的 `setup_sm_control_callback()` 用
  `__atomic_test_and_set` 选举安装线程，但**落败线程立即返回而不等待
  subscribe/enable 完成**，该窗口内的 launch 静默无 mask（复审在拷贝副本里
  插 50 ms sleep 证明窗口真实存在；实机 25/25 未触发，说明窗口亚微秒级）。
  任何多线程用法必须先由单线程调用一次 `libsmctrl_set_global_mask(0)`
  强制安装完成。以及 H100（132 SM / 66 TPC）上 `uint64` 不够用，且 Hopper
  分支会无条件把 `tmd+312/316` 写 -1 永久禁用 TPC 64..127，libsmctrl 自己
  也打印 "untested on Hopper"。
- 2026-08-01 / masked-gate-review-fixes：两轮对抗性复审对 commit `d5d4e59`
  提出的缺陷已全部修复，全量 477 tests OK（21 skipped）。
  R1-SEVERE-1（已复现）：把 `single_tpc_matrix_modes_are_canonical` 放宽成
  「规范序子集」后没有基数下限，**单模式矩阵被放行**（实测
  `permitted=True, failing=[]`），而 `validate_masked_tpc_matrix` 的
  `consistent_across_modes` 在单模式下是空真（实测 `accepted=True`）。
  我的补偿检查堵的是另一个方向的洞（批准了但矩阵没有），提交信息因此
  过度声称。修复：runner 增 `single_tpc_matrix_declares_at_least_two_modes`；
  validator 的 `shape_ok` 增 modes≥2 / bits≥2 / trials≥2 下限并在模式数<2 时
  显式让 `consistent_across_modes` 失败。
  R1-SEVERE-2（已复现）：**stream-only 矩阵被放行**，原先的精确三元组 pin
  隐含「promote stream 就必须同时声明 global+next」这一结构耦合被我删掉了，
  而 `stream_prerequisites_accepted` 只读同一文件里自声明的布尔量，替代不了
  它。修复：增 `single_tpc_matrix_corroborates_stream_with_callback_modes`，
  声明 stream 必须同时声明 global 与 next。
  R1-MINOR-5：矩阵模式多于已批准模式时 runner 全过但 `matrix_complete`
  永不可满足，代价是把 run 跑完才发现。修复：增
  `single_tpc_matrix_modes_match_approved_modes_when_promoted`，仅在
  `experimental_mask_enabled` 为真时要求两者相等，未晋级 manifest 不受影响。
  R1-MODERATE-3：我的负例表里 `repeated`/`unknown`/`empty` 其实由既有的
  `single_tpc_matrix_modes_are_valid_unique_strings` 拦下，删掉新表达式它们
  照样被拒——测试在实现出错的那条轴（基数）上恰好没有覆盖。已删除充数用例，
  补上基数与 stream 耦合的真实负例。
  R2-2/R2-3：`validate_masked_tpc_matrix` 接受 1×1×1 退化矩阵
  （`disjoint_across_bits` 因内层循环从不执行而空真），且
  `restricts_relative_to_unmasked_baseline` 近乎空转——它只比较基数、
  baseline 是未校验的调用方整数、不绑定 GPU、无子集检验，因此「masked 2 个 SM
  对 baseline 3 个 SM」算作限制成功。修复：新增
  `baseline_is_from_the_same_gpu`，要求传入 baseline 的**实际 SM 集合**与
  UUID，并把判据改成**真子集**（mask 若把 kernel 挪到 unmasked 从未触及的 SM
  上，不算限制而算搬家），同时校验传入基数与集合自洽。
  R1-MINOR-4 记录不修：`safety.unknown_driver_policy` 在 `src/` 中从未被读取，
  是死字段；且 driver 13030 不在 `PINNED_VALIDATED_DRIVER_VERSIONS` 内，
  masked 运行必须显式带 `--experimental-allow-unsupported-driver`，与
  manifest 文本声明的 fail_closed 姿态存在措辞落差。
- 2026-08-01 / cuda133-stream-offset-discovered：按「接受版本锁定 hack 的代价」
  的决定执行 stream offset 逆向，**在 GPU 0（非编队卡、全程无他人进程）上完成
  Phase 1 扫描与 Phase 2 证伪**。原始记录固化于
  `experiments/probes/cuda133-stream-offset/`（非正式证据，无 provenance
  绑定，不授权任何东西）。
  结论：CUDA 13.3 的 `CUstream` 结构里 per-stream SM mask 位于**偏移
  `0x5fc`**，即相对 libsmctrl 的 `CU_12_2_MASK_OFF`（`0x4e4`）基址
  `MASK_OFF=+280`。upstream 已验证表最高到 CUDA 12.8 的 `0x4fc`。
  `280 % 8 == 0`，满足既有的 `stream_offset_is_8byte_aligned` 门；总偏移
  `0x5fc % 8 == 4`，与全部已知 x86_64 偏移（`0x49c/0x4ac/0x4cc/0x4e4/0x4ec/
  0x4fc`）同余，未出现新的对齐族。
  Phase 1（`phase1_sweep.json`，sha256 `b5b8c03a…8dae`）：delta −256…+512
  步长 4，每次尝试在短命子进程中隔离，NVML Xid 监视器在子进程**存活期间**
  保持打开（事后再注册会漏掉故障事件）。188 次尝试 → 命中 1、静默空转 141、
  SIGSEGV 43、超时 2、Xid 1。**`MASK_OFF=+492`（`0x6d0`）触发 Xid 31**：
  `MMU Fault: ENGINE GRAPHICS HUBCLIENT_FE faulted @ 0x7f1c_fffff000,
  FAULT_PDE ACCESS_TYPE_VIRT_WRITE`，故障进程为密封的 `memfd:burstserv`
  子进程。扫描按规则当场中止，因此 **delta +496…+512 从未扫描——本次扫描是
  截断的，不是穷尽的**。中止后 GPU 0 立刻跑出 128/128 SM baseline，完全恢复。
  Phase 2（`phase2_verdict.json`，sha256 `363db8fc…1173d`）：证伪判据不是
  「候选能跑出 2 个 SM」，而是「stream 必须与两个 callback 模式在每个 bit 上
  给出完全相同的映射」——后者根本不依赖这个偏移，是独立见证。3 模式 × 4 bit
  × 3 trial = 36 格全部成功，`validate_masked_tpc_matrix` 15 项全 PASS、
  `accepted=true`、0 失败格、0 Xid。
  补全上次的过度外推（`stream_sweep64.json`，sha256 `5162f594…72fff`）：
  不重复「4 个 bit 就下全 die 结论」的错误，stream 在 `0x5fc` 上逐一跑完
  **全部 64 个 bit**，每个都恰好落在 `{2N, 2N+1}`，并集恰为 `{0..127}`，
  即对整块 die 双射覆盖，零失败。
  性质声明：这是一个**逆向得到、锁定于本驱动版本**的常量，靠向不透明驱动
  结构体盲写发现，不是文档化接口。论文中必须如实写成 reverse-engineered、
  version-locked hack，**不得描述为稳定机制**；驱动一升级即失效。
  尚未晋级：promoted manifest 仍为 `approved_mask_modes ["global","next"]`、
  `stream_offset_search_enabled=false`、`global_next_matrix_accepted=false`、
  `stream_mask_off_candidates=[]`。按 manifest 自身 promotion_requirements
  的次序，必须先跑完并接受**正式**的 global/next masked 矩阵，才能把
  `global_next_matrix_accepted` 置真并录入 offset 候选。本次全部为
  scratchpad 探针，未产生任何正式证据，promotion lock 未改动。
- 2026-08-01 / masked-run-uncovers-third-symbol-copy：首次尝试正式 masked
  矩阵，24 格全部被拒，暴露两个只有真机 masked 运行才能触发的缺陷。
  第一次尝试（24 格 exit 4，`preflight_permitted=false`）：失败于
  `formal_git_build_exception_paths_exact`。原因是 `src/burstserve/__pycache__`
  与 `src/burstserve/sim/__pycache__` —— 复审子代理运行 Python 时未带 `-B`
  留下的字节码缓存。形式化源码策略要求未跟踪条目**恰好**等于已认证构建清单，
  多出的 `.pyc` 原则上可遮蔽源码，故 fail closed。这是设计意图而非缺陷；
  清理缓存后快照恢复为精确的 13 条。运行脚本已加前置检查，并对 runner 传入
  `PYTHONDONTWRITEBYTECODE=1`。24 个被拒 run 全部保留未删。
  第二次尝试：第 1 格 `exit 3` 且实际执行了 11 秒，其余 23 格 `exit 1` 秒退。
  根因是 GPU 1 的 lease 被**隔离**（`auto_clear_permitted=false`，
  `monitor_status=monitor_failed`），后续每格因此立即拒绝——安全机制按设计工作。
  监视器本身完全正常：setup 成功、drain 干净、`observed_quiet_ms=1001`、
  0 Xid、`safe_for_acceptance=true`。真正失败的是 runner 侧**独立校验器**的
  `required_symbols_exact`：它仍硬编码 `nvmlDeviceGetHandleByUUID_v2`——
  即 commit `a730731` 已确认**根本不存在**的符号。该常量在仓库里有三份拷贝，
  上次只修了生产侧一份，另外两份（runner 校验器、`test_smctrl_runner.py` 的
  monitor provenance fixture）继续携带错误名，而那个 fixture 断言「接受一份
  任何真实监视器都不可能产出的记录」，等于把 bug 锁死。
  修复保留「独立校验」的设计意图（不 import 生产侧常量，否则生产侧错误会被
  镜像而非被抓住），改为把校验器的字面表提为
  `MASKED_MONITOR_REQUIRED_SYMBOLS`，并新增两项测试：真机导出检查现在覆盖
  **两张表**，外加 `test_monitor_and_validator_symbol_tables_do_not_diverge`
  静态断言二者相等——让「独立」不至于变成「分叉」。fixture 改为从
  `nvml_events._REQUIRED_SYMBOLS` 派生而非手抄。全量 478 tests OK。
  GPU 1 健康未受影响（1 MiB / 33 °C，内核日志中无该卡 Xid；唯一一次 Xid 31
  来自 offset 扫描时的 GPU 0，PCI `0000:01:00`）。隔离记录副本留存于
  scratchpad `streamoff/quarantine_record/` 后再清除。
- 2026-08-01 / masked-gate-a-global-next-accepted：**首次取得正式 masked
  证据**。在 GPU 1（A0 已接受编队卡，UUID `GPU-4cc58bdd…abfb`）上以
  promoted manifest `gate-a-4090-cuda133-20260801-promoted-global-next-gpu1`
  跑完 `{global,next}` × TPC bit `{0,31,32,63}` × 3 trials = 24 格，全部
  exit 0，每格约 10–12 s。另跑同一 manifest 身份下的 unmasked baseline 一格
  （`bs1-2dc25f5f…1838`，128 SM）作为对照。
  校验：24 格逐一通过 `validate_masked_cell_contract`（0 拒绝），
  `validate_masked_tpc_matrix` 15 项检查全 PASS、`accepted=true`。映射
  bit N → SM `{2N, 2N+1}`，跨 3 trial 确定、跨 2 种机制一致、bit 间两两不相交、
  且每个 masked 集合都是同卡 baseline 128 SM 的**真子集**（今日新增的收紧判据）。
  聚合结果存于 `experiments/aggregates/gate_a_masked_global_next_gpu1_20260801.json`。
  到达此结果前有三次失败尝试，全部 run 目录保留未删：第一次 24 格因
  `__pycache__` 被形式化源码策略拒绝；第二次因 runner 校验器的 NVML 符号
  拷贝错误导致首格 monitor_failed 并毒化 GPU lease，其余 23 格连锁快速失败；
  第三次因 lease 的 armed-poison 载荷仍在——清除隔离需要**两步**（删除
  quarantine marker **并**截断 lock 文件），我第一次只做了前一步。相关
  RuntimeError 文本被 `main()` 的 `except BaseException` 整个丢弃，只打印
  `<no-run-directory>`，三次排查都必须靠外部 traceback 注入才拿到原因——
  这是个真实的可用性缺陷，已记录待修。
  尚未晋级 stream：promoted manifest 仍为 `approved_mask_modes
  ["global","next"]`、`stream_offset_search_enabled=false`、
  `global_next_matrix_accepted=false`、`stream_mask_off_candidates=[]`。
  按 manifest 自身 promotion_requirements 的次序，本次 global/next 矩阵被接受
  正是置 `global_next_matrix_accepted=true` 的前提，下一步方可录入
  `MASK_OFF=+280`（总偏移 `0x5fc`）并产出 stream 的正式证据。
- 2026-08-01 / masked-run-diagnostics：修复三处让每次排查都要烧掉一整轮
  24 格矩阵的可用性缺陷。三者都不改变任何判据，只让拒绝说出理由。
  (1) `main()` 的 `except BaseException` 原先把异常文本整个丢弃，stdout 只
  打印 run 目录或 `<no-run-directory>`。现改为把 `类型: 消息` 写入 **stderr**，
  并在没有 run 目录（因而没有 outcome.json 可读）时附完整 traceback；stdout
  仍严格保持单行契约，调用方解析不受影响。
  (2) 形式化源码策略被拒时只有一个布尔量。现在 stderr 逐条列出失败的检查名
  （含所属检查组），并在构建例外记录中新增 `unexpected_untracked_paths` /
  `missing_expected_paths` 一并打印。今天那次真实故障现在直接显示
  `src/burstserve/__pycache__/...`——该目录被 gitignore，`git status` 看不见，
  是脚本预检也漏掉它的原因。
  (3) lease 的两种毒化状态（quarantine marker 与 lock 内的 armed-poison 载荷）
  原先抛出**同一句**消息，清掉 marker 后再运行会看到一模一样的报错，无从得知
  还剩一步。现按实际仍然置位的状态分别列出所需动作与确切路径。
  新增测试三项：`test_quarantine_message_names_the_state_that_is_still_set`
  逐步验证「两者都置位 → 只剩 lock → 照消息做完即可获取 lease」；
  `test_cli_reports_why_a_run_was_rejected` 走真实 CLI 断言 stdout 仍为单行
  而 stderr 含类型、消息与 traceback。全量 480 tests OK（21 skipped）。
- 2026-08-01 / stream-vendor-banner-prefilter：stream 正式格在语义上完全正确
  （观测 2 SM `{0,1}`、0 Xid、监视器干净），唯一失败的是
  `successful_native_stderr_empty`。原因是 vendored libsmctrl 在
  `libsmctrl_set_stream_mask_ext` 里**无条件** `fprintf(stderr, ...)` 公布
  它将要使用的 offset，而探针每次运行调用它两次（设置 mask、发射后清除），
  所以任何 stream 格必然带两行横幅。
  处理方式**不是放宽该检查**。硬约束有两条：(a) 交接文档禁止削弱接受阈值；
  (b) 聚合校验器用**当前代码**重算 `evaluate_probe` 并对记录逐键做
  `_exact_json_equal`，因此**新增或重命名任何检查名都会让已接受的 24 格作废**。
  实现为送入 `evaluate_probe` 之前的**精确前置过滤**
  `residual_native_stderr(stderr, experimental_mask_off=...)`：按本格自己声明的
  offset 逐字节重建 vendored 源码会打印的那一行，只删除完全相同的行，其余
  一律留下继续判失败。实测：不同 offset 的横幅、未声明 offset 时的横幅、
  截断的横幅、附带的真实 warning、非整数/布尔 offset 全部不被吸收。
  `experimental_mask_off is None` 时该函数是恒等映射，故 baseline/global/next
  的行为与检查键集完全不变——已接受的 24 格重新校验仍
  `accepted=true`、0 拒绝。新增常量 `LIBSMCTRL_CU_12_2_MASK_OFF = 0x4E4`
  须与 `vendor/libsmctrl/libsmctrl.c` 的 `CU_12_2_MASK_OFF` 保持同步。
  **记为技术债**：横幅存在的根本原因是 offset 以「实验环境变量 MASK_OFF」的
  身份传入，而它现在已是一个已知常量。工程上正确的终局是把 CUDA 13.3 的
  case 编进 offset 表（那条路径不打印任何东西），但这意味着在已 pin 的 upstream
  源码上携带补丁，属于独立的 provenance 决策，本次未做。
- 2026-08-01 / masked-gate-a-all-modes-accepted：**三模式完整 masked 矩阵通过**。
  在 GPU 1 上以 promoted manifest
  `gate-a-4090-cuda133-20260801-promoted-all-modes-gpu1` 跑完
  `{global,next,stream}` × TPC bit `{0,31,32,63}` × 3 trials = **36 格**，
  全部 exit 0，每格约 10–11 s；另有同 manifest 身份下的 unmasked baseline
  一格（`bs1-2279b0f0…6ec0`，128 SM）。stream 格使用
  `--experimental-mask-off 280`（总偏移 `0x5fc`），即今日逆向所得常量。
  校验：36 格逐一通过 `validate_masked_cell_contract`（0 拒绝），
  `validate_masked_tpc_matrix` 15 项检查全 PASS、`accepted=true`。
  映射 bit N → SM `{2N, 2N+1}`：跨 3 trial 确定、**跨 3 种机制一致**、
  bit 间两两不相交、每个集合都是同卡 baseline 128 SM 的真子集。
  这是 stream 的关键证据：写入不透明结构体得到的映射，与两个**根本不依赖
  该偏移**的 callback 机制逐 bit 完全一致——正是今天新增的
  `single_tpc_matrix_corroborates_stream_with_callback_modes` 所要求的佐证
  结构。聚合结果存于
  `experiments/aggregates/gate_a_masked_all_modes_gpu1_20260801.json`
  （sha256 `a9ae898d…f9a0`）。
  安全：36 格中每格的 NVML 监视器均记录 0 Xid，GPU 1 运行后 1 MiB / 34 °C。
  盲写在正式路径上首次执行，未出现任何故障。此前 12 个 stream 格因 vendor
  横幅被拒的 run 目录全部保留未删。
  边界：该结论锁定于驱动 610.43.02。`0x5fc` 是逆向常量而非接口，驱动升级
  后可能静默失效（写入填充字节、mask 空转）或触发 Xid。论文中必须如实写成
  reverse-engineered、version-locked。
- 2026-08-02 / stream-offset-adaptation-moved-out-of-vendor：偿还
  2026-08-01 `stream-vendor-banner-prefilter` 记下的技术债。
  依据是项目自己冻结的策略——`vendor/LIBSMCTRL_SOURCE.json` 的
  `"policy": "Immutable upstream submodule. Never patch this checkout; keep
  BurstServe guards, probes, and any offset adaptation outside
  vendor/libsmctrl."`——它既禁止打补丁，也直接指明了正路：把 offset adaptation
  放在 vendor 之外。因此不 fork、不 re-pin submodule。
  在 `native/smctrl_probe/smid_probe.cu` 中实现 `apply_adapted_stream_mask`，
  复刻 vendored 源码的 `struct stream_sm_mask_v2` 写入语义
  （`enabled=1`，`mask[0..3] = mask >> {0,32,64,96}`），偏移
  `kAdaptedStreamMaskOffset = 0x5fc` 与驱动版本 `kAdaptedStreamMaskDriverVersion
  = 13030` 均为编译期常量，随构建认证一并固化。
  这比原来的 `MASK_OFF` 环境变量**更严而非更松**：环境变量对任何驱动版本都
  照写不误、且可被外围进程改写以重定向一次盲写；新实现拒绝除验证过的那个
  驱动版本以外的一切版本，拒绝任何与编译进二进制的常量不逐字节相同的偏移，
  并且 runner **不再向子进程注入 `MASK_OFF`**——探针检测到该变量存在即
  fail closed。偏移改走 `--stream-mask-offset` 参数，语义由「相对 `0x4e4`
  的增量」改为**绝对结构体偏移**（`280` → `1532`）。
  实测四种误用全部 fail closed：错误偏移、缺省偏移、`MASK_OFF` 仍被设置、
  在非 stream 模式上传偏移；正确调用 exit 0、观测 SM `{0,1}`、
  **stderr 完全为空**——横幅从根源上消失了，不再依赖前置过滤。
  `stream_offset_is_8byte_aligned` 相应改为 `stream_offset_is_4byte_aligned`
  （`uint32_t` 字段的结构性要求；原规则是「相对 `0x4e4` 的增量 8 字节对齐」，
  等价于总偏移 ≡4 mod 8，那是已观测偏移的巧合而非结构约束）。该模运算已不是
  约束力所在：真正的门是探针只接受与认证构建逐字节相同的那一个值。
  `residual_native_stderr` **保留**：2026-08-01 已接受的 36 格证据的
  `stderr.log` 里确实有横幅，聚合校验器会重算并逐键比对，删掉它会让那批证据
  作废。它现在只服务历史证据，新证据不再触发它。
  重建改变了四个产物摘要，三份 manifest 的 approval pin 全部刷新
  （launcher `c0ffa798…316b`、real probe `d367b99f…b904b`、stamp
  `10d0d2b7…c43d`、attestation `936042f0…4da6`）。已接受的旧证据不受影响：
  每个 cell 内嵌自己的 manifest 副本与 pin，校验对内嵌副本进行。
  全量 481 tests OK（21 skipped）。
- 2026-08-02 / all-modes-matrix-reaccepted-on-attested-offset：以移出 vendor
  之后的机制重跑三模式矩阵并通过。36 格（`{global,next,stream}` × bit
  `{0,31,32,63}` × 3 trials）全部 exit 0，逐格通过
  `validate_masked_cell_contract`（0 拒绝），`validate_masked_tpc_matrix`
  15 项全 PASS、`accepted=true`，映射与此前完全一致
  （bit N → SM `{2N,2N+1}`）。stream 格现在经 `--stream-mask-offset 1532`
  传递绝对偏移，子进程环境中**不再有 `MASK_OFF`**，stderr 为空。
  聚合存于
  `experiments/aggregates/gate_a_masked_all_modes_gpu1_attested_offset_20260802.json`。
  首次重跑时 12 个 stream 格被拒，暴露了我迁移时漏掉的第二处旧契约：
  `validate_masked_cell_contract` 仍要求「stream 模式必须存在 `MASK_OFF`
  环境变量」——正是新机制刻意消除的那个东西。改为：环境里出现偏移只在
  非 stream 模式下算错误（历史证据合法携带它），而 stream 模式在环境未提供
  偏移时必须在 argv 中携带 `--stream-mask-offset`；任何模式下 argv 出现该
  参数而模式不是 stream 亦为错误。这样 2026-08-01 已接受的 36 格与今日的
  36 格都能校验，且两种形态都无法省略偏移来源。
- 2026-08-02 / amd-line-bootstrapped：为跨平台主张开出 AMD 分支，在
  `husrcf@X570`（passwordless ssh，Ubuntu 24.04 / kernel 6.17）建立
  `~/Code/alse` 并同步源码树。
  硬件与环境：**AMD Radeon AI PRO R9700**，`gfx1201`（RDNA4），**64 CU**、
  32 GB VRAM（`34208743424` B），ROCm 7.2.0，`/usr/bin/hipcc` 就绪，
  根分区 2.3 T 可用，外网可达（GitHub 200）。CPU 为 Ryzen 9 5900X（24 线程）。
  **对论文最关键的发现**：AMD 侧的对等能力是**文档化的一等 API**——
  `hipExtStreamCreateWithCUMask`（自 HIP 4.2，已在
  `/opt/rocm/lib/libamdhip64.so` 中导出为 `@@hip_4.2`），并且还有
  `hipExtStreamGetCUMask` 可以**读回**当前掩码。对照 NVIDIA 侧：per-stream
  掩码需要向不透明 `CUstream` 结构体盲写一个逆向所得、锁定驱动版本的偏移，
  且**没有任何读回途径**（我们只能用 `%smid` 直方图间接观测）。这构成一个
  真实的、可写进论文的平台差异，而不是"我们在两个平台上都做了同样的事"。
  同步范围：源码、测试、native、manifests、aggregates、probes、plan.md 与
  git 历史（HEAD `0f8c457`）。**排除** `experiments/runs/`（483 M，且是
  NVIDIA 专属证据）、`build/`、`related_work/`、`vendor/asle/`、`ASLE.tar.gz`。
  已验证：`validate_masked_tpc_matrix` **无需任何修改**即可用于 AMD——
  喂入 AMD 形状的合成矩阵（64 CU、每 mask bit 一个 CU、两种机制 × 4 bit ×
  3 trial）返回 `accepted=true`。该 validator 只消费「观测记录 + 声明的矩阵
  形状 + 硬件形状」，不含任何 CUDA 假设，因此跨平台证据链的判据部分可以共用。
  尚未做（需要方向确认后再展开）：HIP 版 CU-ID 探针（AMD 的对应物是
  `__smid`/`HW_ID` 寄存器读取，与 `%smid` 不同需另行确认）、AMD 侧的
  provenance/promotion 机制是否复用现有 runner、以及是否需要在 X570 上
  建立独立的 Gate A0 基线。
- 2026-08-02 / amd-cu-mask-matrix-accepted：AMD 侧第一步（CU 探针）完成，
  结论比 NVIDIA 侧强得多，且**一次逆向都不需要**。原始记录见
  `experiments/probes/amd-r9700-cu-mask/`（非正式证据，无 provenance 绑定）。
  探针 `native/amd_cu_probe/cu_probe.hip` 读 `HW_ID1`（`s_getreg_b32`，
  HW_REG id 23）作为硬件单元标识，屏蔽低 10 位的 wave/SIMD 字段。字段布局
  **不照规格解码**，而是用 mask API 当 ground truth 反推——单单元掩码若出现
  多个标识即证明切法错了。
  两种独立机制，均为一等接口：`stream_cu_mask`
  （`hipExtStreamCreateWithCUMask`，HIP 4.2 起文档化并导出）与
  `global_cu_mask`（`ROC_GLOBAL_CU_MASK` 进程级环境变量）。因此跨模式一致性
  这条证伪依据在 AMD 上同样成立，无需任何替代方案。
  矩阵：2 机制 × bit `{0,15,16,31}` × 3 trials = 24 格全部成功，喂给
  **未经任何修改**的 `validate_masked_tpc_matrix`（NVIDIA 线同一个函数），
  15 项检查全 PASS、`accepted=true`。
  **NVIDIA 无对应物的能力**：`hipExtStreamGetCUMask` 可读回实际生效的掩码。
  NVIDIA 侧掩码写进不透明结构体后不可观测，"写了但无效"与"写了且有效"只能
  靠 kernel 行为区分；AMD 侧则由 API 直接报告。
  两处被实测纠正的错误读法（均记录在案）：
  (1) `multiProcessorCount` 报 32 而 `rocminfo` 报 64 CU，掩码宽度从外部无法
  判定；且在 `ROC_GLOBAL_CU_MASK` 下该值变为 `popcount(mask)/2`（单 bit 时为
  **0**），一度使我认定向量宽 64 位。**64 位 sweep 推翻了它**：bit 0–31 各自
  约束到恰好一个单元，bit 32–63 则完全不掩码，**并且读回 API 报告掩码与请求
  不符**——即文档所述 "extra elements are ignored"，由读回抓住而非由直方图推断。
  (2) 同一报告怪癖对探针自身是陷阱：掩码生效时无法向设备查询自身掩码宽度，
  故宽度改由调用方经 `--maskable-units` 声明，并仅在无掩码时与设备报告交叉核对。
  另记：掩码 bit 序**不等于**硬件单元序（bit 15 → 单元 29，bit 16 → 单元 2，
  按 baseline 标识符排序的稠密索引）。标识符归一化经 baseline 自身的有序集合
  完成，映射表已随证据保存。
- 2026-08-02 / amd-reduced-contract-scoped-and-enforced：按授权对 AMD 线做
  减法，**并把「仅限本环境」做成机制而非仅写在文档里**。用户明确要求确认：
  这是**只针对 AMD 单卡环境**的减法，不是修订后的标准，NVIDIA 线不得援引。
  账本见新增的 `docs/amd-reduced-contract.md`，逐条列出：删掉了什么、它在
  CUDA 侧防的是什么风险、为什么该风险在此环境不存在、以及**什么条件下必须
  加回来**。删除项与其复原条件例：NVML Xid 监视器/lease 毒化/隔离态——防的是
  盲写把卡打挂（实测过 `Xid 31 ... ACCESS_TYPE_VIRT_WRITE`），此处无盲写，
  **一旦 AMD 侧需要任何未文档化的写入即须恢复**；独占预约与 busy-GPU 预检——
  防的是他人中途抢卡，**一旦出现第二个使用者或调度器即须恢复**；
  sealed-memfd exec 与 parent-death guard——防的是共享租户，同上；MPS bypass
  ——ROCm 此配置无对应物；多卡 A0——单卡无跨卡主张可作，**加第二张卡即须恢复**。
  **未减的部分**（属于证据质量而非风险缓解，全部保留）：内容寻址 run id、
  Git provenance 绑定、构建认证、运行前声明矩阵形状、逐格契约检查，以及
  `validate_masked_tpc_matrix` **完全未改**——含其 ≥2 机制、≥2 bit、≥2 trial
  下限，跨模式一致、bit 间不相交，以及 masked 集合须为同卡 baseline 真子集。
  另有一项是**增项而非减项**：`hipExtStreamGetCUMask` 读回检查，CUDA 侧无法
  实现。
  三重强制机制（文档挡不住渗漏，这些能）：
  (1) 新模块 `src/burstserve/amd_cu_runner.py` **不 import 也不修改**
  `smctrl_runner`，因此任何为 AMD 所做的改动在结构上无法放宽 CUDA 门；
  (2) AMD cell 使用独立 schema `burstserve.amd-cu-cell/v1`，而 CUDA 校验器
  逐字符钉住自己的 schema 串，故 AMD 证据**无法**被计入 CUDA 聚合；
  (3) `tests/test_amd_cu_runner.py` 用 AST 检查真实 import（而非子串——
  子串检查会误报模块 docstring 里那句说明分离的话，我第一版正是这么翻的车），
  并断言未晋级 CUDA manifest 仍拒绝全部三种 masked 模式。
  AMD manifest 契约本身也强制自我声明：`reduced_contract` 必须指向该账本文档、
  `applies_to` 必须恰为 `single-card single-operator gfx1201`（声称更宽范围
  如 "all AMD hardware" 会被拒）、且必须非空列出所删守卫。
  全量 493 tests OK（21 skipped）。
- 2026-08-02 / amd-formal-evidence-accepted：AMD 线产出**第一批正式证据**，
  来源绑定干净、无 dirty 后缀。
  运行环境 `husrcf@X570`，Radeon AI PRO R9700 / gfx1201，manifest
  `amd-r9700-gfx1201-x570-20260802`，source revision
  `04bfc25b16a77dbf699946282a490fbf99b9b4a2`（干净），探针二进制摘要
  `d536d473…a6f1` 逐格写入 cell config。
  矩阵：`{global_cu_mask, stream_cu_mask}` × mask bit `{0,15,16,31}` × 3
  trials = **24 格全部接受、0 拒绝**，另有同 manifest 身份的 unmasked
  baseline（`bs1-5c2669b7…`，32 单元）。判据是**与 CUDA 线共用且完全未改**的
  `validate_masked_tpc_matrix`，15 项全 PASS、`accepted=true`。映射
  bit → 单元 `{0→0, 15→29, 16→2, 31→31}`——掩码 bit 序不等于硬件单元序。
  聚合存于 `experiments/aggregates/amd_r9700_cu_mask_20260802.json`
  （sha256 `bfbeb8e3…8362`）。
  过程中修掉四个真实问题，均**未放宽任何共享代码**：
  (1) X570 的 git 为 2.43，而 provenance capture 使用 2.45 才有的
  `--no-lazy-fetch`，且拒绝 group/world 可写的 git 可执行文件。解法是在用户空间
  conda 环境装 git 2.55（`~/Code/alse-tools`，不动系统、无 root）并 `chmod g-w,o-w`，
  由 `BURSTSERVE_GIT` 指定——**没有为迁就旧环境放宽 capture**，因为 CUDA 线同样
  依赖这两项。
  (2) 仓库内的 libsmctrl submodule 未注册即无法精确描述工作树；由调用方按
  `vendor/LIBSMCTRL_SOURCE.json` 的 pin 显式声明 gitlink。AMD 线不使用该库，
  但共享同一仓库，未登记的 gitlink 是拒绝理由而非可跳过的细节。
  (3) **自指问题**：驱动把聚合写进它刚刚绑定的那棵树里的受跟踪路径，导致每次
  重跑都把上一次的输出看成修改而自我标脏。改为写入允许的未跟踪根
  `experiments/runs`，curated 副本另行收取。
  (4) 构建产物按设计就是未跟踪的，`snapshot.clean` 计入未跟踪条目，因此即使
  零 staged/零 unstaged 仍判为脏。新增**构建清单例外**：接受且仅接受已认证清单
  中的文件、且摘要必须逐字节相符，其余任何未跟踪路径一律拒绝；revision 的
  脏/净判定改依据受跟踪变更。这不是放宽——探针摘要本就参与每格 cell 身份。
  全量 493 tests OK（21 skipped）。
- 2026-08-02 / gate-a-amd-passed：**Gate A-AMD 全部条款通过**。这是补充条款，
  NVIDIA 侧的 Gate A 仍欠五条、第二 GPU SKU 硬门亦未关闭，两者互不顶替。
  (1) **全 die 映射**：manifest `amd-r9700-gfx1201-x570-fulldie-20260802`，
  source revision `8a4006bb…`（干净），双机制 × **全部 32 个 mask bit** ×
  3 trials = **192 格全接受、0 拒绝**，`validate_masked_tpc_matrix`（与
  NVIDIA 线共用且未改）15 项全 PASS。32 个 bit 各自映射到 32 个互不相同的
  单元，并集恰为全部 32 单元。聚合
  `experiments/aggregates/amd_r9700_cu_mask_fulldie_20260802.json`
  （sha256 `87e77c29…`）。**不再是抽样外推**——先前 4/32 的版本已被此结果取代。
  (2) **并发互斥分区**：两个 CU-masked stream 各请求 8 个单元，实测各得
  8 个、**相交 0 个**、时间窗重叠 **100.0 ms**（kernel 内 100 MHz 常速时钟
  记录各 block 进出时刻），`partitioned=true`。**未加掩码的对照组**两臂各
  覆盖全部 32 单元、相交 32 个、`partitioned=false`——判据是「单元数等于
  请求数且不相交且时间重叠」，不是「不相交」。此处顺带纠正一处我自己的
  空真判据：最初的 `matches_alone_oracle` 对未掩码对照恒为真（其"单独跑"
  本就是全 die），已改为上述可判别形式。
  另记平台差异：NVIDIA 侧未掩码对照**也**不相交（调度器按奇偶劈分 die），
  AMD 侧未掩码对照则完全重叠。故「不相交」在 NVIDIA 上毫无判别力，在 AMD
  上有一些——但两边都不采用它作判据。
  (3) **10,000 次动态重配置**：0 崩溃、0 越界、0 错误 mask。每一次均经
  `hipExtStreamGetCUMask` **读回校验**（0 不符），而非仅由 kernel 落点间接
  推断——NVIDIA 侧无此能力。
  (4) **配置延迟，语义差异如实记录**：HIP 在**建流时**固定掩码，不能就地
  更新已有 stream，因此存在两个截然不同的代价。建流
  `hipExtStreamCreateWithCUMask` **p50 6885 μs / p99 7852 μs**——是 NVIDIA
  侧 Gate A 那条 100 μs 预算的**约 70 倍，绝不可置于关键路径**。真正的运行
  原语是**预建掩码流池后在其间切换**：10,000 次实测 **p50 1.11 μs /
  p99 2.22 μs / max 30.4 μs**，0 越界 0 失败，比 100 μs 预算低约 45 倍。
  **这两个数字不可与 NVIDIA 的「mask 更新延迟」直接比较**，机制不同名。
  该发现直接约束调度器设计：AMD 上必须预建流池，不能按需建流。
  (5) **确定性**：固定 seed 校验和在 unmasked 与 6 个不同单掩码下逐位相同，
  且重复发射不改变结果。
  (6) **可观测 overlap**：由 kernel 内时钟给出每个 block 的进入/退出时刻，
  实测重叠 100.0 ms，强于依赖 profiler 截图。
  原始记录 `experiments/probes/amd-r9700-cu-mask/gate_a_experiments.jsonl`。
- 2026-08-02 / amd-cu-quota-reaches-pytorch：Gate B 的前置可行性已验证——
  **CU 掩码穿透到真实 PyTorch 负载**，吞吐随配额单调变化。这是在下载数十 GB
  模型权重之前必须先答的问题：若掩码到不了框架层，整个 Gate B 的 profiling
  计划就建立在一个无效机制上（NVIDIA 侧 `next` 正是这样被证伪的）。
  实测（fp16 4096³ matmul，torch `2.9.1+rocm7.2.0`，R9700）：
  未掩码 131.78 TFLOPS / 32 单元 131.15 / 16 单元 76.71 / 8 单元 43.76。
  即 25% 的单元给出 33% 吞吐、50% 给出 58%——**次线性但单调且幅度显著**，
  正是 Gate B 需要的 quota→吞吐关系。比例高于配额比是预期内的：活跃单元
  少时时钟余量更大、尾部效应更小，具体成因待 profile 阶段拆解。
  由此确定 Gate B 的实现形态：**每个 profiling cell 用一个独立进程并以
  `ROC_GLOBAL_CU_MASK` 固定配额**，co-run cell 用两个各带不相交掩码的进程。
  **无需改动 PyTorch 的 stream 管理**，也就绕开了 per-stream 掩码与框架
  内部发射的耦合问题。
  注意 plan 原文的 quota 列表 `{16,32,...,128}` 是 4090 的 SM 计数；AMD 侧
  32 个可掩码单元对应的等价列表为 `{4,8,12,16,20,24,28,32}`，须在 Gate B-AMD
  条文中另行写明，不得直接套用。
- 2026-08-02 / amd-quota-scaling-is-size-dependent：Gate B profiling harness
  `scripts/amd_profile_cell.py` 跑通（一进程一配额，`ROC_GLOBAL_CU_MASK`
  固定，CV 未达标时自动加采样并记录 escalation 次数）。首轮 quota sweep
  暴露一个必须写进 Gate B-AMD 条文的事实。
  **quota→吞吐关系依赖问题规模，小负载下非单调**。同一 fp16 matmul 链，
  单元数 `{4,8,16,24,32}`：
  1024 行 → `5.11 / 10.29 / 17.10 / 14.68 / 11.75` TFLOPS，**16 单元达峰后
  加更多 CU 反而更慢**（32 单元只有峰值的 69%）；
  4096 行 → `21.88 / 42.81 / 74.16 / 102.56 / 130.58`，单调且接近线性；
  16384 行 → `22.39 / 43.79 / 80.37 / 106.15 / 127.68`，同样单调。
  即：负载不足以填满 die 时，增加 CU 使每 CU 工作量过小，发射与同步开销
  占主导，**掩码收窄反而提速**。这不是掩码失效——先前 4096×4096 单次
  matmul 测得的 131 TFLOPS 与此处 batch≥4 一致。
  对 Gate B 的直接约束：**每个 profile cell 必须记录自己是否处于饱和区**，
  否则 quota→latency 表在小 cell 上会给出「配额越大越慢」的反直觉条目而
  无法与调度器的线性假设对接。plan 原文的 quota 列表 `{16,…,128}` 是 4090
  SM 计数，AMD 侧等价列表为 `{4,8,12,16,20,24,28,32}` 个可掩码单元。
  CV 情况良好：全部 40 个 cell 均 ≤ 6.4%，多数 ≤ 3%，30 采样即达标，未触发
  自动加采样。这与 NVIDIA 侧 `urgent_p50` 单卡 CV 5.5% 的情形不同，原因是
  此处测的是纯 GPU kernel 时间而非端到端请求延迟。
- 2026-08-02 / amd-quota-table-synthetic：产出 Gate B-AMD 的首张 quota 表
  （合成 fp16 matmul 链，非模型负载，记为前置校准而非 Gate B 证据）。
  两个饱和批量各 8 个配额点 `{4,8,12,16,20,24,28,32}`，**均单调**：
  batch=4 → `21.98 / 43.42 / 64.51 / 76.35 / 90.89 / 105.31 / 115.63 / 132.90`
  TFLOPS；batch=16 → `22.34 / 43.50 / 64.10 / 79.50 / 93.15 / 105.91 /
  117.03 / 127.56`。两条曲线在同配额下几乎重合，说明进入饱和区后 quota→吞吐
  与批量无关——这正是调度器模型需要的性质。
  **CV 结果须如实记录：16 格中 14 格满足 ≤5%，2 格不满足**
  （batch=4/28 单元 CV 6.4%，batch=16/4 单元 CV 7.5%）。二者的自动加采样
  **已触发 4 次、采到 480 样本仍未收敛**，即该方差**不是采样噪声、加样本
  无法消除**。按 plan 既定处置（「提高采样量或改用 per-step / 遥测指标」），
  这两格在正式 Gate B 中必须改用 per-step 或遥测指标，不得以端到端计时凑数。
  两个离群格在配额与批量上无明显规律（一个高配额、一个低配额），成因待
  真实模型 profile 阶段用 per-step 数据与时钟/功耗遥测拆解。
  原始记录 `experiments/probes/amd-r9700-cu-mask/quota_table_synthetic_20260802.jsonl`。
- 2026-08-03 / amd-has-no-pcie-telemetry：R9700（gfx1201）**没有可用的硬件
  PCIe 流量计数器**，因此 Gate B 与 Gate B-AMD 中「resident 同模型轮转观测到
  零权重 PCIe 流量」「cold-model 预测严格使用缺失字节和实测带宽」两条在 AMD
  侧不能照搬 NVIDIA 的取证手段。实测:
  `rsmi_dev_pci_throughput_get` 返回 `rc=2`（`RSMI_STATUS_NOT_SUPPORTED`）；
  `rocm-smi --showbw` 报 `get_PCIe_bandwidth, Not supported on the given
  system`；`--showmetrics` 中 `pcie_bandwidth_acc` / `pcie_bandwidth_inst` /
  `pcie_replay_count_acc` 全为 `N/A`（链路本身正常：16 lanes、160×0.1 GT/s）。
  这类遥测主要在 Instinct MI 系列上实现，不是本机配置错误。
  **替代手段是 `rocprofv3 --memory-copy-trace --stats`**，按进程统计 HtoD /
  DtoH 拷贝字节。**这不是阈值放松,判据仍是「权重字节数为零」**——而且
  copy trace 比整卡 PCIe 计数器**更严格**：整卡计数器会把同机其他进程的流量
  一并计入，从而把本该判负的残余权重传输掩盖在噪声里，copy trace 则归属到
  进程。NVIDIA 侧仍按原条文用 PCIe 计数器取证，两条线各自记录手段。
- 2026-08-03 / amd-sdxl-has-a-large-serial-floor：SDXL（1024²、20 步、fp16、
  batch=1）在 R9700 上的 quota→latency **不接近线性**：4 单元 p50 = 24.828 s
  （CV 0.43%，30 采样零加采样），32 单元约 4.67 s，比值仅 5.3×，而配额比是
  8×。按 Amdahl 形式 `latency = serial + parallel/q` 反解，**serial ≈ 1.8 s，
  约占 32 单元时延的 38%**。含义有二：(1) 高配额段边际收益很低，调度器不能
  假设配额与延迟成反比；(2) Gate B 的 held-out MAPE 必须用含 serial 项的
  两参数模型评分，纯比例模型会在高配额端系统性低估延迟。该模型与评分实现
  在 `src/burstserve/quota_model.py`，对两种空洞通过都做了拒绝：三点以下不
  拟合（两参数模型在两点上必然零残差），留出集为空不评分（MAPE 恒为 0）。
- 2026-08-03 / native-build-is-not-reproducible：**`smid_probe.real` 的构建
  不是逐位可复现的**，而三个 NVIDIA gate manifest 用
  `approved_real_probe_sha256` 把它 pin 死了。这意味着 pin 所声称的
  「可验证性」实际不成立：产物一旦丢失就无法重建出同一个二进制来核对。
  发现经过与证据：为修一个 flaky 测试改了 `tests/test_native_parent_guard.py`，
  该文件的 SHA 在 build stamp 内（这是设计使然），于是必须重建。重建后
  `make` 三次得到**同一个 build stamp `10d0d2b7…`（即全部构建输入的 SHA
  完全一致）却得到三个不同的二进制**：
  `d367b99f…`（manifest pin）/ `0cb8ea61…` / `d059b2c0…`。
  根因已定位到**恰好 3 个字节**：nvcc 把自己的临时文件名
  `tmpxft_<PID 的十六进制>_00000000-6_smid_probe.cudafe1.cpp` 作为一个
  `FILE LOCAL DEFAULT ABS` 符号写进了符号表，其中 8 位十六进制是 nvcc 进程的
  PID。验证：对两个不同的二进制各执行
  `objcopy --wildcard -N 'tmpxft_*'` 后，二者变为**逐位相同**
  （`ad2069f0…`）。该符号是纯编译单元名标记，不参与任何重定位。
  只有经 nvcc 的 `smid_probe.real` 带此符号，其余产物（纯 C，由 cc 编译）没有。
  **已造成且不可逆的后果**：`d367b99f…` 那个二进制已被重建覆盖，仓库内无备份
  （`build/` 不入 git），只有 `experiments/runs/*/manifest.json` 里留有它的
  SHA 记录。因此三个 manifest 的 pin 现在指向一个不存在的产物，
  `test_default_manifest_is_artifact_pinned_but_still_unpromoted` 如实判负。
  **这是拒绝而非放行，行为是安全的**：NVIDIA 侧的 masked 运行现在会被拒。
  **补救路径（已于 2026-08-03 全部执行完毕，见下一条）**：
  (1) 在 `REAL_TARGET` 链接后加一步 `objcopy --wildcard -N 'tmpxft_*'`，并把
  `objcopy` 像 `ar`/`cc` 一样纳入 stamp 与 attestation（`OBJCOPY`、
  `OBJCOPY_EXECUTABLE_SHA256`、`OBJCOPY_VERSION_SHA256`），否则等于在证明体系里
  留一个未被证明的工具；(2) 连续两次 `make clean && make all` 验证 bit-identical；
  (3) 更新三个 manifest 的 `approved_real_probe_sha256` /
  `approved_build_stamp_sha256` / `approved_build_attestation_sha256`；
  (4) 用新身份重跑 NVIDIA 侧 baseline 与 masked 矩阵——**不得把旧二进制跑出的
  cell 与新身份混用**。代价可控：NVIDIA Gate A 本就欠五项、本就要重跑。
  连带待办：`tests/test_native_parent_guard.py` 中
  `test_artifacts_have_exact_permissions_links_and_no_capability` 会在
  `/run/user/$UID`（tmpfs，登录会话结束即清空）被清理而 `build/` 尚存时抛
  `FileNotFoundError` 而非 skip——因为 `require_artifacts()` 只覆盖 `build/`。
  修法已验证（拆成独立测试 + 缺失即 skip），但因会触发上述二进制轮换，
  暂缓到补救路径一并执行，本次已 revert。
- 2026-08-03 / native-build-reproducibility-restored：上一条的补救**已全部执行**，
  `smid_probe.real` 现在逐位可复现。
  改动：`Makefile` 在 `$(REAL_TARGET)` 链接后加一步
  `$(BUILD_ENV) $(OBJCOPY) --wildcard -N 'tmpxft_*'`；`objcopy` 与 `ar`/`cc`
  同等纳入构建证明——`OBJCOPY`、`OBJCOPY_EXECUTABLE`、
  `OBJCOPY_EXECUTABLE_SHA256`、`OBJCOPY_VERSION_SHA256` 四个字段进入
  `STAMP_FIELD_ORDER`，`BS_OBJCOPY` 进入 `ATTEST_ENV`，版本记录进入
  attestation 的 `versions`。**不这样做等于在证明体系里留一个未被证明的工具**：
  一个能改写最终二进制的程序却不在 stamp 内。`smctrl_runner.BUILD_STAMP_FIELDS`
  这份运行时副本同步更新（既有测试
  `test_runner_stamp_schema_exactly_matches_native_order` 逐字段比对二者，
  漏改会被判负——它确实判负了，这是该测试第一次真正发挥作用）。
  **验证**：连续两次 `make clean && make all`，九个产物（launcher、real probe、
  exec-test 两个、test helper、两个 identity header、stamp、attestation）
  **全部逐位相同**。另一次独立验证：构建内 objcopy 步骤产出的
  `ad2069f0…` 与先前手工对两个不同二进制执行同一 objcopy 命令所得的值完全一致。
  **一个必须区分清楚的例外**：`build-attestation.json` 在 clean rebuild 之间
  **不**逐位相同，实测两次共 8 处差异**全部且仅仅是 `inode`**（无其他不确定性）。
  这是设计使然而非缺陷：attestation 记录产物的 inode/device 正是为了检测产物被
  替换，它绑定的是「这一次构建产出的这些具体文件」，不是「可重建的构建」。
  因此 `approved_build_attestation_sha256` 只能靠保留文件本身来核对，
  而 `approved_real_probe_sha256` / `approved_launcher_sha256` /
  `approved_build_stamp_sha256` 现在可以靠重建核对——这是本次修复的实质收益。
  三个 manifest 的四个 pin 已全部更新到新身份：
  launcher `0cf66b42…`、real probe `ad2069f0…`、stamp `9aba017e…`、
  attestation `de60bfad…`。**旧二进制 `d367b99f…` 已不可恢复**，故用它跑出的
  NVIDIA cell **不得与新身份混用**；NVIDIA Gate A 本就欠五项、本就要重跑。
  连带完成：`test_artifacts_have_exact_permissions_links_and_no_capability`
  的 tmpfs 缺陷已随本次轮换一并修好（构建锁拆为独立测试，缺失即 skip 并说明
  原因，而非抛 `FileNotFoundError`）。全量测试 598 项通过（21 skipped）。
  **值得记住的一般教训**：二进制身份现在只依赖真正影响它的输入——改
  `tests/test_native_parent_guard.py` 使 stamp 变化（`6b6d4e27…`→`9aba017e…`）
  但 `smid_probe.real` 保持 `ad2069f0…` 不变，因为该测试文件不参与
  `smid_probe.cu` 的编译。stamp 记录更广的上下文，产物摘要记录产物本身，
  两者本就该分层；此前它们同时漂移，掩盖了这一点。
- 2026-08-03 / amd-per-stream-quota-reaches-pytorch：**`hipExtStreamCreateWithCUMask`
  与 `torch.cuda.ExternalStream` 可以组合,进程内即可动态改配额**——不必重启进程。
  这是 ASLE runtime 的关键前提(此前只在 HIP probe 内验证过掩码机制,未验证它
  穿透到 PyTorch 的 stream 管理)。实测(8192² fp16 matmul 链 ×4):
  单元数 `{4,8,16,32}` 的 p50 = `192.5 / 100.5 / 57.8 / 44.7` ms,
  相对默认流 `0.232 / 0.444 / 0.772 / 0.998`×。三点均已核验,缺一不可:
  (1) **掩码被真正执行而非仅仅安装**——满掩码流与默认流之比 0.998,
  低配额流按比例变慢;若只安装不执行,读回会完好而计时不变。
  (2) **掩码是 per-stream 的,没有泄漏到进程**——四个掩码流全部用过之后,
  默认流仍为 44.6 ms(与使用前的 44.6 ms 一致),`default_stream_unaffected`。
  这一条决定了单进程内能否并存不同配额的租户。
  (3) **读回值逐个与请求一致**(`hipExtStreamGetCUMask`)。
  切换开销:在最小/最大配额间逐次交替,相对各自稳态 32 单元 **−0.6%**
  (噪声内)、4 单元 **+6.7%**。故 transition 的一阶模型可取「切换后即为目标
  配额的稳态延迟」,误差上界约 7%,在 Gate B 的 10% 阈值内——但**须用真实模型
  复测**,上述为合成负载。
  注意此处 4→32 加速比仅 4.31×(配额比 8×),与 SDXL 的 5.05× 同样次线性,
  说明 serial 成分不是 diffusion pipeline 特有,合成 matmul 链同样存在。
- 2026-08-03 / amd-cold-model-fails-and-why：Gate B-AMD 的「cold-model 预测
  严格使用缺失字节和实测带宽」一条**判负**,且已定位到原因,不是调参能补的。
  实测(SDXL fp16,6.94 GB):
  **(1) 带宽强烈依赖传输尺寸**——pageable H2D 实测
  1 KB → 0.02、4 KB → 0.10、16 KB → 0.85、64 KB → 2.74、256 KB → 6.28、
  1 MB → 11.71、4 MB → 21.40、64 MB → 27.95、1 GB → 28.28 GB/s,**跨 1400 倍**。
  pinned 与 pageable 在大块上几乎无差(28.41 vs 28.28 GB/s),小块上 pinned 略优。
  **(2) 模型的尺寸分布落在曲线的低端**——6.94 GB 分布在 **2643 个张量,
  中位仅 2.6 KB**。故用单一聚合带宽预测必然大幅低估耗时:
  APE **59–74%**。改成「逐张量、各按其自身尺寸的实测带宽」后降到 **15%**
  (曲线下探到 1 KB 之前是 33%——**带宽曲线的下界必须覆盖负载的实际尺寸分布**,
  这是方法论要点)。此改动**不放松条文**:仍然只用缺失字节与实测带宽,
  零拟合常数(测试解析预测器的返回表达式,存在任何数值字面量即判负)。
  **(3) 但 15% 仍不达标,残差已证明不是传输**。加设「同一尺寸序列的纯拷贝」
  对照(replay,绕开框架):`.to(device)` 实测 0.938 s = replay **0.369 s**
  + 框架开销 **0.569 s**,即 **215 μs/张量,占总时长 61%**。
  框架开销来自 Python 遍历模块树、逐张量分配目标缓冲、重绑定参数——
  **任何「字节 ÷ 带宽」形式的模型都无法表达它**。
  **(4) 该测量本质上是单次的**:模型一旦加载即为热态,重测须重新加载,
  三次独立运行的 observed 为 0.603 / 0.613 / 0.938 s,方差不可控。
  **结论与后续**:本条按现有条文判负并如实记录。若要通过,须先决定条文意图:
  若 cold-start 模型要预测调度器实际承担的墙钟时间,则必须显式包含
  per-tensor 框架开销项(该项**可独立测量**,非从观测反推的拟合常数),
  并把 observed 改为多次重载取分布而非单次;若条文意在只验证传输项,
  则应以 replay 而非 `.to()` 为观测目标。这是条文形式问题,须先定,不得
  以调模型迎合阈值。
- 2026-08-03 / amd-cu-partition-is-not-isolation：**CU 掩码不相交 ≠ 性能隔离**。
  两个各持 16 个不相交单元的独立 SDXL 进程(512²、20 步、各自 30 采样 solo
  基线 + 180 s 并发窗口)实测:
  a 侧 solo p50 **1.786 s → co-run 2.194 s(+22.8%)**,
  b 侧 solo p50 **1.802 s → co-run 2.228 s(+23.7%)**。
  两侧 CV 分别 0.51% / 0.37%,重叠窗口 **180.6 s = 较长窗口的 99.5%**,
  各自 82/83 与 80/81 个样本完全落在共同窗口内——即该数字**不是**「两进程
  错开跑」的假象(错开会给出与零外部性完全相同的读数,这正是必须证明重叠的原因)。
  分区的只有 CU;**L2、显存带宽与命令处理器仍然共享**,约 23% 的外部性由此而来。
  **对调度器的直接约束**:solo 的 quota→latency 表**不能直接用于 co-run 场景**,
  必须带 pairwise externality 修正项;把 CU 配额当作完全隔离的资源会系统性
  低估并发下的延迟约 23%。这也是 Gate B 要求 pairwise externality table 的原因。
  尚未测:externality 是否随配额比例、负载类型(compute-bound vs
  bandwidth-bound)、以及模型对(SDXL+CogVideoX)而变——这些是 externality
  table 的其余条目。
  附带记录:1024² 下两个 16 单元 cell 各需 19.92 GB,合计 39.8 GB **超出 32 GB
  显存**,故 co-run 在 512² 进行。显存是 co-run 的硬约束,harness 现在在
  solo 阶段就据实测峰值预检并拒绝装不下的配对——否则要么 OOM 一无所得,
  要么测到的是 allocator 抖动却被记成 CU 竞争。
- 2026-08-03 / flux-does-not-fit-on-r9700：**FLUX.1-dev 无法在 R9700 上做
  resident profiling**。实测卡上 VRAM 总量 **34.21 GB**（34208743424 B），
  而 FLUX.1-dev 的 fp16 权重合计 **33.74 GB**（transformer 23.8 + T5-XXL 9.5
  + CLIP 0.2 + VAE 0.2），已占 98.6%,不剩激活空间。四个模型的权重量实测:
  SDXL **6.94 GB** ✅ / CogVideoX-2b **13.77 GB** ✅ /
  CogVideoX-5b **21.53 GB**（21.5+激活,偏紧,须实测）/ FLUX **33.74 GB** ❌。
  影响与处置:Gate B 原文要求 profile 四个模型,FLUX 一项在本硬件上**只能**
  以 CPU offload 方式跑,而 offload 会引入 PCIe 传输,测得的就不再是 resident
  solo 延迟——**不得把 offload 结果混入 resident quota→latency canonical
  table**,须另行标注为 state-only / cold residency 档（plan 第 3–4 周本就
  区分 resident / state-only / cold 三档,offload 结果归入后两档是自洽的）。
  这条独立地加强了「第二 GPU SKU 仍须覆盖」这个 Phase 0 硬门的理由:
  H100-80G 能容纳 FLUX 的 resident 全量,R9700 不能,故 AMD 线**在模型覆盖上
  也不足以替代** NVIDIA 线,与此前「AMD 是补充而非替代」的决定一致。
- 2026-08-03 / peak-memory-was-inflated-by-async-execution：**显存峰值的测量
  受同步方式影响,此前的数字偏高**。同一配置(SDXL 1024²、batch=1)在
  per-step 计时改用 CUDA events 并在每次 pipeline 调用后显式
  `torch.cuda.synchronize()` 之后,`max_memory_allocated` 从 **19.92 GB
  降到 10.52 GB**,且 8/12/… 各配额一致(此前只有 4 单元是 10.52 GB,
  ≥8 单元都报 19.92 GB,该「随配额跳变」本身就是异步造成的假象)。
  成因:未显式同步时,相邻两次 pipeline 调用的中间张量在时间上重叠存活,
  峰值把两次调用的激活加在一起。
  **连带修正**:据此,1024² 下两个 16 单元 cell 实际只需 2×10.52 = **21 GB
  < 34.2 GB,是装得下的**;此前依 19.92 GB 判定 39.8 GB 超限而把 co-run 退到
  512²,那个判定基于被测量方法抬高的数字。512² 的 co-run 结论(不相交掩码
  下仍有 +22.8%/+23.7% 外部性)不受影响——它测的是同一现象——但
  **1024² 的 16+16 co-run 应当补测**,因为它更接近真实负载,且是显存峰值表
  与 pairwise externality table 都需要的条目。
  **一般教训**:`max_memory_allocated` 是「历史峰值」,在异步执行下它度量的是
  「同时存活」而非「单次调用需要」。显存峰值表必须声明其同步条件,否则
  两张表在不同 harness 下不可比。
- 2026-08-03 / amd-transition-prediction-passes：**transition prediction 通过**,
  MAPE **7.75%**(阈值 10%,87 个 step 样本)。测法即调度器的真实动作:
  单进程、请求进行中、不重载模型,在 denoising step 之间把租户交给另一个配额的
  stream(`hipExtStreamCreateWithCUMask` + `ExternalStream`)。
  预测器**零自由参数**——「某 step 在配额 q 下的耗时 = 该配额稳态下的 per-step
  中位数」,切换引入的任何瞬态都落进误差而不是被拟合项吸收。
  稳态 per-step 中位数(768²):8 单元 263.34 ms / 16 单元 155.10 ms /
  32 单元 112.36 ms(8→32 加速比仅 2.34×,配额比 4×,与 serial floor 一致)。
  分段:**切换后第一步 MAPE 8.49%,已稳定步 7.59%**——切换确有代价但很小,
  一阶模型「切换后即为目标配额稳态」够用。
  **附带取得一个更强的结果**:三次运行的 latent 校验和**完全相同**
  (`2b7e57c5…`):`steady_8` = `steady_32` = `switching`。即
  (a) 跨 stream 搬运工作不改变计算结果——这是必需的,因为 caching allocator
  并不知道我们用 event 建立的跨流依赖,安全性必须证明而非论证;
  (b) **不同配额下结果逐位相同**,即 CU 掩码不影响数值——Gate A 的确定性
  条文在真实 diffusion 模型上的直接验证(此前只在合成 kernel 上验证过)。
  **过程中修掉一个由数据暴露的 off-by-one**:callback 在 step `index` 结束时
  触发,故 `events[i]→events[i+1]` 测的是 step i+1,应归属**切换之后**的配额;
  原先在切换前记录,把每个区间都记到了上一步的配额上。征兆是反常的分布——
  「切换后第一步 MAPE 2.39% 而已稳定步 78.89%」,恰好反了,只有标签整体
  错位一步能解释(在切换点上,上一步的配额碰巧是「对的答案,错的理由」)。
  修正后 46.10% → 2.52%(冒烟)/ 7.75%(正式)。
- 2026-08-03 / accurate-total-from-cancelling-errors：**一个准确的总数可能来自
  两个方向相反的错误项,必须逐项校验而非只看总误差**。cold-model 加入独立标定
  的框架开销项后,总 MAPE 降到 **3.8%**(阈值 10%),看似通过——但用 replay
  把观测拆成「传输」与「框架」两半后,两项**各自都错**:
  传输项预测 0.508 s 对实测 replay **0.349 s(高估 46%)**;
  框架项预测 0.084 s 对实测 **0.266 s(低估 68%)**;二者相加 0.592 s 恰好
  落在观测 0.615 s 附近。若只报 3.8% 并宣布通过,等于用一个两处都错的模型
  做调度决策。
  标定本身方法正确而量级错误:合成 module 上测得 **31.7 μs/张量**,真实
  pipeline 中实测 **100.6 μs/张量**——差 3 倍,因为 pipeline 的 `.to()` 要
  遍历多个 component,每张量做的事比扁平 module 多。
  处置:harness 现在把每一项对照它所声称建模的那一半观测打分,并输出
  `terms_individually_accurate`。**Gate 判据不变**(仍是严格条文形式的
  per-tensor 17.4%,判负),此项不改变任何裁决,只是使「抵消出来的总数」
  无法被当作可用模型引用。
  这条与 `native-build-is-not-reproducible`、`amd-cold-model-fails-and-why`
  同属一类:**空洞或巧合的通过必须能被机制识别出来,而不是靠人盯数字**。
- 2026-08-03 / corun-externality-is-scale-invariant：补测 1024² 的 16+16
  co-run,与 512² 的结论**高度一致**,说明 CU 分区下的外部性不是某个负载规模
  的偶然:
  512²  → a **+22.8%** / b **+23.7%**(重叠 180.6 s = 99.5%,CV 0.51%/0.37%);
  1024² → a **+24.2%** / b **+24.5%**(重叠 183.6 s = 99.2%,CV 0.20%/0.31%)。
  两个规模相差 4 倍计算量,外部性稳定在 **23–24%**,随规模轻微上升(更大的
  负载 → 更多显存带宽竞争)。**调度器可以把它当作一个近似常数的惩罚项**,
  这比「随负载变化的未知函数」好得多。
  同时**证实了 peak-memory-was-inflated-by-async-execution 的修正**:1024²
  的 16+16 实测只需 **25.2 GB**(预算 30.8 GB),`fits=True` 且顺利跑完——
  此前依未同步时的虚高数字(19.92 GB/cell → 39.8 GB)判定超限而退到 512²,
  那次判定是错的。原始记录:
  `experiments/probes/amd-r9700-cu-mask/corun_sdxl_16_16_{512,1024}_20260803.json`。
  尚未测:非对称配额(如 8+24)、异构模型对(SDXL+CogVideoX)、
  compute-bound 与 bandwidth-bound 负载配对——这些是 pairwise externality
  table 的其余条目。
- 2026-08-04 / cold-model-C-partial：按 C 条文(分项各自达标)重测,
  **传输项达标、框架项未达标**,且两者的原因都已定位。
  **传输项 4.1%** ✓(阈值 10%)。两处修正使其从 46% 降到 4.1%,均无拟合常数:
  (1) 带宽测点加密到 1 KB–1 GB 的**倍增网格**;(2) 取值从**阶梯改为
  log-log 插值**——阶梯取「不超过的测点」,对落在两点之间的尺寸系统性高估
  (倍增网格下仍值 13.4%)。范围外取最近端点而**不外推**,因为外推出的带宽
  不是实测带宽。
  **框架项 88.5%** ✗,未达标。**五种标定方式全部失败,且失败方式各不相同**:
  ① 扁平 module 固定 4 KB 张量 → 32.0 μs/张量(高估 71%);
  ② 8-component 嵌套结构 → 32.2 μs(无改善);
  ③ 加 per-byte 项(`a×count + b×bytes`)→ **429%**,因为 size 轴用
  `module.to()` 标定,它既分配又传输,0.275 ns/byte 等效 3.6 GB/s 正是小块
  传输带宽——**把传输重复计了一次**;
  ④ 尺寸相关曲线(2 KB–64 MB)→ **1448%**,大尺寸段 `to()` 与 replay 都以
  传输为主,差值是噪声而非框架成本(65 MB 处标出 18215 μs/张量);
  ⑤ 小尺寸曲线(2/16/128 KB, count=64)→ 严重低估(89%)。
  真值 18.7 μs/张量,五法给出 2–32 μs。**标定结果对方法极度敏感,尚未找到
  稳定的标定方式**。继续调整标定负载会滑向对目标的拟合,故就此停手并如实判负。
  **过程中取得一个有价值的正面结果:cold 与 warm 是两个状态,不是一个分布加噪声。**
  九次重载实测 observed = `0.585, 0.404, 0.404, 0.405, 0.408, 0.407, 0.410,
  0.410, 0.409`——**只有第一次是离群值**,其后八次 CV < 1%;replay 全程
  CV 0.4%。整体 CV 80.7% 完全来自第一次。成因:进程内首次加载须为每个块向
  驱动 `hipMalloc`,其后复用 caching allocator 的缓存块。
  **冷态 88 μs/张量,热态 18.7 μs/张量,相差 4.7 倍。**
  对调度器:首次 admission 走冷路径,换入换出循环走热路径,**两者必须分开
  建模**,取中位数会两边都描述不准。harness 现在分别记录
  `first_load_seconds` 与 `warm_load_median_seconds`,判据对照**热态**
  (稳态场景),冷态作为 admission 成本单列。
  此前 decision log 中「61% 的加载时长是框架开销」一条据此**修正**:那是
  冷态单次测量的结论;热态下框架仅占 **12%**(0.049 s / 0.402 s)。
- 2026-08-04 / framework-calibration-structured-experiment：按 C 条文补做
  结构化标定实验。**结论:先前五次标定失败的共同根因是「用差值隔离传输」这个
  方法本身不成立,已改为直接测量。**
  **(1) 差值法为何不成立**——「同尺寸序列的纯拷贝」(replay) 作为传输对照,
  其结果强烈依赖 host 侧的分配方式:用**单个大 buffer 的切片**得 **0.351 s**
  (内存连续、已驻留,低估);用**逐张量独立分配**(与真实权重布局一致)得
  **0.493 s**(反而**高于**真实 `.to()` 的 0.454 s,使 framework 变成负数)。
  两种同样合理的构造相差 **40%**,而真值夹在中间——`.to()` 内部的 staging 与
  流水线无法被朴素循环复现。**只要传输靠对照推断,分项判据就无法实施。**
  **(2) 改为直接测量**:`rocprofv3 --memory-copy-trace` 为每次 HtoD 拷贝
  打时间戳,而一个**只加载模型**的进程不产生其他 HtoD 拷贝,故其总时长即传输,
  **无需与进程时钟对齐,也无需任何对照**(`scripts/measure_load_transfer.py`
  + `amd_load_workload.py`)。实测 SDXL:传输 **0.251 s/次**,占 warm 墙钟
  (0.437 s)的 **57.3%**;framework = **0.187 s = 70.6 μs/张量**;
  首次加载 0.642 s(framework 0.391 s),再次印证冷/热两态。
  **(3) 一个使「per-tensor」建模单位失效的事实**:2643 个张量只产生
  **1199 次 HtoD 拷贝**——PyTorch 合并了传输。按张量数线性建模 framework
  在前提上就不成立。
  **(4) framework 的两个分量已直接测得,但只解释了 5.5%**
  (`scripts/amd_framework_calibration.py`,不用差值):
  **遍历** = 把**已在 GPU 上**的 module 再 `.to("cuda")`(无传输、无分配)
  → **2.48–2.60 μs/张量**,且**与张量数无关**(128/512/2048 三点一致)、
  **与模块树结构无关**(depth ∈ {0,2,6} × components ∈ {1,8,32} 九种组合
  全部落在 2.47–2.60 μs)——**这证伪了此前「framework 依赖模块树结构」的假设**;
  **分配** = 直接测 `torch.empty(size, device)` → **1.39–1.48 μs/张量**,
  **与尺寸无关**(2 KB 至 64 MB 一致,warm allocator 下分配是 O(1))——
  **这也证伪了「分配成本随块大小增长」的假设**(方法 ③ 正是据此引入 per-byte 项)。
  两者合计 3.9 μs/张量 = 0.0104 s,仅占 framework 0.187 s 的 **5.5%**;
  其余 **66.7 μs/张量 尚未归因**。候选:pageable→pinned staging 的 host 侧
  memcpy(应与**字节数**而非张量数成正比)、ATen dispatch、以及拷贝合并逻辑
  本身的开销。**下一步是把这三者分别测出来,而不是再换一种猜法。**
  **本条的净进展**:传输项从「靠互相矛盾的对照推断」变为「直接测量」;
  framework 的两个分量有了确定的、与结构和尺寸都无关的常数;
  剩余未归因部分从「原因不明的 88.5% 误差」缩小为「一个已知大小(66.7 μs/张量)
  且有三个具名候选的量」。
- 2026-08-04 / load-cost-fully-decomposed：把 SDXL 的加载成本**逐项拆开,
  每一项都有直接实测支撑**,不含差值推断以外的假设。warm 稳态,2643 个张量
  / 6.94 GB / 中位 2.5 KB:
  | 分量 | 时长 | 占比 | 测法 |
  |---|---|---|---|
  | DMA 传输 | **0.251 s** | 57% | `rocprofv3` copy trace 直测 |
  | `copy_` 的 per-tensor 开销 | 0.059 s | 13% | 用**真实尺寸清单**的合成 replay(0.310 s)减 DMA |
  | `.to()` 相对 `copy_` 的额外开销 | 0.127 s | 29% | 真实 `.to()`(0.437 s)减合成 replay |
  | └ 其中 分配 + 遍历 | 0.010 s | 2% | 直测(1.4 + 2.5 μs/张量) |
  | └ 其中 ATen 路径未归因 | 0.117 s | 27% | 尚待更细的 CPU 端 profiling |
  **两个可证伪的假设已被排除**:
  (a) **staging 不是此尺寸分布下的主因**——同一清单 pinned 0.307 s vs
  pageable 0.310 s,**无差异**。补充实验(固定 2 GB 变张量数)显示 staging
  只在小块**且总量大**时暴露:4096×512 KB 时 pinned 快 26%,64×32 MB 时为 0,
  即 staging 通常能与 DMA 重叠。
  (b) **per-tensor 不是合适的建模单位**——2643 个张量只产生 **1199 次**
  HtoD 拷贝,PyTorch 合并了传输。
  **对调度器的直接价值(本次最有用的产出)**:
  「用真实尺寸清单做的合成 replay」= **0.310 s**,而**尺寸清单是模型元数据,
  无需加载模型即可得到**——即这 0.310 s 是可预测的。真实 `.to()` 要 0.437 s,
  **多出 41%**。若 ASLE 自行管理权重搬运(预分配 + `copy_`)而不走
  `pipeline.to()`,这 41% 即为可回收的成本。这既是一条预测路径,也是一个
  明确的优化机会——**而它只有在分项之后才看得见**,揉进一个总数就没有了。
  **C 条文的当前状态**:传输项已可直接测量(不再依赖会互相矛盾的对照);
  框架项仍未达到 10% 的预测精度,但其未归因部分已从「88.5% 误差、原因不明」
  收敛为「0.117 s / 27%,位于 ATen 的 tensor 构造路径」这一具名且可继续
  细分的量。下一步是 CPU 端 profiling,不是再换一种标定猜法。
- 2026-08-04 / aten-attribution-closes-the-chain：CPU 端 profiling 补上归因链
  的最后一环,并给出一个**对 C 条文本身的限制**。
  `torch.profiler`(CPU activity)对 SDXL 的 `pipeline.to("cuda")`:
  `aten::copy_` **90.11%**(346.6 ms / 2643 次 = 131 μs/次)、
  `aten::empty_strided` **6.95%**(26.7 ms / 2643 次 = 10.1 μs/次,即分配)、
  `aten::_to_copy` 1.59%、`aten::to` 0.78%,其余 < 1.5%。
  **即:没有隐藏的 dispatch 开销**——此前「0.117 s 位于 ATen tensor 构造路径」
  的猜测被修正,九成落在 `copy_` 自身。
  **随之而来的限制**:pageable 源的 `copy_` 是**同步**的,CPU 阻塞直到传输
  完成,故其 CPU 时间**同时是**传输时间。**在 pageable 同步路径下,「传输项」
  与「框架项」不存在干净的边界**——这不是测量手段不足,是被测对象本身没有
  这条界线。C 条文要求两项各自达标,其可实施性因此**依赖于传输路径是否异步**。
  **两个后果,都是正面的**:
  (1) **判据层面**:框架项迟迟无法达标,根因不是模型不好,而是它试图分离
  一个在当前路径下未分离的量。若要严格实施 C,应把观测切换到
  **pinned + 非阻塞**拷贝路径,那时 CPU 提交与 GPU 传输分离,两项才各自有
  明确定义。
  (2) **优化层面**:这正是那 41% 差距的机理。合成 replay(预分配 + `copy_`)
  0.310 s vs `pipeline.to()` 0.437 s——差额来自逐张量的分配(`empty_strided`,
  约 0.020 s)与**无法流水线化的同步拷贝**。用 pinned staging + 异步拷贝 +
  预分配显存池,ASLE 可以把提交与传输重叠,这是**有机理支撑的**优化路径,
  不是猜测。
  **cold-model 条的处置建议**:先把加载路径改为 pinned/异步(那是 runtime
  要做的工作,不只是测量工作),再按 C 验收;在此之前该条如实判负,并记录
  上述机理。**不得在同步路径上继续调标定——被分离的量不存在。**
