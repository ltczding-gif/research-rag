# Research-RAG · Round3 PR复审与状态更新

审查日期：2026-09-17。仓库：`ltczding-gif/research-rag`。本轮审查 PR #5、#6、#7、#8 的改动、固定提交源码、测试与 CI，并对关键发布/恢复边界进行隔离故障注入。本文是审查报告，不是已提交的修复 PR。

## 1. 结论与当前阶段

**这批 PR 的方向正确，且有实质性进展：研究侧修复了“怎么判分”，产品侧补上了“怎么可靠保存、读取和定位证据”。不建议推倒重做。** 但“已有若干 PR 合并”仍不等于“研究和产品两条线已经打通”，更不等于“自动回答已经达到产品发布标准”。当前适合描述为：**有可追溯证据与版本化索引基础的本地科研检索 Alpha，正在完善恢复闭环和独立质量验收。** [S01–S04]

本轮应优先处理四组问题：① 发布提交点后的异常仍可破坏已生效 manifest；② 笔记索引损坏后的同输入重建/复用判定不完整；③ 构建中模型身份变化没有贯穿到每批向量及最终发布校验；④ 人工/agent 裁定 sidecar 的键没有绑定实际问题与参考文本版本。前三组有本轮隔离复现；第四组是源码确认的版本迁移风险，并未证明现有历史结果已受影响。

已修复的问题应正式从活动缺陷列表中关闭，不要把这轮工作变成不断扩大的“重写全部系统”。下一轮应把资源集中于上述恢复/身份边界、真实跨论文找证据、答案引用关系，而非扩大模型矩阵或补复杂界面。

## 2. 审查范围、证据等级与版本

### 2.1 固定版本

| PR | 目标分支 | 最终 head | 合并提交 | 本轮状态 |
|---|---|---|---|---|
| #5 | codex/wave1a-canonical-ir | 2fd8a9ca6e4bf76bae2880c7a015a98a819c0cde | 98ce5c47c66894cd0b922699c32334406812286a | 已合并，研究侧严格证据与缓存修复 |
| #6 | main | abcb0cd58b405004829b4d4156a650faa7ebdc77 | 8de824d6758d91c45a5e750194abc127e8d85961 | 已合并，产品侧版本化索引和查询加固 |
| #7 | codex/wave1a-canonical-ir | 47ce1a2ffb7a00617e0d2d6b57dc8cbeb92d43e8 | 0262686c4536b0f77ef6284bdbf8c92166fea9bb | 已合并，研究检索矩阵预构建 |
| #8 | main | dcfb8e18ad398d50bddee49570925246a335072f | 6865af20471b5c1ae7a1821f35a464d807134b9c | 已合并，安装与 launcher 回归修复 |

当前审查主分支固定为 `6865af20471b5c1ae7a1821f35a464d807134b9c`；研究分支固定为 `0262686c4536b0f77ef6284bdbf8c92166fea9bb`。#5/#7 的实验改进不能当成 main 已经启用的检索算法。#6/#8 没有合并历史 PR #4。这里列出的是固定审查快照，不涵盖此后提交。[S01–S04]

### 2.2 哪些证据是本轮亲自检查的

**源码与 diff：** 通过 GitHub 连接器读取 PR 信息、变更清单、关键补丁和固定 SHA 的源码，重点覆盖严格评分、sidecar、实验缓存、历史重评分、矩阵检索、GenerationStore、notes/PDF builder、嵌入合同、generation reader、查询过滤、日志及安装 smoke。不是对每个变更文件的每一行作穷尽证明。

**CI：** 调取了四个最终 head 的工作流状态：#5 run `35125173446`、#6 run `35133570181`、#7 run `35135807902`、#8 run `35166269522` 均为 success。#8 的 12 个 job 状态均为 success；进一步读取 Ubuntu/Python 3.11 job `105027953952` 日志，确认为 `346 passed in 7.37s`。该 PR CI checkout 为测试合并提交 `7c0f54bcac94ef7471d98cb03d583b5be252b9c2`，不是把这个 SHA 混称为 main 的 squash 合并 SHA。

