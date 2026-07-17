---
name: search-literature
description: >
  本地文献知识库统一检索入口。当用户询问任何与文献、论文、研究现状、实验方法、机理分析、
  原文内容相关的问题时，必须触发此 skill。覆盖从快速笔记查阅到笔记与原文深度交叉检索的全部场景。

  **必须触发的场景**（凡涉及文献内容，一律走此 skill）：
  - "有哪些论文研究了X" / "X的研究进展" / "查一下X"
  - "原文怎么说" / "验证一下" / "找原话"
  - "分析这篇论文" / "详细读一下XX" / "XX论文的结论是什么"
  - "不同论文怎么看X" / "有没有争议" / "研究是怎么发展的"
  - "实验条件是什么" / "怎么测的" / "用了什么表征"
  - 任何提到具体论文标题、作者名、期刊名的查询
---

# Search Literature - 文献检索统一入口

所有文献检索请求的调度中心。负责判断用户意图、选择工作流、调用 search-notes / search-papers 执行、汇总结果。

## 服务状态

**优先走 MCP（无需启动任何服务）**：如果当前会话里有 `research-rag` MCP 工具
（`search_notes` / `search_papers` / `get_note` / `index_status`——仓库自带
`.mcp.json`，Claude Code 进入仓库目录即自动发现），直接用 MCP 工具执行下文
所有 WF 中的检索调用，参数与 HTTP 接口同名同义，跳过本节其余内容。

**HTTP 回退**：`http://127.0.0.1:18810`（端口由 `$LOCALRAG_PORT` 配置，默认 18810）

**HTTP 服务不可用时，按顺序执行**（仅当 MCP 工具也不可用）：

> 注：默认嵌入 provider 已是 fastembed（进程内，无需 Ollama）。只有
> `LOCALRAG_EMBED_PROVIDER=ollama` 时才需要下面第 1 步。

### Unix / macOS
```bash
# 1. 确保 Ollama 在跑
curl -sf http://localhost:11434/api/tags >/dev/null || (ollama serve &)
sleep 2

# 2. 启动查询服务
python service/query_server.py &
```

### Windows (PowerShell)
```powershell
# 1. 确保 Ollama 在跑
try { Invoke-RestMethod http://localhost:11434/api/tags -TimeoutSec 3 | Out-Null }
catch { Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden; Start-Sleep 5 }

# 2. 启动查询服务
Start-Process python -ArgumentList "service/query_server.py" -WindowStyle Hidden
```

端口被占用：
- Unix: `lsof -i :18810`
- Windows: `netstat -ano | findstr 18810`

**入库新内容时的脚本选择**：
- 论文 / 小型 PDF（<200 页）→ `service/build_pdf_db.py`（通过 stub 笔记 + `pdf_0_path` frontmatter 触发）
- 大型教材（≥200 页）→ `service/ingest_textbook.py`（分批提交，避免超时）
  ```bash
  python service/ingest_textbook.py --pdf-path /path/to/book.pdf --zotero-key ZOTERO_KEY
  ```

---

## Step 0：先做 Angle Planning，再执行 WF

默认不要把用户问题直接翻译成一个英文 query 就立刻检索。

每次检索必须包含两个层次：

### 0a. `anchor angle`（强制）

作用：尽量完整保留用户原问题语义，防止后续扩展角度把问题带偏。

规则：
- 若用户问题为英文，优先直接使用原句或仅做极轻微清洗
- 若用户问题为中文，先做忠实英文翻译
- `anchor angle` 不得被 exploratory angles 替代
- `anchor angle` 不是关键词碎片，而是尽量完整保留原问题含义

### 0b. `exploratory angle`（默认至少 1 个）

作用：从研究视角扩展，而不是只做近义词替换。

