# Phase A 执行方案：发布收尾（ADR-001 P1 残项）

**Status:** Approved for execution
**Date:** 2026-07-16
**上游:** `docs/plans/2026-07-15-terminal-first-adr.md`（ADR-001）Action Items 7/10/12 的未完成部分
**执行者:** Opus 子代理（允许中途停下向监督者提问）

---

## 目标

完成开源发布前的最后三件事，全部围绕一个承诺：**fresh clone → 终端 agent 里零 key 可用**。

| 任务 | 一句话 | 风险等级 |
|---|---|---|
| A1 | 在真实 venv 里端到端验证 MCP 检索路径（当前全项目风险最高的未验证承诺） | 高（可能暴露 bug） |
| A3 | `init_environment.py`：新增终端 agent MCP 注册步骤（Codex config.toml）+ 修复 Ollama 步骤与 fastembed 新默认的矛盾 | 中 |
| A2 | README 首屏重写为终端优先叙事 | 低（依赖 A1 结论） |

**执行顺序必须是 A1 → A3 → A2**：A1 可能暴露需要修的 bug；A2 的文案要引用 A1 的验证记录。

---

## 硬边界（先读这节）

1. **不可触碰清单**（ADR-001 原文）：subagent/manifest/exit-200 契约、ProcessorBackend 契约、两阶段流水线、去重设计、WF1a–WF10。本次任务不涉及它们，碰到即越界。
2. **绝不读写用户真实数据**：
   - ⚠ **最危险的一条**：`service/config.py:69` 的 `LOCALRAG_HOME` **缺省值就是 `~/.localrag`**（即用户主目录下的生产 ChromaDB）。也就是说：**忘设环境变量 = 直接碰真实库**。因此凡是会 import `service/config.py` 的进程（build_notes_db、mcp_server、verify 脚本 spawn 的服务器、pytest 里的相关用例），启动时**必须显式设置**这四个变量指向临时目录：`LOCALRAG_HOME`、`LOCALRAG_NOTES_DIR`、`LOCALRAG_QUERY_LOG_ROOT`（query log 会在检索时落盘写文件）、`LOCALRAG_EMBED_PROVIDER=fastembed`。
   - 测试期间**绝不写真实的 `~/.codex/config.toml`**。写入函数必须可注入目标路径，测试只写临时文件。
   - 仓库根**当前没有 `.env`**。`init_environment.py` 的交互流程会创建它——A3 冒烟测试后必须恢复到「不存在」状态，不许留残留。
3. **不引入新的运行时依赖**。TOML 读用 stdlib `tomllib`（Python 3.11+）；写用受控文本操作，不装 `tomli-w`。
4. **Python 路径纪律**：使用受支持的 Python 3.10+ 创建 venv；venv 建好后一律用 `.venv\Scripts\python.exe`（Windows）或 `.venv/bin/python`（POSIX），不依赖 PATH。
5. **手术式修改**：README 只动首屏和被 A1/A3 波及的行；init_environment.py 只动 Ollama 步骤、新增 MCP 注册步骤、步骤计数文案。不顺手重构。
6. 网络下载边界：允许 `pip install` 与 fastembed 首次运行时自动下载嵌入模型（`paraphrase-multilingual-MiniLM-L12-v2`，约 0.22 GB，缓存进用户 HF 缓存目录）。除此之外不下载任何东西。

---

## Task A1 — MCP 路径真实 venv 端到端验证

### 背景事实（已核对，不必重查）

