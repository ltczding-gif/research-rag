# research-rag — 系统架构

> 一个本地优先、Zotero 原生、由 Claude Code 驱动的研究文献 RAG 系统。

## 30 秒电梯版

读 Zotero SQLite → 通过可插拔 LLM 后端（Vertex AI Gemini / 直连 Gemini API / Anthropic Claude / OpenAI 兼容协议 / Claude Code sub-agent）给每篇 PDF 生成结构化中文研究笔记 → 笔记和 PDF 原文都通过本地 Ollama 嵌入到 ChromaDB → 在 Claude Code 里通过 10 个具名工作流（WF1a..WF10）做语义检索。

笔记是 Markdown 文件、ChromaDB 是本地、Ollama 是本地，唯一离开你网络的是 Gemini 调用。

## 三层结构

```
┌─ Skills 层（Claude Code skills，纯 Markdown） ──────────────┐
│                                                            │
│  search-literature ─┬─ search-notes（叶子）                 │
│   (WF1a..WF10)      └─ search-papers（叶子）                │
│                                                            │
│  gemini-literature-processor （扫描器驱动）                  │
│  literature-tagging-pipeline  （Kimi 后处理打标）            │
│                                                            │
│  rag-engineer / vector-database-engineer /                 │
│  embedding-strategies  （基础设施参考，非编排器）             │
└─────────────────────────────┬──────────────────────────────┘
                              │  HTTP localhost:18810
                              ▼
┌─ Service 层（常驻 Flask 边车） ─────────────────────────────┐
│                                                            │
│  query_server.py  ─→  ChromaDB（持久化，两个 collection）    │
│   /search_notes   ──── notes（整篇入库 / cosine / MD5 ID）  │
│   /search_papers  ──── papers（chunk / 默认L2 / 确定性ID）  │
│   /get_note                                                │
│   /write_query_log + /append_query_log_action              │
│                                                            │
│  嵌入：Ollama  qwen3-embedding:0.6b :11434  (默认；可换)   │
└─────────────────────────────▲──────────────────────────────┘
                              │  build_notes_db.py + build_pdf_db.py
                              │  （新增笔记后必须重跑）
                              │
┌─ Generator 层（按需运行） ──────────────────────────────────┐
│                                                            │
│  zotero_batch_scanner.py  （读 zotero.sqlite，去重）         │
│      └── fan-out 子进程                                     │
│  gemini_analyze_pdf.py    （每个 PDF 组的编排器）             │
│      ├── Stage A: Document Profiler （flash tier）         │
│      └── Stage B: Note Generator    （flash 或 pro）        │
│                                                            │
│  backends/                                                 │
│      vertex.py        — Vertex AI + GCS（默认）             │
│      gemini_api.py    — 直连 Gemini Developer API           │
│      anthropic_api.py — Anthropic Claude（tool-use schema） │
│      openai_api.py    — OpenAI 兼容协议（PDF 文本提取）      │
│      subagent.py      — 写 manifest，让 Claude Code 子代理跑 │
│  通过 $LOCALRAG_PROCESSOR_BACKEND / --backend 切换          │
│                                                            │
│  ingest_textbook.py  （大书，分批入库；属 Service 层但放这一起讨论） │
│  verify_and_clean.py / backfill_hash.py / cleanup_gcs.py   │
└────────────────────────────────────────────────────────────┘
```

## 读路径（检索）

```
用户在 Claude Code 里发问
   ↓
search-literature: Step 0 角度规划
   ↓ "我打算用 WF{N}，2 个角度试探，要继续吗？"
用户确认
   ↓
HTTP POST → query_server :18810
   ↓
{notes,papers}_col.query(query_embeddings=[Ollama 嵌入向量])
   ↓
Top-N 结果 → 按 WF 模板装配输出
   ↓
search-literature: Step 5 → POST /write_query_log
```

## 写路径（笔记生成）

