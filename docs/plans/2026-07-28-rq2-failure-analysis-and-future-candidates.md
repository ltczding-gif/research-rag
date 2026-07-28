# `rq-2` 失败归因与后续候选策略

- **Status:** Evidence log; future candidates are not active
- **Date:** 2026-07-28
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
6. 审计发现当前实现使用全局 20 篇共享索引，与 ADR 的 `paper-scoped` 合同不一致；
   已完成 artifact 只保留为 global-corpus diagnostic。当前原子候选结束后停止该语义，
   修复合同并重跑，不能用不合规结果完成 `rq-2`。

### 1.1 P0 合同偏差：当前实现不是 paper-scoped

ADR-003 的 R3 和 sweep design 都明确写了 `paper-scoped`，但
`run_complete_candidate` 当前只构建一份包含 20 篇的 PDF、note 和 parent 共享索引。
每个 query 对三个全局索引直接 `_search`，没有使用 `question.paper_id` 限定候选。现有双
论文 fixture 只断言目标命中，没有断言跨论文结果必须排除，因此未捕获该偏差。

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

`rq-2` 的修复要求：

1. 为每篇建立或选择独立的 PDF/note/parent 检索视图；
2. query 只在其 benchmark paper 内排名；
3. 增加两论文干扰回归，明确断言另一篇的 chunk 不得进入结果；
4. 35 个唯一候选全部在相同 paper-scoped 语义下重新运行和评分；
5. 已完成 global artifacts 更名/隔离为 diagnostic，不进入最终 leaderboard、bootstrap、
   Pareto 或公开报告。

产品若要在未知论文的全库检索中工作，不能使用 benchmark gold `paper_id`。该问题应另建
`document-router -> top-N paper-scoped retrieval` 候选，并把 gold paper_id 结果只作为
oracle 上界；它不属于当前 `rq-2` 修复。

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
`pdf-fixed-800` 的 `0.9036` 和 `pdf-fixed-400` 的 `0.8293`。但它不是所有 slice
都占优：

| 对比 | 结果 |
|---|---|
| fixed-1200 vs fixed-800，逐题 nDCG@10 均值 | `+0.0199` |
| 至少提升 0.1 的题 | 49 |
| 至少退化 0.1 的题 | 34 |
| comprehension | `+0.0563` |
| multi-hop | `+0.0222` |
| lookup | `-0.0013` |
| adversarial（有 reference） | `-0.0183` |
| 2 个 evidence groups | `+0.0539` |
| 3 个 evidence groups | `+0.1023` |

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

`pdf-parent-child` 的 Recall@5 为 `0.8156`，低于固定切分；它的层级组合还存在二次召回
瓶颈，见第 6 节。

### 3.2 后续候选

| ID | 单变量候选 | 目的 | 验证要求 |
|---|---|---|---|
| `F1` | `pdf-multigranular-800-1200-rrf` | 融合 800 的局部精度与 1200 的多证据覆盖 | 分别报告两路命中贡献；不得把重复 span 当新证据 |
| `F2` | `pdf-structure-aware-fallback` | 无结构标记的单文档回退到 fixed-1200 | 报告每篇 fallback 率；不得让一篇失败拖垮全候选 |
| `F3` | `pdf-fixed-1200-adjacent-expand` | 命中后只扩展相邻块，补跨边界证据 | 主排序不变；单独计 expansion 带来的召回和字节成本 |
| `F4` | `pdf-heading-prefixed-1200` | 把 section path 作为可检索上下文 | provenance span 仍只指向正文；标题不得伪装成证据 |
| `F5` | `section-aware-min200-merge` | 合并过短标题/短段，保留结构但减少碎片 | 报告短块率、MRR、跨 section R@5 |
| `F6` | `multi-hop-decompose-diversify` | 生成无答案泄漏的 2–3 个检索子查询并做 page/section 多样化 | 只看线上 query；主门是 all-required-groups success@5/10 |
| `F7` | `adversarial-claim-verification` | 分别检索研究范围与被断言结果，寻找限定/反驳证据 | 不读取 expected_refusal、false_premise 或 gold evidence |

优先级为 `F2`（恢复实验有效性）后 `F1`（最有证据的质量候选）。`F3/F4` 只在
`F1` 不能解释剩余跨边界失败时再测。

所有成功 PDF chunker 都难以处理的 14 题中有 10 题是 multi-hop。三个全策略 R@5 为零
的稳定反驳案例为 `W3094793347_adversarial1`、`W3154248945_adversarial0`、
`W4225278475_adversarial1`；它们都包含错误前提，需要找反驳证据，单纯改块长不够。
典型的跨页、跨 section、多组证据持续失败包括
`W3011534780_multihop0`、`W3086667591_multihop1`、
`W3096486083_multihop0`、`W3154248945_multihop0`、
`W4304202992_multihop0`、`W4389861133_multihop0`。

## 4. 笔记切分失败

### 4.1 聚合结果与 coverage 偏差

| note chunker | 笔记块 | 覆盖论文 | 有引用块 | primary score |
|---|---:|---:|---:|---:|
| `note-whole` | 20 | 20/20 | 20 | 0.2456 |
| `note-section` | 323 | 20/20 | 271 | 0.4172 |
| `note-claim-evidence` | 103 | 20/20 | 103 | 0.3678 |
| `note-reviewer-concern` | 4 | 2/20 | 4 | 0.6770 |

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

