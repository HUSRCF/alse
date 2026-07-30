# Toy bench 初步结论：不要寻找唯一货币

这组实验只验证记账语义，不代表真实 GPU 性能。速度曲线是合成的；真实论文
结果必须用实测的 `(model, step, G, SM quota, corunner)` profile 替换。

## 1. 四种货币在记什么

| 货币 | 配额变化最大记账偏差 | 共跑干扰最大记账偏差 | 适合回答的问题 |
|---|---:|---:|---|
| wall time | 346.91% | 28.16% | 任务占用了多少墙钟时间 |
| SM-time | 65.00% | 28.16% | 分配了多少计算容量 |
| FGE progress | 0% | 0% | 完成了多少标准化 diffusion 工作 |
| dominant-resource time | 11.73% | 26.57% | 占用了多少主导硬件资源 |

这里的“偏差”是：完成完全相同的标准化工作后，因为 SM 配额或共跑干扰变化，
记账量相对 full-GPU solo 情况偏离多少。

FGE progress 的零偏差来自它的定义：

```text
delta_v = canonical_full_gpu_solo_time(completed_work) / weight
```

因此被降低 SM 配额或被共跑拖慢的 victim 不会反而被多收费。这个性质适合作为
长期 progress fairness 的基础，但不能说明它同时实现了资源公平。

这里必须使用固定的 canonical 配置，例如每个模型的 full-GPU 高效参考 tile
`G_ref`。当前调度选择的 `(SM quota, G, corunner)` 只进入完成时间和 slack
预测，不能改变同一语义 step 的 FGE 面值；否则选择低效 tile 会让任务被错误
多收费。若在 step 内按 tile 记账，各 tile 的面值之和也必须严格等于该 canonical
step 的面值。

## 2. 轮转实验

两个永久 backlogged tenant 共享 GPU：各有 25% 基础 SM，剩余 50% SM 根据最小
虚拟服务轮转。

- wall/SM-time 在这个配置下各获得 50% SM，但标准化进度是
  `46.27% : 53.73%`；
- FGE progress 得到近似 `50.01% : 49.99%` 的标准化进度，为此分配的
  SM-time 是 `53.63% : 46.37%`；
- FGE 的总吞吐量比等 SM 分配低约 0.2%。这个 trade-off 不是普遍常数，应在
  实机 profile 上画 fairness-throughput Pareto 曲线。

这说明“公平”的定义必须先写清楚：

- 权重代表完成服务的 entitlement：用 FGE progress；
- 权重代表购买的 GPU 容量：用 SM-time；
- 权重代表多资源份额：用 dominant share。

不能用一个 Jain index 混淆这三种不同目标。

## 3. FGE 的主要风险：预测偏差

当 streamed tenant 的 FGE 记账存在系统性误差时：

- 少记 20%：它实际得到 55.56% progress；
- 多记 20%：它实际只得到 45.46% progress；
- 误差为零：两边各约 50%。

因此 FGE 需要离线 profile 加在线校准。可以用已完成 step 的实测时间更新
模型，但不应该直接把受干扰的 wall time 当成 solo work，否则又会把干扰归罪
给 victim。

## 4. Deadline 不能编码进公平货币

toy burst 先通过 full-GPU solo EDF 的乐观可行性检查。相同资源配置下：

- 只按 virtual service 轮转：1 个 urgent deadline miss；
- 加 least-slack guard：0 miss；
- 两者都把 video 的最大无进展间隔限制在 20 ms；
- slack guard 让 video 完成时间增加约 0.6--1.4 ms。

因此 deadline 应作为独立的安全约束或 override，而不是通过临时修改 weight
污染长期公平账本。

## 5. 推荐的双账本设计

调度排序账本：

```text
V_tenant += FGE_completed / weight_tenant
```

资源账本：

```text
R_tenant += (SM-ms, HBM-bytes, PCIe-bytes, resident-GB-ms)
```

使用方式：

1. slack 安全层先识别必须优先服务的 urgent；
2. 无 deadline risk 时，在 tenant 间选择最小 `V_tenant`；
3. tenant 内部用 EDF/FCFS，而不是让同租户短请求通过新建 request 重置
   virtual progress；
