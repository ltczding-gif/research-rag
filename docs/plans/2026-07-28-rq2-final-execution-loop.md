# `rq-2` 最终执行 Loop

- **Status:** Final review
- **Date:** 2026-07-28
- **Branch:** `codex/wave1a-canonical-ir`
- **Pull request:** #4
- **Scope:** 实现、生成 20 篇笔记、运行 `rq-2` 全策略扫描、产出晨报
- **After approval:** 不再追加方案确认；直接执行本文件，完成后停止，不自动进入 `rq-5`

本文件是 ADR-003 和《`rq-2` 隔夜全策略扫描设计》的执行合同。项目所有者只做最后一次
检查；收到 `开跑` 后，父代理持续推进，只有真实阻塞项才会中止。

## 1. 完成标准

必须同时满足以下条件，才算本轮完成：

1. 新增的离线测试全部通过，仓库既有测试无回归；
2. 20/20 benchmark PDF 和全部 official SI 通过来源、哈希和解析 preflight；
3. 20/20 笔记由仓库 subagent backend 生成，全部强制
   `generic-research-note`，全部通过交叉审计并冻结；
4. ResearchQA evidence-group 映射总覆盖率不低于 95%，每篇不低于 90%；
5. 每个可排名候选完成相同的 20 篇、254 个问题和适用 reference groups；
6. 7 个 PDF chunker、4 个 note chunker、3 个 retriever、5 个 source
   composition、4 个 rerank depth 和最多 16 个最终交叉组合按批准顺序完成；
7. 生成逐配置 raw result、checkpoint、聚合表、Pareto frontier 和晨报；
8. 结果、代码、模型、数据和硬件指纹齐全，provisional winner 可复现；
9. 代码和允许公开的聚合产物提交并推送到现有 PR #4；
10. 停止，不自动启动 `rq-5`。

## 2. 实现边界

### 2.1 新增模块

| 文件 | 单一职责 |
|---|---|
| `benchmarks/researchqa_sources.py` | URL 规范化、下载校验、source manifest、PDF/DOCX/XLSX/CSV 原生坐标 IR |
| `benchmarks/researchqa_notes.py` | note task manifest、通用模板强制、引用解析、审计与冻结 |
| `benchmarks/researchqa_chunking.py` | 7 个 PDF chunker、4 个 note chunker、稳定 chunk ID 与 source spans |
| `benchmarks/researchqa_retrieval.py` | exact dense cosine、BM25、RRF、5 种 source composition、reranker adapter |
| `benchmarks/researchqa_scoring.py` | evidence-group 映射、指标、宏平均、paired bootstrap 和决胜规则 |
| `benchmarks/overnight.py` | 状态机、预算、checkpoint、重试、阶段排名和晨报编排 |
| `benchmarks/scripts/run_researchqa_overnight.py` | `prepare`、`canary`、`run`、`report`、`status` CLI |
| `scripts/run_rq2_overnight.ps1` | 固定 Python 路径和一键启动入口 |
| `benchmarks/configs/rq2-overnight.yaml` | 本轮唯一策略、模型、预算、缓存和 gate 配置 |

同时扩展 benchmark schema，新增 source record、native coordinate、note task 和 run state
schema。测试 fixtures 只放最小、可再分发的 PDF/DOCX/XLSX/CSV 样例；ResearchQA 原文、SI、
模型、向量和逐题结果都留在 F 盘 ignored cache。

### 2.2 依赖与运行时

- 通用下载、解析和笔记编排只使用：
  `C:\Users\Link\AppData\Local\Programs\Python\Python311\python.exe`
- embedding、向量计算和 reranker 只使用：
  `C:\Users\Link\.localrag\venv\Scripts\python.exe`
- benchmark 基础依赖保留在 `requirements-benchmark.txt`；
- live 模型依赖单列 `requirements-benchmark-live.txt`，不污染普通产品安装；
- BM25 在仓库内实现，避免为一小段确定性算法增加运行时依赖；
- `rq-2` 使用缓存后的 NumPy 矩阵做 exact cosine，不使用 Chroma HNSW。这样切分策略比较
  不引入近似索引随机性，产品现有 Chroma 路径保持不变；
- dense 固定 `qwen3-embedding:4b` digest
  `df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907`；
- reranker 固定 `Qwen/Qwen3-Reranker-0.6B` revision
  `e61197ed45024b0ed8a2d74b80b4d909f1255473`；
- Hugging Face、embedding、IR、indexes、runs 全部显式落到
  `F:\research-rag\benchmarks\.cache\researchqa\`。

每个 PowerShell 脚本开头固定声明：

```powershell
$PYTHON = "C:\Users\Link\AppData\Local\Programs\Python\Python311\python.exe"
$PYTHON_RAG = "C:\Users\Link\.localrag\venv\Scripts\python.exe"
```

## 3. 笔记子代理 Loop

现有三次调用协议保持不变：

```text
scanner pass 1
  -> exit 200
  -> manifest-profiler.json
  -> fresh subagent writes 01-document-profile.json

