# research-rag — 组件参考手册

逐组件速查表。每个组件给职责、关键行为、调用契约、已知问题、深读链接。

整体架构与数据流见 [ARCHITECTURE.md](ARCHITECTURE.md)。各组件的完整证据见 [investigation/](investigation/)。

---

## Generator 层

### 1. zotero_batch_scanner.py

**位置**：`scanner/zotero_batch_scanner.py`（401 行）

**职责**：读 Zotero SQLite、按 parent item 分组 PDF 附件、对账去重、fan-out 给 `gemini_analyze_pdf.py`。

**关键行为**：
- `shutil.copy2()` 复制 `zotero.sqlite` 到临时文件（避开 Zotero 锁；Zotero 必须关闭）
- SQL：`SELECT ... FROM itemAttachments JOIN items WHERE path LIKE '%.pdf'`
- 三种 Zotero 路径解析：`storage:` / `attachments:` / 绝对路径
- 自动检测 `baseAttachmentPath`（从 `%APPDATA%\Zotero\Profiles\*\prefs.js` 正则提取；存在 race）
- 两层去重：`processed_history.txt` set + live-vault frontmatter JSON 索引
- Fan-out：≤5 项串行；>5 项 `ThreadPoolExecutor(3)`
- 重试：3 次，429/quota 退避 30s，其它 10s。`NON_RETRYABLE_ERROR[<code>]` 立即终止

**CLI**：`--zotero-dir / --base-dir / --out-dir / --limit N / --since YYYY-MM-DD / --api-keys / --force / --model / --flash-model / --pro-model`

**硬编码**：默认 `--zotero-dir` = `$ZOTERO_DATA_DIR`；默认 `--base-dir` = `$ZOTERO_ATTACHMENT_BASE_DIR`；`APPROVED_MAIN_PYTHON` 是 `Python311\python.exe` 字面量

详见 [investigation/01-note-generation-pipeline.md](investigation/01-note-generation-pipeline.md) §1-§2。

---

### 2. gemini_analyze_pdf.py

**位置**：`scanner/gemini_analyze_pdf.py`（~1900 行）

**职责**：单组编排器——预飞 → GCS 上传 → 两阶段 Gemini → schema 验证 → frontmatter 渲染 → 落盘 → ledger 追加。

**Pipeline**（multifacet-spec 模式，默认）：
1. **预飞**（pypdf）：页数、文件大小；`split_pdf_for_vertex()` 处理 >50MB 或 >1000 页；`PDFPreflightError` for `missing_pdf` / `corrupt_pdf` / `oversize_pdf`
2. **GCS 上传**：`gs://<bucket>/pdf-inputs/<combined_hash>/<idx:02d>_<safename>.pdf`
3. **Stage A — Document Profiler**（永远 `gemini-2.5-flash`）：
   - System: `prompts/document_profiler.system.txt`
   - Schema: `schemas/document_profile.vertex.schema.json`
   - `temperature=0.0`, `response_mime_type="application/json"`
   - 输出存到 `runs/<hash>/01-document-profile.json`
4. **路由决策**（`config/model_routing_policy.json`）：
   - 默认 flash；以下任一触发 pro：`primary_pdf_pages>=30` / `total_pdf_pages>=60` / `document_type ∈ {textbook, phd-dissertation, review, perspective, commentary}` / `is_review_like=true` / `is_multichapter_thesis=true`
5. **Stage B — Note Generator**（flash 或 pro）：
   - System: `prompts/note_generator.system.txt`
   - Schema: `schemas/structured_note.vertex.schema.json`
   - User prompt 嵌入 Document Profile JSON + 模板 ID + 拼接的 `template_rules/<id>.txt + _shared_rules.txt`
6. **Schema 校验**：`_validate_against_schema()` 递归校验；`_sanitize_seed_terms()` 用 `title_en/title_zh/keywords/topic` 反向锚定，防止 hallucinated seed terms 漏掉
7. **渲染**：`build_multifacet_frontmatter()`（line 764）写固定字段顺序的 YAML；注入 Zotero `abstractNote` 作为 `## 英文摘要原文` 段
8. **落盘**：canary 路径或 live vault；按 body 里"推荐保存文件名"行规范化，加 `_review_note.md` 后缀
9. **Ledger**：`combined_hash` hex 追加到 `processed_history.txt`
10. **Post-publish**（可选）：`prefill_candidate_tags.py` / `run_tagging_pipeline.ps1`（Kimi 兜底）/ `export_review_queue.py` / `restart_query`

