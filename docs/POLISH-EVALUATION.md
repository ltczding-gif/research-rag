# research-rag — 优雅化评估

> 在 6 个 commit（feishu 删除 / 后端抽象 / OpenAI 后端 + 各自的 review 修复）之后，
> 系统已经"工作"。本文盘点剩下让它"优雅"还需要做的事，分 Tier 1（清晰可执行）/
> Tier 2（需要设计决策）/ Tier 3（更大的工程）。

调查日期：2026-05-08

---

## TL;DR

距离"开源就绪"的关键缺口（按价值排序）：

1. **删除 `--pipeline-mode legacy`**（Tier 1）—— 死路径、bug 来源、拖累 ~200 行
2. **集中后端工厂**（Tier 1）—— 当前 `make_backend_from_args` 和 `make_backend()` 双层 dispatch
3. **统一 `combined_hash` 算法**（Tier 2）—— scanner 排序、indexer 不排序，长期维护负担
4. **修复 sub-agent 两阶段流**（Tier 2）—— 当前只能写 Stage A manifest，无法完整跑通
5. **最小烟雾测试**（Tier 2）—— 没有任何测试的代码不可信
6. **双语 SKILL**（Tier 3）—— 公开发布前需要英文 mirror

后续按顺序执行；本文档配套的清理 commit 处理 Tier 1。

---

## Tier 1 — 清晰可执行（无设计争议）

### 1.1 删除 `--pipeline-mode legacy` 路径

**位置**：`scanner/gemini_analyze_pdf.py`、`scanner/zotero_batch_scanner.py`

**现状**：
- `--pipeline-mode` 接受 `legacy` 或 `multifacet-spec`
- legacy 走 ~120 行的 `DEFAULT_PROMPT` 中文 freeform 提示 + `client.models.generate_content` 直调
- legacy 仅限 `--backend vertex`（其他后端会显式 sys.exit(1) 报错）
- legacy 是 `bucket` NameError 的来源（已在 `aaa0441` 修复）
- investigation/01 报告确认："Legacy `DEFAULT_PROMPT` is dead code in multifacet-spec mode"

**为什么删**：
- 死路径，多后端时代它存在的唯一意义是"对历史用户兼容"——但本仓库是 staging 副本，没有历史用户
- `DEFAULT_PROMPT` ~120 行死提示词
- legacy 路径里的 main() 分支 ~30 行 + 后续 RAG metadata extraction ~80 行
- 几个 helper 函数（`parse_post_publish_actions` 的 legacy 分支等）也跟着可以简化

**删除范围**：
- `--pipeline-mode` flag 从两个 CLI 移除
- `DEFAULT_PROMPT` 常量删
- main() 里 `if args.pipeline_mode == "multifacet-spec":` 检查及其 else 分支删；保留检测路径作为唯一路径
- `parse_post_publish_actions` 简化（去掉 legacy 模式下的 default 行为差异）
- `split_batch_post_publish_actions` 同上

**影响**：~200 行删除。多后端用户体验从"必须记得加 `--pipeline-mode multifacet-spec`"变成"什么都不加就工作"。

**风险**：低。该路径已知不被任何后端使用，且与正确的 multifacet-spec 路径并行。

**保留**：`legacy_combined_hash`（不同概念！）—— 是 SHA-256 双变体兼容，照顾 2026-03 之前的旧笔记，不能删。

---

### 1.2 集中后端工厂

**现状**：
- `scanner/backends/__init__.py` 有 `make_backend(name, **kwargs)` —— 通用 dispatch
- `scanner/gemini_analyze_pdf.py` 有 `make_backend_from_args(args, run_dir=None)` —— 从 args 解析 env vars 然后调 `make_backend`
- 添加新后端要改 3 个地方：`__init__.py`、`make_backend_from_args`、CLI choices

**改进**：
- 把 `make_backend_from_args` 的 env-var-resolution 逻辑迁移到 `backends/__init__.py` 作为 `make_backend_from_env(name, **overrides)`
- 每个后端的 env var 列表可以放在它自己的模块里（`@classmethod from_env()`）
- 添加新后端 = 一个 PR 一个目录

**影响**：~50 行重组，零功能变化。

**风险**：极低，纯重构。

---

### 1.3 review 报告归档到 `docs/audits/`