- 本机之前跑测试的 Python 缺 `chromadb` / `mcp` / `fastembed`，`tests/test_mcp_server.py`、`tests/test_schema_compat.py` 等一直 **skip**。MCP 链路从未在真实依赖下跑通过。
- 启动链：`.mcp.json`（command=`python`，args=`scripts/run_mcp_server.py`）→ launcher 检测 `.venv\Scripts\python.exe` 存在则 **re-exec** 到 venv → `service/mcp_server.py`（FastMCP，stdio）→ import `query_server`（模块级初始化 ChromaDB，`LOCALRAG_SKIP_CHROMA_INIT=1` 可跳过）。
- 工具面：`search_notes` / `search_papers` / `get_note` / `index_status`。
- 嵌入默认：`service/config.py:100` `LOCALRAG_EMBED_PROVIDER` 默认 `fastembed`；模型 `paraphrase-multilingual-MiniLM-L12-v2`。
- `.gitignore` 已含 `.venv/`。

### 步骤

1. **建 venv 装依赖**（镜像文档化路径）：
   ```powershell
   python -m venv <repo>\.venv
   <repo>\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-test.txt
   ```
   注意 pip 装 chromadb/onnxruntime 可能要几分钟，命令超时给足（≥10 分钟）。

2. **跑全量 pytest**：之前 skip 的测试现在会真跑。记录：多少条从 skip 变 pass / fail。fail 的按「发现的 bug」流程处理（见下）。

3. **搭隔离语料**：临时目录（如 `%TEMP%` 下）作 `LOCALRAG_HOME` 和 `LOCALRAG_NOTES_DIR`；写 3 篇合成研究笔记（frontmatter 含 `zotero_parent_key` / `title_en` / `title_zh` / `year` / `journal` / `doi` 等真实 schema 字段——参照 `service/build_notes_db.py` 与 `query_server.py` 实际读取的字段），内容用可区分的化学主题（如 OER 催化剂 / CO2RR / 燃料电池），保证语义检索能验证「查 A 命中 A 而不是 B」。

4. **build_notes_db**：用 venv Python + 上述 env 跑 `service/build_notes_db.py`，确认 fastembed 首跑下载模型、3 篇笔记入库、ledger 写成 `filename\thash` 格式。

5. **MCP stdio 真实往返**：写 `scripts/verify_mcp_e2e.py`（留在仓库，作为可复跑的验收工具）：
   - 用 `mcp` 包的 stdio client（`mcp.client.stdio` + `ClientSession`）以子进程方式 spawn 服务器；
   - **spawn 命令刻意用非 venv 的 python**（如系统 Python311）跑 `scripts/run_mcp_server.py`，以验证 launcher 的 re-exec 逻辑真实工作；
   - 断言序列：`list_tools` 含四个工具 → `index_status`（notes_ready=true、papers_ready=false、active_embed_model 是 fastembed 模型）→ `search_notes("氧析出反应催化剂")` 命中 OER 那篇且排第一 → `get_note(source=...)` 返回全文 → `search_papers(...)` 在 papers 集合缺失时**优雅降级**（结构化错误信息，进程不崩）。
   - 脚本读环境变量拿隔离目录，退出码 0/1，打印每步 PASS/FAIL。
6. **验证 Claude Code 视角的配置正确性**：不需要真的起 Claude Code，但要人肉核对 `.mcp.json` 的 `command: "python"` 在「PATH 上只有系统 Python」的前提下能走通（第 5 步已覆盖）；在验证报告里写清这个前提与 Windows 上 `python` 不在 PATH 时的失败模式（known limitation）。

### 发现 bug 的处理流程

- 能明确修法且不越界 → 修，**每个 bug 单独 commit**（`fix(scope): ...`），在验证报告记一行。
- 修法有多种或牵动设计 → **停下提问**（见提问协议）。

### 交付物

- `scripts/verify_mcp_e2e.py`（可复跑）
- `docs/audits/2026-07-16-mcp-e2e-verification.md`：环境（OS/Python/依赖版本）、pytest 前后对比（skip→pass/fail 计数）、E2E 每步结果、发现并修复的 bug 清单、known limitations
- 若有 bug 修复：对应源码改动

---

## Task A3 — init_environment.py：MCP 注册步骤 + 嵌入步骤对齐新默认

### 背景事实