### 4.2 后续候选

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
| `dense` | 0.8202 | comprehension、多组证据相对更稳 |
| `bm25` | 0.7966 | lookup 最强，其他题型明显偏弱 |
| `hybrid-rrf` | 0.8302 | 总体第一，单组、lookup、adversarial 受益 |

hybrid 相对 dense 的逐题均值为 `+0.0117`，但 slice 方向不一致：

| slice | hybrid - dense |
|---|---:|
| lookup | `+0.0418` |
| adversarial（有 reference） | `+0.0414` |
| comprehension | `-0.0205` |
| multi-hop | `-0.0090` |
| 1 个 evidence group | `+0.0299` |
| 2 个 evidence groups | `-0.0076` |
| 3 个及以上 evidence groups | `-0.0402` |

等权 RRF 带来稳定的词法补救，但会让 BM25 对多证据问题的弱排序稀释 dense 结果。
下一步应先测固定权重，不使用 benchmark 的 `question_type` 或 gold group count 做路由。

### 5.2 后续候选

| ID | 单变量候选 | 目的 | 验证要求 |
|---|---|---|---|
| `R1` | `hybrid-dense-heavy-fixed` | 保留 lookup 词法补救，减少多证据稀释 | 权重在查看 `rq-10` 前冻结；报告 dense/BM25 独立召回 |
| `R2` | `dense-plus-bm25-rescue` | dense 为主，只补入未覆盖的高置信词法结果 | 固定 score/rank 门槛；补救结果须增加新 evidence group |
| `R3` | `hybrid-diversity-aware` | 减少 top-k 被同页、同段近重复块占满 | 报告去重前后 evidence coverage 和排序变化 |
| `R4` | `adaptive-retrieval-nonoracle` | 用线上可获得的 score gap、熵、两路分歧决定融合强度 | 禁止读取 question_type、paper_id gold 或 expected references |

`R1` 优先于 `R4`；先验证简单固定权重，再考虑自适应路由。

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

对产品级全库搜索可另记 `S3 document-router-top1-3-5`，但它必须用 query-only 路由并单独
报告 paper recall；不得在 ResearchQA 运行时直接读取 gold `paper_id`。

## 7. Reranker 丢分

### 7.1 质量—延迟权衡

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
| `RR1` | `rerank50-base-rank-fusion` | 融合 base rank 与 reranker rank，限制灾难性重排 | 上述 8 个稳定 row 全部列入回归表；不允许新增 hard failure |
| `RR2` | `rerank50-score-calibration` | 归一化 base retrieval 与 cross-encoder score 后固定权重融合 | calibration 不读取 gold；权重在扩大层级前冻结 |
| `RR3` | `rerank50-diversity-aware` | 保留不同页/parent/evidence route 的候选多样性 | 报告 MMR/去重对 multi-hop all-groups success 的影响 |
| `RR4` | `adaptive-rerank-nonoracle` | 只在基础结果不确定或两路分歧高时付出重排延迟 | 路由特征必须是线上可观测量；报告触发率和整体 p95 |
| `RR5` | `rerank50-evidence-intent-prompt` | 使用 support/qualify/refute 均适用的通用相关性 instruction | prompt 在扩大层级前冻结；不得读取 question_type |

`RR1` 是首选；它最直接针对已观察到的“正确基础结果被整体替换”问题。depth 固定为 50，
不再把 100 作为默认优化方向。

## 8. 后续优先级与最小实验矩阵

### P0：先恢复实验有效性

1. `F2 pdf-structure-aware-fallback`
2. `N0 note-route-eligibility-gate`
3. `N3 note-concern-parser-contract`
4. 增加 pre-rerank Recall@20/50/100、evidence-group pre/post rank 和无 reference
   adversarial 分数分布诊断
5. stale candidate 隔离和 final-plan membership 审计；旧 FP16 confirmation
   `018ed...`、`5bb78...`、`ca8...` 只能作为内部迁移审计，不得进入 BF16 排名

P0 不以提升分数为目标，而是确保每个候选覆盖范围可比、失败可以局部降级、恢复不会混入
旧 selection 结果。

### P1：最有证据的质量候选

1. `RR1 rerank50-base-rank-fusion`
2. `F1 pdf-multigranular-800-1200-rrf`
3. `S0 + N1 + S1`：有 eligibility/fallback 的 PDF 主导笔记增强
4. `H1 hierarchical-parent-expand-direct-fallback`
5. `R1 hybrid-dense-heavy-fixed`

这些候选不得一次全叠加。先分别与 fixed-1200 + hybrid + PDF-only + rerank-off 基线做
单变量成对比较，再只对通过 guardrail 的候选做极少量交互确认。

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
filter、depth-100 默认重排。它也暴露了两个必须先修的实验合同：结构切分的单文档
fallback，以及笔记策略的覆盖率可比性。

最值得进入后续单变量验证的不是再换一个更大的模型，而是：

1. 800/1200 多粒度融合；
2. base rank + depth-50 rerank 融合；
3. 有 coverage gate、confidence gate 和 PDF-only fallback 的笔记增强；
4. 真正的 parent 命中后 child 重检索，而不是双重 top-k 硬门。

这些候选在 `rq-2` 完成前只作为 backlog 记录；是否进入 `rq-5` 仍由 ADR-003 的 stop gate
和项目所有者决议控制。