**现状**：`$REPO_ROOT/reviews\` 顶层目录，仅有 2 份审计 markdown。

**问题**：与运行时代码并列，不像历史档案。

**改进**：移到 `docs/audits/`，添加 README 解释这些是"快照式审计"。

**影响**：纯文件移动 + 一个 README。

---

### 1.4 INVESTIGATION-INDEX 现状校准

**现状**：`docs/INVESTIGATION-INDEX.md` 描述的是"调查时刻"的状态——但调查至今已经过 6 个 commit，仓库结构变了：
- README.md 在顶层（不是 README.draft.md）
- 顶层多了 STATUS.md
- 顶层有 `setup.sh / setup.ps1 / requirements-*.txt / .env.example / .gitignore`
- "数据规模快照"段已经过时（笔记和 chunks 数仍然是原系统的，不是 staging 仓库的）

**改进**：把"现状"重写成"调查发起时的原系统快照 + 后续在 staging 里发生了什么"，给读者一条阅读时间线。

---

### 1.5 docs/PACKAGING-PLAN.md 局部更新

**现状**：仍然描述"待清理 30+ 实验脚本"等历史状态——这些实际上从一开始就没有被复制到 staging 仓库。读起来像 todo，实际上已经永久避免了。

**改进**：开头加一段"本仓库当前状态"——明确哪些是已实现的、哪些还是 roadmap。表格里 "B16: 30+ 实验脚本含用户数据" 应该标 ✅ 已规避。

---

## Tier 2 — 需要设计决策

### 2.1 `combined_hash` 算法分歧

**现状**：
| 文件 | 算法 |
|---|---|
| `scanner/zotero_batch_scanner.py` | `sha256(sorted([sha256(file_bytes) for f in group]))` —— **stable variant**，主文+SI 顺序无关 |
| `scanner/gemini_analyze_pdf.py:get_legacy_combined_hash` | 按 path 排序后哈希 —— **legacy variant**，路径顺序相关 |
| `service/build_pdf_db.py:get_combined_hash` | 按声明顺序读字节 + 累计 hash —— **path-order-preserving**，第三种 |

`scanner` 接受双变体（stable + legacy）做匹配；`service` 用第三种。这意味着 ledger 里的同一篇论文可能有 3 种 hash。

**两条路**：

**A. 统一到 stable variant**（推荐）
- 提取 `_hashing.py` 共享模块
- `service/build_pdf_db.py` 改用同一函数
- 旧 ledger 里的非 stable hash 通过迁移脚本一次性补齐（`migrate_combined_hash_to_stable.py` 已存在）
- **代价**：新装的用户需要重跑 build_pdf_db 一次（已有 hash 仍然有效但语义统一）

**B. 文档化现状，接受双变体永远存在**
- 加注释说明 scanner 写多种 hash 到 ledger 以匹配多种生成路径
- 加一个 hash audit 工具
- **代价**：永久维护负担、docs 复杂

**建议**：A。stable variant 是正确设计。

---

### 2.2 sub-agent 两阶段流不可用

**现状**（详见 review 报告 I1）：
- `subagent` 后端的 `call_model` 在 Stage A（profiler）就 raise `SubagentManifestPending`
- main() 捕获后退出
- **Stage B（note generator）从来没机会跑**——它的 user prompt 依赖 Stage A 的 document_profile 输出
- 所以现在 sub-agent 模式只能写一个 manifest 就结束，永远生成不出笔记

**两条路**：

**A. 实现 `--resume <run_dir>` 半自动流**
- 第一次跑：写 Stage A manifest 后退出
- 用户在 Claude Code 里 dispatch 一个 sub-agent 去填 Stage A
- 用户重跑 `python scanner/gemini_analyze_pdf.py --resume <run_dir>`
- 程序读 Stage A 输出 → 渲染 Stage B prompt → 写 Stage B manifest 后退出
- 用户再 dispatch sub-agent 填 Stage B
- 用户再 `--resume` —— 程序读 Stage B 输出 → 渲染笔记 → 落盘
- **代价**：3 次往返 + 实现 `--resume` 状态机

**B. 让 sub-agent 自己跑两阶段**
- Manifest 写一次，里面包含 Stage A schema、Stage B 的 user prompt 模板（带 `{{document_profile}}` 占位符）、两个 expected output path
- 调用 sub-agent 时 prompt 改成："先按 Stage A schema 生成 profile JSON 写到 expected_output_a；读 profile，把 Stage B user prompt 里的 `{{document_profile}}` 替换为该 profile 的 JSON 序列化；按 Stage B schema 生成 note JSON 写到 expected_output_b。"
- 主程序后续读两个文件、finalize 笔记
- **代价**：1 次往返；sub-agent prompt 复杂度增加

**建议**：B。一次往返对 batch 场景更友好。

---

### 2.3 烟雾测试

**现状**：零测试。所有改动靠 `ast.parse` 验证语法，运行时正确性靠人工。

**最小集**：
```
tests/
  test_backend_smoke.py      # 每个后端实例化 + mock SDK + call_model 一次
  test_make_backend.py       # 工厂 dispatch + 别名
  test_subagent_manifest.py  # 验证 manifest JSON schema
  test_pdf_chunking.py       # build_pdf_db 切块的 800/700/100 边界