可选 angle 类型：
- `core_concept`      — 核心概念/主题本身
- `mechanism`         — 机理解释或因果链条
- `evidence`          — 原文证据或关键术语
- `contrast`          — 不同论文的差异、争议、相反结论
- `method`            — 实验条件、表征、参数、SI细节
- `timeline`          — 研究演进、早期/近期变化
- `paper_specific`    — 已知具体论文时的定向深挖

### 0c. 默认执行顺序

默认流程：
1. 先规划 `anchor angle`
2. 再补 1 个最有信息增益的 `exploratory angle`
3. 先跑这 2 个角度做试探检索
4. 若结果仍分散、证据不足、缺方法细节、或存在争议，再扩展到 3-5 个总 angles
5. 若初始 probe 已足够回答问题，则停止扩展

### 0d. 何时扩展

以下情况自动扩展：
- 命中结果太泛，无法形成清晰结论
- 笔记结论和原文证据还没对上
- 用户问题同时要求解释 + 证据
- 当前结果缺实验条件或 SI 参数
- 不同论文明显给出不同解释
- 当前结果只解释了“是什么”，还没解释“为什么”

### 0e. 禁止行为

- 不要把“中文直译成英文后搜一次”当成多角度检索
- 不要把 exploratory angle 写成同一句话的近义词改写
- 不要让 exploratory angles 覆盖或偏离原始问题边界

### 0f. 内部规划格式（不必原样展示给用户）

```
- Angle 1:
  - role: anchor
  - type: anchor
  - purpose:
  - preferred endpoint:
  - query:
- Angle 2:
  - role: exploratory
  - type:
  - purpose:
  - preferred endpoint:
  - query:
```

---

## Step 1：意图识别 → 确认 WF → 执行

### 1a. 内部判断（不说出来）

拿到用户问题后，先完成以下判断：

| 判断维度 | 选项 |
|---------|------|
| 深度 | 快速了解 / 深度分析 |
| 范围 | 单篇 / 多篇横向 |
| 数据源 | 只要笔记结论 / 只要原文 / 两者都要 |
| 特殊需求 | 实验方法细节 / 时间线演进 / 矛盾检测 |
| 初始 angle | `anchor + 1 exploratory` 先从哪两个角度试探 |
| 扩展条件 | 当前问题要不要预留第 3-5 个角度 |

### 1b. 执行前必须告知用户（每次都要做）

判断完成后，**不要直接执行**，先向用户说明：

```
我打算用 WF{编号}·{名称} 来处理这个问题。
会先从 2 个角度试探检索：一个保留你的原问题语义，一个补充最有信息增益的研究角度；如果结果还不够集中，我会自动扩展到更多角度。
{一句话说明为什么选这个 WF，以及会调用哪些端点}。
要继续吗？
```

示例：
> 我打算用 **WF4·笔记→原文联动** 来处理。会先从 2 个角度试探检索：一个保留你的原问题语义，一个补机理/证据角度；如果结果还不够集中，我会自动扩展。会先查笔记库拿到结论和 zotero_parent_key，再用原文中的英文术语精确定位 PDF 段落。要继续吗？

### 1c. 用户不同意时

列出所有工作流供用户选择：

```
当前可用工作流：
WF1a · 快速多篇检索    — 了解某主题有哪些研究
WF1b · 单篇多角度检索  — 深入某一篇的不同段落
WF2  · 指定论文笔记    — 已知论文名，拿笔记摘要
WF3  · 纯原文检索      — 直接找 PDF 原话
WF4  · 笔记→原文联动   — 结论+原文双重验证（复杂问题推荐）
WF5  · 原文→笔记反向   — 看到原文，找当时的分析笔记
WF6  · 横向对比多篇    — 不同论文对同一问题的观点对比
WF7  · 完整精读单篇    — 笔记+原文全量输出
WF8  · 时间线检索      — 按年份展示研究演进脉络
WF9  · 实验方法检索    — 优先查 SI，获取测试参数
WF10 · 矛盾检测        — 找不同论文间的观点分歧

请告诉我用哪个，或者描述你想要的效果，我来判断。
```

