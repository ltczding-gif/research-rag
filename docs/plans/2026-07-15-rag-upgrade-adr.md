# ADR-002：可评测的检索升级路线（RAG v2）

**Status:** Accepted for staged implementation
**Original date:** 2026-07-15
**Revised:** 2026-07-27
**Decider:** 项目所有者
**前置:** ADR-001（终端优先架构）

---

## 1. 决策摘要

RAG v2 不再从某个“看起来更先进”的切分器、embedding 或 reranker 开始。
实施顺序固定为：

1. 先建立 benchmark 合同、隔离运行环境和 S5 真实语料；
2. 抽出行为不变的 legacy baseline seam，并建立页码/section/span 的 canonical IR；
3. 在 canonical IR 上完成 S20 开发基线，同时建设可重复验证集和封存发布集；
4. 在冻结 embedding 与 retriever 的条件下比较切分策略；
5. 在冻结获胜切分的条件下比较 dense、lexical、hybrid 和 reranker；
6. 最后评估 RCS、迭代检索和条件性的 contextual enrichment；
7. claims/GraphRAG-lite 不属于本 ADR，只有 benchmark 证明仍存在结构性瓶颈时才另立 ADR。

后续任何检索改动必须提交同一 benchmark 版本上的前后对比。没有基准报告，
不得用主观体验宣布质量提升。

---

## 2. 当前状态与已确认问题

### 2.1 当前检索链路

- `notes` collection：整篇结构化 Markdown 作为一个 embedding 单元；
- `papers` collection：PDF 文本按固定字符滑窗切分；
- 默认 papers 参数：`CHUNK_SIZE=800`、`CHUNK_STEP=700`、`MIN_CHUNK_LEN=100`；
- 当前 overlap 是 `800 - 700 = 100` 字符，即 `12.5%`，不是 87%；
- 纯 dense vector 召回，无 lexical baseline、hybrid fusion 或确定性 reranker；
- `search_papers` 可返回相邻 chunk，但 chunk 没有页码、section path 或稳定原文 span；
- 当前 E2E 演示验证 MCP 与 notes 检索接线，不验证真实 PDF passage retrieval 质量。

### 2.2 对旧版 ADR 的纠正

旧版 ADR 有三处不再成立：

1. **“87% overlap、成本放大约 8 倍”是计算错误。**
   当前 800/700 滑窗的理论字符冗余约为 `800 / 700 = 1.14x`，实际值还受短尾块和
   References 截断影响，必须以 benchmark 报告实测。
2. **“subagent 让 LLM 检索增强零成本”表述过度。**
   它不需要单独接入一个 LLM SDK，但仍消耗宿主 Agent 的 token、配额、延迟与人工监督。
3. **先做 RCS、再修底层召回会掩盖问题。**
   LLM 可以补偿坏切分或坏排序，但会让错误归因。RCS 和 self-ask 必须在可测的
   deterministic retrieval baseline 之后评估。

### 2.3 工程测试与质量评测的边界

现有单元测试、跨平台 CI、hash/ledger 合约和 MCP round trip 证明“系统按合约运行”。
它们不回答以下问题：

- 正确论文是否进入 top-k；
- 正确证据段是否进入 top-k；
- SI、样品编号、分子式、实验条件和跨语言查询是否能召回；
- 生成回答是否覆盖关键证据、引用正确且没有无依据 claim；
- 新切分或新模型是否比当前 baseline 更好。

RAG v2 的第 0 步专门补上这层证据。

### 2.4 从头评判结论

旧版 ADR 的方向性判断——先建立评测、再逐步增强检索——是对的，但原方案不能直接进入
详细开发。它把测试集缩减为 query list，把多个变量族混在连续步骤中，并把未经验证的
结构切分、RCS、contextual enrichment 和 claims layer 当成预期收益，缺少可复现的
baseline、held-out、许可边界、证据标注、评分公式与回滚门。

| 评判维度 | 旧版状态 | 本次决策 |
|---|---|---|
| 问题定义 | 正确识别纯 dense、精确 token、坏切分等风险 | 保留，但先用 failure slices 验证影响大小 |
| 第 0 步 | 30–50 queries，没有固定 PDF/笔记/证据语料 | 改为 S5/S20/S100 真实 PDF + reference note + qrels |
| 可归因性 | RCS、切分、hybrid、context 和 claims 顺序中仍有相互补偿 | 一次只改变一个变量族，并冻结其他层 |
| 评分 | 只有 Recall/MRR 概念 | 固定 100 分 scorecard、硬门、分 slice 和置信区间 |
| 数据治理 | 未定义版权、hash、版本与 held-out | manifest、许可策略、版本指纹和防泄漏成为合同 |
| 证据追溯 | chunk 没有 page/section/span，后续 claims 无可靠指针 | provenance 成为所有检索增强的前置 |
| 复杂度控制 | 高级方案按预期 ROI 排序 | 只有 held-out 证明收益才升级默认 |
| 可回滚性 | 大多只有“独立可交付”的表述 | collection/version/feature flag/fallback 明确化 |

因此，本 ADR 的最终判断不是“六个功能都应该做”，而是“六个决策门依次打开”。
第 0–1 步是确定投入；第 2–4 步是对照实验；第 5 步是条件性投入；claims/GraphRAG-lite
退出当前实施承诺。

### 2.5 独立审查闭环

2026-07-27 由两个只读审查者分别从架构实施和 benchmark 科学性审查 live repository。
审查发现的硬阻塞已在本版关闭：

- 0C 与 provenance 的依赖倒置：改为 `0A → 0B → 1A → 0C`；
- S5 CI 与宿主 Agent 冲突：拆分 offline component 和 host-assisted E2E；
- passage qrels 依赖 chunk：改为稳定 evidence units 和候选映射；
- 同一 held-out 被逐波查看：改为 D20/V20/H60 生命周期；
- collection 与 ledger 无法原子回滚：改为 side-by-side `IndexArtifact`；
- 当前脚本导入副作用和全局 collection 不利于 A/B：0A 增加纯 baseline seam；
- 评分可能被大量 abstain 利用：增加 completeness 和 answered-positive guardrails；
- 多相关文献漏标：增加 pooling、unjudged 与 annotation agreement 门。
- pooling 覆盖不足会选择性排除困难 query：改为 H60 top-k union 盲态补标，无法补齐则
  整个 release run incomplete；