```

每个测试 < 50 行。用 `pytest` + `pytest-mock`，不需要真实 API key。

**价值**：刹住"改动后是否还能跑"的不确定性，code review 也更聚焦设计而非"会不会崩"。

---

## Tier 3 — 更大的工程

### 3.1 双语 SKILL.md

中文 canonical + 英文 mirror，每个 user-facing skill 出 `SKILL.md` (英文) + `SKILL.zh.md` (中文)。
infra skill（rag-engineer / vector-database-engineer / embedding-strategies）已经是英文。
工作量：~4 个 SKILL × 翻译工作量。

### 3.2 logging 取代 print

当前所有错误/警告都用 `print(file=sys.stderr)`。改成 `logging` 模块：
- 集中级别控制（`LOCALRAG_LOG_LEVEL`）
- 可配置 handler（文件、stdout、syslog）
- 测试时可静默
工作量：~50 处替换。

### 3.3 Docker compose

`Dockerfile` + `docker-compose.yml` 一键拉起 Ollama + ChromaDB 服务 + query_server。
工作量：中。需要测试镜像构建。

### 3.4 service/ 包化

当前 `service/` 不是 Python 包（无 `__init__.py`）。脚本之间 `from config import ...` 依赖运行时 cwd=service/。改成包后可以 `python -m service.query_server` 调用，更稳。

---

## 已知 Bug（不属于"优雅"但应该顺手修）

来自第一次 review 报告，标记为 P1 deferred 的：

1. **`get_parent_key()` 文件名子串匹配**（`scanner/gemini_analyze_pdf.py`）
   - SQL: `WHERE ia.path LIKE '%<filename>%'`
   - 重名附件会撞车，返回错误的 zotero_parent_key
   - 修复：用 attachment item key 精确匹配

2. **PDF 重摄入旧 chunk 不删**（`service/build_pdf_db.py`）
   - 论文更新（hash 变）→ 旧 chunk 留在 ChromaDB → 检索时返回过时内容
   - 修复：`col.delete(where={"group_hash": old_hash})` before add

3. **GCS bucket 永不清理**（`scanner/cleanup_gcs_archive.py`）
   - bucket 名带 `temp` 但没人调 cleanup
   - 修复：scanner 跑完后自动调；或加 cron job 文档

4. **`prefs.js` 自动检测竞态**（`scanner/zotero_batch_scanner.py`）
   - 读 Zotero 配置时 Zotero 进程可能在写 → 偶尔读到部分内容
   - 修复：先 `psutil` 检查 zotero.exe 存活，存活则报错让用户先关

---

## 架构思考（不必现在做）

**`gemini_analyze_pdf.py` 仍然 2300 行**

Phase 7 的 backend 抽象已经把模型调用抽离了，但 pylint 还会抱怨这个文件。它现在的职责：
- PDF preflight + 切片
- model 路由决策
- 后端构造（`make_backend_from_args`）
- 主流程（preflight → backend.attach_pdfs → run_multifacet_spec_pipeline）
- frontmatter 渲染 + validation
- 后处理（prefill、kimi、review_queue）
- run artifact 写入

合理拆法（如果要做）：
```
scanner/
├── pipeline.py          # 高层 orchestration（200-300 行）
├── preflight.py         # PDF preflight + 切片（150 行）
├── routing.py           # model_routing_policy 解析（150 行）
├── rendering.py         # frontmatter 渲染 + validation（400 行）
├── post_publish.py      # 后处理 actions（300 行）
├── runs.py              # run artifact 管理（150 行）
└── gemini_analyze_pdf.py # CLI entry point + main() 编排（200 行）
```

**代价**：大型重构。**收益**：每个文件可独立 review、独立测试。

**判断**：如果有 N+1 个新功能要加（比如新增 backend、新增 post_publish action），值得做。如果系统稳定不再扩展，没必要。

---

## 推荐执行顺序

| Step | 内容 | Tier | 行数变化 |
|---|---|---|---|
| 1 | 删除 `--pipeline-mode legacy`（含 DEFAULT_PROMPT） | T1 | -200 行 |
| 2 | 集中后端工厂到 `backends/__init__.py` | T1 | ~0 |
| 3 | `reviews/` → `docs/audits/` + README 解释 | T1 | ~+30 行 |
| 4 | INVESTIGATION-INDEX + PACKAGING-PLAN 现状校准 | T1 | ~+30 行 |
| 5 | 修 `get_parent_key()` 子串 bug | bug | ~+20 行 |
| 6 | 修 PDF 重摄入 stale chunks | bug | ~+10 行 |
| 7 | 实现 sub-agent 两阶段（方案 B：单 manifest） | T2 | ~+100 行 |
| 8 | 统一 combined_hash 算法（方案 A：stable） | T2 | ~+50 行 |
| 9 | 烟雾测试套件 | T2 | ~+300 行 |
| 10 | 双语 SKILLs | T3 | 翻译工作 |
| 11 | logging 重构 | T3 | ~0 |
| 12 | Docker | T3 | ~+100 行 |

每一步独立 commit，独立 review-able。

---

## 当前 commit 配套的清理动作

伴随本评估文档的 commit 顺手处理：

- ✅ `reviews/` → `docs/audits/`
- ✅ `docs/INVESTIGATION-INDEX.md` 现状校准
- ✅ `docs/PACKAGING-PLAN.md` 现状校准
- ✅ STATUS.md 加"什么仍然不优雅"段，引用本文档

需要单独执行（每步一个 commit）：
- ⏳ Step 1（删 `--pipeline-mode legacy`）
- ⏳ Step 2（集中后端工厂）
- ⏳ Step 5-6（小 bug 修）
- ⏳ Step 7-9（设计选择 + 测试）

剩下的（Tier 3）等用户决定优先级。