4. 用资源账本做 admission、预算和 anti-gaming；
5. 枚举 `(Q, SM quota, tile G)`，选择满足 deadline 与 video progress bound
   的最小资源配置；
6. FGE 效率过低的配置不进入可行集合，防止“低效任务因为进度慢而持续获赠
   更多资源”。

如果论文必须给出一个标量，建议把 FGE progress 定义为公平货币，把 dominant
resource 定义成约束/价格；不要把两者线性混成一个含义不清的 `vruntime`。

## 6. 一个可形式化的“不可能同时满足”命题

设任务 `i` 在 SM 份额 `s` 下的进度速率为 `f_i(s)`，单位时间资源消耗为
`s`。假设存在一个可加的标量货币，单位时间收费为 `c_i(s)`，并同时满足：

1. 等量完成进度必须等价收费，因此 `c_i(s) = a f_i(s)`；
2. 等量 SM-time 必须等价收费，因此 `c_i(s) = b s`。

两式同时成立要求：

```text
f_i(s) / s = b / a
```

也就是所有任务在所有配额下都具有相同且线性的 SM scaling。只要不同 diffusion
模型、tile 或 co-run 情况的 scaling curve 不同，这个条件就不成立。因此单一
可加标量不可能同时精确表达 progress fairness 和 resource fairness。

这个命题可以作为双账本的理论动机。正式写作时还需要明确连续性/可加性假设，
并把 co-run 状态加入 `f_i(s, c)`；结论只会更强。

## 7. I/O reality check：20 GB 只代表 cold whole-model swap

上一版把 20 GB/方向作为每次 rotation 的最坏情况。算术成立，但不能当成
MOSAIC/Bullet 的默认 switch：**峰值显存、模型权重大小和实际 PCIe 字节不是
同一个量**。

本机 RTX 4090 的最大链路是 PCIe 4.0 x16，编码后的单向理论数据率约为
31.51 GB/s。先按 70% 有效率计算：

```text
effective bandwidth = 31.51 * 0.70 = 22.06 GB/s
20 GB / 22.06 GB/s = 0.907 s
```

不同“20 GB switch”定义对应：

| 情况 | 切换时间 |
|---|---:|
| 总共单向搬运 20 GB | 0.907 s |
| 输入实际指 20 GiB | 0.974 s |
| 换出 20 GB + 换入 20 GB，串行 | 1.814 s |
| 换出、换入理想全双工重叠下限 | 0.907 s |

理想全双工要求 H2D/D2H copy engine、root complex、NUMA 内存都能同时维持假设的
70%，所以 0.907 s 应视为乐观下界，而不是默认实测值。

随后在本机空闲 RTX 4090 上用 1 GiB pinned buffer、NUMA-local CPU 实测：

| 路径 | 实测带宽 |
|---|---:|
| H2D | 21.13 GB/s |
| D2H | 22.69 GB/s |
| H2D+D2H 同时提交 | aggregate 21.28 GB/s |

70% 假设与单向实测吻合，但这条 PyTorch/copy-engine 路径没有获得双向吞吐叠加。
所以若确实要 D2H 20 GB 再 H2D 20 GB，约 1.83 s 是可信量级；问题在于正常
rotation 是否真的搬这些字节。

### 实际实现中的三种 switch

| 场景 | 每个服务窗口的 PCIe 数据 | 本机传输时间下界 |
|---|---:|---:|
| Bullet/模型和请求态均 resident | 约 0 | 非零控制、kernel drain 和 cache cost，非模型 DMA |
| 同一 SDXL 模型的 urgent 请求间轮转 | 约 0 | latent 留在 GPU，权重共享 |
| MOSAIC 当前代码，burst 外 stage SDXL 一次 | 5.063 GB H2D + 5.063 GB D2H | 约 0.463 s/burst |
| MOSAIC，immutable host master + drop-only | 5.063 GB H2D | 约 0.240 s/burst |
| 当前 whole StepSwap，Cog2B↔SDXL↔Cog2B | 约 16.9 GB aggregate | 约 0.773 s |
| 两个 20 GB cold model 完整 A↔B↔A | 40 GB H2D + 40 GB D2H | 约 3.66 s |

