# research-rag — 调查与演进时间线

本目录最早是对原系统（personal Windows 上的 `.localrag` + `.agents/skills/...`）的
反向工程产出。staging 仓库 `$REPO_ROOT/` 后来从这些调查报告 fork 出来，并在 6+
个 commit 中演进。本文档是一条阅读时间线。

## 当前仓库结构

```
$REPO_ROOT/                       ← staging 仓库的根
├── README.md                          ← 公开入口
├── STATUS.md                          ← 已做 vs 待做的诚实清单
├── .env.example, .gitignore           ← 配置契约
├── setup.sh, setup.ps1                ← 跨平台引导
├── requirements-rag.txt, requirements-scanner.txt
│
├── service/                           ← Flask 边车 + ChromaDB build/ingest
├── scanner/                           ← Zotero -> note generator
│   └── backends/                      ← 5 个可插拔后端（vertex/gemini/anthropic/openai/subagent）
├── skills/                            ← 给 Claude Code 用的 SKILL.md
│
└── docs/
    ├── ARCHITECTURE.md                ← 系统架构（10 分钟读完）
    ├── COMPONENTS.md                  ← 组件级参考手册
    ├── PACKAGING-PLAN.md              ← 开源化路线图
    ├── POLISH-EVALUATION.md           ← 优雅化清单（哪些不优雅，怎么修）
    ├── INVESTIGATION-INDEX.md         ← 当前文件
    │
    ├── investigation/                 ← 原始调查报告（历史快照，2026-05-08）
    │   ├── 01-note-generation-pipeline.md
    │   ├── 02-build-and-index.md
    │   ├── 03-query-server.md         ← 含原系统的 Feishu 段（已删除，见 STATUS Phase 6）
    │   ├── 04-skills-layer.md
    │   └── 05-packaging-portability.md
    │
    └── audits/                        ← Sub-agent code review 报告（每次重大 commit 一份）
        ├── README.md
        ├── 2026-05-08-backend-pluggability-review.md
        └── 2026-05-08-openai-backend-review.md
```

## 阅读顺序建议

| 你的目的 | 顺序 |
|---|---|
| **了解系统怎么工作** | README → ARCHITECTURE → COMPONENTS（按需深入 investigation/） |
| **了解还有哪些不优雅** | POLISH-EVALUATION → STATUS（按 Phase 顺序） |
| **看历史决策的依据** | investigation/0X-* 原始调查 + audits/ review 报告 |
| **找某个具体组件细节** | COMPONENTS（速查表）→ investigation/0X-*（完整原始报告） |
| **准备开源发布** | PACKAGING-PLAN → POLISH-EVALUATION → STATUS |

## 三层文档

- **整合层**（`README.md`、`STATUS.md`、`docs/ARCHITECTURE.md`、`docs/COMPONENTS.md`、`docs/PACKAGING-PLAN.md`、`docs/POLISH-EVALUATION.md`）：系统视角，描述当前状态。会随仓库演进。
- **调查层**（`docs/investigation/`）：原系统反向工程，2026-05-08 一次性产出的 5 份子代理报告。**历史快照，不维护**。
- **审计层**（`docs/audits/`）：每次重大 commit 的 sub-agent code review 报告。**历史快照，不维护**——发现的问题修在代码里、记在 STATUS.md 里，不改 audit 报告本身。

## 关于飞书功能（已删除）

原系统有一个 `/write_to_feishu` 端点和飞书 OAuth + 周文档登记机制，且原始
`.localrag/` 下 12 个开发实验脚本明文带 app secret。**本仓库已把整段飞书功能从代码、
SKILL、配置、文档里删除**——详见 `PACKAGING-PLAN.md` 的 "✅ 飞书功能已删除" 段，以及
[STATUS.md](../STATUS.md) 的 Phase 6。

`investigation/03-query-server.md`、`04-skills-layer.md`、`05-packaging-portability.md`
里关于 Feishu 的描述是**对原系统**的审计记录，作为历史证据保留不动；它们不反映
本仓库当前状态。

## 数据规模快照（原系统，调查时刻）

> 这些数字描述**原系统**（`$LOCALRAG_HOME\` + `.agents\skills\...`），不是
> staging 仓库本身。staging 仓库不带任何 ChromaDB / 笔记 / ledger 数据——它只有
> 代码、SKILL、文档。

| 项目 | 值（2026-05-08 原系统） |
|---|---|
| 笔记总数 | 794 篇（`$LOCALRAG_NOTES_DIR\*_review_note.md`） |
| `papers` collection chunks | 64,474（900 组论文 + 2 本教材） |
| `processed_history.txt` 条目 | 1183 |
| `processed_groups.txt` 条目 | 1121 |
| `processed_notes.txt` 条目 | 1104 |
| 嵌入模型 | `qwen3-embedding:4b` via Ollama |
| ChromaDB 版本 | 1.5.5（Rust bindings，Python 3.11 venv） |
| 查询服务端口 | 18810 |

## 演进时间线

| Phase | Commit | 内容 |
|---|---|---|
| 0（baseline） | `caf0777` | 从原系统 fork staging；初始 commit 含 6 个源代码 + 5 个 SKILL + 调查报告 |
| 1 | `e8f6da4` | 删除飞书功能（端点 + helper + secrets + SKILL 相关段） |
| 2 | `5560d92` | 后端抽象层 + 4 后端（vertex / gemini-api / anthropic / subagent） |
| 3 | `aaa0441` | 修复 sub-agent review 发现的 3 Critical + 4 Important（bucket NameError、顶层 google import、_bucket 私有访问、port mismatch、auth doc drift） |
| 4 | `8d2aed8` | OpenAI / OpenAI-compatible 后端（DeepSeek / Mistral / OpenRouter / vLLM / Ollama / LM Studio） |
| 5 | `e982d0a` | 修复 OpenAI review 的 2 Critical + 5 Important + 2 Minor（empty choices guard、env reads、aliases、translation tightening、truncation warning） |
| 6 | （本 commit） | docs/audits 归档 + INVESTIGATION-INDEX 校准 + POLISH-EVALUATION 评估 |

完整 STATUS.md 里有每个 Phase 的细节。