- 重叠 chunk 可能重复获取同一 evidence gain：改为唯一 evidence 折叠后计算指标。

本 ADR 目前没有已知的文档级 P0。这里的“implementation-ready”只表示可以从 0A 开始按门
推进，不表示尚未实施的检索方案已经证明有效。

---

## 3. 目标、非目标与原则

### 3.1 目标

- 用真实、多领域、可复现的学术 PDF 衡量检索质量；
- 分离 note generation、document discovery、passage retrieval 和 answer synthesis 的误差；
- 让每次架构升级都有可比较的质量、延迟和索引成本；
- 保持 local-first、terminal-first、可恢复和后端可替换；
- 产出可公开复核的 benchmark contract、baseline report 和 release report。

### 3.2 非目标

- 不以通用公开 benchmark 代替本项目自己的 Zotero/科研工作流 benchmark；
- 不在第 0 步微调 embedding 模型；
- 不把 LLM judge 当唯一 ground truth；
- 不为了追求单一总分牺牲 SI、精确 token、跨语言或负面查询；
- 不在本 ADR 中实现完整 GraphRAG、community detection 或通用知识图谱；
- 不把无法确认再分发权利的 PDF 提交到公开 Git 仓库。

### 3.3 实验原则

1. **一次只改一个变量族。** 切分、embedding、retriever、reranker 和 Agent 策略分阶段比较。
2. **先 component eval，再 end-to-end eval。** 先确认拿到了正确证据，再评价回答。
3. **开发、重复验证与发布盲测分离。** S20-Dev 可调参，V20 可重复回归，H60 每个
   benchmark major 只在整批 release candidate 冻结后揭盲一次。
4. **分领域报告优先于单一平均分。** 宏平均不能掩盖某一领域或 query slice 的崩溃。
5. **自动指标必须保留人工审查出口。** LLM judge 只能作为辅助诊断。
6. **报告绑定版本。** benchmark、corpus、note schema、chunk schema、embedding 和 retriever
   任一版本变化，都产生新报告，不覆盖旧结果。

---

## 4. Benchmark 平面与运行时边界

Benchmark 是独立于生产索引的评测平面，不直接读取用户私人 vault 作为公开 ground truth。

```text
Benchmark corpus
  ├─ PDF / SI manifest + license + SHA-256
  ├─ human-reviewed reference notes
  ├─ query-level answer keys + stable evidence units
  ├─ queries + document/evidence qrels + judgment pools
  └─ S5 / S20 / S100 suite definitions
             │
             ▼
Evaluation runner
  ├─ Extractor adapter
  ├─ Chunker adapter
  ├─ Embedding adapter
  ├─ Retriever adapter
  ├─ Reranker adapter
  └─ Answer/evidence evaluator
             │
             ▼
Versioned JSON + Markdown reports
```

评测接口采用 ports/adapters 边界：

| Port | 输入 | 输出 |
|---|---|---|
| `CorpusProvider` | suite id | 固定 paper/SI 文件与 manifest |
| `Extractor` | PDF | 带页码和结构的 document units |
| `Chunker` | document units + config | `ChunkRecord[]` |
| `Embedder` | text + fingerprint | vectors |
| `IndexBuilder` | chunks + embedder config + index id | `IndexArtifact` |
| `Retriever` | index artifact + query + filters + top-k | ranked hits + scores |
| `Reranker` | query + candidate hits | reranked hits |
| `Evaluator` | hits/answer + qrels | metrics + slice failures |
| `AgentRunner` | query + retrieval runtime + frozen config | answer + trace |

只在这些真实可替换边界使用轻量 `Protocol`/callable，不引入完整 Clean Architecture 或 DDD
目录层。生产代码不依赖 benchmark 实现；benchmark runner 通过 adapter 调用当前生产实现
或候选实现。

Wave 0A 先做一个行为不变的 baseline seam：把当前 PDF 提取和 800/700 切分移入可纯调用、
无导入副作用的模块。导入该模块不得扫描 `NOTES_DIR`、读取 Zotero、初始化 Chroma 或绑定
全局 collection；现有 `build_pdf_db.py` 只作为 CLI adapter 调用它。这不是检索算法变更。

Benchmark runner 必须 fail closed 地与用户运行时隔离：

- 每次运行创建独立 `run_root`、Chroma path、collection、ledger 和 cache；
- 不读取用户 vault、Zotero DB、生产 `LOCALRAG_HOME` 或 query log；
- 如果 benchmark path 与生产 Chroma/ledger 路径相同或互相包含，立即拒绝运行；
- 清理操作只能作用于本次 `run_root`；
- 报告先经 sanitizer 转为 `PublicHit`，删除 `pdf_path`、vault path、用户名和未授权原文。

建议目录合同：

```text
benchmarks/
  README.md
  ANNOTATION_GUIDE.md
  schemas/
    *.schema.json
  corpus/
    manifest.jsonl
  suites/
    s5.yaml
    d20.yaml
    v20.yaml
    h60.yaml
    s100.yaml
  gold/
    notes/
    answers.jsonl
    claims.jsonl
    evidence_units.jsonl
  queries/
    queries.jsonl
    document_qrels.jsonl
    evidence_qrels.jsonl
    judgment_pools.jsonl
  configs/
    baseline-fixed-800.yaml
  locks/
    benchmark-environment.txt
  reports/
    baseline/
  scripts/
    fetch_corpus.py
    validate_benchmark.py
    run_benchmark.py
    compare_reports.py
```

---

## 5. 第 0 步：分层、多领域 Benchmark 合同

第 0 步是后续所有阶段的硬前置，但分层交付，避免等待 S100 全部标注完才开始迭代。

### 5.1 语料层级

保留用户要求的 5/20/100 三层规模，但把 S100 划分为开发、重复验证和一次性发布盲测：

