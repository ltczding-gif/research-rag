# `rq-2` 隔夜全策略扫描设计

- **Status:** Accepted
- **Date:** 2026-07-28
- **Owner:** 项目所有者
- **Benchmark:** ResearchQA `rq-2`，20 篇论文，254 个问题
- **Decision source:** ADR-003

## 1. 目标

第一晚只在固定的 `rq-2` 上完成：

1. benchmark PDF、官方 supplementary files 和多格式来源的可追溯准备；
2. 20 篇 field-neutral 通用模板笔记的子代理生成、独立审计与冻结；
3. ResearchQA evidence 到 benchmark PDF page/span 的统一映射；
4. 所有已批准切分、检索、语料组合和重排序候选的正交扫描；
5. 各阶段前两名的最多 16 行、去重后的兼容交叉确认组合；
6. 可恢复 checkpoint 和无论成功、部分完成或失败都生成的晨报。

本轮只评检索组件，不运行答案生成。所有结果只能产生 `provisional winner`，运行结束后
必须停止并等待项目所有者决定，不能自动进入 `rq-5`。

## 2. 已测来源快照

2026-07-28 的严格 TLS 下载与本地解析审计得到：

| 项目 | 结果 |
|---|---:|
| benchmark PDF | 20/20 |
| benchmark PDF bytes | 82,140,677 |
| benchmark PDF physical pages | 662 |
| benchmark PDF parse failures | 0 |
| 有 external SI 的论文 | 8 |
| external SI files | 12 |
| external SI bytes | 45,601,173 |
| external SI PDF pages | 306 |
| benchmark PDF 内合并 SI 的论文 | 1 |

12 个 external SI 的媒体类型为 7 PDF、2 DOCX、2 XLSX 和 1 CSV。另有一篇 benchmark
PDF 内含 appendices，但没有独立 SI。ResearchQA 的 254 个问题中，3 个问题引用
Technical Appendix，全部位于数据集提供的同一 benchmark PDF；没有问题引用 external SI。

这些原文件、派生文本、逐题数据和审计详情留在 ignored cache，不进入 Git。Git 只提交
可重建代码、schema、配置、指纹和经过许可审查的聚合报告。

## 3. 两个证据 universe

必须区分：

1. **Note source universe**：benchmark PDF 加所有已校验 official SI。它用于生成完整笔记。
2. **ResearchQA gold universe**：数据集提供的 benchmark PDF，包括其中已经合并的 appendix
   或 supplementary section。它用于 primary retrieval scoring。

External SI 没有 ResearchQA gold。SI-only 命中只能报告为 diagnostic/unjudged，不能当作
负样本，也不能计入 primary metric。Note-based 策略最终必须回链到 ResearchQA gold
universe 的 PDF evidence，才能进入主评分。

## 4. 来源身份与原生坐标

每篇论文的 source manifest 至少包含：

```text
paper_id
file_id
source_role
media_type
original_filename
source_url
sha256
bytes
parser_fingerprint
citation_coordinate_type
acquisition_status
```

`source_role` 至少区分 `benchmark_pdf`、`bundled_supplement`、`external_si` 和
`auxiliary_reporting_file`。Scientific supplementary files 按规范化文件名、再按 SHA-256
稳定排序并分配 `SI-01`、`SI-02` 等 ID。

新笔记使用以下原生坐标：

| 媒体 | 坐标示例 | 规则 |
|---|---|---|
| benchmark PDF | `[Main p.5]` | 数字物理页码 |
| SI PDF | `[SI-01 p.3]` | 文件 ID 加数字物理页码 |
| DOCX paragraph | `[SI-02 para.14]` | 非空段落的 1-based 文档顺序 |
| DOCX table | `[SI-02 table.2 rows.3-5 cols.A-D]` | 1-based table/row 和稳定列标 |
| XLSX | `[SI-03 sheet."Table S1" cells.A2:F18]` | 原始 sheet 名和 cell range |
| CSV | `[SI-04 rows.20-35 cols.model,score]` | 含 header 的 1-based data rows |

