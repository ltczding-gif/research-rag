# `rq-2` 失败归因与后续候选策略

- **Status:** Active correction ledger; paper-scoped validity repair in progress
- **Date:** 2026-07-29
- **Benchmark:** ResearchQA `rq-2`，20 篇、254 问、380 个 evidence groups
- **Scope:** 记录 `rq-2` 的失败模式、实现缺陷和可验证优化假设
- **Non-goal:** 本文件不修改冻结问题、冻结笔记、当前候选或自动进入 `rq-5`

本记录是 ADR-003 的诊断附录。它把当前扫描中的丢分拆成可追踪的失败类型，并给每个后续
候选定义单变量边界和验收条件。候选只有在项目所有者审阅 `rq-2` 最终报告后才可进入下一
层；这里的分数是小样本上的 provisional evidence，不是产品质量声明。

## 1. 分析口径

1. 只分析固定的 20 篇论文和全部 254 个问题，不删题、不改题。
2. 254 问中 239 问有可评分 reference groups；15 个无 reference 的 adversarial
   refusal 问题在检索指标中保持 `null`，不把它们错误记成检索失败或成功。
3. Evidence mapping 为 380/380；以下检索失败不是 mapping 缺失造成的。
4. 逐题记录只使用稳定 `row_id`，本文件不复制 ResearchQA 问题、答案、引用原文或论文
   内容。
5. “确认”表示代码路径或 artifact 直接证明；“高置信假设”表示成对结果和实现路径共同
   支持；“待验证”表示只能由下一层单变量实验判断。
6. 历史 run 使用全局 20 篇共享索引，与 ADR 的 `paper-scoped` 合同不一致；该批 artifact
   只保留为 global-corpus diagnostic。实现现已改为逐论文索引，并已启动正式 paper-scoped
   run；global 结果不能完成 ADR 的最终 `rq-2`。

### 1.1 已修复的 P0 合同偏差：旧实现不是 paper-scoped

ADR-003 的 R3 和 sweep design 都明确写了 `paper-scoped`，但旧版
`run_complete_candidate` 只构建一份包含 20 篇的 PDF、note 和 parent 共享索引。
每个 query 对三个全局索引直接 `_search`，没有使用 `question.paper_id` 限定候选。旧双
论文 fixture 只断言目标命中，没有断言跨论文结果必须排除，因此未捕获该偏差。当前实现已
为每篇建立独立检索视图，并有跨论文排除回归。

239 个可评分问题的 global-corpus 诊断为：

| 路线 | Top-10 目标论文占比 | 事后只保留目标论文后的 nDCG 变化 |
|---|---:|---:|
| dense | 95.52% | `+0.0015` |
| BM25 | 85.31% | `+0.0073` |
| hybrid | 94.60% | 约 `+0.0000` |
| PDF-only | 94.60% | 约 `+0.0000` |
| hierarchical | 97.57% | `+0.0018` |
| PDF+note RRF | 47.07% | 约 `+0.2486` |
| note-guided | 10.33% | 大面积无目标论文结果 |
| note-to-PDF | 5.02% | 大面积无目标论文结果 |

这表明 PDF-only、retriever 和 hierarchical 的主要方向基本不由跨论文噪声解释；但当前
三条 note source 路线的大部分灾难性退化来自 global note 污染。事后过滤不是正式分数，
因为它没有重新构建 paper-specific BM25/embedding/rank 语义，只能用于确认根因。

为保留原测试计划的完整性，执行顺序曾冻结为：

1. 当前 global-corpus sweep 按原批准矩阵完成全部 35 个唯一候选；
2. 生成完整但明确标记 `diagnostic` 的同口径报告；
3. 再修复 paper-scoped 实现和两论文干扰回归；
4. 复用不受 retrieval scope 影响的 source、note、chunk 和 embedding 缓存；
5. paper-scoped 的 35 个唯一候选全部完成后，才生成 ADR 最终榜单。

前四步已完成；第五步已写出 35 个 envelope，但仍在修复 9 个旧 adapter 候选和无效 final，
状态详见 1.3。`rq-2` 的合同修复要求是：

1. 为每篇建立或选择独立的 PDF/note/parent 检索视图；
2. query 只在其 benchmark paper 内排名；
3. 增加两论文干扰回归，明确断言另一篇的 chunk 不得进入结果；
4. 35 个唯一候选全部在相同 paper-scoped 语义下重新运行和评分；
5. 已完成 global artifacts 更名/隔离为 diagnostic，不进入最终 leaderboard、bootstrap、
   Pareto 或公开报告。

产品若要在未知论文的全库检索中工作，不能使用 benchmark gold `paper_id`。该问题应另建
`document-router -> top-N paper-scoped retrieval` 候选，并把 gold paper_id 结果只作为
oracle 上界；它不属于当前 `rq-2` 修复。

### 1.2 全 35 候选的假通过复盘

旧 run `rq2-20260728-v12` 已完成 35 个唯一候选中的 34 个，`pdf-structure-aware`
明确失败。旧实现把 `guardrails_passed` 定义成“配置中的指标存在且为有限数”，没有进行
任何相对 baseline 的 slice、hard-failure 或成本检查。因此这个字段不能支持晋级。

按 2026-07-29 固化的修复门重新审计旧 payload：

- 十领域中最多允许一个领域的 primary 退化超过 2 个百分点；
- `multi_hop`、有 reference 的 `adversarial` primary 不得退化超过 2 个百分点；
- 总体 Recall@10 和 all-required-groups success@10 不得退化超过 0.5 个百分点；
- 不允许新增 Recall@10 从非零降为零的 hard failure；
- p95 latency 或 index size 超过 baseline 1.5 倍记为 operational review，不自动伪装成
  生产可用。

复盘结果是：34 个旧“通过”记录中，只有 11 个满足上述质量门，23 个是假通过；13 个还
触发至少一项 1.5 倍成本复核。分阶段结果如下：

| 阶段 | 完成 | 实际通过 | 假通过 | 明确失败 | 成本复核 |
|---|---:|---:|---:|---:|---:|
| PDF chunker | 6 | 1 | 5 | 1 | 3 |
| note chunker | 4 | 1 | 3 | 0 | 0 |
| retriever | 3 | 1 | 2 | 0 | 0 |
| source composition | 5 | 1 | 4 | 0 | 1 |
| reranker | 4 | 1 | 3 | 0 | 3 |
| Top-2 confirmation | 12 | 6 | 6 | 0 | 6 |
| **合计** | **34** | **11** | **23** | **1** | **13** |

这 11 个“通过”包含每组相对比较的 baseline，不能解读成 11 个独立优胜策略。尤其：

1. 旧 provisional winner `top2-confirmation-83a878a5daa76b1bb7ff` 相对对应
   rerank-off baseline 在 biology、machine learning、psychology 三个领域退化超过
   2 个百分点，`adversarial` 退化 `3.19` 个百分点，并新增 1 个 Recall@10 hard
   failure；它不得晋级。
2. 三个 reranker depth 都新增 1 个 hard failure，并在三个领域和 adversarial slice
   退化；depth-20/50/100 的 p95 分别约为 off 的 `7.24x`、`15.04x`、`63.80x`。
3. hybrid 相对 dense 虽提高 primary，但 all-required-groups success@10 退化约
   `1.10` 个百分点并新增 3 个 hard failures，不能只看 nDCG 宣布胜出。