**本轮执行：** 运行了 13 项隔离行为检查：5 个缺陷触发样例（属于 3 组发现），8 个正向/反向控制。真实使用临时文件、manifest 和锁；向量数据库、嵌入服务、部分 gold/group 对象为明确的测试替身。执行选取的原函数定义，而非整库 pytest。`LOCAL_CHECK_RESULTS.json` 记录全部结果。检查显示 `passed` 是“符合所要验证的行为”；对于 `reproduced_gap`，它表示缺陷成功复现，不表示产品通过验收。

**未完成的验证：** 未在本轮获取完整可执行 checkout，未本地运行全仓 pytest，未重跑 GPU 实验、真实 Ollama/FastEmbed 推理或私有 30 题验收，未验证私有答案/向量原始产物。GitHub 源码读取成功，但容器直接拉取仓库受网络解析限制；因此交付的是可核对的原函数摘录和隔离检查，而非伪称完整环境复现。PR 中的私有验收数据一律标为“PR 报告”，不能与本轮独立执行混用。

## 3. 逐个 PR 的复审判断

### PR #5：接受严格证据方向；标注身份还需加固

这不是把旧判分公式换一个名字。现在 `weak_hint` 与 `unmapped` 不计入已验证证据；verified alternative 有独立于 chunk 的原文区间，严格评分按同一文件/页哈希的区间并集，要求一个 alternative 内所有 span 完整覆盖，再执行组内 OR、组间 AND。旧重叠分数放入 `loose_hit_*`，并加协议版本，方向正确。[S01、S06、S07]

本轮反例验证：仅覆盖 `[90,190)` 的 1 个字符不得分；`[0,140)` 与 `[140,220)` 两块同时返回时才于 rank 2 完成；另一文件即使区间全覆盖也不得分；弱证据组被严格评分入口拒绝。这些旧问题可以关闭，而不是只标“似乎修复”。

完成态实验缓存现在检查实现与依赖指纹，代码提交身份保留为审计信息，而不是仅靠一个人工维护的版本字符串。对研究代码修复后的复测，这是正确加固。正分 BM25 与无稀疏证据时的回退，也修正了旧的“零分词项仍投票”问题。[S01]

历史重评分工具要求独立历史 bindings，校验 artifact、payload、candidate、问题文件身份；输出明确为 offline historical rescore，不产生新检索/延迟数据，也不能直接作为发布胜者。全期望组的下界与 verified-only 条件分母分开，比单纯删去未映射组更诚实。[S08、S09]

**未完成的事：** `review-comparison-v1.json` 是冻结对照计划，不是新一轮实验结果。它用 dense PDF、hybrid PDF、section-note-assisted 三条路线，要求每篇有可回链的笔记，保持固定上下文预算，符合上一轮建议；但不能据配置文件就关闭“笔记路线实际有效性未验证”。另外，sidecar 的问题/参考文本身份见 R3-04。[S10]

### PR #6：关键的产品化进展，但恢复与发布身份需要补一个小 PR

认可完整候选 generation、原子 active 指针、先验证后切换、源文件撤回显式确认、源文件及嵌入合同绑定，以及读取时核对原文区间/哈希的实现。过滤条件真正做 AND，已知附件冲突显式报错，旧索引标为 `legacy_unverified`，没有给旧数据假造已验证来源。[S02、S05、S11–S16]

笔记全文实体与检索分块现在分开，分块受实际模型窗口限制；原文 UTF-8 bytes 留存在 generation 中，尾部不再因单一向量的前缀截断而从这条索引路径消失。全文快照也消除了“改名后旧记录继续混在当前集合”的路径，显式撤回有保护。本轮控制样例确认同输入复用、完整字符覆盖、同内容改名以及撤回保护。[S12、S13、S16]

日志改动也应认可：锁覆盖完整读改写、写入原子替换、损坏 registry 拒绝吞掉、孤儿日志可恢复、文件名冲突报错。它明确是**单进程**合同；多 worker 不在承诺范围，不能再把“没有跨进程事务”当成本轮旧缺陷未修好。[S02、S05]

不过，R3-01/02/03 表明，正常路径正确尚不足以覆盖所有失败边界。建议保留整个架构，增加一组聚焦“发布后异常、损坏索引修复、运行中身份漂移”的回归，不需要重写底层存储。

### PR #7：方向及范围正确，不应包装为产品整体提速

将 item matrix 与文档范数从每次查询搬到每个 paper/source index 的构建阶段，是合理的确定性优化；保留旧 one-shot API 和 float32 运算口径，也避免把性能优化与排序算法修改混在一起。[S03]

