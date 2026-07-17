# ADR-001: 终端 Agent 优先（Terminal-First）的默认部署架构

**Status:** Proposed
**Date:** 2026-07-15
**Deciders:** 项目所有者
**输入:** 5 角度 OSS 生态调研（40+ 个项目，8 个 agent）＋ 3 视角独立设计小组（minimalist / agent-ux / maintainer）＋ 37 条多智能体逻辑审查发现（见 `docs/audits/2026-07-15-multi-agent-logic-review.md`）

---

## Context

项目起源是所有者的个人系统，历史默认路径是 Vertex AI（个人有 GCP 额度）。开源后的目标用户没有这个前提。所有者的硬约束：

> 主部署故事必须是「clone 仓库 → 全程在终端 agent（Claude Code / Codex）里使用，宿主 LLM 及其子代理完成模型工作」——默认路径零 API key。云后端降级为可选项。

当前与该约束冲突的现状（截至本 ADR，部分已修复）：

1. ~~`requirements-scanner.txt` 默认安装 `google-genai` + `google-cloud-storage`~~ ✅ 2026-07-15 已改为注释 opt-in
2. 两个 CLI 入口的 argparse fallback 是 `"vertex"`（`gemini_analyze_pdf.py:633`、`zotero_batch_scanner.py:477`），无 `.env` 的 fresh clone 直接命中最重的 GCP 后端
3. 检索链路要求两个常驻进程：Ollama daemon（嵌入）＋ 手动启动的 Flask sidecar（`127.0.0.1:18810`），skills 用 curl 调 HTTP
4. README 把零 API 路径埋在第 200 行以后

**生态调研的三个决定性事实：**

- **「宿主终端 agent 做模型工作」这个位置没有人占。** 40+ 个被调研项目里没有任何一个实现了 subagent/manifest 契约的等价物。这是本项目唯一的差异化资产。
- **赢家的形态是 stdio MCP + 进程内小嵌入模型。** zotero-mcp（4.3k stars）、knowledge-rag、mcp-local-rag、qmd 全部是「宿主 spawn 的 stdio MCP + ChromaDB/嵌入式向量库 + ONNX 小模型嵌入」；AnythingLLM（63k stars）的核心 onboarding 决策就是零依赖默认嵌入器。**没有一个成功项目要求用户先装 Ollama daemon 或手动常驻一个 HTTP server。**
- **反面教材同样清晰。** Aria（1.7k stars，硬绑 OpenAI key，2024 年停滞）；Khoj（Postgres+pgvector+常驻 server，公认摩擦过大）；OpenMemory（"local-first" 营销但要 Docker + 云 key）；Reor / Quivr（独立「第二大脑」App 形态在 2024–2026 间全部衰亡，活下来的都转型成 agent 消费的基础设施层）。

## Decision

**默认路径 = 「零 key、零 daemon、零常驻 server」：**

1. **生成侧**：`subagent` 后端为唯一默认（已是 config 默认；需修复 argparse fallback 与之对齐）。云后端（vertex / gemini-api / anthropic / openai）保留为注释掉的 opt-in 依赖 + 文档「质量升级层」。
2. **检索侧**：Flask sidecar 替换为 **stdio MCP server**（宿主 agent 按会话 spawn，退出即回收，无端口无 daemon）。检索逻辑抽成无传输依赖的 `service/rag_core.py`，MCP 与 Flask 都只是薄壳；Flask 保留一个版本作兼容回退。
3. **嵌入侧**：默认 provider 从 `ollama` 换成 **`fastembed`（进程内 ONNX，Apache-2.0，无 torch）**，默认模型选 zh+en 多语小模型（`paraphrase-multilingual-MiniLM-L12-v2`，~0.22 GB，首查时自动下载）。Ollama 降级为文档化的质量升级层（qwen3-embedding 系列）。集合 metadata 里写入 provider+model+dim，防止换模型后的静默维度错配。
4. **skills 层**：`search-literature` 的 WF1a–WF10 保留（这是检索工作流资产），curl 调用改为 MCP 工具调用；删除「服务不可用时先起 Ollama 和 Flask」的整段应急教程（该故障模式不复存在）。

## Options Considered

### Option A: 维持现状（Vertex 为实质默认，HTTP sidecar + Ollama）

| 维度 | 评估 |
|---|---|
| 复杂度 | 已存在（零迁移） |
| Onboarding 摩擦 | 高：GCP 服务账号 / Ollama 安装 / 手动起 server 三座大山 |
| 生态位 | 与调研中所有停滞项目（Aria、Khoj）同形态 |
| 与所有者约束 | **直接违背** |

