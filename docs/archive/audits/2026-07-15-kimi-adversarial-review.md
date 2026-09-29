# Kimi 对抗性挑刺审查 — 架构 / 精简度 / 速度（2026-07-15）

7 个只读 Kimi Code 会话（6 个首轮 + 1 个窄域重试），经 kimi-supervision 框架监督执行。
每条发现均要求文件:行号证据。本文档记录结论、当日已修复项、以及进入路线图的遗留项。

## 会话与结论

| 会话 | 主题 | 结果 |
|---|---|---|
| rr-arch | 架构第一性原理 | 首轮未产出最终报告（只有开场白）；窄域重试 rr-arch2 补齐 |
| rr-arch2 | subagent 协议是否有更简单等价设计 | **结论：没有**。对比了 stdin/stdout 流、宿主直接 import、文件队列+daemon 三个方案——真正不可替代的是「双角色协议」（scanner 独占状态机，子代理退化为无状态 JSON worker，防止子代理递归驱动流水线）；manifest+exit 200 只是这个分离的最简持久化实现。与 ADR-001 的不可触碰清单互证。 |
| rr-scanner-simplify | scanner 逻辑精简 | 可用。~15 项：约 60 行死代码/shim 可直删；frontmatter 正则、vault 遍历、post-publish 解析等 4 组重复实现（约 120 行可合并）；`run_multifacet_spec_pipeline`（132 行）应拆 4 个阶段函数；`migrate_combined_hash_to_stable.py`（378 行一次性迁移脚本）应移出主包；`kimi_fallback` 动作是维护者私有集成，开源默认不应包含 |
| rr-service-simplify | service 逻辑精简 | 可用（拼音输出——GBK 规避行为）。查询日志渲染约 270+ 行不属于检索服务器；build_pdf_db 与 ingest_textbook 高度重复（约 80-100 行）且 **ingest_textbook 硬编码 OllamaEmbeddingFunction 绕过 provider 抽象**；三个 ledger 格式不统一；/search_notes 与 /search_papers 端点样板可合并 |
| rr-scanner-speed | scanner 运行速度 | 可用。**单 PDF 论文在批量 subagent 模式下被完整 SHA-256 读盘 6 次**（normalize 1 + prefilter 2 + run-dir 1 + worker 2；800 篇×20MB ≈ 96GB I/O，其中 80GB 冗余）；8KB 读块过小；DedupIndex 每个子进程做 800 次 Path.exists()；同一 PDF 被 pypdf 完整解析 1-3 次（preflight/split/slicer 各自 PdfReader）；子进程启动开销 0.5-3s×篇 |
| rr-service-speed | service 运行速度 | （执行中，完成后补录） |
| rr-state-flow | 状态管理与数据流 | 可用。持久化状态 10 种（5 种文本/JSON ledger 可合并为单 SQLite）；KEEP-IN-SYNC 哈希副本已出现真实行为漂移（service 端静默跳过缺失文件，scanner 端抛异常）；frontmatter 有 9 个解析点、3 处纯 regex 不过 yaml；**doctor.py 后端默认值仍是 "vertex"**；**migrate 脚本不经过 .env hydration**；.env.example Zotero 路径仍用 $HOME |

## 当日已修复（本次提交）

1. `scanner/_hashing.py`：`get_file_hash` 增加进程内 memo（键 = abspath+size+mtime_ns），读块 8KB → 1MB。主进程内 6 次哈希坍缩为每文件 1 次读盘（worker 内再 1 次），800 篇×20MB 场景冗余 I/O 从 ~80GB 降到 ~16GB 级
2. `service/ingest_textbook.py`：硬编码 `OllamaEmbeddingFunction` → `get_chromadb_embedding_function()`（否则 fastembed 默认下教材会用不同模型入库同一集合）
3. `scanner/doctor.py`：后端默认值 "vertex" → "subagent"（与 config 对齐；同类漂移第三处）+ 测试同步修正
4. `scanner/migrate_combined_hash_to_stable.py`：import config 触发 .env hydration
5. `.env.example`：Zotero 路径的 $HOME 残留改为注释化跨平台示例

## 进入路线图（未修，方向已认可）

- **P2-精简**：删除 scanner 死代码/shim（~60 行）；合并 4 组重复实现进 `scanner/_common.py`；拆分 `run_multifacet_spec_pipeline`；查询日志渲染迁出 query_server（与 ADR P2-13 合并决策）；`migrate_*` 移入 `scripts/archive/`；`kimi_fallback` 中性化或移出默认
- **P2-合库**：build_pdf_db + ingest_textbook 合并为单入库脚本；三个 ledger 统一 `key\thash` 格式乃至单 SQLite（state-flow 的清单表为迁移蓝本）
- **P2-速度**：preflight/slicer 复用同一 PdfReader；DedupIndex 子进程免 exists() 全检；批量模式经 CLI 传递已算 hash 给 worker（消掉 worker 内 2 次）
- **风险确认后再动**：service 端 `get_combined_hash` 对缺失文件静默跳过 vs scanner 抛异常——对齐会改变含缺失文件组的既有 hash，需迁移方案；frontmatter 统一解析器（9 处收敛）
