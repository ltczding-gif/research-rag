# research-rag — 开源化路线图

> 从"个人 Windows 设置"到"公开的 Claude Code 插件"。

## TL;DR

- **建议命名**：`research-rag`（仓库 + 插件 slug）。
- **三包结构**：`service/`（HTTP 边车）+ `scanner/`（笔记生成器）+ `skills/`（Claude skills）。
- **多语**：英文 SKILL.md + 中文 SKILL.zh.md 双轨（计划中）。
- **跨平台**：service 和 scanner 本身跨平台；只有 SKILL.md 里 PowerShell 启动片段需要写 bash 等价。
- **飞书功能已整体删除**（详见下节），原 P0 凭据泄漏问题不再涉及本仓库。

---

## ✅ 飞书功能已删除（原 P0 安全问题随之解决）

旧版本里包含一个 `/write_to_feishu` 端点和配套的 OAuth + 周文档登记机制，原始
`.localrag/` 下的 30+ 个 Feishu 上传开发实验脚本里以明文出现飞书 app secret。

**本仓库的处理方式**：整段 Feishu 功能从代码、SKILL、配置、文档里全部删除：

- `service/query_server.py` 中所有 Feishu 辅助函数（`get_feishu_token`、`get_current_week_doc`、`register_week_doc`、`build_search_result_blocks`）和 `/write_to_feishu` 路由 — 删
- `service/config.py` 中 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_FOLDER_TOKEN` / `FEISHU_WORKSPACE` / `FEISHU_DOCS_REGISTRY` — 删
- `.env.example` Feishu 段 — 删
- `.gitignore` `feishu_docs.json` — 删
- `skills/search-literature/SKILL.md` 中 Step 4 飞书写入 + 结尾 "保存到飞书" 提示 + 端点表 `/write_to_feishu` — 删
- `skills/search-papers/SKILL.md` 中 `/write_to_feishu` 端点 + 飞书云文档段 — 删
- 30+ 个 Feishu 实验脚本从一开始就没复制到本仓库

**仓库外仍需关注**（独立于本仓库）：

如果原始 `$LOCALRAG_HOME\` 下那些脚本曾经被 commit 到任何远程 git 仓库，
飞书 app secret 仍可能存在于历史中，应该：

1. 在飞书开放平台后台轮换 `FEISHU_APP_SECRET`
2. 跑 `git log --all -S "<REDACTED-ROTATED>"` 检查任何相关 git 仓库
3. 如果命中，用 `git filter-repo` 重写历史

但本仓库（`$REPO_ROOT/`）的初次 commit 已经不含任何飞书代码或凭据。

---

## P0 — 路径模板化

每个运行时服务的绝对路径都必须从环境变量读。当前状态：`query_server.py` / `build_pdf_db.py` / `build_notes_db.py` 里**零**个 env-var-aware 路径。Scanner 已经支持 `GEMINI_LITERATURE_SKILL_ROOT` 等几个，这个模式要扩散到其它脚本。

| 现路径常量 | 文件:行 | 引入的环境变量 |
|---|---|---|
| `CHROMA_PATH = $LOCALRAG_HOME\chroma` | `query_server.py:21`, `build_pdf_db.py:94`, `build_notes_db.py:21`, `ingest_textbook.py:21` | `LOCALRAG_CHROMA_PATH` |
| `ZOTERO_DB = $ZOTERO_DB_PATH` | `build_pdf_db.py:27`, `gemini_analyze_pdf.py:23` | `ZOTERO_DB_PATH` |
| `NOTES_DIR = $LOCALRAG_NOTES_DIR` | `build_pdf_db.py:57`, `build_notes_db.py:23`, `gemini_analyze_pdf.py:36` | `LOCALRAG_NOTES_DIR` |
| `LEDGER_PATH (papers)` | `build_pdf_db.py:96` | `LOCALRAG_PDF_LEDGER` |
| `LEDGER_PATH (notes)` | `build_notes_db.py:25` | `LOCALRAG_NOTES_LEDGER` |
| `LEDGER_PATH (textbook)` | `ingest_textbook.py:23` | `LOCALRAG_TEXTBOOK_LEDGER` |
| `OLLAMA_URL = http://localhost:11434/api/embeddings` | 6 个文件 | `OLLAMA_EMBED_URL` |
| `EMBED_MODEL = qwen3-embedding:4b` | 4 个文件 | `OLLAMA_EMBED_MODEL` |
| `PORT = 18810` | `query_server.py:1125` | `LOCALRAG_PORT` |
| `QUERY_LOG_ROOT = $LOCALRAG_NOTES_DIR\_query_logs` | `query_server.py:33` | `LOCALRAG_QUERY_LOG_ROOT` |
| `APPROVED_MAIN_PYTHON / APPROVED_RAG_PYTHON` | `gemini_analyze_pdf.py:37-38` | `LOCALRAG_MAIN_PYTHON / LOCALRAG_RAG_PYTHON` |
| `--base-dir` 默认 `$ZOTERO_ATTACHMENT_BASE_DIR` | `zotero_batch_scanner.py:402` | `ZOTERO_ATTACHMENT_BASE_DIR` |