### 1d. 特殊情况：执行前需先问用户

以下两种情况在 1b 之前先问清楚：
1. 用户说"这篇论文"但没说论文名 → 先问"哪篇论文？"
2. 用户意图在 WF1a/WF1b 之间模糊 → 先问"要看多篇各自的结论，还是深入某一篇的不同角度？"

---

## Step 2：工作流选择

### WF1a · 快速多篇检索（默认）
**触发**：了解某主题有哪些研究、多篇论文各自说了什么
**特点**：强制去重，确保每篇结果来自不同论文

```
POST /search_notes
{"query": "用户问题", "n": 5, "dedupe": true}
```

### WF1b · 单篇多角度检索
**触发**：用户已锁定某篇论文，想看这篇里的多个相关段落
**特点**：关闭去重，同一篇笔记可以返回多个相关chunk

```
POST /search_notes
{"query": "用户问题", "zotero_parent_key": "KEY", "n": 5, "dedupe": false}
```

> WF1a vs WF1b 判断规则：用户说"有哪些论文"→ WF1a；用户说"这篇里"或已给出论文名 → WF1b

---

### WF2 · 指定论文笔记检索
**触发**：用户明确说要某篇论文的笔记，但不需要完整精读

```
POST /search_notes
{"query": "论文标题或核心主题词", "zotero_parent_key": "KEY", "n": 3, "dedupe": false}
```

> 若不知道 zotero_parent_key，先用标题做 WF1a 搜索，从返回的 metadata 里拿 key，再做 WF2。

---

### WF3 · 纯原文检索
**触发**："原文怎么说" / "找原话" / "验证一下" / 对笔记结论存疑
**注意**：query 必须用英文，中文问题先翻译再传

```
POST /search_papers
{"query": "英文关键词", "n": 3}

# 已知论文时加 key 过滤（同时覆盖主文+SI）
{"query": "英文关键词", "zotero_parent_key": "KEY", "n": 3}
```

---

### WF4 · 笔记→原文深度联动（复杂问题首选）
**触发**：需要结论+原文依据，深度分析某个机理或现象

```
# Step 1: 先查笔记，获取结论和 key
POST /search_notes {"query": "用户问题", "n": 3, "dedupe": true}

# Step 2: 从笔记 content 中提取核心英文术语作为 second_query
# 例：笔记提到 "interfacial water structure"、"free-like water"

# Step 3: 用 key + second_query 精确定位原文
POST /search_papers {
  "query": "用户原始问题",
  "second_query": "从笔记提取的英文术语",
  "zotero_parent_key": "Step1拿到的key",
  "n": 3
}
```

> second_query 必须用英文，直接从笔记里已有的英文专有名词提取，不要自己翻译。

---

### WF5 · 原文→笔记反向联动
**触发**："这段原文当时怎么分析的" / 看到原文想找对应笔记

```
# Step 1: 用原文关键词找论文，获取 key
POST /search_papers {"query": "英文原文关键词", "n": 1}

# Step 2: 用 key 找对应笔记
POST /search_notes {
  "query": "用户原始问题关键词",
  "zotero_parent_key": "Step1拿到的key",
  "n": 3,
  "dedupe": false
}
```

---

### WF6 · 横向对比多篇
**触发**："不同论文怎么看X" / "对比一下" / "研究现状综述"
**特点**：大 n 去重，按 journal/year 分组呈现，每篇只展示前200字符避免过长

```
POST /search_notes {"query": "用户问题", "n": 10, "dedupe": true}
```

呈现时按 `metadata.year` 或 `metadata.journal` 分组，每篇格式：
```
📓 [year] [journal] · {filename}
{content 前200字符}
```

---

### WF7 · 完整精读单篇（笔记+原文）
**触发**："详细分析这篇" / "把XX论文的笔记和原文都给我" / "完整读一下"