PR 报告：254 个冻结问题、20 个论文索引、2153 个 chunk 的 Top-100 ID 完全一致，最大分数差为 0。其本地 CPU 批处理原查询中位数为 1.9511 s，新查询阶段 0.1082 s，加预构建 0.1588 s，总计 0.2669 s。按报告数值计算，包含预构建约 7.31 倍；这不是端到端 7.31 倍，更不宜只取查询阶段比值宣传 MCP 的整体提速。本轮未重跑这组真实数据。[S03]

该 PR 只进入研究分支，main 仍是 Chroma dense。优先保留这项改进，暂不再在这个小内核上消耗主要开发时间。

### PR #8：解决了真实安装缺陷；安装可用仍不同于模型/检索可用

POSIX venv 的 Python 可能通过符号链接指向基础解释器，先 `resolve()` 再比较会丢掉 venv 调用身份；改为保留 invocation path，并验证 base Python 启动后确实进入目标 venv，修复具有针对性。[S04]

三系统 Python 3.11 的 fresh-install smoke 与原有九种 pytest 组合很有价值。它只到真实 stdio 工具发现，不下载模型、不建索引，也不覆盖高级双 venv/交互配置。这个边界在 PR 中明确写出，不能以它为理由要求立刻引入大模型联网 CI，也不能把它称为完整产品 E2E。[S04]

## 4. 前两轮问题状态更新

| 旧问题 | 当前判断 | 关闭条件/仍需工作 |
|---|---|---|
| 页码/章节弱提示当 gold | 研究侧已修 | 保留 weak-only 反例；科学充分性仍需审查 |
| 一字符重叠算完整命中 | 研究侧已修且本轮反例通过 | 区间并集和文件身份控制应加入长期回归 |
| 稀疏 reviewer-only 代表笔记路线 | 新对照已冻结，效果未验证 | 执行覆盖充分的 section 路线，不混用旧结论 |
| 完成态结果缓存不绑定实现 | 研究侧已修 | 重评分/新检索各自保留版本与输入证据 |
| 零分 BM25 扰乱融合 | 研究侧已修 | 新路线复测仍不可省略 |
| 矩阵重复构建、延迟口径不完整 | 内核与服务分段计时已改善 | 受控端到端测量尚不能由此推导 |
| 组合过滤丢附件约束 | main 已修 | 保留实际数据库过滤实参测试 |
| 笔记尾部不进入向量 | main 已修 | 本轮验证 source coverage，不冒充真实模型质量验收 |
| 笔记改名/删除残留 | main 快照路线已修 | 索引损坏后的重建另见 R3-02 |
| 日志并发覆盖 | 单进程承诺内已修 | 跨进程扩展时另立项目，不阻塞当前单用户 Alpha |
| 先删旧库、失败无可靠回退 | 正常候选失败路线已修 | 发布提交点之后的异常仍有 R3-01 |
| 正文/SI 身份及定位不贯通 | main 已有 canonical 源证据路线 | 非 PDF、OCR、科学表格理解不在当前保证内 |
| 模型/切块等版本约束不完整 | 静态合同已有显著进展 | 构建进行中身份变化见 R3-03 |
| 实验结果代表产品能力 | 仍需桥接 | 研究与 main 的源码/后端/输入切块不同，不可混称 |

状态依据为固定 SHA 源码、PR 描述及本轮隔离检查；其中“研究側”明确不等于“主分支已交付”。[S01–S16]

## 5. 新发现与可执行修复

### R3-01 · P1：提交成功之后，中断仍可让 active 变成不可读

**位置：** `GenerationStore.publish/fail/_save_attempt`；`build_notes_generation` 的 `except BaseException`；`build_pdf_db.main` 及 `_record_failed_attempt`。[S11–S14]

**机制：** publish 先将 generation manifest 记为 complete，再切换 active 指针。`fail()` 则可以无条件改写同一 manifest 为 failed。若 active 已切换、函数尚未返回时发生 KeyboardInterrupt，notes builder 会捕获并执行 fail。PDF builder 还在成功 publish 之后、同一个 try 内执行 `print`；输出异常或这一窗口的中断也进入失败记录路径。