---

## P0 — 文档↔代码漂移修正

会让新用户困惑的不一致点（4 处）：

### 1. 端口冲突

| 文件 | 写的端口 |
|---|---|
| `query_server.py:1125` | 18810 ← 真实绑定 |
| `search-literature/SKILL.md` | 18810 |
| `search-notes/SKILL.md` | 18800 ← stale |
| `search-papers/SKILL.md` | 18800 ← stale |

**改**：把 `search-notes` 和 `search-papers` 两个 SKILL.md 全部统一到 18810；同时把 `query_server.py:1125` 改成 `int(os.environ.get("LOCALRAG_PORT", "18810"))`。

### 2. Gemini 认证方式

`gemini-literature-processor/SKILL.md` 写：

> 环境变量方式（推荐）：`$env:GEMINI_API_KEY = "AIza..."`
> 多 Key 轮换：`--api-keys "KEY1,KEY2,KEY3"`

代码实际用：

```python
# gemini_analyze_pdf.py
GOOGLE_APPLICATION_CREDENTIALS = "$HOME\<your-service-account>.json"
GOOGLE_CLOUD_PROJECT = "<your-gcp-project>"
GEMINI_VERTEX_GCS_BUCKET = "<your-gcs-bucket>"
```

无 `GEMINI_API_KEY`、无 `--api-keys` 轮换。SKILL.md 描述的是 legacy 模式（`DEFAULT_PROMPT` 字符串、不经 GCS），代码默认走 `multifacet-spec` 模式（Vertex AI + GCS）。

**改**：重写 SKILL.md 的"API Key 管理"段为"Vertex AI 服务账号"，把 4 个必需 env var 列清楚。如果保留双模式支持，加 `--auth-mode api-key|vertex` 显式开关。

### 3. `dedupe` 参数静默忽略

`/search_notes` 接受 `dedupe: true/false`，但 `query_server.py:484-485` 注释明确："dedupe 对整篇入库无意义"。各 SKILL.md 仍然在 WF1a / WF6 里设 `dedupe: true` 像它有意义。

**改**：要么从 SKILL 的 WF 调用里删 `dedupe`，要么在响应里返 `dedupe_applied: false` 让前端可观测；接口保留向后兼容但显式 no-op。

### 4. GCS bucket 永久累积

bucket 名 `*-gemini-literature-temp` 暗示临时，但 `cleanup_gcs_archive.py` 不在主流水线里。900 组 PDF 持续累积。

**改**：cron / scheduled task 跑 `cleanup_gcs_archive.py`（保留最近 30 天），或把 bucket 重命名为 `*-gemini-literature-archive` 修正命名歧义。

---

## P1 — 清理