4. fixed-1200 相对 fixed-800 新增 4 个 hard failures，并在三个领域及 adversarial
   slice 越线；它的总体 Recall@5 优势是真实观测，但不是无条件晋级依据。
5. 所有旧 note source 分数还受到 global-corpus 污染；这些数值只能帮助定位失败机制，
   不能作为 paper-scoped 正式分数。

这里还必须再区分一层：表中的 11 个是 **stage-local gate pass**。把 `rankable` 和上游
component eligibility 继续传到 12 个最终确认组合后，只有
`fixed800 + dense + pdf-only + rerank-off`
（`top2-confirmation-0d3b98ed0b9c6f728106`）在旧 global diagnostic 中同时满足本组合
局部门和全部上游门。其余 5 个 rerank-off 自基线虽然通过本组合的局部门，仍因 fixed1200、
hybrid、parent-child 或 hierarchical 上游失败而不能成为最终可用组合。6 个 rerank-50
组合则全部至少新增 1 个 hard failure。

35/35 的逐项状态、失败门、hard-failure 数量、成本复核和对应修复候选已固化到
[`2026-07-29-rq2-global-35-strategy-audit.csv`](2026-07-29-rq2-global-35-strategy-audit.csv)。
以后提到“通过”必须显式写成 `stage-local pass`、`component pass` 或 `final pass`，禁止再用
一个无层级的布尔标签混写三种含义。

这次盘点也区分了“假分数”和“真实但很差的策略”：

| 项目 | 结论 | 处理 |
|---|---|---|
| global shared index | 违反 benchmark retrieval scope，正式榜无效 | 已改为 paper-scoped；旧 run 隔离为 diagnostic |
| finite-only `guardrails_passed` | 假通过的直接来源 | 已改为相对 baseline 的可审计门 |
| reviewer-concern 稀疏高分 | 只覆盖极少内容，不能竞争通用路线 | 已设 `rankable: false` |
| structure-aware 9/20 失败 | 真实失败，不是零分或假分 | 保留 failed；后续只测显式 fallback 新 ID |
| note/hierarchical 低分 | 实现语义下的真实低分，但旧 scope 无效 | paper-scoped 重跑后再判断 |
| 15 个无 reference adversarial | 检索指标应为 null，不是成功或失败 | 新 run 保存 top-1 score 分布，并明确不等于拒答准确率 |
| pre-rerank 覆盖 | 旧 artifact 缺失，无法区分候选缺失和重排破坏 | 新 run 保存 Recall@20/50/100 与 pre/post rows |
| Pareto/public export | 旧 Pareto 缺 stage/status/rank，导出会失败 | 已补齐并要求 Pareto 全部为 eligible completion |
| latency | 数值真实但受固定顺序、温度和 throttling 混杂 | 只作 observed cost；并列 finalist 需受控复测 |
| 宏平均 | question→paper→domain→overall 实现正确 | 保留，不作为阻塞项 |

为保证仍执行批准的完整矩阵，Top-2 维度由配置冻结；即使某个冻结实验臂在前序阶段未
通过，它仍会完成 confirmation 以保留诊断可比性，但上游失败状态会传递到 Top-2，不能
因在 confirmation 中成为 rerank-off 自身 baseline 而“洗白”。最终 winner、Pareto 和
公开报告只允许使用通过全部上游与本阶段门的候选。

修复 scope 前后需要成对比较，所以 Top-2 不再根据 paper-scoped 新分数临时换臂。配置
已冻结为 PDF `fixed-800/fixed-1200`、retriever `dense/hybrid-rrf`、source
`pdf-only/hierarchical-pdf`、reranker `off/depth-50`；16 个笛卡尔组合按 hierarchical
兼容规则去重为 12 个，整个 sweep 始终是 35 个唯一候选。

同理，正交阶段的串联 anchor 也冻结为旧 global diagnostic 实际使用的
`fixed-1200 + note-reviewer-concern + hybrid-rrf + pdf-only`。这里保留
`note-reviewer-concern` 只是为了让 scope 修复前后 35 个 config ID 可成对比较；它本身
仍为 `rankable: false`，后续 `N0/N3` 才会用通用笔记 eligibility 修复替代它。

### 1.3 Paper-scoped v1 的有效性复盘

正式 run `rq2-20260729-paper-scoped-v1` 已在相同的 20 篇、254 问、239 个可评分问题和
380/380 evidence groups 上写出 35 个候选 envelope，但不能据此发布 winner。终态为：

| 阶段 | 完成 | 确定性策略失败 | 基础设施失败 |
|---|---:|---:|---:|
| PDF chunker | 6 | 1 | 0 |
| note chunker | 4 | 0 | 0 |
| retriever | 3 | 0 | 0 |
| source composition | 5 | 0 | 0 |
| reranker | 3 | 0 | 1 |
| Top-2 confirmation | 6 | 0 | 6 |
| **合计** | **27** | **1** | **7** |

`pdf-structure-aware` 的 `StrategyContractError` 是可复现的策略合同失败；depth-100 的
`SystemError` 和 6 个 rerank-50 confirmation 的 CUDA illegal-memory-access 是基础设施
失败，不能转换成策略分数。旧 runner 仍在这种状态下生成了 `decision-summary`、
leaderboard 和 Pareto，因此这些 final artifacts 已隔离，不能保留或发布。

reranker 基础设施失败的直接根因已经由代码和 live 资源共同确认：

1. 旧 `_score_batch` 调用 `model(**batch)`，让 causal LM 生成
   `[batch, sequence, 151669]` 的完整词表 logits，随后才取最后 token；
2. 实际最长格式化输入约 1,171 tokens，旧进程曾占用约 7.70/8 GB GPU memory；
3. 固定模型的 `forward` 原生支持 `logits_to_keep=1`；
4. 4 个真实 query/passage pair 的 canary 旧、新 score 最大绝对差为 `0.0`，排序一致；
5. 第一个完整 depth-20 候选进一步证明 254/254 的 ranked item IDs、逐题 metrics、
   aggregate 和 mapping 全部相同；只有 1 个 rank-7 raw BF16 score 从 `2.25` 变为
   `2.375`，最大差 `0.125`。因此合同应写成 rank/metric parity，而不是宣称所有 raw
   score 位级一致；
6. 该候选 observed p95 从 `1822.03 ms` 降到 `1447.25 ms`（约 `20.6%`），但 GPU
   software thermal slowdown 为 active，所以不能把这次差值当作最终受控 latency；
7. 修复后的 fresh CUDA 进程运行时约占 2.1–2.4 GB，尚未观察到 CUDA/OOM；该项在
   9 个定向候选全部完成前只记作运行健康证据，不记作完成。
8. 第二个完整 depth-50 候选也通过新旧 payload SHA 校验，input fingerprint 按新 adapter
   发生变化；254/254 ranked item IDs、逐题 metrics、pre-rerank IDs/metrics、aggregate 和
   mapping 全部一致。4 个 raw reranker scores 发生 BF16 量化差异，最大绝对差 `0.125`，
   未改变任何排序或指标。observed p95 从 `6751.62 ms` 降到 `3667.80 ms`（约
   `45.7%`），但同样处于 software thermal slowdown，不能用于正式决胜。