- `S5 ⊂ S20-Dev`；
- `S100 = D20 + V20 + H60`；
- D20 即公开的 `S20-Dev`，用于调参和失败分析；
- V20 是公开 validation，可供各 subwave 重复回归，但不能用于调参；
- H60 的 queries/qrels 在 release candidate 全部冻结前封存，只在一次 paired release run
  中同时运行 baseline 和 candidate。

| Suite / partition | 规模 | 用途 | 运行位置 | 是否允许调参 |
|---|---:|---|---|---:|
| `S5 Smoke` | 5 篇 | 验证离线 PDF、解析、切分、索引、qrels 和评分接线 | Ubuntu/Python 3.11 专用 job | 否 |
| `S20-Dev / D20` | 20 篇 | 开发、消融、失败分析和参数选择 | 手动 PR / nightly | 是 |
| `S100 / V20` | 20 篇 | 每个 subwave 的重复验证与回归门 | nightly / workflow_dispatch | 否 |
| `S100 / H60` | 60 篇 | 整批 release candidate 的最终盲测 | owner release workflow | 否，且揭盲一次 |

H60 揭盲并公开后转为下一版本的公开 regression 数据；下一个 benchmark major 必须补充
新的 sealed H60。发现泄漏时立即废弃该 H60 的“held-out”身份，不能继续用它声称发布提升。

S5 只能证明链路正常，S20 只能选参与诊断，V20 只能重复验证。任何“发布默认值变更”必须
把所有拟发布 subwave 冻结后，通过 H60 paired release run。这样既允许快速迭代，又不让
同一 held-out 被每一波反复查看。

### 5.2 领域分层

固定五个领域。每个领域在 S5/D20/V20/H60 中分别占 1/4/4/12 篇，因此 S100 每个领域
共 20 篇：

1. **催化与材料化学**：SI、样品编号、实验参数、谱图与性能表；
2. **生物医学**：方法、样本量、终点、统计结果与 Supplement；
3. **计算机科学 / 机器学习**：算法、公式、数据集和 benchmark 表；
4. **环境、能源与地学**：长文档、单位换算、跨章节证据和复杂表格；
5. **社会科学 / 经济学**：因果主张、回归表、研究限制和争议解释。

最终选文以明确许可和文档结构覆盖为准；如某领域无法获得合适的可再分发语料，
允许替换领域，但必须保持五领域平衡并发布变更记录。

### 5.3 文档结构分层

完整 S100 还要覆盖：

- 原始研究、review/perspective、methods、theory；
- 有/无 SI；
- 单栏与双栏；
- 表格密集、图注密集、公式密集和纯文本；
- 短文、中等长度和长文；
- native text PDF 与少量 OCR/扫描失败案例。

S5 只要求五领域各一篇，不承担结构标签或 query taxonomy 的统计覆盖。S100 manifest
必须报告每个结构标签的 paper/query 数；少于 10 篇或少于 30 条 query 的 slice 只作
描述性诊断，不宣称具有普遍结论。领域平衡不能替代结构平衡。

### 5.4 PDF 获取与许可

公开 benchmark 只接受：

- 明确为 CC-BY、CC0、public domain 或其他允许 PDF 与必要 annotation 再分发的语料；
- 有稳定 source URL、可验证 license URL、SHA-256 和 attribution 的版本。

每条 manifest 至少包含：

```json
{
  "paper_id": "stable-id",
  "domain": "catalysis",
  "document_type": "research-article",
  "main_pdf": {
    "source_url": "https://...",
    "sha256": "...",
    "license": "CC-BY-4.0",
    "license_url": "https://...",
    "redistribution": "allowed",
    "verified_at": "YYYY-MM-DD",
    "attribution": "..."
  },
  "si": [],
  "doi": "...",
  "language": "en",
  "structure_tags": ["two-column", "table-heavy"]
}
```

100 篇 PDF 不直接进入主 Git 历史。它们放在带版本与 checksum 的独立 benchmark dataset
或 GitHub Release；主仓库保存 manifest、annotation 和获取/校验脚本。普通 PR CI 不从
出版商临时下载 PDF，也不依赖首次模型下载；固定 S5 artifact 与模型 cache 缺失时该专用
job 明确失败，不回退到私人文件。

仅允许下载但不可再分发、许可不明确或私人持有的论文只能进入 `local-extension`，不得
混入官方 S5/D20/V20/H60 分数，也不得用其结果做公开质量声明。

### 5.5 对应笔记与三条评测轨道

每篇 PDF 必须有一篇 human-reviewed reference note。系统生成结果不能直接当 gold：

1. 先由当前系统生成 candidate note；
2. 标注者逐项对照 PDF 修订；
3. 修订后的 note 才进入 `gold/notes/`；
4. candidate、model、prompt、domain pack 与生成时间单独记录。

为检查 candidate 对 gold 的结构性锚定偏差，至少 20% reference notes 由标注者在不看
candidate 的条件下独立制作，再与 candidate-assisted notes 比较遗漏分布。

Benchmark 同时运行三条轨道：

| Track | 索引内容 | 目的 |
|---|---|---|
| `PDF-only` | 源 PDF chunks | 单独评价 passage retrieval |
| `Gold-note` | reference notes | 隔离 note generation 后评价 note discovery |
| `Generated-note E2E` | 当前系统生成 notes + PDF | 评价真实产品链路 |

另设 `Oracle-paper` 诊断：已知正确 `zotero_parent_key` 后只在该论文内搜 passage，
用于区分“找错论文”和“论文内找错段落”。

三条轨道分成两个 runner，不能把宿主 Agent 假装成普通 CI 依赖：

| Runner | 内容 | 门禁 |
|---|---|---|
| `offline-component` | 固定 PDF、gold notes、冻结的 generated-note fixtures；运行 PDF-only、Gold-note 和固定 Generated-note retrieval | S5 专用 CI / D20 / V20 |
| `host-assisted-e2e` | 通过现有 manifest/resume 协议重新生成 candidate notes/answers，记录 backend/model/prompt/token/latency | manual / workflow_dispatch，不是普通 PR required check |

S5 CI 不实时调用 LLM/subagent。未运行 answer track 时，对应指标记为 `N/A`，不能记零、
不能重归一化，也不能输出 100 分总分。完整 100 分只在 host-assisted E2E 全部完成时计算。