```
Zotero SQLite（先关 Zotero）
   ↓ shutil.copy2 到临时文件
zotero_batch_scanner.py 按 parent item 分组附件
   ↓ 解析 storage:/attachments:/绝对路径三种 schemes
combined_hash = SHA256(sorted([SHA256(file_bytes) for f in group]))
   ↓ 命中 processed_history.txt 或 live-vault 索引则跳过
gemini_analyze_pdf.py（每组一个子进程）
   ↓ pypdf 预飞 → >50MB 或 >1000 页则切片
上传到 GCS gs://<project>-gemini-literature-temp/pdf-inputs/<hash>/
   ↓
Stage A — Document Profiler（flash tier 模型，由所选后端实现）
   ↓ document_type, recommended_template, routing_confidence
路由决策（model_routing_policy.json）
   ↓ <30 页主文用 flash；textbook/dissertation/review 用 pro
Stage B — Note Generator（Flash 或 Pro）
   ↓ 结构化 JSON：frontmatter dict + body markdown
渲染 frontmatter（build_multifacet_frontmatter）
   ↓ 注入 Zotero abstract、combined_hash、pdf_N_path、candidate_tags=[]
写到 $LOCALRAG_NOTES_DIR\<year>-<journal>-..._review_note.md
   ↓ append combined_hash 到 processed_history.txt
可选 post-publish：prefill 候选 tags、kimi 兜底打标、重启 query_server
```

## 关键设计选择

### 一个 collection 一个轴向，差异是有意为之

两个 ChromaDB collection 共用同一个持久化目录但走相反规则：

| 维度 | `notes` | `papers` |
|---|---|---|
| 粒度 | 整篇文档 | 800 字符 chunk（步长 700） |
| 距离 | cosine | 默认 L2 |
| ID | `MD5(filename)` | `group_{g}_file_{f}_chunk_{k}`（确定性） |
| 写入 | `upsert`（重跑幂等） | `add`（重复 ID 报错） |
| 嵌入 | 每次查询手动 `urllib` POST | ChromaDB 绑定的 `OllamaEmbeddingFunction` |
| 返回 | 每条前 3000 字 | 单 chunk + 可选 ±1 邻接 |

`/search_notes` 上的 `dedupe=true` **被静默忽略**——因为笔记本来就一篇一文档。这是历史接口契约，向后兼容。

### Stub-note 发现机制

`build_pdf_db.py` 不爬 Zotero。它扫 `$LOCALRAG_NOTES_DIR\*.md`，从每个 frontmatter 里读 `pdf_0_path`/`pdf_1_path` 字段去发现 PDF。Gemini 生成的笔记里都已经有这些字段，PDF 索引就自动跟着笔记库走——不需要单独的 manifest，不需要 Zotero 侧目录。

"Stub note" = 只有 frontmatter、没有正文的占位 Markdown，用来把一个 PDF 拖进索引而不必先跑 Gemini。

### 跨语种两遍检索（WF4）

笔记是中文、PDF 是英文。WF4 先打 `/search_notes` 拿中文结论，从笔记里**已存在的**英文术语提取出 second_query，再打 `/search_papers` 用这个英文词去英文语料里精确定位。原始中文 query 仍然回显在响应里供日志用，但向量检索只用 second_query。

关键点：second_query 必须从笔记里**已有的**英文专名提取，不是 LLM 翻译——这避免了术语漂移。

### 结构化日志是强制的

每个 workflow 结尾必须 `POST /write_query_log`，带 `idempotency_key`、`anchor_query`、`planned_angles`、`executed_angles`、`search_runs[]`。后续动作（展开上下文、深挖某篇）必须 `POST /append_query_log_action` 追加到同一条日志。**一个研究问题 = 一份日志文件**，存在 `$LOCALRAG_NOTES_DIR/_query_logs/YYYY-MM/`。

### 笔记生成的两层去重

`combined_hash` = "对每个文件取 SHA-256，把这些 hash 排序后再 SHA-256 一遍"。排序顺序无关——主文和 SI 谁先谁后无所谓。检查两个地方：
1. `processed_history.txt` 平面 ledger（当前 1183 条）
2. Live-vault frontmatter 索引（捕获盘上有笔记但 ledger 没记的情况）

`verify_and_clean.py` 负责对账。

### LLM 调用是两阶段流水线

不是一次大 prompt 解决一切：

| 阶段 | 模型 | 任务 | 输出 |
|---|---|---|---|
| A. Document Profiler | 永远 flash tier | 分类论文类型、决定模板 | `recommended_template`、`document_type`、`is_review_like` 等 |
| B. Note Generator | flash 或 pro tier | 写笔记本体 | `frontmatter` 对象 + `body_markdown` 串 |

"flash" 和 "pro" 是抽象 tier 名（由 `model_routing_policy.json` 配置，每个后端把它们翻译成自家模型——Gemini 后端用 `gemini-2.5-flash` / `gemini-2.5-pro`，Anthropic 后端用 `claude-haiku-4-5` / `claude-sonnet-4-6`，可被 env 覆盖）。