9. 截至 2026-07-29 04:44（Asia/Shanghai），定向进程已写出 depth-20/depth-50 两个候选，
   depth-100 在途，stderr 仍只有 tokenizer 性能提示。两个候选的
   `guardrail_finalized=false` 是 reranker stage 尚未整体完成的 pending 状态，不是 pass。

代码提交 `48d4d01` 同时关闭了三个复用漏洞：last-token-only logits、显式
`adapter_revision`、以及 rerank-enabled candidate fingerprint。`rerank-off` fingerprint
保持不变。冻结配置重新生成 candidate plan 后，受旧 adapter 影响的精确集合是 **9 个**：
reranker depth 20/50/100 三个，加 6 个 rerank-50 confirmation；不是先前估计的 10 个。
其余 26 个 checkpoint 的质量 payload 未受 adapter 影响，但不能直接视为满足新状态合同。
旧 9 个 envelope 和 17 个旧 final/stage/report 文件已移动到 run-owned quarantine，并
逐文件验证 SHA-256。

同一提交还把 `execution_complete` 与 `guardrail_finalized` 分开：没有
`guardrail_diagnostics` 的 completed envelope 只能是 `pending`，不能称 pass；
incomplete、pending、基础设施或未知失败会阻止 stage/final/public export。失败 envelope
现在预留 candidate、failure kind、phase/row/pass/progress 和 traceback 字段。当前回归证据为
`416 passed, 2 skipped`，benchmark validator 在仓库 Wave 0A 合同下通过。

2026-07-29 对当前 paper-scoped candidate 目录的逐 envelope 审计进一步确认：

1. 当前 28 个正式 envelope 的 payload SHA 全部与 canonical payload 一致，没有哈希损坏；
2. 其中 27 个 completed，1 个为 structure-aware 的确定性
   `StrategyContractError`；当前候选数量与“depth-100 和 6 个 reranked confirmation
   尚未落盘”的运行状态一致；
3. 两个 fresh reranker envelope 已显式包含 `execution_complete=true`；其余 **25 个**
   legacy completed envelope 没有该字段。它们有完整的 20 paper IDs、254 question IDs、
   254 rows 和 metric bundle，因此旧质量结果不是凭空生成，但仍不足以证明新 envelope
   合同已满足；
4. legacy structure-aware 失败 envelope 只有 `error/error_type`，缺少显式
   `execution_complete=false`、`failure_kind=strategy` 和 `failure_context`；
5. 根因是 `48d4d01` 改变了 checkpoint/发布状态语义，但
   `SWEEP_ENGINE_REVISION` 仍为 `researchqa-sweep-v9`，当前 loader 也没有拒绝缺少新
   必填字段的旧 envelope。最终修复必须让 loader 对 completed/incomplete/failed 分别
   验证新必填字段并 fail closed；定向 rerank 退出后只重执行这 26 个 legacy 集合，
   不能静默把旧 `status=completed` 翻译成新 `execution_complete=true`。这样既保留
   fresh 9 个 adapter 修复结果，又不以迁移字段伪装重新验证。

这只关闭了 fail-open 发布，还没有实现候选内部恢复。`run_complete_candidate` 当前先执行
全部 254 条 quality rows，再执行 warmup/timed latency passes，最后才返回完整结果；
`execute_stage` 看不到内部进度。若中途抛异常，新失败 envelope 仍只能写
`phase=candidate-execution`、`row_id=null`、`pass_index=null` 和空的已完成论文/问题列表。
旧隔离区中的 depth-100 `SystemError` payload 甚至只有 `error/error_type`，6 个 CUDA
失败也无法证明发生在 quality 还是 latency。因此，“字段存在”不能当作“原子 resume
已经实现”。

在后续实现前冻结恢复合同：

1. 使用独立且绑定 input fingerprint 的 progress artifact，至少包含 candidate ID、
   engine/progress schema revision、已完成 quality rows、当前 phase 和 payload SHA-256；
   partial artifact 永不参与排名，也不被 stage ranking 当作 candidate envelope。
2. quality phase 在完整论文边界原子 checkpoint，失败时记录精确 `paper_id/row_id`；
   resume 必须验证 row ID 唯一且属于预期集合，并验证已存 question results 的哈希，只执行
   缺失 rows。
3. latency 只在完整 pass 边界 checkpoint。warmup/timed pass 内失败时丢弃该不完整 pass，
   只从下一完整 pass 恢复，保证候选 sample count 一致；artifact 记录 phase、pass
   kind/index、当前 row、固定 performance row IDs 和已完成 samples。
4. `ModelTransportError` 仍可交给外层有界重试，但最新已验证 progress artifact 必须保留；
   infrastructure/unknown failure 引用其路径与 SHA，不得再用空 progress 覆盖。
5. 只有 quality 完整、latency 完整、聚合完成且 final envelope 原子写入并验 hash 后，
   progress 才能 finalized。source/note/model/adapter/config/code 任一 fingerprint 不同都使
   resume 失效并 fail closed。

当前 latency 还有独立的假 winner 风险。`rank_candidates` 对 primary 差不超过 `0.005`
的候选立即用 `p95_latency_ms` 决胜，Pareto 也无条件把该值当作可比维度；但 sweep 是逐
候选串行测量，未平衡候选顺序和 GPU 热状态。修复后的 depth-20 实测期间 GPU software
thermal slowdown 已为 active，所以其 p95 只能解释为 observed cost，不能作为并列决胜
证据。

冻结 controlled finalist latency 合同：

1. 每个 latency artifact 显式写 `validity=decisive|observed-only`、原因、相同的 40 个
   performance row IDs、每 pass 样本、模型/adapter、batch/dtype、CPU/RAM/GPU 和热降频
   状态；聚合值不能丢掉 pass-level 证据。
2. 初始全矩阵的串行 p95 默认 `observed-only`，只用于成本诊断。先按质量与 guardrails
   形成 `0.005` tie group，再只对该小组做受控复测，避免为了延迟重跑全部策略。
3. 每个 finalist 先独立 warmup；timed passes 在候选间按固定 seed 轮换/交错，保证相同
   pass 数和 sample count。中断只丢弃当前不完整 pass，并按上面的原子 progress 合同恢复。
4. 任一 timed pass 出现 hardware/software thermal slowdown 或状态未知，该 pass 不得成为
   decisive。若一次冷却后的交错复测仍无法取得无热降频环境，保留 observed-only 数值并在
   tie-break 中跳过 latency，继续使用 index bytes、chunk count、config ID；不得无限重跑
   到偶然有利。
5. observed-only latency 不得支配 Pareto 点，也不得产生 winner 文案。只有完整受控 artifact
   通过相同输入、等样本和环境门后，p95 才能进入正式 tie-break。

Paper-scoped 结果还更新了三个后续判断：

- `F1` 800/1200 多粒度路线停止：2,152/2,153 个 fixed-1200 chunk 已被 fixed-800 高度
  覆盖，平均 overlap 约 `0.965`，核心指标没有变化；
- `RR1` 冻结为 depth-20、保留 base top-1、再做等权 rank-RRF。离线重放 primary
  `0.853774`，相对 rerank-off `+0.019248`，没有新增 hard failure；