**本轮复现：** 用原 publication 方法执行真实临时文件原子写入，在 active 指针写入完成后注入 KeyboardInterrupt；原 notes builder 失败处理将已生效 manifest 改为 failed。`load_active()` 随后抛出 `Active index manifest is incomplete or inconsistent`。旧 generation 文件仍然 complete，但 active 没有恢复。常规 rollback 本身也从 load_active 开始，因此不能把“旧目录还在”当成自动可恢复。没有删掉原文 PDF/笔记，这里是元数据一致性和可用性问题，而不是原始科研资料丢失。

**修复：** 明确提交点；已完成的 manifest 保持不可变，发布操作状态/后续 warning 与 manifest 分开。fail 至少不能改写已生效 generation；不能只依赖一个可能未赋值的本地 success 标志判断原子切换是否已经发生。成功后的日志输出放在索引失败处理边界之外。对原子写“已提交但调用者收到异常”的结果，读取并核对指针后报告真实状态，不能一律声称 active 未改变。

**验收：** 在 manifest 保存前/后、active replace 前/后、返回前、成功输出时分别注入异常/中断；active 始终能加载某个完整 generation，并可按明确步骤恢复。保留“提交前失败不改 active”的已有控制。

### R3-02 · P1：笔记索引损坏后无法按原输入重建，复用又漏掉 collection 身份

**位置：** `build_notes_generation` 的 `same_inputs` 分支、`_collection_count` 与 CLI。[S12、S13、S17]

**机制及复现：** 初次成功后，删除当前 collection，再运行相同输入，抛出 Cannot verify collection；仅删除其中一个 chunk，则报 count mismatch，不创建替代 generation。反过来，仅把 collection metadata 的 generation_id 改错、数量不变，builder 仍返回 reused=True。查询读取端会检查该身份，因此会出现“构建声称可复用，读取却拒绝”的分裂。

**判断：** 损坏时拒绝直接拿旧集合继续用是正确的；缺口在于没有安全的候选重建出口。当前 notes CLI 没有与 PDF `--rebuild` 对应的强制重建选项。现有 review 测试覆盖“笔记 artifact 缺失后重建”，没有覆盖以上 collection 边界。

**修复：** 仅当 manifest、artifact、collection 的数量和 generation identity 全部一致时才能复用。集合缺失/数量错误/身份错误，应从可读完整源快照构建新候选，再原子发布；提供显式 `--rebuild-notes` 或统一 rebuild 接口。若 active manifest 自身不可验证，不应简单吞错，应给出独立的受控修复/恢复流程。

**验收：** collection 缺失、少一条、同数量错误 generation_id、artifact 缺失四类分别测试；成功修复后有新 generation，原文件不变，不先删除旧索引，不误伤其他 namespace。

### R3-03 · P1（可变模型部署）：模型合同在起点检查，但没有锁住整个构建

**位置：** notes builder 的一次 `embedding_contract_fn()`、PDF builder 的初始 `_build_contract` 与 `_write_candidate`，以及 `embed_index_text`。[S12、S14、S16]

**机制：** 初始合同写入模型 revision，后续每段 embedding 主要检查维数、有限值、非零。接口没有把这一 generation 所期望的模型身份传到每批输入/输出校验，发布前也没有统一的身份复验。若模型 tag 在构建中发生同维度变化，合同 A 与部分 B 向量可能一起提交。

**本轮复现：** 注入可变测试适配器，第一次返回 A 向量，从第二段起变为 B，同维度且所有数值合法。整个原 notes builder 只读取合同 1 次，最终 active 合同仍是 A，数据库测试替身中已有两种 revision 生成的向量。没有把该测试替身说成已复现真实 Ollama 在线改模。

**修复：** 优先使用不可变的部署模型版本；明确 generation 级 expected_embedding_contract，校验批次身份并在发布前复验。前后检查能发现部分变化，但 A→B→A 不能仅靠末尾一次比较排除，应有部署冻结/版本固定约束。对于无法独立证实 revision 的远端 provider，保留 operator_declared 边界，不要假称自动证明。

**特别区分：** PR #6 自报的同源重建 23/550 向量变化，原因仍未知。本发现不能直接作为那个现象的根因。即便模型身份不变，也仍需独立调查输入字节、推理选项、量化、runtime、数值确定性及其对排名的影响。

**验收：** 相同维度 A→B 的中途变化必须阻止新候选成为 active，旧版继续可读；错误不能被当成可自动混合的重试。向量逐位一致与功能稳定分别测量，不做未经验证的 bitwise 声明。

