# ADR-003：以 ResearchQA 作为唯一活跃 RAG 迭代基准

**Status:** Accepted for implementation; amended for the `rq-2` overnight sweep
**Date:** 2026-07-28
**Decider:** 项目所有者
**Supersedes:** ADR-002 中的 S5/D20/V20/H60、自建 query/gold 与 note/SI benchmark 计划
**Keeps:** ADR-002 的 canonical IR、隔离运行、版本绑定与回滚原则

## 1. 决策

当前迭代只使用公开的 ResearchQA，不并行接入其他外部 benchmark，也不继续人工设计
S5 问题。S5 语料、候选笔记、query design 和 Wave 1A artifact 作为冻结历史资产保留，
但不参与当前调参、验证或发布结论。

ResearchQA 的论文是抽样单位。四个嵌套层级固定为：

| Tier | 每领域论文数 | 总论文数 | 问题数 | 用途 |
|---|---:|---:|---:|---|
| `rq-2` | 2 | 20 | 254 | 完成笔记前置、全部候选的正交扫描和少量交叉确认；只产生 provisional winner |
| `rq-5` | 5 | 50 | 638 | 第一轮跨领域失败分析和单变量迭代 |
| `rq-10` | 10 | 100 | 1,263 | 扩大样本后的确认与回归 |
| `rq-all` | 全部 | 494 | 6,211 | 完整公开 benchmark 报告 |

每篇入选论文使用 ResearchQA 自带的全部问题。禁止为提高分数而删题、改题、补写答案或
另行生成问题。四层必须由同一固定 seed 排序产生并保持
`rq-2 ⊂ rq-5 ⊂ rq-10 ⊂ rq-all`。

当前第一轮只运行 `rq-2`。它在相同的 20 篇论文、冻结笔记和 254 个问题上覆盖全部已批准的
切分、检索、语料组合和重排序候选，但不运行全部笛卡尔积，也不自动晋级 `rq-5`。各变量族
先正交扫描，最后只组合各阶段前两名。`rq-2` 结果用于定位组件差异，不能作为公开质量声明。

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
- 从出版方获取的 supplementary files；
- 基于 ResearchQA 论文生成、尚未完成发布许可审查的笔记；
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

- `rq-2` 验证链路、比较全部已批准候选并产生 provisional winner，不作质量声明；
- `rq-5` 允许一次基于失败证据的参数修正；
- 第一次查看 `rq-10` 前必须冻结候选配置；
- `rq-all` 第一次运行前必须冻结该变量族最终候选；
- 报告同时给出累计层级和新增 ring，避免已看过的小层级掩盖新增论文上的退化；
- 一旦依据 `rq-10` 或 `rq-all` 逐题结果继续调参，后续结果只能称 regression，不再称
  held-out confirmation。

当前 `rq-2` 全策略扫描完成后必须停下，由项目所有者审阅晨报并决定是否进入 `rq-5`。

## 5. 评测链路