- `main()`（`scanner/init_environment.py:609`）顺序：python 版本 → env → vault → backend 选择 → 凭据 → zotero → **step_ollama（第 7 节，无条件）** → domain pack → skills → doctor。文案称「9 setup steps」。
- 矛盾点：`step_ollama` 无条件引导安装 Ollama + pull 模型，但服务端默认嵌入已是 fastembed（零 daemon）。fresh-clone 用户会被引去装一个默认路径根本不用的 daemon。
- ADR-001 P1-10：Claude Code 侧零注册已由仓库 `.mcp.json` 覆盖；**Codex 侧需要 `init_environment.py` 写 `config.toml`**。

### A3a — 新增「终端 agent MCP 注册」步骤

放在 skills 步骤之后、doctor 之前。行为：

1. **Claude Code**：不写任何文件。打印说明：仓库自带 `.mcp.json`，在仓库目录里启动 Claude Code 会弹一次批准提示；附手动注册命令 `claude mcp add research-rag -- python scripts/run_mcp_server.py`。
2. **Codex**：询问用户是否注册（默认 No，保持保守）。若 Yes：
   - 目标文件：`$CODEX_HOME/config.toml`，`CODEX_HOME` 未设时用 `~/.codex/config.toml`。
   - **写之前先在本机核实格式**：用 `codex mcp --help`（本机装有 codex CLI）或 codex 官方文档确认 `mcp_servers` 表的准确 schema（`command` / `args` / 可能的 `env`）。**以本机 CLI 帮助输出为准，不凭记忆写格式。**若本机探测不到且文档拿不准 → 停下提问。
   - 写入逻辑抽成纯函数 `register_codex_mcp(config_path: Path, repo_root: Path) -> str`（返回 written/updated/already-registered/skipped 之一）：
     - 文件不存在 → 创建，写 `[mcp_servers.research-rag]` 段；
     - 已有该段且内容一致 → no-op 汇报；
     - 已有该段但内容不同 → 先备份 `config.toml.bak-<timestamp>`，再精确替换该段（只动这一段的行，不重排别人的内容）；
     - 检测用 `tomllib` 解析；写入用文本操作。
   - args 里的 launcher 路径写**绝对路径**（Codex 不一定从仓库目录启动）。
3. **单元测试**：`tests/test_init_codex_mcp.py`，全部打临时路径：创建 / 幂等 / 更新+备份 / 保留无关内容 四个 case。

### A3b — step_ollama 改为按 provider 分流

1. 从 `.env` lines 读 `LOCALRAG_EMBED_PROVIDER`（缺省视为 `fastembed`）。
2. `fastembed`（默认分支）：不再检查/引导 Ollama。打印：当前嵌入=fastembed（进程内 ONNX，零 daemon），模型名，首次建库自动下载约 0.22 GB;然后问一句「要切换到 Ollama 以用更大模型吗？(y/N)」——Yes 则写 `LOCALRAG_EMBED_PROVIDER=ollama` 进 .env 并走现有 Ollama 流程；No 则跳过。
3. `ollama`：走现有流程不变。其他值（openai-compat 等）：打印当前值并跳过。
4. 段落标题从「Ollama embedding service」改为「Embedding provider」；`main()` 的「9 setup steps」计数与各 `_print_section` 序号同步更新。
5. **现有 Ollama 流程本体（`_ollama_running`/`_ollama_models`/pull 交互）不改一行**，只是套上分流。

### 交付物

- `scanner/init_environment.py` 改动 + `tests/test_init_codex_mcp.py`
- 本机验证：`--non-interactive` 路径不回归；交互路径至少用管道喂输入冒烟一次（或把分流逻辑抽成可测函数）

---

## Task A2 — README 首屏重写（终端优先叙事）

### 原则

首屏（H1 到第一个 `---`，现约 1–28 行）重写；其余章节**只做被 A1/A3 波及的最小修订**。不重写整个 README。

### 首屏目标结构