- `R1` 冻结为保留 dense top-1，再做 dense:BM25=`2:1`、`k=60` 的 RRF。离线重放
  primary `0.831811`，相对 dense `+0.010229`，没有新增 hard failure。

这些离线重放只用于冻结下一候选，不替代正式 runner、完整 guardrails 或最终报告。

## 2. 总体判断

当前主要瓶颈不是 embedding 模型，而是五类策略边界：

| 类别 | 当前证据 | 判断 |
|---|---|---|
| PDF 粒度 | 1200 字符总体最好，但 800 字符在单组、lookup 和部分 adversarial 更稳 | 不存在单一全局最优粒度，优先测多粒度互补 |
| 笔记切分 | reviewer-only 表面得分高，但仅覆盖 2/20 篇 | 这是 coverage 偏差，不是通用笔记路线胜出 |
| 检索融合 | hybrid 总体略优，但 dense 在多组和 comprehension 上有互补 | 等权 RRF 不是终点，应测固定 dense-heavy 或词法补救 |
| 语料组合 | 所有当前笔记路线都显著弱于 PDF-only | 当前硬过滤/等权融合放大了稀疏笔记噪声，必须有 eligibility 和 no-op fallback |
| 重排 | depth-50 提升多跳/多证据题，但伤害部分单证据和 adversarial | 应融合基础排序与重排分，而不是无条件替换原排序 |

## 3. PDF 切分失败

### 3.1 聚合结果

`pdf-fixed-1200` 的 evidence-group Recall@5 为 `0.9143`，高于
`pdf-fixed-800` 的 `0.9047` 和 `pdf-fixed-400` 的 `0.8312`。但它不是所有 slice
都占优：

| 对比 | 结果 |
|---|---|
| fixed-1200 vs fixed-800，逐题 nDCG@10 均值 | `+0.0213` |
| 至少提升 0.1 的题 | 49 |
| 至少退化 0.1 的题 | 33 |
| comprehension | `+0.0559` |
| multi-hop | `+0.0222` |
| lookup | `-0.0013` |
| adversarial（有 reference） | `-0.0078` |
| 2 个 evidence groups | `+0.0539` |
| 3 个及以上 evidence groups | `+0.0837` |

`fixed-800` 更适合局部精确命中，`fixed-1200` 更容易把跨句或跨段的完整证据留在同一块。
因此平均分提升同时伴随细粒度问题退化，属于真实的粒度权衡。

fixed-1200 的 R@5 在单页证据为 `0.954`，跨页降到 `0.852`，跨 section 为
`0.754`，multi-hop 也只有 `0.754`；切分优化仍不能替代多证据覆盖检索。fixed-400 的
chunks 是 fixed-1200 的约 3.1 倍，但 R@5 低约 8.5 个百分点，显示小块同时造成语义碎裂
和 top-k 拥挤。`section-aware` 的中位块长只有 201 字符，5543 个块中 2755 个短于 200；
`page-aware` 也有 149 个短块。标题和短段被过度拆分是它们 MRR 退化的高置信原因。

六个成功 PDF chunker 的逐题 oracle R@5 为 `0.9637`，比 fixed-1200 高约 4.94 个
百分点；这只证明多粒度有互补空间，不能用 row、domain 或 gold 题型做 oracle 路由。

`pdf-structure-aware` 在 `W2800002600` 触发
`structure-detection-failed`，导致整个候选失败；对 20 篇逐篇执行同一纯 chunk 检查时，
共有 9/20 篇未检测到结构标记。代码当前按文档设置 `detected=False`；某篇文档没有命中
启发式结构标记时直接返回 failed，而不是局部记录“不适用”或切换到另一个有独立 ID 的
确定性策略。这是确认的候选适用性问题，不能据此断言结构切分本身无效。

只在 `detected=False` 时回退 fixed-1200 的初版 F2 虽能覆盖 20/20、映射 380/380，
仍不能直接进入正式比较。其总 chunk 数只是 fixed-800 的 `1.170x`，但逐论文 expansion
p95 为 `2.095x`、最大 `2.167x`：`W3040245690` 从 126 块增至 273 块；
`W2792307011` 产生 251 块，中位长度仅 51 字符，167 块短于 100 字符且有 25 个重复文本；
`W3094793347` 产生 1,061 块并有 65 个重复文本。根因是 heading heuristic 过检后，
聚合被限制在每个伪 section 内，短 section 无法跨边界合并。因此 F2 的 fallback 必须同时
覆盖 detection failure 和“检测成功但病态碎片化”，并在查看质量分数前冻结逐论文
expansion、短块率、重复文本数和全局成本门。

2026-07-29 已在不读取任何质量分数的前提下冻结 F2 结构门：

1. `structure-detection-failed`：该论文回退 fixed-1200；
2. structure chunk count / fixed-1200 chunk count `> 2.5`：该论文回退；
3. 长度 `<100` 字符的 chunk 占比 `> 0.40`：该论文回退；
4. exact duplicate text 至少 5 个且占比 `>= 0.04`：该论文回退；
5. 完成逐论文回退后，全局 chunk count / fixed-1200 chunk count 必须 `<= 1.25`；
   超过则候选合同失败，不查看质量分数后继续补调阈值。

F2 的代码接入边界也在质量评测前冻结，避免把修复候选混入原 35 项基线或复用错误
provenance：

1. `PDF_CHUNKER_IDS`、`rq2-overnight.yaml` 和 config schema 继续只表示原批准的 7 个
   PDF chunker；F2 进入独立的 repair/extension ID 集合，不能改变原候选数、原候选
   fingerprint 或 stage membership；
2. chunk dispatch 和 `StrategyCandidate` 只把
   `pdf-structure-aware-fallback` 加入“可执行 ID”全集；原
   `generate_orthogonal_candidates()` 仍严格验证并生成原 7 项，F2 由显式 repair
   candidate 入口创建，`stage_id=pdf-chunker` 且使用新的候选 config ID；
3. 无论论文采用 structure 路线还是 fixed-1200 fallback，都必须用 F2 的 chunker ID、
   canonical source spans、文本和 F2 fingerprint 重新物化 chunk ID；禁止只替换
   `config_id` 或复用 `pdf-structure-aware` / `pdf-fixed-1200` 的旧 chunk ID；
4. 每篇输出 detection 状态、structure/fixed-1200 块数比、短块率、重复数与比例、
   是否 fallback 及唯一 fallback reason；这些诊断和阈值 revision 进入候选输入指纹。
   全局输出总块数比、fallback paper IDs/rate 和最终合同状态。partial/超成本结果
   不可排名，也不能覆盖原 structure-aware 的确定性失败记录。

`2.5` 相对 800/1200 的目标粒度仍保留了充足结构开销；`40%` 短块意味着近半索引单位
已经与 800 字符目标相悖；重复门同时要求绝对数量和比例，避免短论文因单个重复误触发。
在当前 20 篇生产输入上，该门使 9 篇 detection failure 和 4 篇病态碎片化论文回退；
总块数为 2,520，即 fixed-1200 的 `1.170x`、fixed-800 的 `0.820x`，逐论文相对
fixed-800 的 p95/max 从 `2.095x/2.167x` 降到 `1.481x/1.678x`。这些统计只证明结构与
成本合同，不构成 F2 的质量结论。

`pdf-parent-child` 的 Recall@5 为 `0.8156`，低于固定切分；它的层级组合还存在二次召回
瓶颈，见第 6 节。