**Frontmatter 字段**（固定写出顺序，line 764+）：
- 身份：`title_en` / `title_zh` / `authors` / `year` / `journal` / `doi`
- 索引键：`keywords[5..10]` / `topic[1..8]`
- 路由：`research_domain`（8 enum）/ `document_type`（10 enum）/ `note_template`（7 enum）/ `seed_terms` / `scope_hint`（core|other|needs-body-evidence）/ `signal_quality` / `routing_confidence`
- 溯源：`combined_hash` / `pdf_N_name` / `pdf_N_path` / `zotero_parent_key`
- 打标壳：`tags=[]` / `candidate_tags_high=[]` / `candidate_tags_medium=[]` / `candidate_tags_low=[]` / `human_reviewed=0`

**Body 段**（electrocatalysis-experimental 模板；其它模板段位不同）：
1. 文献基本信息 / 2. 英文摘要原文 / 3. 客观摘要 / 4. 研究问题 / 5. 体系信息 / 6. 核心测试条件与定量方法 / 7. 核心性能指标 / 8. 逐图证据路径总结 / 9. 关键主张-证据路径图 / 10. 深度机理视角提炼 / 11. 方法亮点与审稿人陷阱扫描 / 12. 主观打分 / 13. 核心结论总结

**combined_hash 算法**（stable variant）：

```python
file_hashes = sorted([sha256(f.read_bytes()) for f in group])
combined_hash = sha256(b"\n".join(h.hex().encode() for h in file_hashes))
```

主文/SI 顺序无关。**注意**：还有一个 legacy 变体按路径排序而非 hash 排序，`find_processed_hash_match()` 接受双变体；但 `verify_and_clean.py` 只实现了 stable，老笔记可能被误判为 orphan。

**认证**：Vertex AI 服务账号。**不是 `GEMINI_API_KEY`**（虽然 SKILL.md 这么写）。需要的环境变量：
- `GOOGLE_APPLICATION_CREDENTIALS` = 服务账号 JSON 路径
- `GOOGLE_CLOUD_PROJECT`
- `GOOGLE_CLOUD_LOCATION`
- `GEMINI_VERTEX_GCS_BUCKET`

**已知问题**：
- `candidate_tagger.system.txt` 与对应 schema 存在但**主流水线未调用**——是 post-publish 的 prefill 步骤独立用的
- `get_parent_key()` 用 `WHERE ia.path LIKE '%<filename>%' LIMIT 1`——文件名子串匹配，重名会撞车（潜在 bug）
- GCS 对象只增不减；bucket 名带 "temp" 但实际是永久归档
- Lines 1638-1758 的 `DEFAULT_PROMPT` 是 legacy 模式残留死代码

详见 [investigation/01-note-generation-pipeline.md](investigation/01-note-generation-pipeline.md)。

---

### 3. ingest_textbook.py / verify_and_clean.py / backfill_hash.py

**ingest_textbook.py**（~5KB）：CLI-only，跟 `build_pdf_db.py` 同样切块逻辑，但单文件、强制 `--zotero-key`、批量 50 条 chunk 写入避超时、`paper_group=-1` 哨兵、`textbook_ledger.txt`（单文件 SHA-256）。

**verify_and_clean.py**：从每篇笔记的 `pdf_N_path` 重算 `combined_hash`，对账 `processed_history.txt`，识别 ghost（ledger 有笔记没/PDF 没）和 orphan（笔记有 ledger 没）。`--clean` 重写 ledger 留有效项，时间戳备份。

**backfill_hash.py**：给 2026-03-14 之前的老笔记补写 frontmatter 里的 `combined_hash` 字段（不调 API，纯本地）。

详见 [investigation/01-note-generation-pipeline.md](investigation/01-note-generation-pipeline.md) §2。

---

## Service / Indexer 层

### 4. build_notes_db.py

**位置**：`$LOCALRAG_HOME\build_notes_db.py`（5.7KB）

**职责**：扫 `$LOCALRAG_NOTES_DIR\*_review_note.md`，整篇嵌入 ChromaDB collection `notes`。