旧笔记的 `[SI p.X]` 继续兼容；新生成笔记一律携带文件 ID。不得把 DOCX、XLSX 或 CSV
转换后的派生页码写成原文件页码。

## 5. 笔记生成合同

20 篇论文全部强制使用仓库内 `generic-research-note`：

- 不运行领域路由；
- 不加载 active domain pack 的 seed guidance、命名语义、trap scan 或评分轴；
- 正文使用 benchmark PDF；
- official scientific SI 全部输入；
- reporting summary 等 auxiliary 文件单独标记，不能冒充 SI 实验数据；
- 官方没有 scientific SI 时记录 `si_status: not_available`，不阻塞；
- 官方明确存在但下载、解析或校验失败时阻塞；
- 不使用 Gemini；使用 repository subagent backend；
- 每篇使用独立上下文、独立运行目录和独立 manifest。

三名生成子代理按总页数和媒体复杂度均衡分配论文。生成完成后轮换审计：

```text
generator A -> auditor B
generator B -> auditor C
generator C -> auditor A
```

生成者不能审计自己的输出。审计至少检查：

- 事实是否来自当前 source set；
- `Main`、各 `SI-NN` 与 auxiliary 是否区分正确；
- 原生坐标是否可解析并回到源文件；
- Claim–Evidence 引用是否完整；
- 数值、单位、条件、样本口径和来源冲突是否保留；
- reviewer verdict 是否只攻击 load-bearing claims；
- 生成 JSON、渲染 Markdown 和 manifest 是否一致。

只有生成校验和独立审计都通过的笔记才能冻结。冻结后，所有候选共享相同 note SHA-256；
隔夜运行不得重新生成笔记。

## 6. 切分候选

### 6.1 PDF

| ID | 确定性规则 |
|---|---|
| `pdf-fixed-400` | size 400、step 320、min 80 |
| `pdf-fixed-800` | size 800、step 700、min 100；C0 baseline |
| `pdf-fixed-1200` | size 1200、step 1000、min 120 |
| `pdf-page-aware` | 不跨物理页；段落聚合 target 800、hard max 1200 |
| `pdf-section-aware` | 不跨识别章节；段落聚合 target 800、hard max 1200 |
| `pdf-structure-aware` | 图注、表格、公式和邻接解释形成原子块，再按章节聚合 |
| `pdf-parent-child` | child 400/step 320 负责命中，返回 800–1600 的父块 |

Page/section/structure 检测失败必须写入 artifact status，不能退化到 fixed-800 后继续使用原
config ID。所有 chunk 保留 source spans、section path、previous/next、parent ID 和内容哈希。

### 6.2 笔记

| ID | 确定性规则 |
|---|---|
| `note-whole` | 整篇笔记；只作产品现状诊断 |
| `note-section` | 按 Markdown 二级、三级标题切分 |
| `note-claim-evidence` | 一个 `C*` claim 与其引用的 `E*` evidence 为一块 |
| `note-reviewer-concern` | surviving concern、对应 claim/evidence 和决定性补证为一块 |

笔记块解析全部 source citation。没有有效来源坐标的块可进入完整性诊断，但不能导出最终
PDF evidence。`note-whole` 在 paper-scoped evidence ranking 中不与 PDF span 指标混算。

## 7. 检索与语料组合

### 7.1 基础检索器

- `dense`：Ollama `qwen3-embedding:4b`，模型 digest
  `df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907`，
  2560 维，cosine；
- `bm25`：BM25 `k1=1.2`、`b=0.75`；NFKC、小写、保留内部连字符和数字，不做词干化或
  领域词典扩展；
- `hybrid-rrf`：dense 和 BM25 各取 Top-100，等权 RRF，`k=60`。

Embedding 必须走 batch endpoint。缓存键为
`model_digest + normalization_revision + text_sha256`，query embedding 跨候选复用。

### 7.2 Source composition