### 3.2 后续候选

| ID | 单变量候选 | 目的 | 验证要求 |
|---|---|---|---|
| `F1` | `pdf-multigranular-800-1200-rrf` | 融合 800 的局部精度与 1200 的多证据覆盖 | 已停止：chunk 高度重叠且核心指标零变化 |
| `F2` | `pdf-structure-aware-fallback` | 无结构或病态碎片化时回退 fixed-1200 | 冻结逐论文 expansion/短块/重复/全局成本门；报告 fallback 率 |
| `F3` | `pdf-fixed-1200-adjacent-expand` | 命中后只扩展相邻块，补跨边界证据 | 主排序不变；单独计 expansion 带来的召回和字节成本 |
| `F4` | `pdf-heading-prefixed-1200` | 把 section path 作为可检索上下文 | provenance span 仍只指向正文；标题不得伪装成证据 |
| `F5` | `section-aware-min200-merge` | 合并过短标题/短段，保留结构但减少碎片 | 报告短块率、MRR、跨 section R@5 |
| `F6` | `multi-hop-decompose-diversify` | 生成无答案泄漏的 2–3 个检索子查询并做 page/section 多样化 | 只看线上 query；主门是 all-required-groups success@5/10 |
| `F7` | `adversarial-claim-verification` | 分别检索研究范围与被断言结果，寻找限定/反驳证据 | 不读取 expected_refusal、false_premise 或 gold evidence |

优先级只保留 `F2`（恢复实验有效性）。`F1` 已停止；`F3/F4` 不在当前 rq-2 自动执行范围。

所有成功 PDF chunker 都难以处理的 14 题中有 10 题是 multi-hop。三个全策略 R@5 为零
的稳定反驳案例为 `W3094793347_adversarial1`、`W3154248945_adversarial0`、
`W4225278475_adversarial1`；它们都包含错误前提，需要找反驳证据，单纯改块长不够。
典型的跨页、跨 section、多组证据持续失败包括
`W3011534780_multihop0`、`W3086667591_multihop1`、
`W3096486083_multihop0`、`W3154248945_multihop0`、
`W4304202992_multihop0`、`W4389861133_multihop0`。

## 4. 笔记切分失败

### 4.1 聚合结果与 coverage 偏差

| note chunker | 笔记块 | 覆盖论文 | 可回链块（fixed-1200） | paper-scoped R@5 | 旧 global R@5 |
|---|---:|---:|---:|---:|---:|
| `note-whole` | 20 | 20/20 | 20 | 0.4688 | 0.2456 |
| `note-section` | 323 | 20/20 | 270 | 0.4608 | 0.4172 |
| `note-claim-evidence` | 103 | 20/20 | 103 | 0.4651 | 0.3678 |
| `note-reviewer-concern` | 4 | 2/20 | 4 | 0.8878 | 0.6770 |

`note-reviewer-concern` 的表面第一名不是广泛有效的 reviewer 检索。当前解析器只把 verdict
表中结尾为 `major` 的行生成 concern chunk；通用模板允许论文没有 fatal/major concern，
于是 18 篇产生零块。该候选几乎退化成 PDF-only，只在两篇上加入 4 个稀疏块，因此它与
全覆盖 note chunker 的分数不可直接比较。

引用投影还存在扇出放大。`note-whole` 每篇只有一个约 10k 字符块，平均投影到 90.1 个
PDF chunks；`note-section` 和 `note-claim-evidence` 的单块 backlink 中位数仍约为 25
和 27。equal-weight RRF 会把一个宽泛笔记命中的大量同页 chunks 同时推入排序，削弱块间
区分。reviewer 路线的 4 个块又来自两篇论文；全库 top-5 诊断中，目标论文占比只有
0.427，1270 个 top-5 hits 里有 605 个集中到 `W2792307011`。这解释了稀疏笔记为何仍能
污染其他论文的查询，而不是简单地“没有贡献”。

这不是要求生成器虚构 major concern。正确修复是对策略施加 coverage/eligibility
合同、限制 citation fanout，并把 reviewer concern 当辅助信号。

8 篇论文带 external SI。当前一些 note chunks 混合 Main/SI 内容，但反链只能投影到 Main
PDF；因此可能出现“由 SI 语义命中、却给 Main 页加权”的 provenance 错配。现有 SI 与
non-SI 论文之间的分数差不能作因果判断，后续必须用同一论文的 Main-only 与 SI-informed
note 做受控消融。

### 4.2 N0/N3 生产输入审计与冻结合同

2026-07-29 对冻结的 20 篇笔记、对应 Main native IR 和 `pdf-fixed-1200` chunks 做了只读
重算。N0 所需的逐论文 coverage 不是全局平均值：

| note chunker | 非空论文 | 至少一块可回链论文 | 非空块 | 可回链块 | 可回链率 |
|---|---:|---:|---:|---:|---:|
| `note-whole` | 20/20 | 20/20 | 20 | 20 | 100% |
| `note-section` | 20/20 | 20/20 | 323 | 270 | 83.59% |
| `note-claim-evidence` | 20/20 | 20/20 | 103 | 103 | 100% |
| `note-reviewer-concern` | 2/20 | 2/20 | 4 | 4 | 100% |

据此冻结 `N0 note-route-eligibility-gate`：

1. 在每篇论文自己的 PDF chunks 上计算 eligibility；至少存在一个 `text.strip()` 非空且
   backlink 集合非空的 note chunk 才算该论文 eligible。不得用全局块数或平均回链率替代
   逐论文判定。
2. note 增强路线对不 eligible 的论文必须确定性退化为该候选的 direct PDF-only 路线，
   并报告 eligible/fallback paper IDs、块数、可回链块数和 fallback rate。它不是策略失败，
   但在 diagnostics finalized 前不得排名。
3. `note-reviewer-concern` 当前有 18/20 篇触发 fallback，因此继续保持
   `diagnostic-only`/`rankable: false`；不能因为补了 PDF fallback 或表面分数较高就晋级。
4. backlink fanout 只作为报告字段，不能暗中变成 N0 调参门。span cap 属于独立的 N4，
   不在观察本轮质量分数后反向写入 N0。

当前 `_VERDICT_ROW_RE` 不是完整的模板解析器。它只接受首单元格以一个 `C<数字>` 开头的
行，因此只解析 58 条真实 verdict rows 中的 51 条：4 `major`、47 `minor`；漏掉 7 条
`C1/C2`、`C2–C5` 等多 claim/range 行，其中包括 `W3094793347` 的全部 3 行。按
“Adaptive Red-Team Verdict”节内、末列 severity 的表格边界重新审计后，生产输入为：

- 58 条结构化 verdict rows，且没有未识别 severity；
- 0 `fatal`、4 `major`、54 `minor`、0 显式 `zero`；
- 2/20 篇至少有一个 `fatal`/`major`，18/20 篇只有 `minor`；
- 当前 4 个 reviewer chunks 正好对应 4 条 `major`，均可回链；高覆盖的 54 条
  `minor` 不能伪装成 54 个 surviving major concerns。

据此冻结 `N3 note-concern-parser-contract`：

1. 只在明确的 `## 审稿人视角（Adaptive Red-Team Verdict）` 节内解析 verdict 表；
   缺节、坏表头、未知/缺失 severity 必须 fail closed，不能当成 `zero`。