```text
pinned ResearchQA JSONL
  -> deterministic paper/question tiers
  -> strict-TLS benchmark PDF acquisition
  -> official supplementary-material discovery and acquisition
  -> multi-format canonical source IR
  -> generic note generation, independent audit and freeze
  -> ResearchQA evidence-to-benchmark-PDF mapping
  -> orthogonal chunking/retrieval/source/reranking sweeps
  -> retrieval-only ResearchQA evidence evaluation
  -> limited Top-2 interaction confirmation
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

ResearchQA 的 primary evidence universe 只包含数据集给出的 benchmark PDF，包括已合并在
该 PDF 内的 appendix 或 supplementary section。另行下载的 external SI 必须参与笔记生成，
但 ResearchQA 没有为它提供 gold；因此 SI-only 命中只进入诊断，不能静默作为负样本或
primary metric 的相关文档。笔记命中也必须回链到 benchmark PDF evidence，才能进入主评分。

`rq-2` 的真实来源审计确认：

- 20/20 个 benchmark PDF 下载和解析成功，共 662 页；
- 8 篇有已下载的 external SI，共 12 个文件，其中 7 个 PDF、2 个 DOCX、2 个 XLSX、
  1 个 CSV；
- 1 篇在 benchmark PDF 内合并了 Supplementary Material；
- 254 个问题中只有 3 个引用 supplementary/appendix，且都指向已合并在 benchmark PDF
  内的 Technical Appendix；没有问题引用 external SI。

多格式 SI 使用稳定 `SI-NN` 文件 ID 和原生坐标：PDF 用物理页码，DOCX 用段落或表格坐标，
XLSX 用工作表和 cell range，CSV 用行范围和列名。不得为了继续使用 `[SI p.X]` 而伪造
非 PDF 文件页码。旧式单 SI 引用保持兼容；新生成笔记必须使用带文件 ID 的坐标。

## 6. 活跃 baseline

首个质量 baseline 固定在
`benchmarks/configs/baseline-fixed-800.yaml`：

- canonical pdfplumber page IR，按 PDF content stream 读取
  （`use_text_flow=true`、`x_tolerance=1`）；
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
| chunking | evidence-group Recall@5 | Recall@10、MRR、unmapped rate |
| dense/lexical/hybrid | coverage-nDCG@10 | Recall@5、Recall@10、MRR |
| source composition | coverage-nDCG@10 | PDF 回链率、SI-only diagnostic |
| reranker | coverage-nDCG@10 | Recall@10、p95 latency |
| embedding | evidence-group Recall@5 | nDCG@10、index size、build time |
| answer/agent | answer rubric score | citation precision、grounded refusal |

另行固定：

- multi-hop all-required-groups success@k；
- adversarial 有反驳 evidence 时的 refutation-evidence Recall@5/10；
- adversarial 没有 reference 时的候选分数分布；检索阶段不宣称拒答正确率；
- 每领域宏平均和十领域宏平均；
- 每题型结果；
- chunks/paper、embedding input、index size、build time、query p50/p95。

算法变更必须与同一 source revision、同一 tier、同一论文顺序和同一 query 集上的 baseline
成对比较。不得只报告总平均，不得用 lookup 的数量优势掩盖 multi-hop 或 adversarial
退化。

Evidence mapping 在策略排名前必须达到全体 evidence groups 至少 95%，且每篇论文至少
90%。所有候选必须使用相同的可评分集合。coverage-nDCG 只奖励第一次覆盖的新 evidence
group，重复返回同一 group 不重复得分。

在首轮 baseline 分布产生前，不冻结任意百分比提升阈值。迁移生产默认至少要求：

1. primary metric 相对 baseline 改善；
2. 十个领域中不超过一个领域出现超过 2 个百分点的退化，且必须解释；
3. multi-hop 与 adversarial guardrail 不退化超过 2 个百分点；
4. p95 latency 或 index size 超过 1.5 倍时必须有明确质量收益和关闭开关；
5. 配置、模型、source revision、代码 commit 和硬件记录完整。

## 8. 当前 `rq-2` 正交扫描顺序

1. **R0 Source adapter**：严格 TLS 获取 benchmark PDF，发现并校验官方 SI，构建 PDF、
   DOCX、XLSX 和 CSV 原生坐标 IR。
2. **R1 Notes**：20 篇全部使用 field-neutral `generic-research-note`，由子代理生成、另一
   子代理审计后冻结。官方没有 SI 记 `not_available`；明确存在但无法获取或解析才阻塞。
3. **R2 Evidence adapter**：把 ResearchQA reference groups 映射到 benchmark PDF
   page/span。先对 NFKC 小写字母数字流做页内精确定位，再用 chunk 的 source span 投影到
   实际重叠字符范围；仅对版本差异残余使用 ResearchQA 官方 page/section hint，在限定范围
   内选择词面最接近的 chunk，不得把整页或整节全部标成 relevant。完成后通过 95%/90%
   gate。
4. **R3 Chunking**：扫描 7 个 PDF chunker 和 4 个 note chunker；Qwen 4B、dense、
   paper-scoped、无 reranker 固定。
5. **R4 Retrieval**：扫描 dense、BM25 和等权 RRF hybrid；使用 provisional winning
   chunker。
6. **R5 Source composition**：扫描 PDF-only、note-to-PDF、PDF+note RRF、
   note-guided PDF 和 hierarchical PDF。
7. **R6 Reranking**：扫描 off、Top-20→10、Top-50→10、Top-100→10；质量 reranker 固定。
8. **R7 Interaction confirmation**：只运行 Top-2 chunker × Top-2 retriever × Top-2
   source composition × rerank off/on，共最多 16 行；按有效 config ID 去重后只执行唯一且
   兼容的组合。`hierarchical-pdf` 强制使用 `pdf-parent-child`，不得把由此折叠的重复行
   伪装为不同实验。
9. **R8 Stop gate**：生成晨报并停止，不自动进入 `rq-5`。

正交扫描允许在同一设计 PR 中定义多个变量族，但每个候选配置必须独立、可复现、可关闭，
且评分时一次只比较一个变量族。benchmark source、问题和期望答案不能在算法实现中修改。
详细合同见 `docs/plans/2026-07-28-rq2-overnight-strategy-sweep-design.md`。

## 9. 明确不做

当前 ResearchQA 周期不做：

- 自建或改写 benchmark 问题；
- 人工补 S5 gold/qrels；
- 把 external SI 当作 ResearchQA 已标注 gold；
- 第一晚运行答案生成、LLM judge、query expansion、RCS 或 self-ask；
- 在 `rq-2` 完成后自动进入更大层级；
- 运行全部变量的笛卡尔积；
- 中文 query 到英文论文的跨语言质量声明；
- 化学、材料、物理和工程领域泛化声明；
- 把 ResearchQA 分数解释为完整产品体验。

这些能力必须经过新的项目所有者决议；不得在当前隔夜运行中悄悄加入。

## 10. 本次迁移完成标准

- [x] 固定 ResearchQA revision、bytes、SHA-256、领域与问题类型计数；
- [x] 定义每领域 2/5/10/all 四层合同；
- [x] 定义确定性、嵌套、全问题保留规则；
- [x] 明确数据不进入 Git 和 CC-BY-NC 边界；
- [x] 固定 C0 + Qwen 4B 质量 baseline 配置；
- [x] 下载并审计 20 个 `rq-2` benchmark PDF；
- [x] 发现、下载并校验当前官方页面暴露的 scientific SI；
- [x] 批准多格式 SI 原生坐标合同；
- [x] 批准 `rq-2` 正交扫描、重排序和最多 16 个交叉确认组合；
- [ ] 实现多格式 source manifest 和 canonical IR；
- [ ] 生成、交叉审计并冻结 20 篇通用模板笔记；
- [ ] 完成 ResearchQA evidence-to-page/span adapter；
- [ ] 产出首份 `rq-2` C0/Qwen baseline；
- [ ] 完成全部 `rq-2` 正交扫描和晨报；
- [ ] 项目所有者审阅后另行决定是否进入 `rq-5`。