| ID | 输出 |
|---|---|
| `pdf-only` | 直接返回 benchmark PDF chunks |
| `note-to-pdf` | 检索 note chunks，再按 citation 回到 benchmark PDF chunks |
| `pdf-note-rrf` | direct PDF 与 note-derived PDF 两路等权 RRF |
| `note-guided-pdf` | note 先确定 claim/section/page 范围，再在范围内检索 PDF |
| `hierarchical-pdf` | 召回 parent section，再在其 child chunks 内排序 |

`hierarchical-pdf` 使用 `pdf-parent-child` 的固定父子结构，是命名复合候选，不伪装成只改变
一个 flat chunker 参数的实验。

## 8. 重排序

质量 reranker 固定为：

- model：`Qwen/Qwen3-Reranker-0.6B`；
- revision：`e61197ed45024b0ed8a2d74b80b4d909f1255473`；
- license：Apache-2.0；
- 输入：ResearchQA question 与 benchmark PDF passage；
- 输出：按 cross-encoder relevance score 降序，同分按稳定 `chunk_id`。

候选：

```text
rerank-off
rerank-20-to-10
rerank-50-to-10
rerank-100-to-10
```

任何截断必须记录原始长度、保留长度和截断方向。OOM 时只允许降低 batch size；batch size 1
仍失败则该 config 失败，不能静默切 CPU、换模型或缩小候选池。

## 9. 正交扫描和交叉确认

隔夜按以下顺序运行，每阶段固定其他变量：

1. 7 个 PDF chunker；
2. 4 个 note chunker，其中 `note-whole` 只给 diagnostic；
3. dense、BM25、hybrid-RRF；
4. 5 个 source composition；
5. 4 个 rerank depth；
6. Top-2 PDF chunker × Top-2 retriever × Top-2 source composition ×
   rerank off/最佳 depth，最多 16 行，只执行唯一且兼容的组合。

配置指纹相同的重复 baseline 直接复用。所有候选都有独立 score row，但不运行全部
chunker × retriever × source × reranker 笛卡尔积。

`hierarchical-pdf` 的有效实现必须使用 `pdf-parent-child`。当它进入 source composition
Top-2 时，两个固定 PDF chunker 分支会归一为同一候选；runner 必须在执行前按 config ID
稳定去重。本轮因此从 16 行得到 12 个唯一确认候选，不重复运行或重复计分。

## 10. Evidence mapping 和评分

ResearchQA reference groups 之间是 AND，同组 alternatives 是 OR。一个 group 只需一个
alternative 成功映射即可评分。PDF page IR 使用 content-stream reading order
（`use_text_flow=true`、`x_tolerance=1`）。映射顺序为：NFKC 小写字母数字流页内精确
定位 → source-span 字符区间投影 → ResearchQA 官方 page hint 限域最佳 chunk →
section hint 限域最佳 chunk → 版本化且有阈值的全局模糊匹配。hint fallback 不得把整页
或整节全部标成 relevant；失败必须进入 `unmapped`。

排名 gate：

- 全体 evidence-group mapping coverage 至少 95%；
- 每篇论文至少 90%；
- 所有候选使用完全相同的 evaluable set；
- 任一候选必须完成全部 20 篇和全部适用问题。

指标：

| 阶段 | Primary | Guardrails |
|---|---|---|
| chunking | evidence-group Recall@5 | Recall@10、MRR、unmapped |
| retriever | coverage-nDCG@10 | Recall@5/10、MRR |
| source composition | coverage-nDCG@10 | PDF 回链率、SI-only diagnostic |
| reranker | coverage-nDCG@10 | Recall@10、p50/p95 latency |
| multi-hop | all-required-groups success@5/10 | 平均已覆盖 groups |

`coverage-nDCG` 只奖励首次覆盖的新 group。聚合顺序为先论文、再领域内、最后十领域等权宏
平均；题型分开报告。