### ✅ 已规避（这些文件从一开始就没被复制到 staging 仓库）

| 类别 | 文件 | 数量 | 状态 |
|---|---|---|---|
| 飞书相关全部 | `upload_*.py` / `create_doc*.py` / `write_doc_content.py` / `fix_doc_content.py` / `check_feishu.py` / `check_doc.py` / `check_status.py` / `verify_upload.py` / `analyze_folder.py` / `batch_*.py` / `delete_duplicates.py` / `try_*.py` / `simple_upload.py` / `test_feishu_upload.py` 等 | 30+ | ✅ 已规避 + Phase 6 删除了相关代码/SKILL |
| ChromaDB 调试（实际目标是别的 SQLite 系统） | `check_db.py` ~ `check_db4.py` | 4 | ✅ 已规避 |
| 早期原型 | `test_build.py`（用 docling）, `test_like.py` | 2 | ✅ 已规避 |
| 已废弃服务器（用 SQLite，非 ChromaDB） | `query_server_v2.py` | 1 | ✅ 已规避 |
| 用户状态 | `processed_groups.txt.bak`, `build_log.txt`, `build_run_live.txt`, `build_debug.log`, `test_output.txt` | 5 | ✅ 已规避（且 .gitignore 拦截后续意外） |
| 不相关项目 ledger | `wave8_gold_ledger.txt`（属 `chroma_wave8_gold/` 独立 collection） | 1 | ✅ 已规避 |