### R3-04 · P1（发布研究结论前）：裁定 sidecar 没有绑定问题/参考文本版本

**位置：** `evidence_alternative_id`、`_validated_adjudication`、`map_reference_groups` 的 overrides 分支及 sidecar v1 schema。[S06、S18、S19、S20]

**源码确认：** alternative ID 只由 row_id、group_index、alternative_index 生成。裁定记录校验源文件、页、区间、quote/hash、gold_version 和 agent provenance，但没有校验本条裁定原本对应的 question/expected-reference 内容哈希。若保持 row_id 和位置，只修改/重排参考内容，旧 key 仍可能对应上，并用原文合法但语义已过期的 span 覆盖新参考。

**边界：** 这是验证 API/schema 的版本绑定缺口，不是说所有裁定都是错的，也没有证明本轮现有结果被污染。实验缓存包含 questions 的新哈希，只能让旧计算失效，不能自动让错误套用的旧人工/agent 裁定失效。给 sidecar 文件自身做哈希也不等于绑定它所标注的问题。

**修复：** sidecar v2 绑定 dataset/question-manifest 指纹、paper_id、question fingerprint、每个 alternative 的 reference SHA-256；继续保留现有原文 provenance。旧 v1 不能自动填入“当前参考 hash”然后视为可信迁移，必须经过重核对。不要要求原文 quote 与参考文字逐字相同，因为裁定可能合法处理版本差异/转述；要求的是“这一裁定明确针对哪个参考版本”。

**验收：** 同一 row_id 下改问题文本、改参考数值/否定、交换 alternatives、换 paper 或 dataset revision 都必须拒绝旧 sidecar；完全相同的冻结输入仍可复用。该项本轮为静态确认，未执行整个 mapper/sidecar CLI 的端到端测试。

## 6. 最新验收数据应如何解释

以下均来自 **PR #6 对私有冻结验收的报告**，不是本轮独立重跑：[S02]

| 层级 | PR 报告数值 | 能说明什么 | 不能说明什么 |
|---|---:|---|---|
| 冻结证据完整覆盖 | 16/22 | 小验收集上存在可用证据检索路径 | 泛化准确率、所有任务生产可用 |
| 原文 lookup | 8/10 | 简单定位仍有少量缺失 | 所有数值/单位提取可靠 |
| 跨论文问题 | 2/6 | 当前最明显的功能短板之一 | 不能已宣传自动高质量跨文献综述 |
| SI 问题 | 6/6 | 这组 PDF SI 任务结果良好 | 非 PDF SI/OCR/任意表格已解决 |
| 最终生成版本 v4 有答案达标 | 11/22 | 答案模型/上下文/引证仍有明显问题 | 不能用源证据 verified=true 替代回答验收 |
| 无答案达标 | 3/8 | 无证据时的行为未达门槛 | 不能宣传可靠拒答 |
| 答案级引用错误 | 11 项 | 需要 claim→evidence 的单独验证 | 不等于原文页码/哈希验证也失败 |
| core/MCP parity | 30/30，距离最大差0 | 同版本同输入的接口一致性 | 不是两次完整重建逐位相同 |
| 同源重建 | 23/550 向量变动，影响6个答案输入 | 必须调查复现性及输入影响 | 本轮没有证明具体 runtime 根因 |

因此下一步不应简单换更大的回答模型。应先区分：根本没找全证据、预算裁掉证据、找到了但不会组织比较、找到了却绑定错引用、无证据仍然生成。只有这些层次的失败归因明确，模型升级才有可解释的收益。

建议做一次 oracle-context 对照：对失败题喂入人工确认的完整证据片段，在模型/prompt/预算固定的情况下看是否仍错。若 oracle 仍错，先修回答/结构化引用；若 oracle 正常而检索上下文错误，优先检索/路由/预算。oracle 对照是诊断，不是正式检索成绩。现有 30 题已被观察过，应作为开发回归，再另设冻结 holdout，避免反复调到这 30 题通过后当独立测试。

## 7. 后续 PR 顺序与两天范围

**第一优先：main 上的索引状态机与恢复 PR。** 合并处理 R3-01/02/03，不引入新检索算法；补提交点故障注入、同输入修复、同维度模型漂移。建议继续单用户、单进程、发布后重启采用新 generation，暂不引入热切换和在线清理。