**Pros:** 零工作量；所有者个人环境已验证。
**Cons:** fresh-clone 用户第一小时就流失；「本地优先」的 README 承诺与实际路径矛盾。

### Option B: 终端优先 — MCP stdio + fastembed + subagent 默认（选定）

| 维度 | 评估 |
|---|---|
| 复杂度 | 中：新增 ~250 行 MCP server + ~40 行 fastembed 分支；核心逻辑只做搬移不重写 |
| Onboarding 摩擦 | 低：clone → `pip install` → `init`（写 .mcp.json）→ 直接在 Claude Code 里问 |
| 生态位 | 占据「宿主 agent 做模型工作」的空位；形态与 4.3k-star 的 zotero-mcp 同构 |
| 迁移风险 | 低：rag_core 抽取是纯重构，Flask 壳保留一个版本；现有 800 篇笔记 / ChromaDB 数据不动 |

**Pros:** 三个设计视角独立收敛到同一方案；每一步可增量落地、可单独验证。
**Cons:** 多一个 `mcp` 依赖；fastembed 默认模型质量低于 qwen3-embedding:4b（以升级层弥补）。

### Option C: 激进重写 — 纯 MCP 包，删除全部云后端与 Flask

**Pros:** 最小表面积。
**Cons:** 丢掉 ProcessorBackend 可插拔资产与已有用户路径；maintainer 视角明确反对——现有 145 个测试和所有者生产环境都押在现结构上。**否决。**

## Trade-off Analysis

- **fastembed vs Ollama 默认**：默认值优化的是「第一次成功」，不是「最佳质量」。调研中所有高星项目都把小模型内嵌为默认、把重模型做成显式升级。维度错配风险用集合 metadata 戳记（B 案第 3 条）对冲。
- **MCP vs HTTP**：stdio MCP 把「server 生命周期」责任从用户移给宿主 agent——这正是 cookjohn zotero-mcp 用「藏进 Zotero 进程」解决的同一问题的终端版答案。查询日志等纯渲染逻辑（query_server.py 约 450 行）不进 MCP：minimalist 案主张直接删（宿主 agent 自己会写日志），maintainer 案主张搬进 rag_core 保留。**采用折中：先搬移保留，MCP 工具面只暴露 search/get/reindex/status，日志渲染标记 deprecated，观察一个版本后再删。**
- **pdfplumber 保留**：调研结论明确——docling/MinerU 质量更高但重量级（后者 16GB RAM），PyMuPDF4LLM 是 AGPL。pdfplumber（MIT、纯 Python、零模型）对「openai 后端的文本降级路径」这个用途仍是正确默认。不动。

## 不可触碰清单（三个设计视角一致）

1. **subagent/manifest/exit-200 宿主契约**（`scanner/backends/subagent.py`、`--resume` 循环、`list_pending_subagent_runs.py`）——唯一生态空位 + 测试覆盖最好的部分。围绕它重新定位，不要重构它。
2. **ProcessorBackend 契约 + 各后端 fail-fast ImportError**——正是 opt-in 依赖模式的支撑。
3. **两阶段流水线与 domain-pack 架构**（Stage A 前 3 页画像 → 模板路由 → Stage B）。
4. **去重设计**（stable+legacy 双哈希 ∪ vault 扫描 ∪ parent_key 兜底）——修 bug（见审计报告），不改设计。
5. **WF1a–WF10 检索工作流**——README 自称的 headline contribution，调研确认（beaver-zotero 验证了同类需求）。只换传输层。

## Consequences

- **变容易**：新用户 onboarding（4 条命令、零凭据）；跨平台故事（无 daemon）；「local-first」营销与实际一致。
- **变难**：嵌入质量分层需要文档解释；MCP + Flask 双壳期间要维护两个入口。
- **需要重访**：一个版本后决定是否删 Flask 壳与查询日志渲染；样本语料库（audit 指出无法验证 E2E）仍是缺口。

## Action Items

**P0 — 发布阻断（先修，见审计报告详情）：**
1. [ ] `gemini_analyze_pdf.py` 补回 `if __name__ == "__main__": main()`（当前整条流水线是静默 no-op）
2. [ ] 两个入口 argparse `--backend` fallback 改为 `PROCESSOR_BACKEND`（import config 的值），与「fresh clone 零凭据」承诺对齐
3. [ ] 统一 ledger 真值：worker 写入路径改用 `config.PROCESSED_HISTORY_PATH`（修 split-brain）
4. [ ] `migrate_combined_hash_to_stable.py` 模块级 `os.environ` NameError（缺 import）
5. [ ] `.env.example` 的 `$HOME` 默认值改为注释示例 + 双平台写法
6. [ ] 增加一条端到端冒烟测试：subprocess 方式跑 `gemini_analyze_pdf.py --backend subagent`,断言 exit 200 + manifest 落盘（这条测试本可抓住 P0-1/P0-2 两个回归）

