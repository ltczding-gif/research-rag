# ADR-003：以 ResearchQA 作为唯一活跃 RAG 迭代基准

**Status:** Accepted for implementation
**Date:** 2026-07-28
**Decider:** 项目所有者
**Supersedes:** ADR-002 中的 S5/D20/V20/H60、自建 query/gold 与 note/SI benchmark 计划
**Keeps:** ADR-002 的 canonical IR、隔离运行、一次只改一个变量族、版本绑定与回滚原则

## 1. 决策

当前迭代只使用公开的 ResearchQA，不并行接入其他外部 benchmark，也不继续人工设计
S5 问题。S5 语料、候选笔记、query design 和 Wave 1A artifact 作为冻结历史资产保留，
但不参与当前调参、验证或发布结论。

ResearchQA 的论文是抽样单位。四个嵌套层级固定为：

| Tier | 每领域论文数 | 总论文数 | 问题数 | 用途 |
|---|---:|---:|---:|---|
| `rq-2` | 2 | 20 | 254 | 快速验证 PDF 获取、解析、切分、索引、检索和评分接线；不作质量声明 |
| `rq-5` | 5 | 50 | 638 | 第一轮跨领域失败分析和单变量迭代 |
| `rq-10` | 10 | 100 | 1,263 | 扩大样本后的确认与回归 |
| `rq-all` | 全部 | 494 | 6,211 | 完整公开 benchmark 报告 |

每篇入选论文使用 ResearchQA 自带的全部问题。禁止为提高分数而删题、改题、补写答案或
另行生成问题。四层必须由同一固定 seed 排序产生并保持
`rq-2 ⊂ rq-5 ⊂ rq-10 ⊂ rq-all`。

## 2. 固定数据源

唯一源合同是
`benchmarks/sources/researchqa.yaml`：

- repository：`khoj-ai/ResearchQA`；
- revision：`33f3d7a83a1ae61511b4e3bfadab2f866eff2a03`；
- file：`eval_dataset.jsonl`；
- bytes：`15,921,446`；
- SHA-256：`681af78bcb1b60d7740a481a9d37ef3af7d9326a72174dc6798c7e87aaa99b73`；
- license：`CC-BY-NC-4.0`，外部评测专用。

当前固定快照包含 494 篇论文和 6,211 个问题。实际 `domain` 字段有十类：

1. `biology`
2. `economics`
3. `education`
4. `environmental_science`
5. `history_humanities`
6. `machine_learning`
7. `mathematics`
8. `psychology`
9. `public_health`
10. `social_science`

问题类型及数量固定为：

| 类型 | 数量 | 评测含义 |
|---|---:|---|
| `lookup` | 1,999 | 单节或局部证据查找 |
| `comprehension` | 1,999 | 基于论文内容的理解与解释 |
| `multi_hop` | 992 | 组合两个或更多相距较远的章节 |
| `adversarial` | 1,221 | 错误前提、论文未覆盖内容或应拒答问题 |

数据卡、论文摘要或网页文案不是计数真值。生成器必须以固定 JSONL 的实测分布为准；任一
论文数、问题数、领域数或问题类型计数漂移都 fail closed。

## 3. 数据与许可证边界

主 Git 历史只提交：

- 固定 source/selection 合同；
- schema、下载与校验代码；
- benchmark adapter、算法配置、测试和可公开报告。

以下内容必须留在忽略目录或使用者指定的外部缓存：

- ResearchQA 原始 JSONL；
- 从 ResearchQA 派生的题目子集；
- 论文 PDF；
- PDF 提取文本、chunks、embeddings、vector index；
- 未经专门审查的逐题结果和引用原文。

ResearchQA 的 annotation 使用 CC-BY-NC-4.0；其中引用的论文原文仍受各论文原始许可
约束。项目不能把 ResearchQA 数据或 PDF 描述为本仓库许可证覆盖的资产。公开发布默认只
提供可重建脚本、版本指纹和聚合报告。

## 4. 确定性分层与防泄漏

论文排序键固定为：

```text
sha256(seed + NUL + domain + NUL + paper_id)
```

每个领域独立排序，层级取各领域有序前缀。`rq-all` 使用全部论文。入选论文的所有问题按
`paper_id`、`row_id` 稳定排序，不做难题增采样，也不改变原始题型比例。

四层是规模递增实验，不是假装存在四个相互独立的 held-out：

- `rq-2` 只验证链路和明显故障；
- `rq-5` 允许一次基于失败证据的参数修正；
- 第一次查看 `rq-10` 前必须冻结候选配置；
- `rq-all` 第一次运行前必须冻结该变量族最终候选；
- 报告同时给出累计层级和新增 ring，避免已看过的小层级掩盖新增论文上的退化；
- 一旦依据 `rq-10` 或 `rq-all` 逐题结果继续调参，后续结果只能称 regression，不再称
  held-out confirmation。

## 5. 评测链路

```text
pinned ResearchQA JSONL
  -> deterministic paper/question tiers
  -> PDF acquisition from paper_s3_url
  -> canonical page/span IR
  -> candidate chunker
  -> candidate embedder/index
  -> retrieval
  -> ResearchQA evidence/answer evaluation
  -> per-domain, per-question-type and efficiency report
```

ResearchQA 字段直接承担 benchmark 合同：