### 5.6 Gold annotation

Gold passage relevance 不得引用候选 `chunk_id`。否则 C0–C4 一改变边界，ground truth
就会跟着失效。每篇文献至少包含：

- bibliography 与主文/SI 关系；
- reference note；
- query-level answer key 与核心 claims；
- claim 对应的稳定 evidence units；
- 关键实验条件、数值、单位、样品编号；
- 允许“不足以回答”的边界说明。

`EvidenceUnit` 至少包含：

```text
evidence_id
paper_id / file_id
pdf_page_index
printed_page_label (optional)
canonical_page_hash
verbatim_quote + quote_hash
bbox or offsets-in-canonical-page
evidence_group_id
role: required | supporting
```

页码采用明确基准的 `pdf_page_index`；印刷页码只作可选展示。canonical page text 的
规范化规则和 extractor fingerprint 一并冻结。Evaluator 根据预注册的 quote/token overlap
规则把任意候选 `ChunkRecord` 映射到 evidence units；切分器不能改变 gold。

Document qrels 和 evidence qrels 使用 0–3 graded relevance，`-1` 表示 unjudged：

| Label | 含义 |
|---:|---|
| -1 | 尚未判断，不能静默当作不相关 |
| 0 | 不相关或误导 |
| 1 | 背景相关，不能直接支持答案 |
| 2 | 可支持答案的一部分 |
| 3 | 核心证据或最佳命中 |

Multi-hop query 把所需证据拆成多个 `required evidence_group_id`；只有全部 required groups
命中才记 complete success，同时报告 evidence-unit recall，避免“命中任意一跳”虚高。

topic discovery、cross-paper 与 contradiction qrels 使用 pooling：合并 dense、lexical、
hybrid、reranker 与人工候选的 top-k 后标注。报告 `judged@10` 和 pool coverage。

H60 paired run 产生 baseline/candidate 结果后、计算指标前，由不知道 run identity 的标注者
对两者 top-k union 做盲态补标；命中顺序和系统标签先打乱。补充 qrels 冻结后才评分。
不得因为 `judged@10` 不足选择性排除困难 query；如果无法完成补标，整个 release run 标记
`incomplete`，不能得出 ship/no-ship 结论。

Evaluator 把 ranked chunks 映射并折叠为唯一 `evidence_id/evidence_group_id`。同一 query
中，同一 evidence 只有排名最高的首次命中可以贡献 gain；后续重叠 chunk 的 gain 为 0。
`Evidence nDCG@10`、Evidence Recall 和 multi-hop complete success 均在折叠结果上计算，
避免 chunk 更多或 overlap 更高的方案重复得分。

所有 release evidence 经过第二人复核；随机至少 20% 由第二位标注者盲态独立重标，
报告 0–3 qrels 的 weighted κ 或 Krippendorff α，以及 evidence span token-F1。
一致性门在 0A 预注册；默认要求 qrels agreement `>= 0.70`、span F1 `>= 0.80`。
未达到时先修订指南并重标，不能继续生成 baseline。分歧由项目所有者或第三标注角色仲裁。

### 5.7 Query taxonomy

Query 不从单一模型批量生成后直接作为 ground truth。来源优先级：

1. 真实 query log，经匿名化和许可处理；
2. 研究者根据实际任务撰写；
3. 从 gold claim 反向构造，但必须人工改写；
4. LLM 只用于提出候选和 hard negatives，不能独立定稿。

每条 query 至少记录 `query_id`、`partition`、`domain`、`slice_ids`、`answerability`、
`expected_claim_ids`、`required_evidence_group_ids` 和 language direction。

D20/V20/H60 整体覆盖以下 slice：

- topic / literature discovery；
- paper-specific lookup；
- method、实验条件和 SI；
- 精确数值、单位、分子式、样品编号、作者和 DOI；
- mechanism / causal explanation；
- cross-paper comparison 与 contradiction；
- timeline / development；
- 中文查询检索英文论文、英文查询检索英文论文；当前跨语言硬门只针对
  “中文 query → 英文 corpus”，其他方向先作诊断；
- multi-hop；
- negative：语料无答案、错误前提、实体歧义、证据冲突四类。

目标规模：

| Suite | Query 数 | 说明 |
|---|---:|---|
| S5 | 25 | 每篇约 5 条，覆盖接线和边界 |
| D20 | 100 | 每领域约 20 条，用于开发 |
| V20 | 60+ | 不参与调参，用于重复验证 |
| H60 | 140+ | 封存到 release run，并含跨论文问题 |
| S100 total | 300+ | 五领域各至少 40 条单领域 query，另含跨领域 query |

exact-token、SI、跨语言、multi-hop 和 negative 每个关键 slice 在 S100 至少 30 条 query；
slice 可以重叠。H60 中 negative 至少 30 条，其他四个 release-critical slice 各至少
20 条；answer track 至少产生 100 个可评估 cited claims 和 100 个 positive expected
claims。达不到 H60 最低分母时 release run 标记 `incomplete`，不能得出 ship/no-ship
结论；不能改用全 S100 聚合绕过 H60。

### 5.8 评分标准

主报告使用 100 分 scorecard，但 ship/no-ship 仍受硬门约束。权重在读取 H60
结果前冻结：

| 维度 | 子指标 | 分值 |
|---|---|---:|
| 论文级检索 | Document Recall@5 | 8 |
|  | Document nDCG@10 | 8 |
|  | Document MRR@10 | 4 |
| 段落级证据 | Evidence Recall@5 | 10 |
|  | Evidence Recall@10 | 6 |
|  | Evidence nDCG@10 | 8 |
|  | Gold page/span hit rate | 6 |
| 回答与引用 | Claim faithfulness | 6 |
|  | Citation precision | 6 |
|  | Citation coverage | 5 |
|  | Answer completeness / gold claim recall | 5 |
|  | `1 - unsupported claim rate` | 3 |
| 鲁棒性 | exact-token / SI / 跨语言 / multi-hop / negative-abstention | 每个 slice 3，共 15 |
| 工程效率 | chunks per paper / embedding input size / index size / build time / p95 latency | 每项 2，共 10 |