**第二优先：研究分支上的 gold/sidecar 身份 PR。** 补 R3-04，冻结 sidecar v2 和同一 evidence/denominator 合同；复核弱映射与科学证据是否足够。保留现有缓存加固，不重写全部 runner，不直接合并历史 PR #4。

**第三优先：实验与产品的薄适配/验收 PR。** 将 main 实际返回的 evidence segments 转为严格评分输入，并导出 query text、全部过滤条件、来源/模型/generation 身份、检索轨迹及上下文预算。不要把另一个 NumPy runner 的指标直接赋给 Chroma 服务。研究三路线对照保持冻结配置；未知目标论文的全库任务另设协议，不暗传正确 paper_id。

**第四优先：证据导向的回答试验与演示 PR。** 每个 claim 只绑定这次检索返回的证据 ID；将结构有效性、来源定位正确性、证据对主张的支持度分层。无法覆盖完整跨论文比较时，显示缺口或仅给证据清单，不伪装成完整结论。自动长综述暂作可选实验功能。

若仍按原来两天资源窗口推进，建议把范围收敛为：**安全恢复闭环＋可信评测协议＋可复查的证据检索演示**。前两个 PR 优先，第三个做最小适配；第四个只有通过门槛才进入发布展示。不要用增加功能来延迟已经确认的 P1 修复，也不保证在缺少人工科学核查的情况下两天就能达到自动问答产品质量。

## 8. 发布与简历表述

当前可展示：Zotero 文献/正文-SI 的来源身份、完整笔记与分块索引、版本化候选发布、源片段回查、严格评测合同、跨平台安装和 MCP 工具调用。对尚存恢复边界应先修好再作相应稳定性承诺。[S01–S05]

不宜展示为已完成能力：生产级自动综述、可靠拒答、高质量跨论文比较、所有格式 SI 支持、端到端固定倍数提速、同源重建逐位可复现、研究分支全部能力已进入 main。

可用项目描述草稿：

> 面向科研文献调研，设计并开发本地优先的 Zotero/MCP 文献检索工作流，构建原文与持久研究笔记双索引；实现版本化索引、正文/补充材料的页码与片段级溯源，并建立区分完整证据覆盖、来源正确性和答案支持度的检索评测与跨平台回归流程。

“设计并开发/主导/独立完成”等角色词仍应按本人实际分工选择。检索优化数字应限定研究内核与固定实验集；未完成的独立 holdout/答案门槛不写成已有成绩。

## 9. 交付与复核

`audit_checks.py`：13 项本轮隔离检查，可用 Python＋PyYAML 执行。`snippets/` 为所需原定义的摘录及清晰标识的装配环境，不是源码完整副本。`LOCAL_CHECK_RESULTS.json` 和 `LOCAL_CHECK_STDOUT.txt` 为本轮实际输出。`NEXT_PR_PLAN.md` 给出可交给开发 agent 的实施顺序及验收要求。`SOURCE_MANIFEST.json` 保存固定 SHA、Git blob SHA、原文件位置与证据等级。`verify_excerpt_fidelity.py` 可用完整本地 checkout 对摘录函数做 AST 语义核对；本轮因未取得完整 checkout，未运行这项外部核对。

本报告没有修改远端仓库、提交 review、创建/合并 PR、改变私人 active 指针或运行费用型模型调用。

### 来源索引

S01 PR #5；S02 PR #6；S03 PR #7；S04 PR #8；S05 docs/INDEX_GENERATIONS.md；S06 benchmarks/researchqa_scoring.py（数据结构/ID）；S07 同文件（严格覆盖评分）；S08 benchmarks/researchqa_review.py；S09 benchmarks/scripts/rescore_review_rankings.py；S10 benchmarks/configs/review-comparison-v1.json；S11 service/index_generation.py；S12 service/build_notes_db.py；S13 同文件 CLI/异常出口；S14 service/build_pdf_db.py；S15 service/generation_query.py；S16 service/embedding_client.py；S17 tests/test_generation_reuse_review.py；S18 benchmarks/researchqa_strategy.py（裁定验证）；S19 benchmarks/researchqa_scoring.py（override 拼装）；S20 docs/reviews/STRICT_EVIDENCE_PROTOCOL.md；S21 service/query_server.py。具体固定版本地址及 blob 身份见 SOURCE_MANIFEST.json。