| ResearchQA 字段 | 用途 |
|---|---|
| `paper_id`, `paper_doi`, `paper_s3_url`, `domain` | 论文身份、获取和领域分层 |
| `row_id`, `question_type`, `question` | query 身份与 slice |
| `expected_answer`, `judge_rubric` | answer evaluation |
| `expected_references` | 分节证据组；组间 AND、组内 alternatives OR |
| `metadata_page_hint`, `metadata_section`, `metadata_source_text` | lookup/comprehension 的定位与诊断 |
| `metadata_required_sections`, `metadata_reasoning_chain` | multi-hop 完整性诊断 |
| `metadata_false_premise`, `expected_refusal` | adversarial 拒答评测 |

在评分前，evidence adapter 必须把 `expected_references` 的原文 alternatives 映射到
canonical page/span。精确匹配失败可进入显式 `unmapped` 队列，但不能静默当作检索失败；
映射规则本身必须版本化。

## 6. 活跃 baseline

首个质量 baseline 固定在
`benchmarks/configs/baseline-fixed-800.yaml`：

- canonical pdfplumber page IR；
- C0 fixed-character：size 800、step 700、min length 100；
- Ollama `qwen3-embedding:4b`；
- 固定本机模型 digest；
- cosine dense retrieval；
- `top_k=10`；
- 无 reranker。

FastEmbed/MiniLM 只保留为产品快速上手默认和可选 smoke 对照，不能替代 ResearchQA
质量 baseline，也不能与 Qwen 结果混在同一报告中。

## 7. 指标与决策门

检索与生成分开报告。当前优先实现检索指标：

| 变量族 | Primary | Guardrail |
|---|---|---|
| chunking | evidence-group Recall@5 | document Recall@5、unmapped rate |
| dense/lexical/hybrid | evidence-group nDCG@10 | Recall@5 |
| reranker | evidence-group nDCG@10 | Recall@10、p95 latency |
| embedding | evidence-group Recall@5 | nDCG@10、index size、build time |
| answer/agent | answer rubric score | citation precision、grounded refusal |

另行固定：

- multi-hop all-required-groups success@k；
- adversarial false-answer rate 与 grounded-refusal rate；
- 每领域宏平均和十领域宏平均；
- 每题型结果；
- chunks/paper、embedding input、index size、build time、query p50/p95。

算法变更必须与同一 source revision、同一 tier、同一论文顺序和同一 query 集上的 baseline
成对比较。不得只报告总平均，不得用 lookup 的数量优势掩盖 multi-hop 或 adversarial
退化。

在首轮 baseline 分布产生前，不冻结任意百分比提升阈值。迁移生产默认至少要求：

1. primary metric 相对 baseline 改善；
2. 十个领域中不超过一个领域出现超过 2 个百分点的退化，且必须解释；
3. multi-hop 与 adversarial guardrail 不退化超过 2 个百分点；
4. p95 latency 或 index size 超过 1.5 倍时必须有明确质量收益和关闭开关；
5. 配置、模型、source revision、代码 commit 和硬件记录完整。

## 8. 单变量迭代顺序

1. **R0 Benchmark adapter**：完成四层生成、PDF 获取、evidence 映射和 C0/Qwen baseline。
2. **R1 Chunking**：fixed C0、page-aware、section-aware、structure-aware；embedding 与
   retriever 固定。
3. **R2 Retrieval**：dense、lexical、hybrid/RRF；获胜 chunker 固定。
4. **R3 Reranking**：只比较候选池大小和 reranker；chunker/retriever 固定。
5. **R4 Embedding**：在获胜检索链路上比较 embedding；Qwen 4B 始终保留为质量基线。
6. **R5 Agent/answer**：只有 component retrieval 稳定后才比较 query expansion、RCS、
   self-ask 和回答策略。

每个阶段按 `rq-2 -> rq-5 -> freeze -> rq-10 -> freeze -> rq-all` 推进。一个 PR 只改变一个
变量族；benchmark source、问题和期望答案不能夹带在算法 PR 中修改。

## 9. 明确不做

当前 ResearchQA 周期不做：

- 自建或改写 benchmark 问题；
- 人工补 S5 gold/qrels；
- note-only、PDF+note 或 main/SI 联动评测；
- 中文 query 到英文论文的跨语言质量声明；
- 化学、材料、物理和工程领域泛化声明；
- 把 ResearchQA 分数解释为完整产品体验。

这些能力只有在 ResearchQA 周期完成并证明值得扩展时，才通过新的 ADR 恢复；不得在当前
算法迭代中悄悄加入。

## 10. 本次迁移完成标准

- [x] 固定 ResearchQA revision、bytes、SHA-256、领域与问题类型计数；
- [x] 定义每领域 2/5/10/all 四层合同；
- [x] 定义确定性、嵌套、全问题保留规则；
- [x] 明确数据不进入 Git 和 CC-BY-NC 边界；
- [x] 固定 C0 + Qwen 4B 质量 baseline 配置；
- [ ] 生成并验证四层本地索引；
- [ ] 下载 `rq-2` PDF 并建立 canonical IR；
- [ ] 完成 ResearchQA evidence-to-page/span adapter；
- [ ] 产出首份 `rq-2` C0/Qwen baseline；
- [ ] 按 R1–R5 逐变量推进。