计算规则：

- 所有质量指标先映射到 `[0, 1]`，再乘对应分值；
- 领域总分先在每个领域内按 query 宏平均，再对五个领域宏平均，避免大领域支配结果；
- robustness 的五个 slice 分别使用该 slice 的主指标：前四项用 Evidence Recall@5，
  negative-abstention 用 `1 - false-answer rate`；
- 每个效率项使用 `min(1, baseline_value / candidate_value)`，比 baseline 更快或更小不额外
  获得超过满分的奖励；
- citation precision 是“被引用 source span 中真正支持对应 claim 的比例”；
- citation coverage 是“gold answer 中需要证据的 claim，被正确 source span 覆盖的比例”；
- answer completeness 是“answer key 中 expected claims 被最终答案正确覆盖的比例”；
- unsupported claim rate 是“最终答案中无法被召回证据支持的可核验 claim 占比”；
- faithfulness/unsupported claim 的 claim segmentation 规则、judge model、prompt、重复次数和
  人工仲裁样本在 0A 冻结；LLM judge 只产候选判断，硬门样本必须可回溯到人工 gold；
- 总分只用于排序和沟通；原始指标、领域结果、slice 结果和失败案例必须同时发布。

硬门：

- scorecard 只用于展示；ship/no-ship 只由 primary、guardrails 和以下 hard gates 决定；
- citation precision 点估计 `< 0.95` 或单侧 95% lower bound `< 0.90`：不得发布为默认；
- negative false-answer rate 点估计 `> 0.05` 或单侧 95% upper bound `> 0.10`：
  不得发布为默认；
- answered-positive rate `< 0.90`、比 baseline 下降超过 2 个百分点，或 answer completeness
  比 baseline 下降超过 2 个百分点：不得发布，防止靠大量 abstain 刷高 precision；
- 任一关键 slice 相对 baseline 下降超过 2 个百分点：必须解释并由所有者显式接受；
- 每个实验 PR 在看 V20 前预注册一个 primary metric；V20 必须先通过 guardrails；
- 最终 H60 使用 candidate-baseline paired delta：primary relative delta 至少 `5%`，
  且 paired 95% CI 不跨 0，才允许为复杂度更高的方案迁移生产默认；
- baseline primary 为 0 时，必须在 0A/对应 PR 中预注册 absolute delta，不能揭盲后改门槛；
- 与 primary metric 配对的 guardrail metric 不得下降超过 2 个百分点：chunking 以
  Evidence Recall@5 为 primary、Document Recall@5 为 guardrail；fusion/rerank 以
  Evidence nDCG@10 为 primary、Evidence Recall@5 为 guardrail；Agent 阶段以
  claim faithfulness 为 primary、citation precision 为 guardrail；
- p95 latency 或索引体积超过 baseline 1.5x 时，必须证明对应质量收益并提供关闭开关；
- 发现错引 paper、错页码或伪造 source span：作为 P0 缺陷处理，不由总分抵消。

比较采用 paired、domain-stratified、paper/source-cluster bootstrap 95% CI，不能把同一论文
的多个 query 当作独立样本。cross-paper query 以其 query group 为 cluster。效率指标在同一
硬件、依赖锁和 warm/cold 约定下至少重复 3 次，报告 median 与 p95。

### 5.9 版本与防泄漏

每份报告记录：

- `benchmark_version`、`corpus_version`；
- S5/D20/V20/H60 paper ids；
- note、query、qrels commit hash；
- `extractor_version`、`chunk_schema_version`；
- chunk config；
- embedding provider/model/revision/dimension；
- distance metric；
- retriever、fusion、reranker config；
- Agent/model/prompt 版本；
- build/query hardware 与时间；
- 随机种子。

benchmark 专用依赖和模型 revision 使用独立 lock/fingerprint，不能仅依赖仓库中的宽版本
范围。公开报告只含 sanitized `paper_id/file_id/page/evidence_id`，不含用户绝对路径、
私有 vault 文本、API key 或可识别 query log。

H60 的 queries/qrels 在 release 前存放于维护者控制的 sealed bundle；corpus manifest、
schema 和验证器仍公开。release run 完成后发布 H60 queries/qrels、baseline/candidate
原始结果和报告，使结论可复现。发现泄漏或揭盲后继续调参时，递增 benchmark major，
重新建设 H60，旧 H60 仅作为 regression。

### 5.10 第 0 步交付顺序与完成标准

| 子阶段 | 交付物 | 初始预算 | 完成标准 |
|---|---|---:|---|
| 0A Contract | schema、validator、隔离 runner、纯 baseline seam、评分/标注指南 | 2–3 人日 | 空 corpus contract tests；导入无副作用；生产路径隔离测试 |
| 0B S5 | 5 PDF + notes + qrels + offline runner | 2–3 人日 | 固定 artifact/cache 的专用 CI 可重复运行；legacy partial report |
| 1A Canonical IR | page/span IR + C0 adapter | 2–4 人日 | evidence mapping 和 provenance tests 通过 |
| 0C D20 | 20 PDF + 100 queries + C0 full baseline | 60–120 人时 | 完整 component report + annotation audit |
| 0D V20 | 20 PDF + 60+ queries | 60–120 人时 | 可重复 validation report |
| 0E H60 | 60 PDF + 140+ sealed queries | 180–360 人时 | sealed bundle + 一次 release workflow |

标注预算以 S5 实测吞吐校准，不承诺不现实的自然日。1A 在 0B 后立即启动；D20/V20/H60
annotation 可并行。Wave 2–4 在 D20 full baseline 完成后逐步开发，以 V20 为重复回归；
更改生产默认必须等整批候选通过一次 H60 release run。

---

## 6. 第 1 步：证据可追溯的 PDF 表示

### 6.1 决策

先修复数据表示，再比较切分器。Extractor 不再把所有页直接拼成一个无边界字符串。
标准中间表示至少包含：

