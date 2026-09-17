---
name: search-papers
description: 在 PDF 原文库中检索原始论文段落，用于验证笔记结论、获取实验细节、查找原话。支持 zotero_parent_key 过滤，一次返回主文+SI 的所有相关内容。
---

# Search Papers - PDF 原文检索

查询 ChromaDB 向量索引，获取论文原文段落（主文 + SI）。

## When to Use

**触发场景**：
- "原文怎么说"
- "验证一下..."
- "具体的实验条件原文"
- "找原话"
- 对笔记结论存疑，需要原始依据

**与 search-notes 的关系**：
- search-notes → 获取人类撰写的结论摘要
- search-papers → 获取原始论文段落（可验证、可引用）

## Endpoints

> **MCP 优先**：若会话内有 `research-rag` MCP 工具，直接调 `search_papers`
> 工具（参数与下方 HTTP 接口同名同义），无需启动任何服务。
> MCP 默认带上下文；HTTP 调用需显式传 `include_context: true`。

```
POST http://127.0.0.1:18810/search_papers
```

## Parameters

若目标是回答研究问题，而非仅展示原始命中，优先用 `prepare_answer`：
传 `query`、`n`、`budget_codepoints`（默认且最多 8000），可附 parent/attachment/role
过滤。返回去重、限长的 canonical 证据包；依其 instructions 逐项写 claims，
再用 `check_answer(packet, answer)` 校验引用。引用 ID 必须来自该包；可选
quote 必须逐字匹配，不填时自动绑定整段。科学条件是否支持主张仍须由回答者审阅。
HTTP 对应 `/prepare_answer` 和 `/check_answer`。原始段落展示仍使用下方 search_papers。

### search_papers

```json
{
  "query": "搜索关键词（中英文均可）",
  "n": 3,
  "zotero_parent_key": "ABC12345",  // 推荐：覆盖主文+SI
  "paper_group": 1-6,               // 向后兼容
  "pdf_filename": "...pdf",          // 向后兼容
  "second_query": "英文术语版",      // WF4：笔记结论的英文翻译
  "include_context": true           // 带可回查的原文上下文
}
```

## Response Format

```json
{
  "results": [
    {
      "content": "原始匹配的 chunk（排名及内容保持不变）",
      "context": "前文 [MATCH]原始匹配的 chunk[/MATCH] 后文",
      "evidence": {"verified": true, "chunk_id": "...", "segments": []},
      "context_source": {
        "verified": true,
        "for_chunk_id": "...",
        "text_format": "verbatim_canonical_pdf_text",
        "boundary_status": {"start": "sentence_heuristic", "end": "source_end"},
        "segments": []
      },
      "metadata": {
        "pdf_filename": "...",
        "zotero_parent_key": "ABC12345",
        "is_main": true,
        "is_si": false,
        "chunk_index": 5
      },
      "distance": 0.234
    }
  ],
  "query": "原始查询",
  "effective_query": "实际用于检索的查询（second_query 优先）"
}
```

## Cross-reference with Notes

- 优先使用 `zotero_parent_key` 过滤，一次覆盖主文+SI
- 返回的 `is_main` / `is_si` 标记区分主文和补充材料
- 实验细节通常在 SI（is_si: true）

## Output Guidelines

**展示格式**：
```
📄 {pdf_filename}（{主文/SI}）
> {原文英文段落（至少3-5句）}
译：{中文翻译}
```

**重要**：
- 有 `context` 时用它展示匹配段与周围原文；`content` 始终只是原始命中块。
- canonical 上下文按同一附件的原文坐标生成，通常不超过 3200 字符，不重复拼入相邻块的重叠文本；超长原始命中不会被静默截断。
- 引用扩展内容时使用 `context_source.segments` 的页码、坐标和引句；原始命中仍使用 `evidence`。`context_source` 取代旧版只列相邻块的 `context_evidence`。
- `budget_cut` 表示该侧尚未补到句界，不能把当前片段当作完整条件。`sentence_heuristic` 仅表示检测到句界，不代表科学证据完整。
- 保留原文数值、单位和图注；不要把相邻 MOR/ORR、主文/SI 的条件互相套用。PDF 抽取的上下标不明确时回查原 PDF，不自行修正单位。
- 先展示原文，再提供翻译

## 入库新 PDF（维护说明）

**论文 / 小型 PDF（<200 页）**：`service/build_pdf_db.py`。在 `$LOCALRAG_NOTES_DIR/` 创建含 `pdf_0_path` frontmatter 的 stub 笔记即可触发——脚本扫所有笔记的 `pdf_N_path` 字段做发现。

**大型教材（≥200 页）**：`build_pdf_db.py` 一次性 `col.add()` 会超时；改用：
```bash
python service/ingest_textbook.py --pdf-path /path/to/book.pdf --zotero-key ZOTERO_KEY
```
`ingest_textbook.py` 分批提交（每批 `$LOCALRAG_TEXTBOOK_BATCH_SIZE` 个 chunk，默认 50），已入库的文件会自动跳过（`$LOCALRAG_TEXTBOOK_LEDGER`）。

## Error Handling

服务不可用时，按顺序执行：

### Unix / macOS
```bash
curl -sf http://localhost:11434/api/tags >/dev/null || (ollama serve &)
sleep 2
python service/query_server.py &
```

### Windows (PowerShell)
```powershell
try { Invoke-RestMethod http://localhost:11434/api/tags -TimeoutSec 3 | Out-Null }
catch { Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden; Start-Sleep 5 }
Start-Process python -ArgumentList "service/query_server.py" -WindowStyle Hidden
```

端口被占用时（默认 18810）：
- Linux: `lsof -i :18810`
- macOS: `lsof -i :18810`
- Windows: `netstat -ano | findstr 18810`