2. severity 规范化为 `fatal|major|minor|zero` 的结构化字段；显式 `zero` 与“没有
   fatal/major”是两件事，不得自动补造 verdict row。
3. claim 单元格支持单 ID、斜线列表和同前缀数字范围；例如 `C1/C2` 解析为
   `C1,C2`，`C2–C5` 确定性展开为 `C2,C3,C4,C5`。所有 ID 必须存在于 claim blocks，
   重复项去重，逆序或无效范围 fail closed。
4. `fatal`/`major` 才能进入 optional reviewer-concern 检索支路；`minor`/`zero`
   保留为结构化诊断或 claim metadata，不生成 surviving concern。没有 fatal/major 的
   论文在 N1 中自然使用全覆盖的 `note-claim-evidence` 底座，不虚构科学质疑。
5. reviewer-only 候选始终只作 parser/coverage diagnostic。N3 单测必须覆盖四种 severity、
   多 claim、范围、无 major/fatal、未知 severity 和缺失 reviewer 节；本修复不以质量分
   提升为验收条件。

`N1 note-claim-plus-reviewer` 也据此冻结为确定性组合，不重新生成笔记：

1. 每篇的 `note-claim-evidence` 是必需底座；当前生产输入为 20/20 篇、103/103 块可回链。
   任一论文底座不满足 N0 时，该论文按 N0 退化为 PDF-only，不能仅靠 reviewer 块补成
   eligible。
2. N3 解析出的 `fatal`/`major` chunks 作为可选 reviewer 支路加入同一论文；没有
   fatal/major 时，N1 必须与该论文的 claim route 等价。`minor`/`zero` 只保留 metadata。
3. base/reviewer chunk role、severity、claim/evidence IDs、backlinks 和逐论文块数进入
   diagnostics 与 fingerprint。不得按当前问题、gold evidence 或观察到的质量分决定是否
   启用 reviewer 支路。
4. N1 使用新 config ID；现有 `note-claim-evidence` 和 diagnostic reviewer-only 结果继续
   保留，不能被覆盖或改写成 N1 分数。

### 4.3 后续候选

| ID | 单变量候选 | 目的 | 验证要求 |
|---|---|---|---|
| `N0` | `note-route-eligibility-gate` | 防止稀疏 note route 以近似 no-op 获得虚高排名 | 报告非空论文率、块数/篇、可回链率；不满足阈值不得排名 |
| `N1` | `note-claim-plus-reviewer` | claim/evidence 做全覆盖底座，reviewer concern 作为标签或加权支路 | reviewer 为空时自然退化到 claim route，不生成虚假质疑 |
| `N2` | `note-section-plus-claim` | 兼顾主题导航和可回链证据单元 | 对重复引用去重，并报告每一路独立贡献 |
| `N3` | `note-concern-parser-contract` | 覆盖 fatal/major/minor/零 concern 的模板—解析器合同 | 这是测试/有效性修复，不作为质量增益候选 |
| `N4` | `note-backlink-span-cap` | 避免一个宽泛引用扩散到整页大量 PDF chunks | 报告每块 backlink 分布、截断率和被保留 span |
| `N5` | `main-si-dual-note-index` | 分开 Main-only、SI-only 和 mixed provenance | Main benchmark 中分别报告 Main-only 与 SI-assisted 消融 |

## 5. Dense、BM25 与 hybrid 丢分

### 5.1 聚合结果

| retriever | coverage-nDCG@10 | 明显特征 |
|---|---:|---|
| `dense` | 0.8216 | comprehension、多组证据相对更稳 |
| `bm25` | 0.7850 | lookup 最强，其他题型明显偏弱 |
| `hybrid-rrf` | 0.8345 | 总体较高、lookup 受益，但当前 guardrail 不通过 |

paper-scoped hybrid 相对 dense 的 primary 为 `+0.01294`，逐题 nDCG 均值为
`+0.01436`，但 slice 方向不一致：

| slice | hybrid - dense |
|---|---:|
| lookup | `+0.0391` |
| adversarial（有 reference，逐题均值） | `+0.0173` |
| comprehension | `-0.0054` |
| multi-hop | `+0.0023` |
| 1 个 evidence group | `+0.0230` |
| 2 个 evidence groups | `+0.0114` |
| 3 个及以上 evidence groups | `-0.0181` |

等权 RRF 带来稳定的词法补救，但会让 BM25 对多证据问题的弱排序稀释 dense 结果。
宏平均的 adversarial slice 实际退化 `-0.0521`，all-required-groups success@10 退化
`-0.00608`，并新增 3 个 Recall@10 hard failures，因此不能被总体增益洗白。下一步应先测
固定权重，不使用 benchmark 的 `question_type` 或 gold group count 做路由。

### 5.2 后续候选

| ID | 单变量候选 | 目的 | 验证要求 |
|---|---|---|---|
| `R1` | preserve dense top-1 + dense:BM25 `2:1` rank-RRF (`k=60`) | 保留 lookup 词法补救，减少多证据稀释 | 参数已冻结；正式 runner 报告 dense/BM25 独立召回和 hard failures |
| `R2` | `dense-plus-bm25-rescue` | dense 为主，只补入未覆盖的高置信词法结果 | 固定 score/rank 门槛；补救结果须增加新 evidence group |
| `R3` | `hybrid-diversity-aware` | 减少 top-k 被同页、同段近重复块占满 | 报告去重前后 evidence coverage 和排序变化 |
| `R4` | `adaptive-retrieval-nonoracle` | 用线上可获得的 score gap、熵、两路分歧决定融合强度 | 禁止读取 question_type、paper_id gold 或 expected references |

`R1` 优先于 `R4`；当前只验证上述冻结权重，不再扫描更多比例。

## 6. Source composition 丢分

### 6.1 聚合结果

以下为发现合同偏差前的 global-corpus diagnostic，只用于定位 source 路线的噪声机制；
最终 ADR 合规排名必须来自 paper-scoped 重跑。

| source composition | coverage-nDCG@10 | 相对 PDF-only 的判断 |
|---|---:|---|
| `pdf-only` | 0.8302 | 当前可靠基线 |
| `hierarchical-pdf` | 0.7290 | 有 26 题改善，但 103 题退化 |
| `pdf-note-rrf` | 0.5487 | 只改善 3/239，退化 171/239 |
| `note-guided-pdf` | 0.0827 | 硬过滤造成大面积召回坍塌 |
| `note-to-pdf` | 0.0073 | 只依赖笔记引用投影，几乎丢失直接 PDF 召回 |

paper-scoped 重跑把 global note 污染从结果中移除，但没有把这些路线变成合格候选：

| source composition | paper-scoped coverage-nDCG@10 | 相对 PDF-only | 新 Recall@10 hard failures |
|---|---:|---:|---:|
| `pdf-only` | 0.8345 | baseline | 0 |
| `pdf-note-rrf` | 0.8027 | -0.0318 | 3 |
| `hierarchical-pdf` | 0.7343 | -0.1002 | 9 |
| `note-guided-pdf` | 0.0810 | -0.7535 | 209 |
| `note-to-pdf` | 0.0186 | -0.8160 | 221 |