scanner pass 2: execute parent_agent_task.resume_command exactly
  -> exit 200
  -> manifest-note_generator.json
  -> fresh subagent writes 02-note-draft.json

scanner pass 3: execute parent_agent_task.resume_command exactly
  -> exit 0
  -> render Markdown and update benchmark note ledger
```

为本 benchmark 做三个窄扩展：

1. 新增显式 `--note-template generic-research-note`。即使 Stage A profiler 给出其他建议，
   Stage B 也只能加载仓库内通用模板；domain pack 的 seed、trap scan 和领域评分轴不得进入
   prompt；
2. manifest 增加 `source_artifacts`。PDF 继续通过 `pdf_paths` 读取；DOCX/XLSX/CSV 先由
   父代理生成带原生坐标的只读 source packet，子代理同时读取，不制造派生页码；
3. benchmark note ledger 独立于用户本机生产 ledger，避免把实验笔记写进私人运行状态。

20 篇论文按 `PDF 页数 + SI 文件数 + 非 PDF SI 权重` 稳定排序后，使用三条生成队列均衡
分配。每篇仍是独立 run directory、独立上下文、独立 manifest。三名子代理之间轮换审计：

```text
generator A -> auditor B
generator B -> auditor C
generator C -> auditor A
```

审计失败只退回该篇及失败字段；不得重做已通过论文。结构错误最多重派两次，第三次转为
`blocked`。生成者不得审计自己的笔记。20 篇全部通过后写 `frozen-notes.jsonl`，记录每篇
Markdown、draft JSON、source manifest 和 audit record 的 SHA-256；此后策略扫描只读。

## 4. 自动执行状态机

实际 runner 的主循环固定为：

```python
while not all_completion_criteria_verified():
    state = load_and_verify_state_and_artifact_hashes()

    if not state.sources_ready:
        prepare_and_validate_all_sources()
        checkpoint_atomically("sources")
        continue

    if not state.notes_frozen:
        emit_pending_note_manifests(max_active=3)
        dispatch_fresh_subagents_by_manifest()
        run_each_parent_resume_command_exactly()
        rotate_independent_note_audits()
        validate_native_citations_and_freeze_notes()
        checkpoint_atomically("notes")
        continue

    if not state.evidence_gate_passed:
        map_researchqa_evidence_groups()
        enforce_overall_95_and_per_paper_90_percent_gates()
        checkpoint_atomically("evidence-map")
        continue

    for stage in (
        "pdf-chunker",
        "note-chunker",
        "retriever",
        "source-composition",
        "reranker",
        "top2-confirmation",
    ):
        for config in pending_complete_candidates(stage):
            if not fits_current_unattended_window(config):
                checkpoint_and_yield_to_next_15m_continuation()
                break
            run_config_on_all_20_papers_and_254_questions(config)
            validate_same_evaluable_set_and_artifact_hashes(config)
            checkpoint_atomically(stage, config)
        rank_only_complete_candidates(stage)

    write_morning_report_even_if_partial()
    stop_without_starting_rq5()
```

这不是全笛卡尔积。每阶段固定其他变量做正交扫描，最后只运行：

```text
Top-2 PDF chunker
× Top-2 retriever
× Top-2 source composition
× rerank off / best depth
= 最多 16 行、仅执行唯一且兼容的确认组合
```

`hierarchical-pdf` 必须使用 `pdf-parent-child`，因此若它进入 source
composition Top-2，两个固定 PDF chunker 分支会归一为同一策略。该情形必须在执行前稳定
去重（本轮为 12 个唯一确认组合），不得重复计分或用不同 ID 伪装成 16 个实验。

`note-whole` 只做产品现状诊断，不参与 PDF span 主排名。相同 config fingerprint 直接复用
已有 artifact，不重复 embedding 或评分。

## 5. Checkpoint、恢复与预算

状态目录：

```text
benchmarks/.cache/researchqa/runs/<run_id>/
  run-state.json
  sources/
  note-runs/
  frozen-notes/
  evidence-map/
  chunks/
  embeddings/
  indexes/
  raw-results/
  scores/
  report/