```text
DocumentPage:
  paper_id
  file_id
  pdf_page_index
  printed_page_label
  normalized_text
  page_text_hash

ChunkRecord:
  chunk_id
  paper_id
  file_id
  is_main / is_si
  start_page / end_page
  section_path
  source_spans[]
  text
  chunk_schema_version

SourceSpan:
  file_id
  pdf_page_index
  char_start_in_normalized_page
  char_end_in_normalized_page
  page_text_hash
```

Caption、表格文本与所在页必须保留来源；无法可靠解析时显式标记 extraction warning，
不能静默伪装为普通正文。`chunk_id` 绑定 `file_hash + extractor_fp + chunker_fp +
source_spans + text_hash`。相邻关系使用显式 `previous_chunk_id/next_chunk_id`，不再从 ID
后缀推导。

### 6.2 架构边界

- Extractor 负责 PDF → page/section units；
- Chunker 只消费 units，不直接打开 PDF；
- Index builder 只消费 `ChunkRecord`；
- Retriever 内部返回完整 provenance，公开 adapter 只返回 sanitizer 后的 `PublicHit`；
- MCP/HTTP adapter 不自行推断页码。

索引生命周期使用一个原子 artifact：

```text
IndexArtifact:
  index_id
  collection_name
  ledger_path
  extractor_fp / chunker_fp / embedding_fp
  distance_metric
  schema_version
  built_commit / corpus_hash
```

`IndexBuilder.build(chunks, embedder_config, index_id) -> IndexArtifact`；
`Retriever.search(index_artifact, query, filters, top_k) -> RankedHit[]`。filters 至少覆盖
`paper_id/zotero_parent_key/file_id/is_main/is_si`。collection 与 ledger 必须由同一
`index_id` 隔离，doctor 核对完整 fingerprint；模型维度相同但 fingerprint 不同也要
fail closed。

### 6.3 验收门

- S20 中 100% 可索引 chunk 带 `file_id`、页码和稳定 span；
- gold evidence span 可映射到至少一个 chunk；
- 主文/SI provenance 无混淆；
- 重建后 chunk id 对相同内容稳定；
- 当前 baseline 的 document Recall@5 不下降超过 2 个百分点；
- schema migration、doctor 检查与 rebuild 命令齐备。

### 6.4 回滚

默认 side-by-side 构建新 collection 与独立 ledger，验证后通过 active-index manifest /
`LOCALRAG_PAPERS_COLLECTION` 切换并重启服务。旧 `index_id` 至少保留一个成功发布周期；
回滚只切回旧 index，不重建、不删除。in-place `--rebuild` 仅保留为显式维护命令，不能作为
迁移默认。不得让新旧 chunk 或 ledger 混写。

**估时：** 2–4 天。

---

## 7. 第 2 步：切分策略对照

### 7.1 冻结变量

比较切分时固定：

- D20 corpus/query/qrels；
- repository 默认 FastEmbed 模型与 revision；
- distance metric；
- dense-only retriever；
- top-k；
- 不使用 reranker、RCS、query expansion 或 contextual prefix。

### 7.2 候选方案

| ID | 策略 | 参数 |
|---|---|---|
| C0 | 当前 baseline | fixed char 800 / step 700 |
| C1 | 固定窗口消融 | target 800，overlap 0% / 10% |
| C2 | paragraph packing | target 800 / 1200，max 1600，保持段落完整 |
| C3 | section-aware | section/heading 边界，target 800 / 1200 / 1600，max 2000 |
| C4 | section-aware + caption binding | C3，并把 figure/table caption 与最近正文绑定 |

不在本阶段默认引入 LLM semantic chunking。近期研究对 semantic chunking 是否稳定优于
简单句子/结构切分结论并不一致，因此它只能作为后续候选，不作为预设答案。
C4 只有在 Extractor 已可靠输出 caption/table structure 时才进入候选；否则标记
`capability-gated`，不为赶进度伪造结构。

### 7.3 选择规则

- 在 D20 上调候选参数；
- 先看 Evidence Recall@5、Evidence nDCG@10，再看 index/latency；
- 选择一个候选后在 V20 确认，不查看 H60；
- 默认目标是 C3/C4，但若其 V20 提升未越过硬门，则继续保留更简单的 C0/C2；
- fixed chunker 永久保留为 fallback 和 benchmark baseline。

### 7.4 必须输出的消融

- chunk size；
- overlap；
- 是否保留 paragraph；
- 是否保留 section；
- caption binding；
- 每篇 chunk 数、重复字符比例、跨页 chunk 比例；
- 各领域和 query slice 指标。

**估时：** 3–5 人日，不含 annotation。

---

## 8. 第 3 步：召回、融合、重排与 embedding

### 8.1 实施顺序

先冻结获胜 chunker，依次比较：

| ID | Candidate |
|---|---|
| R0 | dense-only 当前 baseline |
| R1 | lexical-only（SQLite FTS5 trigram） |
| R2 | dense + lexical，Reciprocal Rank Fusion |
| R3 | R2 top-30 + 本地多语言 ONNX reranker → top-5/10 |

FTS5 能力必须由 setup/doctor 实测。环境不支持 FTS5 时回退 dense-only，并明确报告，
不能静默返回不同算法的结果。

### 8.2 为什么先 hybrid

Dense retrieval 擅长语义近似；lexical retrieval 是样品编号、化学式、DOI、作者与精确术语的
必要 baseline。两路分数不可直接相加，默认使用 RRF；fusion 常数进入配置和报告。

### 8.3 Embedding 消融

只有 R0–R3 确定后才比较 embedding，且固定 chunker/retriever/reranker：

- E0：repository 默认 FastEmbed；
- E1：Ollama 本地多语言模型；
- E2：OpenAI-compatible embedding endpoint。

Wave 3C 前引入显式 `EmbedderConfig` 与 factory，使 notes/papers 可以独立固定 provider、
model、revision 和 dimension；现有全局 env 只作为兼容默认。不同 embedding fingerprint
必须使用独立 `IndexArtifact`，不能仅凭 dimension 相同复用 collection。

### 8.4 默认选择门

- R2 必须显著改善 exact-token/SI slice，且 dense semantic slice 不下降超过 2pp；
- R3 必须先在 V20 比 R2 提升预注册 primary，且通过 guardrails；
- reranker p95 不超过无 reranker 1.5x，或提供 `rerank=false` 快速路径；
- embedding 迁移只有随整批 release candidate 在 H60 获得显著提升时才改变 repository 默认。