因此 global scope 的确严重夸大了 `pdf-note-rrf` 的损失（从 `-0.2815` 收窄到
`-0.0318`），但 hard-filter 和 note-only 路线的失败仍然是 paper-scoped 下的真实结果。

已确认的实现行为：

- `note-to-pdf` 只把命中的 note chunk 投影到其引用的 PDF chunks，没有 direct-PDF
  fallback。
- `pdf-note-rrf` 对 direct PDF 和 note-derived PDF 做等权 RRF，不考虑笔记 coverage、
  route confidence 或 citation fanout。
- `note-guided-pdf` 把 direct PDF hits 硬过滤到 note backlinks 集合；当 reviewer route
  只有 4 块/2 篇时，大多数论文直接失去候选。
- `hierarchical-pdf` 先取 parent hits，但 children 只来自已经进入全局 top-k 的
  `pdf_hits`。相关 child 若没有先通过全局 child 召回，即使 parent 命中也无法恢复，形成
  “parent gate + global child gate”双重瓶颈。

### 6.2 后续候选

| ID | 单变量候选 | 目的 | 验证要求 |
|---|---|---|---|
| `S0` | `note-route-confidence-fallback` | note route 为空、低 coverage 或低置信时严格退化为 PDF-only | no-op 行为有测试；不得产生空结果 |
| `S1` | `pdf-note-weighted-0.9-0.1` | direct PDF 主导，笔记只做轻量补充 | 权重只在 dev 层选择一次；报告 note 独立增益与污染 |
| `S2` | `pdf-note-novel-evidence-only` | note route 只补充 direct PDF 未覆盖的来源 span | 去重以 canonical span/evidence group 为准 |
| `H1` | `hierarchical-parent-expand-direct-fallback` | 命中 parent 后在其全部 children 内重检索，并与 direct child 路线融合 | 分开报告 parent recall、child-given-parent recall、direct fallback |
| `H2` | `hierarchical-parent-child-score-fusion` | 校准 parent、child 和 direct 分数，避免双重硬门 | 不用 benchmark gold 调权 |

`S0` 是所有笔记增强路线的前置合同。`S1` 只有与 `N0/N1` 配套才有公平比较价值。

本轮只实现一个冻结的 `S0+N1+S1` 单变量候选，不扫描其他权重：

1. direct PDF 与 N1 note-derived PDF 使用 `k=60` rank-RRF，权重固定为 `0.9/0.1`；
2. 论文未通过 N0，或某个 query 的 note projection 为空时，必须直接调用 PDF-only 路线，
   其 item IDs、顺序、scores 和 source metadata 全部与同输入 PDF-only 输出相同；不设置
   观察分数后才能确定的“低置信”阈值；
3. 报告每题 note projection 是否非空、note 独有 top-10 项、排序变化、rescue/loss 和新增
   hard failures，并单列有/无 fatal-major reviewer 的论文；
4. 只有 primary 改善且所有现有 guardrails 通过才晋级；否则保留为已验证失败，不继续把
   `0.1` 调成其他权重。

对产品级全库搜索可另记 `S3 document-router-top1-3-5`，但它必须用 query-only 路由并单独
报告 paper recall；不得在 ResearchQA 运行时直接读取 gold `paper_id`。

## 7. Reranker 丢分

### 7.1 质量—延迟权衡

下表是旧 global-corpus diagnostic，用于保留历史机制分析；paper-scoped depth-20/50 的
新 adapter 对账见 1.3，完整当前表须等待 depth-100 和六个 confirmation 全部结束。

| reranker | coverage-nDCG@10 | p95 latency |
|---|---:|---:|
| off | 0.8302 | 约 0.23 s |
| depth-20 | 0.8302 | 约 1.69 s |
| depth-50 | 0.8358 | 约 3.50 s |
| depth-100 | 0.8353 | 约 14.86 s |

depth-50 相对 off 有 43 题提升至少 0.1、35 题退化至少 0.1。它救回 4 个原 hard
failure，但新增 1 个 hard failure。slice 显示它更擅长多证据重排，同时会破坏部分基础
排序已经正确的单证据问题：

| slice | off | depth-50 | 方向 |
|---|---:|---:|---|
| 1 个 evidence group | 0.8956 | 0.8787 | 退化 |
| 2 个 evidence groups | 0.7367 | 0.7608 | 改善 |
| 3 个及以上 evidence groups | 0.8126 | 0.8700 | 改善 |
| adversarial（有 reference） | 0.796 | 0.723 | 退化 |
| multi-hop | 0.595 | 0.650 | 改善 |

depth-100 没有带来可辨认的质量增益，却把 p95 推到 depth-50 的约 4.2 倍，因此不应作为
默认候选。固定 seed 的论文级、领域分层 paired bootstrap 进一步显示：

- depth-50 - off 为 `+0.005557`，95% CI `[-0.017565, 0.028922]`；
- depth-100 - depth-50 为 `-0.000468`，95% CI `[-0.000936, 0]`；
- depth-50 在 biology、machine learning、psychology 均退化超过 2 个百分点。

因此 depth-50 在 `rq-2` 上只是有针对性的候选，不满足 ADR 的生产迁移门槛。239 个可评分
问题中，depth-50 相对 off 有 12 个 coverage rescue、4 个 coverage loss、41 个纯排序
增益、39 个纯排序损失、143 个不变。depth-100 相对 depth-50 没有新的 Recall rescue，
反而新增 1 个 Recall loss，237/239 题指标不变。

代码还固定使用“web search query / passages that answer the query”式 instruction，并逐
chunk 独立打分后完全替换基础排序。它与 ResearchQA 中“找出限定/反驳证据”和“覆盖多个
evidence groups”的目标不完全一致，这是 adversarial 退化和多样性不足的高置信机制假设。

### 7.2 稳定失败案例

下列 `row_id` 用于后续回归，不包含题目正文：

| row_id | 现象 |
|---|---|
| `W3096486083_adversarial0` | relevant chunk 原在 1/15/16，重排后 top-10 全失；新增 hard failure |
| `W3198685994_chunk2_comprehension` | relevant chunk 从第 1 降到第 8，nDCG 约 0.3155 |
| `W3100994248_chunk0_comprehension` | relevant chunk 从第 1 降到第 5，nDCG 约 0.3869 |
| `W3198685994_chunk2_lookup` | nDCG 从 1 降到约 0.3869 |
| `W4304202992_chunk0_comprehension` | best relevant chunk 从第 8 升到第 1，nDCG 升到 1 |
| `W2988916019_adversarial1` | best relevant chunk 从第 7 升到第 1，nDCG 升到约 0.8155 |
| `W2792307011_chunk1_comprehension` | best relevant chunk 从第 14 升到第 2，Recall@10 从 0 升到 1 |
| `W3096486083_multihop0` | best relevant chunk 从第 16 升到第 1，nDCG 从 0 升到 0.5 |

这些案例说明 cross-encoder 既能恢复语义相关但基础排序靠后的块，也能把正确的词法/语义
首位结果整体挤出 top-10。直接替换 rank 是主要风险。

### 7.3 后续候选