1. H1 + 一句话定位：**「在你的终端 agent（Claude Code / Codex）里直接跑的本地文献 RAG——零 API key、零 daemon、零常驻 server」**。宿主 LLM 及其子代理完成模型工作，这是与所有同类项目的差异点，必须出现在前三行。
2. 紧跟 **4 条命令的最短路径**（clone → setup → init → 在 Claude Code 里直接问），当前埋在 90 行以后的 Bootstrap 摘要提到首屏。MCP 自动发现（`.mcp.json`）要在这里点名。
3. alpha 状态横幅**保留**，但把「no clean-room end-to-end bootstrap …has been recorded yet」更新为引用 A1 的验证记录（如实陈述：在维护者机器的全新 venv 里完成了 MCP 端到端验证，链接 `docs/audits/2026-07-16-mcp-e2e-verification.md`；跨 OS 清洁室仍未做）。
4. 「Pick your start path」表保留，subagent 行保持第一。

### 波及修订（点名清单，改完即止）

- `## Why this exists` 第 2 条「ChromaDB + Ollama stay local」→ 改为 fastembed 进程内默认、Ollama 是可选升级。
- Bootstrap 第 2 步注释「checks Ollama, pulls the embedding model」→ 与 A3b 后的实际行为一致。
- Bootstrap 若新增了 MCP 注册步骤（A3a），第 2 步的步骤描述数字/文案同步。
- 如 grep 到其他「Ollama 是默认/必需」的表述（`## Requirements`、`### Retrieval` 段），一并对齐，但**不改写句子风格**。

---

## 提问协议（对执行代理）

以下情形**必须停下**，把问题作为你的最终输出（格式见下），等监督者答复后继续：

1. A1 暴露的 bug 有多种修法、或修复会触碰硬边界清单。
2. Codex `config.toml` 的 schema 在本机 CLI 帮助里探测不到、且官方文档与你的先验不一致。
3. 任何步骤要求写入 `<repo>` 与临时目录之外的位置（唯一例外：A3 单测的临时路径）。
4. pytest 出现与本方案无关的既有失败，拿不准是否顺手修。
5. README 现有内容与 A1 实测结果矛盾且矛盾不在上面的点名清单里。

提问格式（你的最终消息）：

```
QUESTIONS FOR SUPERVISOR
1. [背景一句话] 问题？我的倾向：X，因为 Y。
2. ...
已完成进度：[到哪一步，已 commit 哪些]
```

不要为琐碎决定停下（变量命名、报告措辞、commit 拆分粒度按本方案已有约定自行决定）。

## 提交纪律

- 分组提交，顺序建议：
  1. `fix(...)` × N —— A1 发现的每个 bug 单独一条
  2. `test(e2e): MCP stdio 端到端验证脚本 + 真实 venv 验证报告`（verify_mcp_e2e.py + 审计报告）
  3. `feat(init): Codex MCP 注册步骤 + 嵌入步骤对齐 fastembed 默认`（A3 全部 + 单测）
  4. `docs(readme): 终端优先首屏重写`（A2）
- commit message 尾部加 `Co-Authored-By:` 按仓库现行惯例。
- 不 push。

## 验收清单（监督者复核用）

- [ ] `.venv` 在仓库根，`pytest` 全量通过（或失败均有处置记录）
- [ ] `verify_mcp_e2e.py` 退出码 0，且 spawn 用的是非 venv Python（证明 re-exec 生效）
- [ ] 验证报告落盘 `docs/audits/2026-07-16-mcp-e2e-verification.md`
- [ ] `register_codex_mcp` 四个单测 case 全绿；用户真实 `~/.codex/config.toml` 的 mtime 未变
- [ ] fresh-clone 叙事自洽：README 首屏、init 步骤文案、`.env.example`、doctor 输出四处对「默认=subagent+fastembed+MCP」口径一致
- [ ] 用户真实数据零接触：`~/.localrag`、`<notes-dir>` 的 mtime 未变
- [ ] `<repo>/.env` 执行前后都不存在（A3 冒烟不留残留）
