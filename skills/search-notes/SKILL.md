---
name: search-notes
description: 搜索本地 md 笔记库，返回相关文献结论和摘要。每篇笔记整篇入库（无切块），天然去重。用于快速了解研究现状、查找实验条件、获取机理洞察。
---

# Search Notes — 笔记库检索

查询 ChromaDB `notes` collection 中的文献阅读笔记。每篇笔记整篇入库、不切块。

**原则**：先查笔记，有需要再验证原文。不要两个都查再合并，会造成信息过载。

## 数据来源

- **来源目录**：`$LOCALRAG_NOTES_DIR/*_review_note.md`（默认 `~/research-note`），Gemini 生成的结构化阅读笔记
- **存储**：ChromaDB `notes` collection（与 `papers` 共享同一 chroma 目录）
- **入库方式**：整篇作为一条记录，metadata 从 YAML frontmatter 提取
- **构建脚本**：`service/build_notes_db.py`
- **Ledger**：`$LOCALRAG_NOTES_LEDGER`（默认 `$LOCALRAG_HOME/processed_notes.txt`）

## When to Use

**触发场景**：
- "有哪些论文研究了 xxx"
- "xxx 的研究进展"
- "查找关于 xxx 的文献"
- 需要快速了解某个主题的研究现状

**与 search-papers 的区别**：
- search-notes：返回 Gemini 生成的结构化阅读笔记（有结论、有洞察、中英双语）
- search-papers：返回 PDF 原文段落（原始英文、可验证）

## Endpoints

> **MCP 优先**：若会话内有 `research-rag` MCP 工具，直接调 `search_notes` /
> `get_note` 工具（参数与下方 HTTP 接口同名同义），无需启动任何服务。

```
POST http://127.0.0.1:18810/search_notes
POST http://127.0.0.1:18810/get_note   # 获取完整笔记内容
```

端口可通过 `$LOCALRAG_PORT` 覆盖。

## Parameters

### search_notes

```json
{
  "query": "用户问题（中英文均可）",
  "n": 5,                      // 返回结果数量，默认5
  "dedupe": true,              // 接受但静默忽略：整篇入库本来就一篇一文档
  "zotero_parent_key": "..."   // 可选：只搜索指定论文的笔记
}
```

> ⚠️ **`dedupe` 参数是 no-op**：notes collection 每篇整篇入库（无切块），同一篇返回多个碎片的情况不存在。参数保留是为了向后兼容，传 true 或 false 行为一样。如果你的工作流依赖去重语义，那是 `papers` collection 的特性，不在这里。

### get_note

```json
{
  "zotero_parent_key": "ABC12345",  // 通过 Zotero key 获取
  "summary_only": true               // true=只返回前500字符，false=完整内容
}
```

## Response Format

```json
{
  "results": [
    {
      "content": "笔记内容（前3000字符）...",
      "metadata": {
        "source_file": "2025-AngewChem-Teng-xxx_review_note.md",
        "score": 0.7534,
        "note_rank": 1,
        "zotero_parent_key": "9QHR5X2S",
        "title_en": "...",
        "title_zh": "...",
        "year": "2025",
        "journal": "Angew. Chem. Int. Ed.",
        "authors": "Teng, ...",
        "doi": "10.1002/anie.202507604"
      }
    }
  ]
}
```

## Cross-reference with PDF Library

- 结果包含 `zotero_parent_key`，可用于 search-papers 过滤
- 推荐工作流：
  1. search-notes 获取笔记结论 + zotero_parent_key
  2. 如需原文验证，用 key 过滤 search-papers
  3. 这样一次覆盖主文 + SI 的所有内容

## Output Guidelines

**展示格式**：
```
📓 来源笔记：{source_file}（第{note_rank}相关，相似度 {score}）
{content 摘要}
```

**注意事项**：
- 每篇笔记整篇入库，不存在同一篇返回多个碎片的问题
- content 是 Gemini 生成的结构化阅读笔记，不是 PDF 原文
- 如需原文验证，请调用 search-papers

## Error Handling

服务不可用时，按顺序执行：

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

端口被占用时（默认 18810）：
- Linux: `lsof -i :18810`
- macOS: `lsof -i :18810`
- Windows: `netstat -ano | findstr 18810`

笔记库为空时，运行构建脚本：`python service/build_notes_db.py`
