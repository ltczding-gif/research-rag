# 第三轮评审核验与计划合并

> 文档发布范围：本计划及第三轮审查。标为“本地证据，未上传”的引用仅用于追溯，不是仓库内可下载文件；公开材料不能独立复现私有实验。见 [发布说明](../README.md)。

日期：2026-09-17。结论：**4 项新增缺口均确认，纳入 revision 5；当前只完成核验和计划更新，未实施修复。** 应先处理产品索引发布/恢复和模型身份，再补研究裁定的版本绑定，随后开展质量诊断。此前通过的用例和已经完成的实验保留，但不扩大其证明范围。

## 1. 输入、版本与核验边界

评审虽然存放在 raman-simplified 下，正文审查的是 Research-RAG；本轮工作对象仍是现有 Research-RAG checkout。

- 用户提供的 MD 原件未修改，归档副本为 [ROUND3_REVIEW_2026-09-17.md](ROUND3_REVIEW_2026-09-17.md)，SHA-256 `82d3e4944e439431e011e12f62ea122cc118a398ff58d44a8fb7da3d4b1a7a54`。
- 本地产品 main：`6865af20471b5c1ae7a1821f35a464d807134b9c`；研究分支：`0262686c4536b0f77ef6284bdbf8c92166fea9bb`。均与第三轮所指版本一致。没有切换分支。
- 审查中提到的 13 项隔离检查及其脚本/摘录/结果包没有随本次单份 MD 提供；本轮不把它们记作自己的实测。改用完整本地 notes builder / GenerationStore，以及从指定研究提交取出的完整 mapper 相关模块做独立小型核验。
- 本轮实际 **17 个离线场景：10 个触发缺口、7 个正常或拒绝控制**，涉及真实函数和临时文件；Chroma collection、embedding 是替身。没有调用真实模型、真实 Chroma 索引或付费 API，也没有重跑完整回归套件。这些是缺陷核验，不是修复验收。
- revision 4 两份计划的原始副本已保存在私有审查基线中，来源及 hash 见 [SOURCE_MANIFEST.json](SOURCE_MANIFEST.json)。原评审、原始实验、旧裁定及旧分数保持原样。

## 2. 逐项结论

### R3-01：确认，P1，产品 PR 首项

源码位置：`service/index_generation.py:156` 的 `_save_attempt`、`:162` 的 `fail`、`:197` 的 `publish`；`service/build_notes_db.py:360–373` 的发布及异常出口。PDF 的 `service/build_pdf_db.py:584–607` 同样将发布、成功输出和失败记录放在同一异常边界内。

真实原子写函数完成 active 替换后注入 KeyboardInterrupt，notes builder 调用 fail，把新 active 指向的 manifest 改成 failed；load_active 随即拒绝加载。对照是在替换前注入，此时旧 active 保持可读。原旧 generation 的 complete manifest 仍在，但 rollback 自身先调用 load_active，不能简单承诺原命令能修复这个坏 active。

因此不是“原子替换没做”，而是**提交后仍走提交前失败语义**。最小修复应以实际指针判定提交结果、保持已提交 manifest 不变、把后续错误单独记录，并补明确的损坏 active 恢复路径。只加一个本地 success 布尔变量不足以处理“替换成功后才抛错”。PDF 成功输出异常及完整故障矩阵本轮仅静态核验，列入修复后验收。

证据：7 项 generation 检查（本地证据，未上传：`.codex/round3-review-20260917/generation-checks/results.json`）、说明（本地证据，未上传：`.codex/round3-review-20260917/generation-checks/REVIEW.md`）。

### R3-02：确认，P1，与 R3-01 合并处理

源码位置：`service/build_notes_db.py:286–299`。同输入先检查 collection count，抛错后到不了 artifact 修复分支；复用成功路径未核验 collection generation metadata。