```

规则：

- 原子任务 ID：
  `run_id + stage_id + paper_id + config_id + input_fingerprint`；
- artifact 先写临时文件，schema 和 SHA-256 校验通过后原子替换；
- resume 同时验证 code、source、note、model 和 config 指纹，不能只看文件是否存在；
- 网络、Ollama 短暂失败最多重试 3 次，退避 5/20/60 秒；
- schema、citation、hash、mapping 等确定性错误不循环重试；
- 单篇阻塞时继续处理其他独立论文，但包含缺失论文的候选标记 `incomplete`，不得排名；
- 默认 10 小时只作为单次无人值守运行窗口和候选启动参考，不是完成时限，也不得据此
  删除问题、缩减语料、跳过策略或降低审计粒度；
- 当前窗口不足以按同阶段移动平均完成一个完整候选时，先原子落盘并交给下一次 15 分钟
  continuation 续跑；完成条件仍是 20/20 笔记和全部批准策略有可审计终态；
- 无论成功、预算到期、部分完成、进程中断或真实失败，都必须先落盘状态并生成报告。

## 6. 测试与自动 Gate

收到 `开跑` 后不再请求阶段确认，按以下 gate 自动前进：

### Gate A：离线实现

新增测试覆盖：

- ResearchQA URL 规范化和严格 TLS；
- stable source ID、SHA-256、source role；
- PDF 页、DOCX 段落/表格、XLSX cells、CSV rows/cols 原生坐标；
- 11 个 chunker 的边界、回链和稳定 ID；
- batch embedding cache key、exact cosine、BM25、RRF；
- 5 种 source composition 和 note citation -> PDF 回链；
- evidence group 的 AND/OR 语义；
- coverage-nDCG、multi-hop、adversarial、宏平均、bootstrap；
- checkpoint 恢复、坏 artifact 拒绝和预算停止；
- subagent manifest、模板强制、交叉审计和冻结哈希。

验证：

```powershell
& $PYTHON -m pytest -q
& $PYTHON benchmarks/scripts/validate_benchmark.py
```

失败则只修当前失败，不扩大重构范围。

### Gate B：单论文 live canary

选定一篇同时包含主文和 external SI 的论文，贯通：

```text
source -> native IR -> subagent note -> audit -> freeze
       -> chunk -> qwen embedding -> retrieval -> rerank -> score
```

canary 必须验证 batch `/api/embed`、2560 维、reranker revision、source 回链和恢复执行。
通过后自动进入 20 篇；失败则记录准确阻塞点并停止，不拿坏配置跑全量。

### Gate C：20 篇笔记

父代理一次最多维持 3 个 fresh subagent 任务；按 manifest 协议推进三次 scanner pass，
随后轮换审计。20/20 冻结前不启动策略扫描。

### Gate D：Evidence mapping

总覆盖率低于 95% 或任一论文低于 90% 时停止排名，输出 unmapped 清单。不得通过删除难题
或改变 evaluable set 绕过 gate。

映射固定为版本化的 page/span 流程：content-stream PDF IR → NFKC 字母数字精确页内定位
→ source-span 字符投影；仅对 ResearchQA 版本差异残余使用官方 page/section hint 限域
选择最佳 chunk。不得通过降低阈值或把整页/整节全部标成 relevant 绕过 gate。

### Gate E：策略扫描

质量使用全部 254 问；性能使用固定的 40 问分层样本，warm-up 后 3 个 timed passes。
每个 config 必须完整后才落 score row 和参加本阶段排名。

### Gate F：最终报告

执行 10,000 次按领域分层的论文级 paired bootstrap，生成：

- `morning-report.md`：人读报告；
- `leaderboard.csv`：完整候选表；
- `paper-domain-breakdown.csv`：论文、领域、题型拆分；
- `pareto-frontier.json`：质量、延迟、索引大小前沿；
- `run-manifest.json`：所有输入和 artifact 指纹；
- `blocked-and-unmapped.jsonl`：失败、阻塞、未映射明细。

前两名 primary 相差不超过 0.5 个百分点时视为实质并列，再依次按 p95 latency、index
bytes、chunk count、config ID 决胜。所有 winner 只标记 `provisional`。

## 7. Git Loop

不建立 worktree，不切第二条迭代分支，只在当前
`codex/wave1a-canonical-ir` 上做外科式提交并更新 PR #4：

1. `feat(benchmark): prepare ResearchQA native source IR`
2. `feat(notes): add benchmark subagent note loop`
3. `feat(benchmark): add retrieval sweep and scoring`
4. `test(benchmark): verify rq-2 live canary`
5. `bench(rq2): publish reproducible aggregate results`

每个提交前执行相应测试，成功后立即 push。Raw PDFs、SI、模型、向量、逐题结果和未审计
笔记不得提交。若中途停止，已验证提交保留在 PR；未完成候选不会伪装成结果。

## 8. 只允许中止的真实阻塞

父代理只在以下情况停止并向项目所有者报告，不再写新计划：

1. official SI 明确存在，但严格 TLS 下无法获得或任何受支持解析器都无法解析；
2. 固定 Ollama embedding 模型或固定 reranker revision 无法加载；
3. 单论文 canary 在修复实现问题后仍不能完成端到端回链；
4. 笔记在两次返工后仍不符合 schema、引用或科学审计；
5. evidence mapping 未达到 95%/90% gate；
6. 磁盘、内存、GPU 或权限不足，且不能在既定模型和指标不变的前提下继续；
7. 测试发现会污染用户私人 ledger、仓库外运行状态或公开提交内容。

其余情况，包括个别下载重试、子代理重派、batch size 降低、进程恢复和无人值守窗口
续接，全部由 loop 自动处理。不得以预算为由裁掉候选。

## 9. 唯一启动指令

项目所有者检查本文件后回复：

```text
开跑
```

收到后，父代理从 Gate A 开始实现并持续执行至 Gate F 或真实阻塞；单次运行窗口到点只
触发原子 checkpoint 和后续 continuation，不构成停止条件，也不会再要求中间方案确认。