**关键行为**：
- Collection: `notes`，`metadata={"hnsw:space": "cosine"}`（line 114）；**未绑定 embedding_function**
- ID: `MD5(filename)`（line 154）
- 文档内容：包含 frontmatter 的全文
- 写入：`col.upsert`——文件名相同则覆盖
- 嵌入：直接 `urllib.request` POST 到 `http://localhost:11434/api/embeddings`，`qwen3-embedding:4b`，文本截断到 `MAX_EMBED_CHARS=12000`，60s 超时，无重试
- 启动预飞 `get_embedding("test")`；失败 `sys.exit(1)`
- Ledger: `processed_notes.txt`，每行一个文件名（不跟踪内容 hash）

**Metadata 键**（line 140-151）：`source_file` / `zotero_parent_key`（必需，缺则跳过）/ `year` / `journal` / `title_en` / `title_zh` / `doi` / `authors`（list 拼成串）

**怪癖**：
- 改笔记正文但不改文件名 → 不会重嵌入（filename-based ledger）
- 缺 `zotero_parent_key` 的笔记被跳过但仍写 ledger 防止反复警告

详见 [investigation/02-build-and-index.md](investigation/02-build-and-index.md) §2、§4、§5。

---

### 5. build_pdf_db.py

**位置**：`$LOCALRAG_HOME\build_pdf_db.py`（9.4KB）

**职责**：通过 stub-note frontmatter 发现 PDF，切块、嵌入、写到 ChromaDB collection `papers`。

**发现机制**（`extract_pdf_groups_from_notes`，line 59-88）：
1. 走 `$LOCALRAG_NOTES_DIR\*.md`，解析 YAML frontmatter
2. 收集 `pdf_0_path` / `pdf_1_path` / ... 字段，组成 group
3. `os.path.exists` 不通过则 `[WARN]` 跳过；零有效路径的 group 整体丢弃

**Group 哈希**：`get_combined_hash()`（line 106-114）按 group 列表**声明顺序**hash（**非排序**——这跟 scanner 的 stable hash 不同！）；ledger `processed_groups.txt`（当前 1121 条）。

**抽取**（`extract_text_pdfplumber`，line 116-130）：
- pdfplumber 逐页 join with `\n`
- `REF_PATTERN` 正则查最后一次 `References` / `Bibliography` / `参考文献` / `Acknowledgements` 出现，从此截断
- `re.DOTALL | re.IGNORECASE`

**切块**（`chunk_text`，line 132-139）：
- `CHUNK_SIZE=800` / `CHUNK_STEP=700` → overlap 100
- 丢弃 <100 字的尾部 chunk
- 无句/段感知；无 page metadata

**嵌入**：ChromaDB `OllamaEmbeddingFunction(model_name="qwen3-embedding:4b", url=...)` 绑定到 collection；批量/重试逻辑由 ChromaDB 内部处理（不可见）。

**Schema**（每 chunk）：
- ID: `"group_{group_idx}_file_{file_idx}_chunk_{k}"`（确定性；ledger 清空后重跑会撞 ID）
- Metadata: `pdf_path` / `pdf_filename` / `paper_group`（1-based）/ `file_index`（0=主文，1+=SI）/ `chunk_index` / `is_main` / `is_si` / `group_hash`（前 16 字符 hex）/ `zotero_parent_key`（从 Zotero SQLite 查 filename，或 `""`）

**怪癖**：
- PDF 内容变化 → 旧 chunk 不删（无 `delete_where` before add）
- 默认 L2 距离（**未显式设 cosine**），跟 `notes` 不一致
- `paper_group` 是 1-based；`ingest_textbook.py` 用 `-1`

详见 [investigation/02-build-and-index.md](investigation/02-build-and-index.md) §3-§5。

---

### 6. query_server.py（核心服务）

**位置**：`$LOCALRAG_HOME\query_server.py`（~1128 行，Flask）

**Bind**：`127.0.0.1:18810`（line 1125）。单线程 Werkzeug。无监督进程。

**启动**：连 `CHROMA_PATH = $LOCALRAG_HOME\chroma`；给 `papers` 绑 `OllamaEmbeddingFunction`，`notes` 不绑（用 manual embedding）。任一 collection 加载失败只 warn，置 `*_ready=False`，对应端点返回 503。`LOCALRAG_SKIP_CHROMA_INIT=1` 跳过启动初始化（测试用）。