**P1 — 终端优先迁移（本 ADR 主体）：**
7. [~] 抽取 `service/rag_core.py` — 2026-07-15 部分完成：`get_note_payload` / `search_papers_chroma` 已抽为无传输依赖函数（Flask 路由变薄壳）；完整 rag_core.py 拆分待做
8. [x] 新增 `service/mcp_server.py`（stdio；工具面：`search_notes` / `search_papers` / `get_note` / `index_status`；`reindex` 待做）+ `scripts/run_mcp_server.py` 跨平台启动器 + 仓库根 `.mcp.json`（Claude Code cd 即发现）
9. [x] `embedding_client.py` 增加 `fastembed` provider 并设为默认；集合 metadata 戳 provider/model
10. [~] 仓库自带 `.mcp.json` 已覆盖 Claude Code 的零注册；`init_environment.py` 写 Codex `config.toml` 待做
11. [~] skills 三件套已加「MCP 优先」路由指引（参数同名同义），Ollama 应急段落已标注仅限 ollama provider；HTTP 段落保留为回退（MCP 路径端到端验证后再删）
12. [~] README：Bootstrap 第 6 步改为 MCP 优先 + Flask 回退；Retrieval 段更新。首屏 headline 重写待做

**P2 — 观察期后：**
13. [ ] 决定查询日志渲染（~450 行）去留；决定 Flask 壳退役
14. [ ] FTS5 trigram 关键词索引 + RRF 混合检索（零模型检索地板，RAG-Assistant-for-Zotero 的教训）
15. [ ] 开放语料 E2E fixture；PaperQA2 式 RCS / 引文优先合成配方写进 WF 说明（零代码改动）

---

## 附录：生态调研要点（40+ 项目，2026-07 数据）

| 项目 | 角度 | 活跃度 | 对本项目的一句话教训 |
|---|---|---|---|
| zotero-mcp (54yyyu) | zotero | 4.3k★, 2026-07 活跃 | 直接蓝本：stdio MCP + sentence-transformers 进程内默认嵌入 + extras 分层依赖 + `setup` 自动写客户端配置 |
| PaperQA2 (Future House) | 文献 QA | 8.9k★ | numpy-by-default/重存储 opt-in 的零基建哲学；RCS（检索块先做查询条件化摘要再合成）值得进 WF |
| kotaemon | 文献 QA | 25.5k★ | 引文契约：每条结论带 inline citation + 相关度分 + 可跳转 PDF 精确位置 |
| RAGFlow | 文献 QA | 85k★ | 只学两点：chunk 质量主导检索质量；重基建形态不要学 |
| GPT Researcher | 文献 QA | 28k★ | 分发策略：把 MCP server 拆成小仓库专供 `claude mcp add` |
| AnythingLLM | local-first | 63k★ | 本领域最成功者的核心 onboarding 决策＝零依赖默认嵌入器 + 嵌入式向量库 |
| Khoj | local-first | 36k★ | 反例：Postgres+pgvector+常驻 server = 个人知识库过重基建 |
| Aria (lifan0127) | zotero | 1.7k★, 已停滞 | 反例：硬绑单一云厂商付费 API → 生态一动就死。正是本项目要摆脱的形态 |
| Reor / Quivr | local-first | 已归档/转型 | 独立「第二大脑」App 形态已死；活路是做 agent 消费的基础设施层 |
| knowledge-rag (lyonzin) | agent-native | 227★, 活跃 | service/ 层的目标形态几乎完全体：stdio MCP + ChromaDB + ONNX 小模型 |
| mcp-local-rag (shinpr) | agent-native | 342★ | skills-accompany-MCP 模式：MCP 提供原语，skill 提供查询策略层 |
| basic-memory | agent-native | 3.4k★ | 插件市场形态：MCP server + Claude Code plugin（skills/commands）双件套 |
| FastEmbed | infra | 3.1k★, Apache-2.0 | 结论：可以替掉 Ollama daemon——chromadb 1.5.5 已硬依赖 onnxruntime，边际成本极小 |
| pdfplumber | infra | 10.6k★, MIT | 维持默认：docling/MinerU 更强但重；PyMuPDF4LLM 是 AGPL；Marker 是 GPL+RAIL |
| MinerU / Docling | infra | 74k★ / 63k★ | zh 抽取质量天花板，但 16GB RAM / 重装机不配当默认；作为文档化的可选升级 |

完整调研数据（40+ 项目全字段）与三视角设计小组原始提案存档于 workflow run `wf_ae9ff5f3-5a8`。