实际从 `.localrag/` 排除了 ~45 个文件。它们仍然在你本地的 `$LOCALRAG_HOME\`
里（按用户要求未动），只是不在 `$REPO_ROOT/` 这个 staging 仓库里。

### ⏳ 仍待清理（在 staging 里，但不优雅）

详见 [POLISH-EVALUATION.md](POLISH-EVALUATION.md)。要点：
- `--pipeline-mode legacy` 死路径 + DEFAULT_PROMPT 死提示词（~200 行）
- `make_backend_from_args` / `make_backend()` 双层 dispatch
- `combined_hash` 3 种算法分歧
- sub-agent 流缺 Stage B

### `gemini-literature-processor` SKILL.md 多副本

| 位置 | 行数 | 状态 |
|---|---|---|
| `.agents\skills\gemini-literature-processor\SKILL.md` | 181 | **canonical**（Vertex / GCS / multifacet-spec） |
| `.claude\skills\gemini-literature-processor\SKILL.md` | 230 | **过时**（API key / `.openclaw` 路径 / 无 Vertex 段） |
| `.openclaw\skills\...\SKILL.md` | (与 .claude 同) | 镜像 |
| `.cc-switch\skills\...\SKILL.md` | (估同) | 镜像 |

**改**：开源仓库只发布 `.agents\` 版本；其它三个本地副本要么改为单行 redirect，要么直接删（前提是用户确认不再用其它 Claude agent runner）。

### Bug 修（在发布前可选，但建议）

- `get_parent_key()` 文件名子串匹配（`gemini_analyze_pdf.py`）→ 改用 attachment item key 精确匹配
- PDF 重摄入时残留 chunk（`build_pdf_db.py`）→ `col.delete(where={group_hash: old_hash})` before `add`
- `prefs.js` 正则解析竞态（scanner）→ 检测 Zotero 进程存活，存活则 bail
- `combined_hash` 算法 scanner ↔ indexer 分歧 → 锁定其中一个；旧数据可保留双变体兼容

---

## P2 — 完善

- **双语 SKILLs**：每个 skill 出 `SKILL.md`（英文）+ `SKILL.zh.md`（中文 canonical）。中文是个人长期使用版；英文是社区公开版。基础设施 skill（rag-engineer / vector-database-engineer / embedding-strategies）已是英文，原样发布。
- **跨平台启动器**：把 SKILL.md 里 PowerShell 启动片段抽到 `service/start.sh` 和 `service/start.ps1`，SKILL 指向脚本而非内联命令。
- **Docker compose**：`service/Dockerfile` 一键拉起 Ollama + ChromaDB + query_server。
- **冒烟测试**：`pytest` 跑每个端点对一个 seed 过的小型 ChromaDB（3-5 个假笔记 + 假 PDF）。
- **最小语料 fixture**：3-5 个公开 PDF + 假 `processed_history.txt`，让新用户验证安装。

---

## 仓库结构建议

```
research-rag/
├── README.md                 ← 公开入口（见 README.draft.md）
├── ARCHITECTURE.md
├── COMPONENTS.md
├── PACKAGING-PLAN.md         ← 本文件
├── .env.example
├── .gitignore
├── pyproject.toml            ← service + scanner；锁 chromadb==1.5.5
├── requirements-rag.txt      ← chromadb / pdfplumber / flask / pyyaml / requests
├── requirements-scanner.txt  ← scanner deps (vertex + gemini-api default; anthropic / openai opt-in)
│
├── setup.sh                  ← Unix 引导（venv + pip + ollama pull）
├── setup.ps1                 ← Windows 引导
│
├── service/                  ← 常驻 HTTP 边车（不是 skill）
│   ├── query_server.py       ← 入口；所有路径从 env 读
│   ├── build_pdf_db.py
│   ├── build_notes_db.py
│   ├── ingest_textbook.py
│   ├── start.sh / start.ps1
│   └── tests/
│
├── scanner/                  ← Gemini 笔记生成
│   ├── zotero_batch_scanner.py
│   ├── gemini_analyze_pdf.py
│   ├── verify_and_clean.py
│   ├── backfill_hash.py
│   ├── cleanup_gcs_archive.py
│   ├── prompts/
│   │   ├── document_profiler.system.txt
│   │   ├── note_generator.system.txt
│   │   └── candidate_tagger.system.txt
│   ├── schemas/
│   │   ├── document_profile.vertex.schema.json
│   │   ├── structured_note.vertex.schema.json
│   │   └── candidate_tagging.vertex.schema.json
│   ├── template_rules/
│   │   ├── _shared_rules.txt
│   │   ├── electrocatalysis-experimental.txt
│   │   ├── thermocatalysis-experimental.txt
│   │   ├── review-or-perspective.txt
│   │   ├── phd-dissertation.txt
│   │   ├── methods-or-materials-synthesis.txt
│   │   ├── foundational-theory.txt
│   │   └── generic-research-note.txt
│   └── config/
│       └── model_routing_policy.json
│
└── skills/                   ← 复制到 ~/.claude/skills/ 即可启用
    ├── search-literature/
    │   ├── SKILL.md          ← 英文（公开）
    │   └── SKILL.zh.md       ← 中文（canonical）
    ├── search-notes/
    │   ├── SKILL.md
    │   └── SKILL.zh.md
    ├── search-papers/
    │   ├── SKILL.md
    │   └── SKILL.zh.md
    ├── gemini-literature-processor/
    │   ├── SKILL.md
    │   ├── SKILL.zh.md
    │   └── references/
    │       ├── workflow-runbook.md
    │       ├── incremental-note-contract.md
    │       └── maintenance-tools.md
    ├── literature-tagging-pipeline/
    │   ├── SKILL.md
    │   └── scripts/
    │       └── watch_tagging_pipeline.ps1
    └── infra/
        ├── rag-engineer/SKILL.md
        ├── vector-database-engineer/SKILL.md
        └── embedding-strategies/SKILL.md
```

### .gitignore

```gitignore
.env
.env.*
.venv/
service/venv/
service/chroma/
service/processed_groups.txt
service/processed_notes.txt
service/textbook_ledger.txt
scanner/processed_history.txt
scanner/processed_history.txt.*
*.bak
__pycache__/
*.pyc
vertex-ai-*.json
*.log
build_log.txt
build_run_live.txt
build_debug.log
test_output.txt
runs/
```

---

## .env.example（关键变量）

```dotenv
# ============================================================
# research-rag 配置
# 复制为 .env 并填值
# ============================================================