实测集合缺失、少一条记录均直接失败，没有新的完整替代 generation；同计数但 metadata.generation_id 错误仍返回 reused=True，而现有严格 collection validator 会拒绝。正常同输入复用和删除 artifact 后成功重建两个控制均成立。因此只需统一复用健康检查及修复分支，不必重写 notes builder。

后续要提供显式 notes rebuild，且区分“有效 manifest 指向的集合/工件损坏”与“active manifest 自身损坏”。前者从可读源建立新 candidate；后者走明确恢复流程，不能用宽泛 catch 把状态错误吞掉。新建前不删除旧集合，不触碰其他逻辑索引。

### R3-03：确认，P1，构建身份修复

源码位置：`service/build_notes_db.py:262–264` 只取一次合同、`:326–339` 只检查向量维度/有限值；`service/build_pdf_db.py:557–590` 的合同与 candidate 发布；`service/embedding_client.py:233–283` 的推理和身份合同。

完整 notes builder 的 3 个受控场景中，稳定 A 正常发布；A→B 和 A→B→A 都在同一 generation 中写入来自两个假模型的 2 维向量，manifest 却只记录 A 并发布 complete。每次构建 contract 函数仅调用一次。PDF 入口本轮静态确认同类边界，未声称跑了 PDF 真模型集成测试。

这确认缺少构建全程身份约束，**不证明实际 Ollama 曾在历史构建中换模型**，也不能解释先前 23/550 向量波动。后者已有同 digest、同会话、同请求仍波动的独立证据。

修复时固定实际推理目标和预期身份，按 provider 能力验证。只加最后一次身份检查漏掉 A→B→A；只轮询可变端点也不能消除全部竞态。远端人工声明 revision 保持 operator_declared，不冒充可验证的工件身份。不得通过禁止所有浮点差异来混淆“同模型数值波动”和“不同模型空间”。

证据：3 个模型身份场景（本地证据，未上传：`.codex/round3-review-20260917/model-identity-results.json`）。

### R3-04：确认，P1，新研究结论前置

研究提交中的源码位置：`benchmarks/researchqa_scoring.py:57–64,489–504`，`benchmarks/researchqa_strategy.py:657–677,719–844,994–1010`。alternative ID 只包含 row_id 和位置；裁定 validator 核验原文 provenance，没有当前 question/reference/paper/dataset 身份输入。

完整 mapper 的 7 个离线场景中：原样输入接受；同 row 改问题、把参考改为相反条件、交换 alternatives、相同 canonical 字节/hash 下换 paper_id 均仍接受旧裁定。改 row_id 或 canonical source/hash 则正确拒绝，说明现有来源保护仍生效。

这比原评审的静态判断增加了可复现证据；**仍没有证明任何历史 sidecar 或分数受到污染**。更换 dataset 的拒绝要求来自 schema 缺字段的静态判断和新验收设计，本轮没有宣称单独运行该变体。

最小范围是 sidecar v2 的目标身份验证和 live/offline 入口传递，不必重做所有 evidence ID、评分器或缓存。旧 v1 必须重新核对后才生成 v2，不能用当前题目 hash 自动补全旧裁定。reference 与 quote 可以合法不同；验证的是裁定针对的版本，而不是强迫二者逐字相等。

证据：7 个 mapper 场景（本地证据，未上传：`.codex/round3-review-20260917/sidecar-checks/controlled-results.json`）、源码身份（本地证据，未上传：`.codex/round3-review-20260917/sidecar-checks/source-inspection.json`）、说明（本地证据，未上传：`.codex/round3-review-20260917/sidecar-checks/REPORT.md`）。

## 3. 评审中需要按本地后续证据修正的状态

评审主要使用公开 PR；本地私有实测补充了它无法看到的内容。归档原评审不改，修正写入计划。

