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

```
POST http://127.0.0.1:18810/search_papers
```

## Parameters

### search_papers

```json
{
  "query": "搜索关键词（中英文均可）",
  "n": 3,
  "zotero_parent_key": "ABC12345",  // 推荐：覆盖主文+SI
  "paper_group": 1-6,               // 向后兼容
  "pdf_filename": "...pdf",          // 向后兼容
  "second_query": "英文术语版"       // WF4：笔记结论的英文翻译
}
```

## Response Format

```json
{
  "results": [
    {
      "content": "前后 chunk 拼接后的原文（约2400字符）",
      "content_original": "原始匹配的 chunk（800字符）",
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
- 展示 content 字段的完整内容（已拼接前后 chunk，约2400字符）
- 不要只挑一句话，展示完整段落
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