**估时：** 3A hybrid 2–4 人日；3B reranker 1–2 人日；3C embedding 1–2 人日。

---

## 9. 第 4 步：查询编排与 Agent 侧重排

### 9.1 决策

底层 deterministic retrieval 固定后，再评估：

1. anchor + exploratory query planning；
2. 宽召回 top-20/30；
3. RCS relevance summary + keep/drop；
4. 最多两轮 self-ask；
5. 明确 stop/expand/abstain 条件；
6. 最终回答区分 note synthesis 与 PDF evidence。

RCS 是可选的高质量路径，不替代本地 reranker，也不描述为“零成本”。
Agent benchmark 通过 `AgentRunner.run(query, retrieval_runtime, config) -> AnswerTrace`
和现有 manifest/resume 协议执行，不要求普通 CI 能直接调用宿主 Agent。

### 9.2 Trace 合同

每次 benchmark query 保存：

- 原问题；
- query variants；
- 每轮 candidate ids/scores；
- fusion/rerank 决策；
- keep/drop 与理由；
- 使用的 source spans；
- 最终 claims/citations；
- token、延迟和停止原因。

不得记录 API key、私人绝对路径或可识别用户信息。

### 9.3 验收门

- answer faithfulness 与 citation coverage 达到评分硬门；
- RCS 相对 deterministic reranker 有可测增益；
- negative queries 能停止并明确“语料不足”；
- self-ask 平均轮数、p95 latency 和 token 使用进入报告；
- 如果增益只来自更大 token budget，必须单独说明。

**估时：** 4A trace/planner 1–2 人日；4B RCS 1–2 人日；4C self-ask/answer 2–3 人日。

---

## 10. 第 5 步：Contextual enrichment（条件性）

Contextual enrichment 不是既定默认，只在以下条件同时满足时启动：

- D20/V20 failure analysis 显示指代密集、缺失 section context 是主要错误源；
- section-aware chunking + hybrid + reranker 仍未解决；
- 预计收益值得额外的入库 token、时间和缓存状态。

实现要求：

- 每个 chunk 生成 50–100 token 的位置/指代上下文；
- cache key 包含 chunk hash、document hash、prompt 和 model fingerprint；
- original text、context prefix 分字段保存，展示引用时只能引用 original text；
- 走可恢复 manifest，不允许半完成 enrichments 混入正式 collection；
- 与无 enrichment 的 R3 baseline 做同 corpus A/B。

只有 V20 先通过、并随最终候选在 H60 达到预注册门槛且 index/latency 不越过硬门时，
才允许成为可选默认。否则保留实验功能或拒绝合并。

**估时：** 3–5 人日。

---

## 11. 延后事项与单独 ADR

### 11.1 Claims / GraphRAG-lite

Claims 表会改变核心数据模型、annotation 与查询方式，不再塞进 RAG v2。只有以下证据出现时
才启动新 ADR：

- WF6 横向比较、WF8 时间线或 WF10 矛盾检测在完成第 4 步后仍显著落后；
- failure analysis 显示瓶颈是 claim relation，而不是召回或引用；
- S100 中存在足够的 multi-paper gold claims 可评测。

### 11.2 暂不采纳

| 方案 | 原因 |
|---|---|
| 完整 GraphRAG/community clustering | 当前规模与 Zotero/frontmatter 已提供结构，复杂度过高 |
| ColBERT/late interaction 默认化 | 索引和部署成本较高，先验证 hybrid + reranker |
| embedding 微调 | 尚无足够训练/验证语料，先做 benchmark 与 reranking |
| 无评测的 LLM semantic chunking | 不可归因、不可复现、可能把生成偏差写入索引 |
| 直接增加超长 context | 会引入噪声、延迟和 lost-in-the-middle 风险 |

如果 Gold-note track 显示整篇笔记 embedding 本身是主要瓶颈，另立 note-retrieval ADR；
本 ADR 的 chunking/hybrid 结论默认只改变 papers 路径，不能顺手改 notes。

---

## 12. 实施波次与依赖

```text
Wave 0A  Contract + isolated runner + pure legacy seam
   ↓
Wave 0B  S5 offline smoke + legacy partial report
   ↓
Wave 1A  Canonical page/span IR + C0 benchmark adapter
   ↓
Wave 0C  D20 full C0 baseline ───┬── Wave 0D V20 validation
   │                             └── Wave 0E H60 sealed curation
   ↓
Wave 1B  Side-by-side production IndexArtifact migration
   ↓
Wave 2   Chunking A/B: D20 tune → V20 confirm
   ↓
Wave 3A  Lexical + RRF
   ↓
Wave 3B  Reranker
   ↓
Wave 3C  Embedding isolation + ablation
   ↓
Wave 4A  Trace + planner
   ↓
Wave 4B  RCS
   ↓
Wave 4C  Self-ask + answer
   ↓
Freeze complete release candidate
   ↓
One paired H60 run: baseline vs candidate
   ↓
Release / rollback / new benchmark major
   ↓
Wave 5 optional contextual enrichment (new release cycle)
```

Wave 2 及之后的每个 subwave 使用同一个迭代闭环；0A/0B/1A 按 5.10 的专项完成标准：

1. **Freeze**：登记 corpus/qrels、baseline、唯一变量族、primary metric 和 guardrails；
2. **Build**：实现最小候选和必要的 adapter/feature flag，不同时改下游策略；
3. **Verify**：先跑 contract/unit tests，再跑 S5，随后跑 D20；
4. **Diagnose**：输出分领域、分 slice、失败 query、成本和置信区间，不只看总分；
5. **Decide**：满足门槛则冻结候选；未满足则回滚或最多进行一次基于 D20 失败证据的
   参数迭代，不能根据 V20 query-level 结果调参；
6. **Confirm**：单个 subwave 在 V20 确认；H60 留给整批 release candidate；
7. **Publish**：H60 只运行一次 paired release；报告绑定 commit/config，默认变化同步
   migration、doctor、README 和 rollback。