| 事项 | 核验后的状态 | 对未来计划的影响 |
|---|---|---|
| 三路线是否执行 | dense/hybrid 完成；section-note-assisted 首次 HTTP 400 后原配置重试完成，明显弱于 dense | 不作为未开始任务重跑，不预设 notes 必须胜出 |
| 三路线与全库 pilot | 三路线是固定 paper-scoped 研究比较；追加全库 pilot 执行前取消 | 另立全库协议，不把研究分数赋给服务 |
| 无答案 3/8 | 旧代理标准下 7/8 拒答判断正确、3/8 综合达标 | 分别诊断拒答决策、拒答后陈述、引用问题 |
| L04 数值 | −0.6 V 活化能交叉点有 E6 支持；原“数值错误/无依据”指控已撤回 | 保留未正确引用 E6 及必答范围争议；不改旧分数 |
| 六个完整 gold 缺失题 | 原文 hash/span 均有效、已在 550 chunks 中；10 个候选均落入 8k 预算，但 Top-10 没找全 | 从候选/选段层诊断；不再笼统归因抽取或预算 |
| 向量波动 | 同身份同请求的本机波动已复现，具体内部原因未定位 | 对照固定同一向量缓存；与 R3-03 分开处理 |
| PR #8 / PR #7 | 安装与 launcher 三平台 smoke 已通过；7.31× 仅研究内核测量 | 不追加昂贵 GPU CI，不写成 MCP 提速承诺 |

三路线协议：20 篇、254 题（239 有 reference），固定来源/gold、qwen3-embedding:4b、2560 维、1200 字符 PDF 块、paper-scoped、无 rerank；按 question→paper→domain→overall 宏平均 strict coverage nDCG@10：dense 0.764617、hybrid 0.773079、note-assisted 0.354938。hybrid 的 Top-10 recall/所有必需组成功率并未全面超过 dense；笔记结论只限该配置。source provenance 和代理裁定存在，不等于完成全部人类科学审阅。

主要依据：首轮执行报告（本地证据，未上传：`docs/reviews/2026-09-17-execution/EXECUTION_REPORT.md`）、后续勘误和漂移报告（本地证据，未上传：`docs/reviews/2026-09-17-followup/FOLLOWUP_REPORT.md`）、本轮状态对账（本地证据，未上传：`.codex/round3-review-20260917/status-reconciliation.md`）。

## 4. 计划怎么变

合并到 [revision 5 计划](../IMPROVEMENT_PLAN.md) 第 7 节，并同步 [机器台账](../improvement-plan.json)：

1. **A 产品 PR：R3-01/02/03。** 发布提交点、损坏恢复、同输入修复和模型身份一并收口，先不加检索功能。独立对账建议把模型身份另开 PR；本计划选择与同一构建合同合并验收，避免在身份未收口前启动质量实验。若后续实现显示 provider 改动较大，可以拆提交/PR，但仍是质量实验前置。
2. **B 研究 PR：R3-04。** sidecar v2 与旧裁定重核对；不整体合并 PR #4，不自动升级旧标注的信任等级。
3. **C 最小服务评测衔接与诊断。** 先复用已有 core/MCP parity，只补必要 evidence 转换；旧 30 题作为开发回归，oracle-context 只作诊断；另冻结独立 holdout，固定向量缓存。
4. **D 有条件的回答/演示。** 按失败归因只改一个因素；引用结构、来源正确性、主张支持度分开评价。质量门通过前保持 Alpha。

L04/L03/L10/C05/C06 的人工科学核查可独立开展，正式答案质量结论等待它们收口。当前不需要为了更新计划向用户请求新授权；也不把旧已收尾的一小时/十二小时窗口自动续跑。实际实施范围、合并和正式索引迁移按后续用户指令处理。

原 15 个问题及 29 项验收记录作为历史保留（28 通过、1 发布门失败）；新增 4 个 confirmed_unfixed 问题，新增 9 组修复后验收全部 not_run。报告中的 17 个缺陷核验场景不是这 9 组验收“已通过”。核验阶段只修改本地评审/计划和私有核验工件，没有修改产品或研究代码。随后按用户明确指令将计划及第三轮审查文档单独提交；原始运行工件保留本地。