```
# Step 1: 定位论文，获取 key
POST /search_notes {"query": "论文标题或作者", "n": 1, "dedupe": true}

# Step 2: 获取完整笔记（不截断）
POST /get_note {"zotero_parent_key": "KEY", "summary_only": false}

# Step 3: 获取该论文原文段落（主文+SI 全覆盖）
POST /search_papers {"query": "论文核心主题（英文）", "zotero_parent_key": "KEY", "n": 5}
```

---

### WF8 · 时间线检索
**触发**："研究是怎么发展的" / "最新进展" / "早期工作是什么"

```
POST /search_notes {"query": "用户问题", "n": 10, "dedupe": true}
```

返回后按 `metadata.year` **升序**排列，呈现研究演进脉络：
```
[year] 📓 {filename}
→ {核心结论一句话}
```

---

### WF9 · 实验方法检索
**触发**："怎么测的" / "实验条件" / "用了什么表征" / "具体参数"
**注意**：优先关注 `is_si: true` 的结果，实验细节通常在 SI 里

```
# 先查原文（实验方法在原文里，不在笔记结论里）
POST /search_papers {
  "query": "实验方法关键词（英文）",
  "n": 5
}

# 若需要笔记里的方法总结，再补一步
POST /search_notes {"query": "用户问题", "n": 3, "dedupe": true}
```

呈现时优先展示 `is_si: true` 的结果，明确标注"来源：SI"。

---

### WF10 · 矛盾检测
**触发**："有没有争议" / "结论一致吗" / "不同论文观点" / "谁对谁错"

```
POST /search_notes {"query": "用户问题", "n": 10, "dedupe": true}
```

返回后分析每篇对同一问题的立场，主动识别并标注矛盾点：
```
⚠️ 观点分歧：
• [year A] {paper A} → 认为：{结论A}
• [year B] {paper B} → 认为：{结论B}
矛盾点：{具体说明分歧在哪里}
```
若无明显矛盾，说明"当前库内论文在此问题上结论一致"。

---

## Step 2.5：WF 与 angle 搭配建议

保留 WF 作为执行骨架，angle 负责决定“从哪些研究视角去查”。

- WF1a（快速多篇）→ 优先 `anchor + core_concept`，必要时扩展 `contrast` 或 `timeline`
- WF1b（单篇多角度）→ 优先 `anchor + paper_specific`，必要时扩展 `mechanism` 或 `evidence`
- WF2（指定论文笔记）→ 优先 `anchor + paper_specific`
- WF3（纯原文）→ 优先 `anchor + evidence`
- WF4（笔记→原文联动）→ 优先 `anchor + mechanism`，常扩展到 `evidence`
- WF5（原文→笔记）→ 优先 `anchor + evidence`，再补 `paper_specific`
- WF6（横向对比）→ 优先 `anchor + contrast`
- WF7（完整精读）→ 优先 `anchor + paper_specific`，再补 `mechanism` / `evidence`
- WF8（时间线）→ 优先 `anchor + timeline`
- WF9（实验方法）→ 优先 `anchor + method`
- WF10（矛盾检测）→ 优先 `anchor + contrast`

---

## Step 3：标准输出格式

### 笔记结果
```
📓 来源笔记：{filename}（第{note_rank}相关，相似度 {score}）
{content 摘要}
```

### 原文结果
```
📄 {pdf_filename}（{主文 / SI}）
> {原文英文段落，至少3-5句，展示 content 完整内容}
译：{中文翻译}
```

### 固定结尾（每次检索完成后必须追加）
```
---
📎 {WF编号} | {命中笔记数}篇笔记 / {命中原文段落数}条原文
🔍 如需深入某篇，告诉我论文名称
📖 如需查看某条原文的上下文，回复"展开第N条"
```

### 上下文展开（用户回复"展开第N条"时）

1. 对该条结果重新请求，加入 `include_context: true`：
```
POST /search_papers {
  ...原始请求参数不变...,
  "include_context": true
}
```