**端点**：

| 方法 | 路由 | 用途 |
|---|---|---|
| GET | `/health` | 存活探针；live-ping Ollama（5s 超时）；都就绪返 200，否则 503 |
| POST | `/search_notes` | `notes` 向量检索。Body: `{query, n=5, dedupe=true（忽略）, zotero_parent_key?}` |
| POST | `/get_note` | 元数据 fetch（**非向量**）。Body: `{source? OR zotero_parent_key?, summary_only=false}` |
| POST | `/search_papers` | `papers` 向量检索。Body: `{query, n=3, zotero_parent_key?, paper_group?, pdf_filename?, second_query?, include_context=false}` |
| POST | `/write_query_log` | 仅追加的 Markdown 日志，带幂等。Required: `workflow_id, workflow_name, status, query, anchor_query, final_response_snapshot, idempotency_key, planned_angles[], executed_angles[], search_runs[]` |
| POST | `/append_query_log_action` | 在已有日志的 `## Follow-up Actions` 下追加 `### {timestamp}`。Required: `log_path, log_id, action, result` |

**`/search_notes` 流**：

```
get_embedding(query)    # urllib POST，60s，截 12000 字
↓
notes_col.query(query_embeddings=[vec], n_results=n, where={zotero_parent_key:k}?)
↓
解析每个返回 doc 的 YAML frontmatter，补 metadata 缺失字段
↓
content 截断 3000 字
↓
返回 {score=1-cosine_distance, note_rank=1-based}
```

**`/search_papers` 流**：

```
effective_query = second_query or query
↓
pdf_col.query(query_texts=[effective_query], n_results=n, where=...)
                # ChromaDB 内部用绑定的 ef 嵌入
↓
若 include_context=true:
  构造邻接 chunk_id "group_{pg}_file_{fi}_chunk_{ci±1}"
  pdf_col.get(ids=[...]) 拼 prev + "[MATCH]content[/MATCH]" + next
↓
返回 {distance（原始，非 1-d）, metadata}
```

**filter 优先级**：`zotero_parent_key > paper_group > pdf_filename`（先非 null 胜出，只用一个）。`zotero_parent_key` 一次同时盖主文+SI。

**`second_query` 语义**（WF4）：原始中文 query 回显在响应里供日志用；嵌入用 second_query（从笔记里**已存在的**英文术语提取）。这种非对称是有意为之——见 ARCHITECTURE 的"跨语种两遍检索"段。

**幂等**：`/write_query_log` 写之前查 `_query_logs/_registry.json`；`idempotency_key` 命中且文件还在 → `{"deduplicated": true}` 不写。

**硬编码**（已全部 env 化，详见 `service/config.py`）：
- `CHROMA_PATH`、`COLLECTION_NAME=papers`、`NOTES_COLLECTION_NAME=notes`
- `QUERY_LOG_ROOT`、`QUERY_LOG_SCHEMA_VERSION=1`
- `HOST=127.0.0.1`、`PORT=18810`

**已知问题**：
- 单线程；任何上游卡住（Ollama）整服务排队
- `include_context` 假设 chunk-id scheme 跟 `build_pdf_db.py` 一致；textbook 路径用 `textbook_{hash}_chunk_{k}` 不同，无声 fallback

详见 [investigation/03-query-server.md](investigation/03-query-server.md) §2-§5、§9-§10。

---

### 7. 查询日志（在 query_server.py 内部）

**根目录**：`$LOCALRAG_QUERY_LOG_ROOT`（默认 `$LOCALRAG_NOTES_DIR/_query_logs/`）

**月分子目录**：`{YYYY-MM}/` 如 `2026-05/`

**文件名**：`{YYYYMMDD-HHMMSS}_{workflow_id}_{query-slug}_{SHORT_ID}.md`

`query-slug` 由 `slugify_query_title()` 生成；`SHORT_ID` 是 4 字符大写 hex。

**`log_id`** 格式：`ql-{YYYYMMDD-HHMMSS}-{SHORT_ID}`