`rq-2` 的 50 个 adversarial 问题全部执行。35 个带 reference 的问题报告
refutation-evidence Recall@5/10；15 个无 reference 的问题只报告候选分数分布。本轮不生成
答案，因此不宣称得到 refusal accuracy 或 false-answer rate。

候选先通过完整性和 guardrails，再按 primary 排序。前两名相差不超过 0.5 个百分点视为
实质并列，依次用 p95 latency、index size、chunk count、config ID 决胜。报告另用固定 seed
`research-rag-rq2-bootstrap-v1` 完成 10,000 次按领域分层的论文级 paired bootstrap，给出
95% 区间。所有 winner 均标记 provisional。

## 11. 性能测量

质量评分使用全部 254 个问题。性能样本由每个 `domain × question_type` 中
`sha256(row_id)` 最小的一个问题组成，共 40 个。每个配置先做一次不计时 warm-up，再对
40 个问题运行 3 个 timed passes；报告 query 和 rerank p50/p95。

另行记录：

- build wall time；
- chunks/paper；
- embedding input count 和 cache hit；
- index bytes；
- reranker pairs/second；
- total wall time；
- CPU、RAM、GPU 和模型/硬件指纹。

## 12. 隔夜状态机

执行 DAG：

```text
source preflight
  -> note generation
  -> independent note audit and freeze
  -> evidence mapping
  -> chunker sweeps
  -> retriever sweeps
  -> source-composition sweeps
  -> reranker sweeps
  -> Top-2 confirmation
  -> morning report
```

原子任务 ID 为
`run_id + stage_id + paper_id + config_id + input_fingerprint`，状态只能是：

```text
pending -> running -> completed
                   -> failed
                   -> blocked
```

产物先写临时路径，校验后原子重命名。恢复时同时检查输入指纹和 artifact SHA-256，不能只
凭文件存在跳过。网络和 Ollama 短暂不可用可指数退避；确定性校验错误不无限重试。某篇阻塞
时其他独立论文继续，但不完整候选不排名。

默认 wall-clock budget 为 10 小时。根据同阶段已完成原子任务的移动平均估时；剩余预算
不足以完成一个完整候选时不启动它。到期、中断、失败或成功都必须生成晨报。

所有 ResearchQA 数据、PDF、SI、Hugging Face cache、向量和 raw results 明确写到 F 盘
ignored cache。严格 TLS 保持开启。ResearchQA 的无效 virtual-host S3 URL 必须规范化为
合法 path-style S3 URL，不能通过关闭证书验证绕过。

## 13. 验证

实现按以下 gate 推进：

1. 单元测试 URL 规范化、source ID、原生坐标、切分边界、BM25、RRF、citation 回链、
   evidence groups、评分、状态恢复；
2. 仓库内微型 PDF/DOCX/XLSX/CSV fixtures 的无网络集成测试；
3. 单论文 live canary，覆盖 note、IR、embedding、reranker 和 scoring；
4. `rq-2` 20 篇来源和冻结笔记 preflight；
5. 正式隔夜运行。

CI 不下载 ResearchQA、论文、模型或逐题结果。Live checks 必须显式执行。

## 14. 晨报

晨报必须包含：

- run/source/code/model/hardware fingerprints；
- 20 篇 source 和 note 状态；
- evidence mapping coverage；
- 每个候选的完整分数；
- 分领域、分题型、multi-hop 和 adversarial 结果；
- paired bootstrap 区间；
- 时间、索引大小、缓存命中和 reranker 成本；
- Pareto frontier；
- provisional winner；
- completed、partial、blocked、failed 和 unevaluable 明细；
- 下一步建议，但不自动启动 `rq-5`。

## 15. 非目标

本设计不包括：

- 改写 ResearchQA 问题、答案或 reference；
- 生成答案或使用 LLM judge；
- query expansion、RCS、self-ask；
- 把 external SI 扩写成新 gold；
- 全变量笛卡尔积；
- 自动晋级更大 tier；
- 把 `rq-2` 分数当作最终产品或跨领域质量声明。