2. 返回的 `context` 字段中，`[MATCH]...[/MATCH]` 之间是原始匹配段，展示时**加粗**：

```
📄 {pdf_filename}（{主文/SI}）· 含上下文

{前段原文}
**{[MATCH]和[/MATCH]之间的匹配段，加粗展示}**
{后段原文}

译：{完整段落的中文翻译}
```

若用户要求把检索结果整理成可复用的本地调研/综述报告，最终 Markdown 报告保存到 `$LOCALRAG_NOTES_DIR/reports/`；`_query_logs` 只保存检索会话日志，`progress/` 只保存过程产物。
新建报告文件名统一使用 `YYYY-MM-DD-{topic-slug}.md`：日期用报告创建日期，`topic-slug` 使用英文小写 kebab-case，只包含 `a-z`、`0-9` 和连字符，并在 slug 里体现主题与报告类型/用途；不要使用空格、中文、下划线或过长标题；历史报告不强制重命名。

---

## Step 4：查询日志写入（每次主查询完成后执行）

每次主查询 workflow 完成并形成最终回答后，都要写一次查询日志。

### 4a. 主查询完成后

调用：
```
POST /write_query_log
```

至少要传：
```json
{
  "workflow_id": "WF4",
  "workflow_name": "笔记→原文联动",
  "status": "success",
  "query": "用户原始问题",
  "idempotency_key": "同一研究会话稳定不变的唯一键",
  "anchor_query": "尽量完整保留原问题语义的英文版本",
  "planned_angles": ["anchor", "mechanism"],
  "executed_angles": ["anchor", "mechanism", "evidence"],
  "expansion_reason": "若有扩展则填写",
  "stop_reason": "停止扩展的原因",
  "search_runs": [
    {
      "role": "anchor",
      "purpose": "当前轮次的检索目的",
      "endpoint": "/search_notes",
      "query": "当前 angle 的 query",
      "filters": "本轮过滤条件",
      "hits": 3
    }
  ],
  "notes": [...],
  "papers": [...],
  "final_response_snapshot": "最终发给用户的总结性回答"
}
```

注意：
- `idempotency_key` 为强制字段，用于避免同一研究会话因重试而生成重复日志
- `anchor_query` / `planned_angles` / `executed_angles` / `search_runs` 现在都是强制字段
- `search_runs` 必须为非空数组

### 4b. 后续追记

若用户之后执行以下动作，不新建日志文件，而是追加到同一条日志：
- "展开第N条"
- "继续深挖某篇"
- "查看上下文"

调用：
```
POST /append_query_log_action
```

至少要传：
```json
{
  "log_path": "已有日志文件路径",
  "log_id": "已有日志文件的 log_id",
  "timestamp": "ISO8601 时间",
  "action": "expand result 1",
  "result": "context appended"
}
```

注意：
- `append_query_log_action` 现在要求同时传 `log_path` 和 `log_id`
- 必须校验追加的是同一条日志，不要只靠路径猜测

### 4c. 记录原则

- 一个用户研究问题 = 一条日志文件
- 多个检索角度 = 同一日志中的多个 `Search Runs`
- follow-up 动作 = 同一日志中的 `Follow-up Actions`
- `Final Response Snapshot` 为强制字段

---

## 端点速查

| 端点 | 用途 |
|------|------|
| `POST /search_notes` | 查笔记库（ChromaDB notes collection，整篇入库） |
| `POST /search_papers` | 查 PDF 原文库（ChromaDB papers collection） |
| `POST /get_note` | 获取单篇完整笔记 |
| `POST /write_query_log` | 写入单次研究会话 Markdown 日志 |
| `POST /append_query_log_action` | 对已有查询日志追加 follow-up 动作 |

所有端点：`http://127.0.0.1:18810`（端口由 `$LOCALRAG_PORT` 配置）