# --- Python 解释器 ---
LOCALRAG_MAIN_PYTHON=/usr/bin/python3.11
LOCALRAG_RAG_PYTHON=./.localrag/venv/bin/python

# --- 路径布局 ---
LOCALRAG_NOTES_DIR=~/research-note
LOCALRAG_CHROMA_PATH=~/.localrag/chroma
LOCALRAG_PDF_LEDGER=~/.localrag/processed_groups.txt
LOCALRAG_NOTES_LEDGER=~/.localrag/processed_notes.txt
LOCALRAG_QUERY_LOG_ROOT=~/research-note/_query_logs

# --- Zotero ---
ZOTERO_DB_PATH=~/Zotero/zotero.sqlite
ZOTERO_DATA_DIR=~/Zotero
ZOTERO_ATTACHMENT_BASE_DIR=          # 用 linked-file 模式时填

# --- Ollama 嵌入服务 ---
OLLAMA_EMBED_URL=http://localhost:11434/api/embeddings
OLLAMA_EMBED_MODEL=qwen3-embedding:4b

# --- 查询服务 ---
LOCALRAG_PORT=18810

# --- Gemini / GCP ---
# 二选一：API key 模式
GEMINI_API_KEY=AIza...
# 或：Vertex AI 服务账号模式（4 个都给）
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=global
GEMINI_VERTEX_GCS_BUCKET=your-gcs-bucket-name