| ID | 单变量候选 | 目的 | 验证要求 |
|---|---|---|---|
| `RR1` | depth-20 + preserve base top-1 + equal-rank RRF | 融合 base rank 与 reranker rank，限制灾难性重排 | 参数已冻结；上述稳定 row 全部列入回归表，不允许新增 hard failure |
| `RR2` | `rerank50-score-calibration` | 归一化 base retrieval 与 cross-encoder score 后固定权重融合 | calibration 不读取 gold；权重在扩大层级前冻结 |
| `RR3` | `rerank50-diversity-aware` | 保留不同页/parent/evidence route 的候选多样性 | 报告 MMR/去重对 multi-hop all-groups success 的影响 |
| `RR4` | `adaptive-rerank-nonoracle` | 只在基础结果不确定或两路分歧高时付出重排延迟 | 路由特征必须是线上可观测量；报告触发率和整体 p95 |
| `RR5` | `rerank50-evidence-intent-prompt` | 使用 support/qualify/refute 均适用的通用相关性 instruction | prompt 在扩大层级前冻结；不得读取 question_type |

`RR1` 是首选；它最直接针对已观察到的“正确基础结果被整体替换”问题。正式候选固定
depth-20、强制保留 base top-1，再以等权 rank-RRF 融合；不再把 50/100 作为默认深度，
也不继续扫描融合权重。

## 8. 后续优先级与最小实验矩阵

### P0：先恢复实验有效性

| 项目 | 当前状态 | 下一验证 |
|---|---|---|
| paper-scoped 独立索引 | 35 个候选均已执行到可审计终态 | 定向补跑 9 个旧 adapter 候选 |
| 相对 baseline guardrail | paper-scoped completed 候选已有 diagnostics | 新 rerank envelope 必须 finalized |
| 上游 eligibility 传递 | paper-scoped artifacts 已验证 | 修复后 confirmation 不得洗白失败 component |
| Top-2 维度冻结 | 配置与 schema 已修复 | scope 修复前后均为相同 12/35 矩阵 |
| 正交阶段 anchor 冻结 | 配置、schema 和公开 manifest 已修复 | 新旧 35 个 config ID 成对一致 |
| reviewer-concern rankability | 已设为 diagnostic-only | 新 run 不得进入排名 |
| pre-rerank Recall@20/50/100 和 pre/post rows | 已实现 | 新 reranker artifact 必须非空 |
| 无 reference adversarial 分数分布 | 已实现 | 15/15 保持 null retrieval metric，并输出 score diagnostic |
| stale FP16 candidate 隔离 | 已移动到 `stale-candidates` | final-plan membership 必须仍为 35 |
| Pareto/public exporter 完整性 | fail-closed 代码已修复，旧假 final 已隔离 | 公开导出必须在零基础设施失败后原子通过 |
| reranker last-token-only + adapter identity | 代码、parity 和回归测试已通过 | 精确 9 个候选 fresh-CUDA 定向重跑 |
| execution/guardrail/failure 状态分离 | 代码和回归测试已通过 | 新 stage/final 必须 fail closed |
| 候选内部原子 progress | 合同已冻结；当前 executor 仍只在整项结束后返回 | 定向 rerank 退出后实现逐论文 quality 与完整 pass latency resume |
| 旧 adapter 与假 final 隔离 | 9 个候选、17 个 artifact 已移入 run-owned quarantine | 新旧 SHA ledger 对账 |
| `F2 pdf-structure-aware-fallback` | 生产输入结构门已冻结，代码待实现 | 使用新 config ID，不按质量分数调阈值 |
| `N0 note-route-eligibility-gate` | 已冻结逐论文同一非空块须 backlinkable；失败逐论文 PDF-only fallback | paper-scoped 基线后实现并报告 fallback IDs/rate |
| `N3 note-concern-parser-contract` | 已冻结 58 行生产分布、多 claim/range、四级 severity 与 fail-closed 边界 | 实现结构化 parser；reviewer-only 仍 diagnostic-only |
| controlled finalist latency | 已确认串行 p95 在热降频下只能 observed-only；合同已冻结 | 只交错复测质量 tie group；不可比时跳过 latency 决胜 |

P0 不以提升分数为目标，而是确保每个候选覆盖范围可比、失败可以局部降级、恢复不会混入
旧 selection 结果。

### P1：最有证据的质量候选

1. `RR1`：depth-20 + preserve base top-1 + equal-rank RRF
2. `R1`：preserve dense top-1 + dense:BM25 `2:1` RRF (`k=60`)
3. `S0 + N1 + S1`：仅在 N0/N3 和全论文 N1 claim+reviewer 路线完成后进入
4. `H1 hierarchical-parent-expand-direct-fallback`

这些候选不得一次全叠加。先分别与 fixed-1200 + hybrid + PDF-only + rerank-off 基线做
单变量成对比较，再只对通过 guardrail 的候选做极少量交互确认。

`F1` 已因 chunk 高度重叠且零质量增益停止；不得为了填满矩阵重新启动。当前也不继续扫描
RR1/R1 权重。

### P2：证据不足或过拟合风险较高

- `R4 adaptive-retrieval-nonoracle`
- `RR4 adaptive-rerank-nonoracle`
- query decomposition / expansion
- 新 embedding 模型
- answer generation、LLM judge 或 self-ask

P2 只有在 P1 无法解释剩余错误、且项目所有者另行批准后才进入设计。当前 `rq-2` 不运行
这些候选。

## 9. 下一层验收门

任何后续候选至少同时满足：

1. 使用相同 source revision、冻结笔记、问题顺序和 evaluable set；
2. 报告总体、10 领域、4 题型、evidence-group count 和 hard-failure slice；
3. primary metric 改善，且 multi-hop/adversarial guardrail 不退化超过 ADR 阈值；
4. 与基线相比不得新增未解释的 Recall@10=0；如果修复一个 slice 却制造新的 hard
   failure，默认不晋级；
5. 笔记路线额外报告非空论文率、chunks/paper、citation backlink coverage 和 fallback
   触发率；
6. 层级路线额外报告 parent recall 和 child-given-parent recall；
7. 重排路线额外报告触发率、p50/p95、GPU dtype/batch 和 base-rank preservation；
8. 使用分领域、论文级 paired bootstrap；实质并列按 latency、index bytes、chunk count
   决胜；
9. 只能使用生产查询时可获得的特征，禁止把 `question_type`、gold group count、
   expected references 或 benchmark paper identity 变成推理 oracle；
10. 在第一次查看 `rq-10` 前冻结候选和参数；`rq-all` 只做最终确认或明确标记 regression。

## 10. 结论

`rq-2` 已经足以排除三条粗糙路线：纯 note-to-PDF、无 fallback 的 note-guided hard
filter、depth-100 默认重排。当前仍没有可发布 winner：必须先让 9 个 rerank-enabled
候选在 fresh CUDA 进程中取得可审计终态，并证明基础设施失败、pending eligibility 和
假 final 都为零。

基线关闭后，最值得进入后续单变量验证的不是再换一个更大的模型，而是：

1. 保留 base top-1 的 depth-20 重排融合；
2. 保留 dense top-1 的 dense-heavy RRF；
3. 有逐论文 coverage gate 和 PDF-only fallback 的笔记增强；
4. 真正的 parent 命中后 child 重检索，而不是双重 top-k 硬门。

这些候选在 `rq-2` 完成前只作为 backlog 记录；是否进入 `rq-5` 仍由 ADR-003 的 stop gate
和项目所有者决议控制。