如果同一变量族在 D20 上连续两次未越过预注册门槛，该 subwave 暂停，不靠增加 LLM、扩大
top-k 或修改其他层掩盖失败。

Wave 2 及之后使用统一 DoR/DoD：

**Definition of Ready**

- 上游 `IndexArtifact`、benchmark 与 dependency/model lock 已冻结；
- 唯一变量族、primary metric、guardrails、运行预算和 rollback command 已登记；
- required corpus 在目标 runner 可获得，且不触碰生产路径。

**Definition of Done**

- contract/unit/S5 通过；
- D20 before/after、failure slices 和 V20 confirmation 已生成；
- 适用时，index manifest、doctor、activation、rollback 与 sanitizer tests 通过；
- 未通过 H60 release gate 前，不改变 repository 默认。

### 12.1 分支与 PR 规则

每个 subwave 独立小 PR，不把 benchmark、chunker、retriever 和 Agent prompt 混在一个 diff：

- PR 必须声明唯一变量族；
- 附 before/after report；
- 附失败 slice；
- 附 rebuild/migration/rollback；
- benchmark corpus/qrels 变更与算法变更分开 PR；
- H60 结果由 sealed release workflow 生成，不由开发者手改；
- corpus/qrels 变更先生成新 benchmark version，算法 PR 不能夹带 gold 修改。

### 12.2 生产默认变更

满足全部条件才切换默认：

1. D20 开发结果和 V20 confirmation 通过；
2. 整批 release candidate 的 H60 paired run 通过硬门；
3. 三平台 unit/integration CI 通过；
4. migration、doctor、rebuild 与 rollback 已验证；
5. README 中的默认值、数据边界和限制同步更新；
6. 发布 report 固定到 commit 与 benchmark version。

---

## 13. Action Items

### Wave 0：评测地基

- [x] 定义 manifest、query、answer key、evidence units、qrels、pool 和 report schema；
- [x] 实现 `validate_benchmark.py`；
- [ ] 抽出无导入副作用的 legacy extractor/chunker baseline seam；
- [ ] 实现生产路径隔离、report sanitizer 与测试；
- [ ] 建立 S5 五领域真实 PDF、reference notes 与 25 queries；
- [ ] 增加不调用 LLM 的单一 S5 offline benchmark CI job；
- [ ] 建立 D20 与 100 queries；
- [ ] 在 canonical IR 上运行 fixed-800/dense-only full baseline；
- [ ] 并行建设 V20 和 sealed H60。

### Wave 1：provenance

- [ ] 先完成 1A page/section/source-spans canonical IR 与 C0 adapter；
- [ ] `ChunkRecord` schema 与版本迁移；
- [ ] `IndexArtifact`、独立 collection/ledger 与 active-index manifest；
- [ ] MCP 返回 page/span provenance；
- [ ] doctor、side-by-side activation、rollback 与 sanitizer tests。

### Wave 2：chunking

- [ ] C0–C4 adapters；
- [ ] D20 chunking ablation；
- [ ] 冻结候选并跑 V20；
- [ ] 冻结候选或保留 baseline；此时不改变 repository 默认。

### Wave 3：retrieval

- [ ] 3A：FTS5 capability check、lexical baseline 与 RRF hybrid；
- [ ] 3B：ONNX reranker；
- [ ] 3C：notes/papers 独立 `EmbedderConfig` 与 provider ablation。

### Wave 4：Agent workflow

- [ ] 4A：retrieval trace 与 planner；
- [ ] 4B：RCS keep/drop；
- [ ] 4C：self-ask stop rules、answer/citation evaluator 与 cost report；
- [ ] 冻结整批 candidate，执行一次 H60 paired release run。

### Wave 5：可选增强

- [ ] 根据 D20/V20 failure slices 决定是否启动 contextual enrichment；
- [ ] 如需 claims layer，另立 ADR。

---

## 14. 参考依据

- BEIR：多领域 information retrieval benchmark 与 nDCG/Recall 评估
  <https://openreview.net/pdf?id=wCu6T5xFjeJ>
- RAGChecker：拆分 retriever 与 generator 的细粒度指标
  <https://arxiv.org/abs/2408.08067>
- RAGAS：faithfulness、context precision/recall 等 RAG 指标
  <https://aclanthology.org/2024.eacl-demo.16/>
- PaperQA2：科学文献检索、metadata-aware embedding 与 RCS
  <https://github.com/future-house/paper-qa>
- Anthropic Contextual Retrieval：contextual embedding、BM25 与 reranking 对照
  <https://www.anthropic.com/engineering/contextual-retrieval>
- 2026 chunking 系统分析：size、overlap、sentence/semantic 策略应通过任务实测
  <https://arxiv.org/abs/2601.14123>
- 2026 academic-text chunking study：复杂 semantic chunking 不保证优于简单策略
  <https://arxiv.org/abs/2607.01852>
- NIST/TREC relevance judgments：qrels 与 corpus 绑定，未判断文档不能等同于不相关
  <https://trec.nist.gov/data/reljudge_eng.html>
- NIST/TREC pooling：合并多系统候选池以降低多相关文档漏标
  <https://trec.nist.gov/howto.html>
- Creative Commons FAQ：再分发、文本数据挖掘与底层作品权利仍受具体许可约束
  <https://creativecommons.org/faq/>

---

## 15. Consequences

### 正向

- 后续检索升级可归因、可复现、可回滚；
- 多领域和 held-out 设计降低对催化语料及开发集过拟合；
- note generation、论文发现、段落检索和回答合成可以分别诊断；
- 开源项目能够发布真实 baseline，而不只展示架构和 toy demo；
- 复杂技术只有在证明收益后才进入默认路径。

### 代价

- 第 0 步不再承诺不现实的自然日；主要成本按 S5 实测吞吐换算为人工标注人时；
- 页码/span provenance 会触发 papers collection schema migration；
- S100 release benchmark 需要额外存储、模型下载与运行时间；
- 维护者必须同时版本化 corpus、qrels、模型与算法配置；
- 分阶段 PR 会降低短期“功能上线速度”，但避免不可证明的系统膨胀。