Stage A 的输出直接喂给 Stage B 作为 user prompt 的一部分，并决定调用 flash 还是 pro。这把"分类"和"写作"解耦——同一个 Document Profiler 做所有论文，结构稳定；Note Generator 按 7 个模板（electrocatalysis-experimental / thermocatalysis-experimental / review-or-perspective / phd-dissertation / methods-or-materials-synthesis / foundational-theory / generic-research-note）有不同 body 结构，但共用 `_shared_rules.txt` 约束。

### 后端可插拔

Stage A 和 Stage B 都通过一个 `ProcessorBackend` 接口走，每个后端负责自己的 PDF 传输 + 模型调用：

| 后端 | PDF 传输 | 模型调用 | 结构化输出 |
|---|---|---|---|
| `vertex` | 上传到 GCS bucket，传 gs:// URI | `genai.Client(vertexai=True)` | Vertex 原生 `response_schema` |
| `gemini-api` | inline `Part.from_bytes` | `genai.Client(api_key=...)` | 同上 |
| `anthropic` | base64 encode 成 `document` content block | `anthropic.messages.create` | tool-use（schema 当 tool input_schema，强制 tool_choice） |
| `openai` | 本地 pdfplumber 抽文，文本拼进 user message | `openai.chat.completions.create`（任何 OpenAI 兼容 API） | tool-calling（schema 当 function parameters，强制 tool_choice），失败则回退解析 message.content 里的 JSON |
| `subagent` | 仅记录路径 | （不调用）写 manifest 后 raise sentinel 异常 | 由 sub-agent 在 Claude Code 里完成 |

prompt、schema、template_rules 全部共享——只是模型调用层切换。这意味着切换后端不会改变笔记结构。

**注意**：`openai` 后端因为只能传文本，会丢失图表/示意图——其他后端都能让模型直接看到 PDF 视觉内容。对图表敏感的笔记段（"逐图证据路径"等）质量会下降。Tradeoff：换来对所有 OpenAI 兼容服务的全覆盖（DeepSeek / Mistral / OpenRouter / 本地 vLLM / Ollama / LM Studio）。

## 文档↔代码漂移（历史快照）

> **历史记录** — 此表来自 2026-03 调查时点；自此 4 项均已对齐到代码侧。保留作为
> "为什么文档/代码漂移会发生"的可读证据，不是当前 bug 列表。

调查时发现 4 处文档跟代码不一致（**已修复**）：

| 议题 | 文档曾说 | 代码做的 | 现状 |
|---|---|---|---|
| Gemini 认证 | `GEMINI_API_KEY` + 多 key 轮换 | Vertex AI 服务账号；无轮换 | 文档已改 — 现在两个 backend 都列出，认证方式各自标清 |
| 查询端口 | `18800`（在 search-notes/papers SKILL 里） | `18810`（在 query_server.py 和 search-literature SKILL 里） | 全部统一到 `18810` |
| `dedupe` 参数 | `/search_notes` 接受 `dedupe: true/false` 有意义 | 静默忽略 | 已实装 |
| GCS bucket 名 | `*-gemini-literature-temp`（暗示临时） | 永不清理；累积无限 | 已加 `cleanup_gcs_archive.py` |

详见 `investigation/03-query-server.md` §10、`investigation/01-note-generation-pipeline.md` §10。

## 系统的"性格"

- **本地优先**：所有持久化都在你机器上。云端只在 Gemini 推理那一瞬间触达。
- **幂等**：扫描可以反复跑、查询可以反复打、日志带 idempotency_key——任何一步失败重跑都不会污染状态。
- **写读分离**：写路径（生成笔记）和读路径（语义检索）完全解耦，靠 `processed_*.txt` ledger 对接。生成挂掉不影响检索；检索挂掉不影响生成。
- **基于 frontmatter 的隐式 manifest**：没有 SQLite 元数据库做"哪些 PDF 该入库"的清单，直接靠扫描笔记 frontmatter 的 `pdf_N_path` 字段。这意味着笔记本身就是真理之源——删笔记 = 移除入索引意图。
- **一个用户问题一份 Markdown 日志**：所有跟随动作（展开、保存、深挖）追加到同一个文件。后续可以反向追溯一次研究的全过程。

## 详细资料

- 数据流和组件细节 → [COMPONENTS.md](COMPONENTS.md)
- 五份原始调查报告 → [investigation/](investigation/)
- 开源化工作清单 → [PACKAGING-PLAN.md](PACKAGING-PLAN.md)
