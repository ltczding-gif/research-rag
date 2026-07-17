# ADR-002: 检索质量的跳跃性升级路线（RAG v2）

**Status:** Proposed
**Date:** 2026-07-15
**Deciders:** 项目所有者
**前置:** ADR-001（终端优先架构）；`docs/audits/2026-07-15-kimi-adversarial-review.md`（速度审查）；2026-07-15 生态调研（PaperQA2 / kotaemon / RAG-Assistant-for-Zotero / paperetl / Ai2 ScholarQA 等 40+ 项目）

---

## Context

当前检索栈：800 字符 / 700 步长滑窗切块（相邻块 ~87% 重叠，存储与嵌入成本放大约 8 倍）、
notes 整篇嵌入 + papers 块嵌入双集合、纯向量单路召回、无重排、无评测集。
WF1a–WF10 工作流层质量高，但底层召回是纯向量近邻，精确 token 查询（分子式、
样品编号、作者名）和指代密集段落（"this catalyst / the sample"）是已知死角。

**本系统独有的结构性杠杆**：subagent 架构使「LLM 参与入库和查询」的边际成本为零——
宿主 agent 就是模型。教科书里最贵的高级 RAG 技术（LLM 切分标注、LLM 重排、
上下文富化）在这里不产生 API 账单。升级路线应围绕这个杠杆排序，而不是照抄
通用优先级。

## Decision

按以下顺序落地六步，每步独立可交付、可评测：

### 第 0 步 — 评测基建（先有尺子，硬前置）

- `tests/retrieval_bench/`：30–50 条真实查询（从 query log 抽取）+ 人工标注的
  预期命中（论文级 + 段落级），一个脚本输出 top-k recall / MRR。
- 规则：**此后任何检索改动，PR 里必须带评测前后对比。** 没有这一步，
  后面五步全部是不可证明的自我感动。
- 成本：约半天。

### 第 1 步 — RCS 查询侧重排（零代码，纯 skill 层）

- PaperQA2 的核心配方：检索取宽（top 20–30）→ 宿主 agent 对每个块写一句
  「查询条件化的相关性摘要 + keep/drop」→ 只用存活块合成。
- 落点：`skills/search-literature/SKILL.md` 的 WF4 / WF6 / WF7 增加 RCS 步骤
  说明；配套把这些 WF 的默认 `n` 调大。
- 成本：1–2 小时（纯文档）。预期是全部选项中 ROI 最高的一步。

### 第 2 步 — 结构感知切分替代 800/700 滑窗

- 现状的 87% 重叠是用 overlap 掩盖坏切分（rag-engineer 明确的 bad pattern）；
  paperetl 的教训：检索单元应是语义 section，不是字符窗口。
- 两档实现：
  - **穷人版（先做）**：pypdf outline + 标题正则获得章节树，按 section/段落边界
    切，图表 caption 与邻近正文绑定。零新依赖。
  - **可选升级档**：pymupdf4llm（注意 AGPL，只能做可选依赖不能进默认）或
    docling（重，文档质量天花板）。
- 收益：命中精度提升 + 存储/嵌入成本直接砍 ~8 倍（Kimi rr-service-speed
  会话的量化结论出来后回填此处）。
- 成本：1–2 天。需要重建 papers 集合（走 .env.example 里的 rebuild 流程）。

### 第 3 步 — 混合召回 + ONNX 重排

- FTS5（trigram tokenizer，中文可用）建关键词索引，BM25 与向量双路，RRF 融合
  （RAG-Assistant-for-Zotero 验证过的组合；也是 ADR-001 P2-14 的正式化）。
- 精排：fastembed 自带的 ONNX reranker（bge-reranker-v2-m3，多语 zh+en）把
  top-30 精排到 top-5 —— 延续零 daemon 原则。
- 附带收益：FTS5 路是「零模型检索地板」——嵌入模型没配好时系统仍可用。
- 成本：1–2 天。

### 第 4 步 — Contextual enrichment（「切分时先过一遍模型」的正确形态）

- Anthropic Contextual Retrieval：每个 chunk 入库前由 LLM 生成 50–100 token 的
  「此块在全文中的位置/指代说明」前缀再嵌入。对实验文献的指代密集段落
  （this catalyst / the sample / 条件承接）收益最大。
- **走 subagent 流水线**：入库富化做成与笔记生成同构的 manifest 任务
  （exit 200 → 宿主 agent 批量填充 → resume），零 API 费。这是本系统独有的
  免费午餐，其他栈做这步要为每个 chunk 付一次 LLM 调用。
- 成本：2–3 天（复用现有 manifest 基建）。

### 第 5 步 — Claims 结构化层（GraphRAG-lite）

- 入库时（同样走 subagent）把每篇笔记抽成 claims 表：
  `论文 → 论断 → 证据指针（chunk id / 页码）`，存 SQLite
  （与 Kimi 审查建议的 ledger 统一 SQLite 顺路合并）。
- WF6 横向对比 / WF8 时间线 / WF10 矛盾检测从「向量近邻碰运气」变成
  「结构化查询 + LLM 验证」。这是 WF 体系相对所有被调研竞品的护城河加深项。
- 成本：3–5 天。

### 第 6 步 — 迭代式检索正式化

- MCP 工具就位后，把 self-ask 循环（检索 → 读 → 改写查询 → 再检索）与
  「何时停/何时扩」写成 WF 规则；`include_context` 升级为独立的
  `expand_context(chunk_id, radius)` MCP 工具（deep-zotero 模式）。
- 成本：1 天（skill 文档 + 一个 MCP 工具）。

## Options Considered（不采纳项）

| 方案 | 不采纳理由 |
|---|---|
| ColBERT / late-interaction | 基建重（索引体积、专用服务），对 ~800 篇量级收益不成比例 |
| 嵌入模型微调 | 语料规模不足以稳定收益；先用第 3 步的重排器吃掉大部分差距 |
| 完整 GraphRAG（社区聚类） | 语料已有 Zotero 元数据 + 结构化 frontmatter，不需要从纯文本无中生有建图；claims 表是够用的轻量替代 |
| HyDE 独立实现 | WF 的 angle planning 已是多查询变换；RCS（第 1 步）+ 迭代检索（第 6 步）覆盖其收益 |

## Consequences

- **变容易**：每步独立可测（第 0 步保证）；第 1/6 步纯 skill 层，随时可回滚；
  第 4/5 步复用 subagent manifest 基建，不引入新的模型依赖。
- **变难**：第 2 步需要重建 papers 集合（一次性）；第 3 步引入 SQLite FTS 索引
  文件（新状态，需进 doctor 检查）；第 5 步的 claims 抽取质量需要抽检机制。
- **需要重访**：第 2 步穷人版对双栏 PDF 的效果如果不达标，再评估 AGPL/重依赖
  的升级档；rr-service-speed 的量化结论回填第 2 步收益估算。

## Action Items

1. [ ] `tests/retrieval_bench/`：查询集 + 标注 + recall/MRR 脚本（第 0 步）
2. [ ] WF4/6/7 增加 RCS 重排说明（第 1 步）
3. [ ] 结构切分穷人版 + papers 集合重建（第 2 步）
4. [ ] FTS5 混合召回 + RRF + ONNX 重排（第 3 步）
5. [ ] subagent 版 contextual enrichment 入库管线（第 4 步）
6. [ ] claims SQLite 层 + WF10 结构化改造（第 5 步）
7. [ ] self-ask WF 规则 + `expand_context` MCP 工具（第 6 步）