# --- 高级 ---
GEMINI_LITERATURE_SKILL_ROOT=~/.agents/skills/gemini-literature-processor
```

完整版见 `investigation/05-packaging-portability.md` §4。

---

## 引导（新用户，10 步）

1. Clone this repository, then run `cd research-rag`.
2. `cp .env.example .env`，填必需变量
3. `./setup.sh`（Unix）或 `./setup.ps1`（Windows）→ 创建 `service/venv`、pip install
4. `ollama pull qwen3-embedding:4b`
5. 配 Gemini 认证（API key 或 Vertex AI）
6. **关 Zotero**，跑 `python scanner/zotero_batch_scanner.py --limit 5` 验证
7. `python service/build_notes_db.py && python service/build_pdf_db.py`
8. `python service/query_server.py`（或 `service/start.sh`）
9. `cp -r skills ~/.claude/skills/`
10. 在 Claude Code 里：`/search-literature 你知道哪些关于 CO2 还原的论文？`

---

## 跨平台计划

### 真正 Windows-only 的（要替换）

| Windows | macOS / Linux |
|---|---|
| `Start-Process … -WindowStyle Hidden` | `subprocess.Popen(..., start_new_session=True)` |
| `netstat -ano \| findstr 18810` | `ss -tlnp \| grep 18810`（Linux）/ `lsof -i :18810`（mac） |
| `Get-Process python \| Where-Object …` | `pkill -f query_server.py` |
| `C:\Users\…\Python311\python.exe` | `python3.11` via pyenv |
| `C:\…\ollama.exe` | `ollama` 在 `$PATH` |
| `$ZOTERO_DB_PATH` | `~/Zotero/zotero.sqlite` |
| 文档里反斜杠路径 | 正斜杠；`pathlib.Path` 接受两者 |

### 天生跨平台的

ChromaDB 1.5.5（Rust bindings ship Linux/macOS/Windows wheels）、Ollama、Flask、Python 脚本（一旦 env-var 化）、Zotero（仅默认数据目录不同）。

### 跨平台动作

把 SKILL.md 里 PowerShell 启停片段统一替换为 `python service/start.py` 或 `service/start.sh / start.ps1` 双脚本。Windows 步骤标 `<!-- Windows -->` 注释。

---

## 命名

**推荐**：`research-rag`（仓库 + 插件 slug）。

考虑过的备选：
- `zotero-claude-rag` — 显式但长；"claude" 可能踩 Anthropic 品牌指南。
- `localrag` — 太泛；不传达 Zotero + Claude Code 这个组合的特异性。
- `research-rag` — 描述用例（研究文献）但不强绑数据源（Zotero / Mendeley / 一堆 PDF 都能套）。

README 一句话简介："A Claude Code plugin for local-first literature RAG over Zotero PDF libraries."

---

## 阻塞清单速览（已处理状态）

| # | 阻塞 | 状态 | 解法 |
|---|---|---|---|
| B1 | `FEISHU_APP_SECRET` 泄漏 | ✅ 飞书功能整体删除 | 不再适用 |
| B2 | `FEISHU_APP_ID` 字面量 | ✅ 飞书功能整体删除 | 不再适用 |
| B3 | 飞书工作区子域硬编码 | ✅ 飞书功能整体删除 | 不再适用 |
| B4 | `FEISHU_FOLDER_TOKEN` 字面量 | ✅ 飞书功能整体删除 | 不再适用 |
| B5 | `feishu_docs.json` 含真实 doc ID | ✅ 飞书功能整体删除 | 不再适用 |
| B6 | `CHROMA_PATH` 硬编码 | ✅ 已 env 化 | `LOCALRAG_CHROMA_PATH` 通过 `service/config.py` |
| B7 | `ZOTERO_DB` 硬编码 | ✅ 已 env 化 | `ZOTERO_DB_PATH` |
| B8 | scanner `--zotero-dir` 默认 | ✅ 已 env 化 | argparse default 走 `ZOTERO_DATA_DIR` |
| B9 | `NOTES_DIR` 硬编码 | ✅ 已 env 化 | `LOCALRAG_NOTES_DIR` |
| B10 | `QUERY_LOG_ROOT` 硬编码 | ✅ 已 env 化 | `LOCALRAG_QUERY_LOG_ROOT` |
| B11 | `FEISHU_DOCS_REGISTRY` 硬编码 | ✅ 飞书功能整体删除 | 不再适用 |
| B12 | `CANONICAL_SKILL_ROOT` 默认 | ✅ 已 env 化 | 通过 `scanner/config.py`；SKILL.md 已更新 |
| B13 | scanner `--base-dir` 默认 | ✅ 已 env 化 | `ZOTERO_ATTACHMENT_BASE_DIR` |
| B14 | SKILL.md 烘焙 Python311 字面量 | ✅ 已替换 | 改为 `$LOCALRAG_*_PYTHON` 引用 |
| B15 | `APPROVED_*_PYTHON` 字面量 | ✅ 已 env 化 | `LOCALRAG_MAIN_PYTHON / LOCALRAG_RAG_PYTHON` |
| B16 | 30+ 实验脚本含用户数据 | ✅ 不在本仓库 | 仅留在用户本地 `.localrag/` |
| B17 | 中文 UX 烘焙到所有 SKILL | ⏳ P2 deferred | 双语轨待写 |
| B18 | Vertex 项目 / bucket 烘焙到 SKILL | ✅ 已 env 化 | SKILL.md 已重写 |
| B19 | `APPROVED_MAIN_PYTHON` 含用户家目录 | ✅ 已 env 化 | 同 B15 |
| B20 | 18810 vs 18800 端口分歧 | ✅ 已统一 | 4 个 SKILL.md + server + env (`LOCALRAG_PORT`) 全部 18810 |

剩余待办都是 P2 / 测试类（双语 SKILL、smoke tests、Docker 等），见 STATUS.md。

---

## 资料引用

- `investigation/05-packaging-portability.md` — 主要来源
- `investigation/03-query-server.md` §7 — query_server 常量
- `investigation/02-build-and-index.md` §8 — build 脚本常量
- `investigation/01-note-generation-pipeline.md` §8 — scanner 常量
- `investigation/04-skills-layer.md` §9 — SKILL 硬编码路径