MOSAIC 的 `serve_pending` 在一次 stage-in/out 之间连续服务整个 pending burst；
因此 burst size 为 4 时，当前 0.463 s 传输摊到每个请求约 0.116 s，而不是每个
请求都付 0.463 s。论文给出的 urgent 自身服务约 0.4 s，因此这时 I/O 占该
urgent residency window 约 22%；若用论文的 13--17 s arrival-to-response latency
作分母，则只占约 3%，因为真实瓶颈是等待最长约 24 s 的 video step boundary。

此外，inference 权重不变，而 pinned host master 已经存在。当前 `Swap.out()`
仍把 urgent 权重 D2H 写回，属于可优化实现选择，不是语义要求；drop-only
可以去掉这半程流量。Step 内 K/V、hidden 和 workspace 在边界释放重建，也不应
作为 continuation state 复制。

Bullet 的路径更接近第一行：weights/KV 预先放在共享 GPU pool，通过 CUDA IPC
映射；SM rotation 主要是修改 stream mask。论文报告 resource reconfiguration
平均约 4.1 us、metadata 收发平均约 0.21 ms。这里还必须另测最长不可抢占
kernel 的 drain、同步、L2/TLB 冷启动和共跑 HBM 干扰，但不能用 20 GB/PCIe
替代这些成本。

### 20 GB cold-swap 假设成立时

只有在每次 quantum 后确实发生完整 cold swap 时，下表才适用：

| compute quantum | 乐观 0.907 s switch 的 I/O 占比 | 串行 1.814 s switch 的 I/O 占比 |
|---:|---:|---:|
| 20 ms | 97.84% | 98.91% |
| 100 ms | 90.07% | 94.77% |
| 1 s | 47.56% | 64.46% |
| 5 s | 15.35% | 26.62% |
| 10 s | 8.31% | 15.35% |

将 I/O 占比压到 10%，驻留计算 epoch 至少要达到 8.16 s（乐观）或
16.32 s（串行）。这和 step-scale、百毫秒 SLO 的轮转不是同一个时间尺度。

因此调度器仍适合两级轮转，但不是所有工作负载都付外层代价：

1. **resident-set 内**：状态已共驻留，按 step/kernel 边界做 FGE + slack
   细粒度轮转；
2. **resident-set 间**：只按缺失权重字节计算 sequence-dependent setup
   cost，采用 batching 和 hysteresis；若都已 resident，这一层退化为零 DMA。

deadline 预测必须加入转移代价：

```text
slack_j =
    deadline_j - now
    - C_switch(current_resident, j)
    - T_remaining(j, SM, G, corunner)
```

若 urgent 到达时即使 full GPU 也有 `slack_j < 0`，公平策略和 SM partition
都无法挽救；必须让模型/权重共驻留、只搬更小的 request delta、使用另一张 GPU，
或者拒绝该 SLO。以 100 ms I/O budget 为例，70% Gen4 x16 最多单向搬
2.21 GB；串行对称换入换出时，每份 state 只能约 1.10 GB。

更合适的模型是：

```text
C_switch(a, b) =
    C_kernel_boundary
  + C_control
  + C_cache
  + missing_weight_H2D_bytes(a, b) / measured_H2D_BW
  + required_state_D2H_bytes(a, b) / measured_D2H_BW
```

PCIe 时间不应加进 FGE 公平进度，因为它没有完成 diffusion work；应记入
`PCIe-bytes` 资源账本和 transition objective。若把 setup wall time 计入
`vruntime`，会再次错误惩罚被迫换出的 victim。

## 8. 下一组实机 microbench

最小 profile 矩阵：

```text
model × denoising phase × tile G × SM quota × corunner type
```

每个点至少采集：

- step/phase wall time；
- full-GPU solo-equivalent work；
- SM active/occupancy；
- DRAM throughput；
- PCIe/NVLink throughput；
- 切换时间和驻留显存。

优先验证四个可证伪假设：

1. 同一 step 在不同 SM quota 下，FGE charge 保持不变；
2. victim 遭受共跑 slowdown 时，FGE 不多收费；
3. tenant-level FGE scheduler 的 weighted service lag 有界；
4. slack guard 在可行 burst 上减少 miss，同时 video no-progress interval
   不超过设定上界。