**Frontmatter 字段**（~28 个）：`schema_version` / `log_id` / `idempotency_key` / `created_at` / `month` / `workflow_id` / `workflow_name` / `status` / `query` / `query_title` / `session_summary_title` / `query_language` / `anchor_query` / `anchor_query_source` / `saved_by` / `search_runs`（计数）/ `search_run_details`（数组）/ `planned_angles` / `executed_angles` / `expansion_reason` / `stop_reason` / `notes_hits` / `papers_hits` / `zotero_parent_keys` / `source_note_files` / `effective_queries` / `second_queries` / `log_path`

**Body 段**（固定）：
```
# Query Record
## User Query
## Workflow Decision
## Search Plan
## Search Runs (### Run 1, ### Run 2, ...)
## Result Summary
## Notes Hits (### N1, N2, ...)
## Paper Hits (### P1, P2, ...)
## Final Response Snapshot
## Follow-up Actions (### {timestamp} 后续追加)
```

**幂等**：`idempotency_key` 在 `_registry.json` 里有 + 文件还在磁盘 → 不写、返 `{"deduplicated": true}`。

详见 [investigation/03-query-server.md](investigation/03-query-server.md) §5。

---

## Skills 层

### 9. 10 个 skill 的分布

| Skill | 类型 | 目录（canonical） | 支持文件 |
|---|---|---|---|
| `search-literature` | Tier 1 编排 | `.claude\skills\search-literature\` | SKILL.md only |
| `search-notes` | Tier 1 叶 | `.claude\skills\search-notes\` | SKILL.md only |
| `search-papers` | Tier 1 叶 | `.claude\skills\search-papers\` | SKILL.md only |
| `gemini-literature-processor` | Tier 2 写入 | `.agents\skills\gemini-literature-processor\` | scripts/, prompts/, schemas/, template_rules/, references/, processed_history.txt |
| `literature-tagging-pipeline` | Tier 2 写入 | `.claude\skills\` (SKILL) + `.agents\` (scripts) | watch_tagging_pipeline.ps1 |
| `rag-engineer` | Tier 3 元 | `.claude\skills\rag-engineer\` | SKILL.md only |
| `vector-database-engineer` | Tier 3 元 | `.claude\skills\vector-database-engineer\` | SKILL.md only |
| `embedding-strategies` | Tier 3 元 | `.claude\skills\embedding-strategies\` | SKILL.md only |

`gemini-literature-processor` 在 `.openclaw\` 和 `.cc-switch\` 有镜像；`.agents\` 是 canonical，其它是给别的 agent runner 兼容的副本。

### 10. search-literature 工作流协议（系统的 UX 头牌）

5 步状态机：

**Step 0 — 角度规划**（每次检索强制）
- `anchor angle`（强制）：忠实保留用户原问题语义。中文 → 英文翻译，但不替换为 exploratory。
- `exploratory angle`（≥1）：从类型词典选——`core_concept` / `mechanism` / `evidence` / `contrast` / `method` / `timeline` / `paper_specific`。
- 默认探针 = anchor + 1 个 exploratory；试探后视情况扩到 3-5 个。

**Step 1 — 意图识别 + 用户确认**（强制 gate）
- 内部按 6 维度分类：深度 / 范围 / 数据源 / 特殊需求 / 初始 angle / 扩展条件
- 必须先告知用户："我打算用 WF{N}·{名}……要继续吗？" 再执行
- 用户拒绝 → 列出全部 10 个 WF 选

**Step 2 — WF 选择**

| WF | 名称 | 主调用 | 关键参数 |
|---|---|---|---|
| WF1a | 快速多篇检索 | `POST /search_notes` | `dedupe: true, n: 5` |
| WF1b | 单篇多角度检索 | `POST /search_notes` | `zotero_parent_key`, `dedupe: false` |
| WF2 | 指定论文笔记 | `POST /search_notes` | `zotero_parent_key, n: 3` |
| WF3 | 纯原文检索 | `POST /search_papers` | 必须英文 query |
| WF4 | 笔记→原文联动 | `/search_notes` → `/search_papers` | `second_query` 取自笔记里已有的英文术语 |
| WF5 | 原文→笔记反向 | `/search_papers` → `/search_notes` | 从原文结果取 `zotero_parent_key` |
| WF6 | 横向对比多篇 | `POST /search_notes` | `n: 10, dedupe: true`，按 year/journal 分组 |
| WF7 | 完整精读单篇 | `/search_notes` → `/get_note` → `/search_papers` | `summary_only: false` |
| WF8 | 时间线检索 | `POST /search_notes` | `n: 10`，按 `metadata.year` 升序 |
| WF9 | 实验方法检索 | `POST /search_papers` 优先 | 优先展示 `is_si: true` |
| WF10 | 矛盾检测 | `POST /search_notes` | `n: 10, dedupe: true`；LLM 主动识别分歧 |

**Step 3 — 输出格式**：固定模板（📓 笔记、📄 原文）+ 强制结尾（展开/深挖菜单）

**Step 4 — 查询日志**（每次主流程结束强制）：
- `POST /write_query_log`，带 `idempotency_key` / `anchor_query` / `planned_angles` / `executed_angles` / `expansion_reason` / `stop_reason` / `search_runs[]`
- 后续动作（展开、深挖）→ `POST /append_query_log_action` 追加到同一文件
- **一个用户研究问题 = 一份日志文件**

详见 [investigation/04-skills-layer.md](investigation/04-skills-layer.md) §3。

### 11. gemini-literature-processor 协议

**前置**（必问）：
1. Zotero 是否已关闭？
2. 处理范围：A 全量 / B `--limit N` 测试 / C `--since YYYY-MM-DD --limit N` 增量 / D 用户给路径的单篇

**Python**（通过 env 变量配置；defaults to `python3` on PATH）：
- `$LOCALRAG_MAIN_PYTHON`（Gemini / GCS / Zotero scanner）
- `$LOCALRAG_RAG_PYTHON`（ChromaDB build / query server）

**自动入库**（生成完毕后无需再问用户）：
1. 确保 Ollama 在跑
2. `build_notes_db.py`
3. `build_pdf_db.py`
4. 重启 query_server.py

**报告**：处理数 / 跳过数 / 笔记总数 / PDF chunk 总数 / 失败明细。

详见 [investigation/04-skills-layer.md](investigation/04-skills-layer.md) §5。

### 12. literature-tagging-pipeline

**关系**：`gemini-literature-processor` 生成笔记带 `tags=[]` 和 `candidate_tags_high/medium/low`（机器候选）。`literature-tagging-pipeline` 跑 Kimi 把最终 tags 填上、管理 review 生命周期。两个 skill 是**串行关系**：generate → tag → human review。

**状态文件**：
- `$LOCALRAG_NOTES_DIR\progress\tagging_state.jsonl` — 权威状态
- `$LOCALRAG_NOTES_DIR\status\current-status.md` / `task-log.md`
- `$LOCALRAG_NOTES_DIR\wiki\taxonomy.yaml` / `tag_aliases.yaml` / `material_hierarchy.yaml` — 标签本体
- `$LOCALRAG_NOTES_DIR\wiki\tagging_prompt.md` — Kimi prompt
- Gate reports / pipeline reports

**安全规则**：禁止并行跑同 vault；禁止手改 progress/* 假装 gate 通过；新 tag 必须先标 `CANDIDATE_NEEDED`。

---

## 关键不一致（需在开源前修）

| # | 议题 | 解法 |
|---|---|---|
| 1 | 端口 18800（search-notes/papers SKILL）vs 18810（server + search-literature SKILL） | 统一到 18810；改两个叶 SKILL |
| 2 | SKILL 说 `GEMINI_API_KEY` + 多 key 轮换；代码用 Vertex AI 服务账号 | 改 SKILL.md 反映 Vertex 路径 |
| 3 | `dedupe` 在 `/search_notes` 是静默 no-op | 文档化；考虑废弃 |
| 4 | `combined_hash` 在 scanner（按 hash 排）和 indexer（按路径排）算法不同 | 选一个；旧数据接受双变体或 backfill |
| 5 | `candidate_tagger.*` 存在但主流水线未调用 | 文档化为独立 prefill；或接入 |
| 6 | GCS bucket 名带 "temp" 但永不清 | 调度 `cleanup_gcs_archive.py` 或重命名 |
| 7 | `query_server_v2.py` 死代码（用 `.openclaw` SQLite，无 Vertex/Chroma） | 删 |
| 8 | `check_db*.py`（4 版本）目标是另一套 SQLite 系统 | 从此仓库剔除 |
| 9 | `wave8_gold_ledger.txt` 描述独立的 `wave8_gold_claims_v1` collection（在 `chroma_wave8_gold/`） | 不在范围；归档别处 |

详见各 investigation 报告 §10 段。
