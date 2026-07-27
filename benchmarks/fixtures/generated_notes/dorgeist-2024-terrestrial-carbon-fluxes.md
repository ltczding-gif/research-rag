---
title_en: A consistent budgeting of terrestrial carbon fluxes
title_zh: 陆地碳通量的一致性核算
authors:
  - Lea Dorgeist
  - Clemens Schwingshackl
  - Selma Bultan
  - Julia Pongratz
year: 2024
journal: Nature Communications
doi: 10.1038/s41467-024-51126-x
keywords:
  - terrestrial carbon budget
  - land-use change emissions
  - natural land sink
  - BLUE bookkeeping model
  - TRENDY DGVMs
  - transient environmental conditions
  - replaced sinks and sources
  - loss of additional sink capacity
topic:
  - 陆地碳收支一致性
  - 土地利用变化排放
  - 自然陆地碳汇
  - 地球系统模型核算
research_domain: other
document_type: research-article
note_template: generic-research-note
seed_terms:
  - terrestrial carbon budget
  - BLUE bookkeeping model
  - TRENDY DGVMs
  - land-use change emissions
  - natural land sink
  - replaced sinks and sources
scope_hint: other
signal_quality: strong
routing_confidence: high
combined_hash: ba3a674e57e6d602736a5e3ef12ce4a8179634d3be5cbbee999a180288089a6b
pdf_0_name: main.pdf
pdf_0_artifact_path: corpus/files/dorgeist-2024-terrestrial-carbon-fluxes/main.pdf
pdf_1_name: si.pdf
pdf_1_artifact_path: corpus/files/dorgeist-2024-terrestrial-carbon-fluxes/si.pdf
tags: []
candidate_tags_high: []
candidate_tags_medium: []
candidate_tags_low: []
human_reviewed: 0
---

这是一篇以模型方法与全球碳收支核算为主的研究论文。作者把 TRENDY 动态全球植被模型（DGVM）的瞬态碳密度引入空间显式的 BLUE 土地利用排放记账模型，以同一套概念边界估算土地利用变化排放 E_LUC 与自然陆地碳汇 S_LAND。最重要的结论是：采用实际瞬态土地覆盖后，常规方法在 2012–2021 年把自然陆地碳汇高估约 23%；同时纳入瞬态环境效应会把 E_LUC 相对当前 GCB 风格的现今碳密度估算提高约 14%，最终得到更弱的净陆地碳汇 [p.1, p.5]。它值得保留，因为它不仅给出数值修正，还提供了可复用的通量归因与防止漏算、重复计算的框架。

## 文献基本信息

- 推荐保存文件名：`2024-NatCommun-ClemensSchwingshackl-TerrestrialCarbonBudget-FluxAttribution-BLUE-TRENDY-ConsistentLandFluxAccounting_review_note.md`
- 标题：A consistent budgeting of terrestrial carbon fluxes
- 期刊与年份：Nature Communications，2024
- DOI：10.1038/s41467-024-51126-x

## 文档类型与边界（Document Type / Boundary）

本文最接近“环境模型方法论文”：主导材料是作者新构造的 BLUE 模拟体系及其与 TRENDY、Global Carbon Budget（GCB）和大气反演的定量比较，而不是综述或理论推演。原始证据层包括模型输出、区域图、时间序列、敏感性范围和方程；作者综合层负责解释为何既有 E_LUC 与 S_LAND 来自概念不一致的模型家族；背景层则说明净零目标与全球碳预算的重要性 [p.1-2, p.7-11]。现有模板库没有地球系统建模或环境核算专用模板，`methods-or-materials-synthesis` 又以材料合成流程为中心，无法自然承接本文，因此暂留 generic。若未来增加“computational environmental methods”模板，本文应优先迁移。

## 重新路由触发条件（Re-routing Triggers）

- 若模板库新增地球系统模型、碳收支核算或计算环境科学的方法模板，应从 generic 迁出。
- 若后续评测确认全文始终由模型构建、方程定义、敏感性分析和模型间比较主导，则应将本文视为稳定的方法论文，而非混合型来源 [p.9-11]。
- 若只为维持 generic 而需要把模型方程、数据流和不确定性压成背景说明，即说明当前路由过宽。

## 核心问题（Main Question）

当前 GCB 将记账模型估算的 E_LUC 与在前工业土地覆盖上运行的 DGVM 估算 S_LAND 相加；前者通常忽略随时间变化的环境效应，后者却包含现实中已因历史生态系统退化而消失的假想碳汇，因而两项不能在同一概念边界下直接闭合。本文要回答的是：能否让 BLUE 同时承接瞬态环境条件和瞬态土地覆盖，分离 E_LUC、S_LAND、replaced sinks and sources（RSS）与 loss of additional sink capacity（LASC），从而形成一致的陆地碳预算 [p.2, p.7]？

## 主导证据类型（Dominant Evidence Type）

主导证据是模型模拟与模型间 benchmarking。作者用 TRENDY S2/S3 的 PFT 级植被和土壤碳密度缩放 BLUE 默认碳密度，分别运行前工业、现今、瞬态、瞬态加环境效应以及无土地利用变化的五种 BLUE 设置，再通过碳池差分和方程分离各通量 [p.9-11]。这类证据最擅长检验“不同核算定义会造成多大系统偏差”，但不能单独证明某个全球通量是真实观测值；Earth observation 和 FLUXCOM 也不能在缺少驱动假设时直接拆分 S_LAND 与 E_LUC [p.5]。因此本文是一个强模型柱，加上多个外部模型与大气约束的间接支撑，而不是直接观测闭环。

## 证据层级与混合处理（Evidence Hierarchy / Mixed Evidence）

1. **直接模型证据**：BLUE 不同设置之间的差值、区域分布和长期积分，是本文核心事实锚点 [p.2-8]。
2. **方法闭合证据**：五种模拟设置及方程（1）–（11）明确规定 E_LUC、S_LAND、RSS 和 LASC 的归属，避免仅凭术语解释推断 [p.10-12]。
3. **SI 支撑**：PFT 映射、碳密度比值、不同 DGVM 的绝对碳密度差异、区域时间序列和分期表格负责检验实现与敏感性 [SI p.2-3, SI p.6-17]。
4. **间接比较**：TRENDY、GCB、大气反演和 δ13C 重建提供外部一致性，但它们的边界和不确定性并不相同，不能视为独立真值 [p.5-7, SI p.17]。
5. **作者解释**：关于政策核算、碳信用和生态恢复激励的讨论属于应用推论，证据权重低于模型结果 [p.8-9]。

## 核心内容（Core Content）

### 1. 概念不一致来自通量归属边界

GCB 的 E_LUC 主要由静态或现今碳密度记账模型给出，而 S_LAND 来自在前工业土地覆盖上运行的 TRENDY DGVM。后者会把历史上已被土地利用变化移除的生态系统仍可能产生的环境响应算入自然碳汇，即 RSS [p.1-2]。作者的最强朴素 claim 是：先统一“环境条件”和“土地覆盖”两个边界，再谈净陆地通量，否则源与汇之间会漏算或重复计算。

### 2. 方法路径：把瞬态 DGVM 碳密度嵌入 BLUE

作者使用 8 个 DGVM 的植被碳密度和 5 个 DGVM 的土壤碳密度，将各模型 PFT 映射到 BLUE 的 11 个 PFT 和四类土地覆盖；时间序列经 20 年移动平均后，以约 1980 年为基准缩放 BLUE 默认碳密度，前工业碳密度取 1720–1740 年平均 [p.9-10]。SI 说明了森林、灌木、苔原、农地和牧场的匹配规则，以及 C3/C4 扩张权重 [SI p.2-3]。这使 BLUE 在保留土地利用事件可追踪性的同时，表达环境驱动下随时间变化的碳储量。

### 3. 土地利用排放被低估

相对前工业环境条件，瞬态环境条件使 2012–2021 年 E_LUC 增加 28%（五个估算范围 21%–38%），即增加 0.34 GtC yr⁻¹（0.18–0.56 GtC yr⁻¹）；相对现今固定碳密度的 GCB 风格设置，增加 14%（8%–23%）[p.2]。原因链是：环境变化通常提高植被和土壤碳密度 → 砍伐或采伐时可释放的碳更多 → 瞬态 E_LUC 高于静态基准。SI 显示这一环境贡献主要来自毁林和木材采伐，但再造林与采伐后恢复会部分抵消 [SI p.8-9]。该链条是模型内因果分解，不是直接观测的因果证明。

### 4. 常规自然陆地碳汇包含已消失的假想汇

在实际瞬态土地覆盖下，S_LAND,trans 于 2012–2021 年为 −3.0 GtC yr⁻¹（−3.9 至 −2.2），而前工业土地覆盖下为 −3.7 GtC yr⁻¹（−5.2 至 −2.6）；两者差异 RSS 为 0.7 GtC yr⁻¹（0.3–1.3），对应把碳汇高估 23%（8%–33%）[p.3, p.5]。区域上，巴西和东南亚各贡献约 0.1 GtC yr⁻¹ 的 RSS，南亚的 S_LAND,trans 相对前工业覆盖低 38%，东南亚低 28%，中美洲低 27%，中国低 25% [p.5]。SI 的区域时间序列进一步显示该偏差并非全球平均值的单点产物 [SI p.12]。

### 5. 一致核算得到更弱的净陆地碳汇

BLUE 的一致净陆地通量在 2012–2021 年为 −1.2 GtC yr⁻¹（−2.1 至 −0.5），与 TRENDY 的 −1.4 ± 0.7、大气反演的 −1.4（−2.0 至 −0.3）和大气 O₂ 估算的 −1.2 ± 0.8 接近，但弱于概念不一致的 GCB 估算 −1.9 ± 1.0 GtC yr⁻¹ [p.5]。1850–2021 年累计 BLUE 结果为净源 53 GtC（−21 至 117），但不确定范围跨过零，因此不能断言历史陆地究竟是累计源还是汇 [p.5]。替换通量后，全球碳预算不平衡由 −0.4 GtC yr⁻¹ 移到 +0.3 GtC yr⁻¹，而非变为零，说明其他预算项仍有误差 [p.6]。

## 最强证据或论证（Strongest Evidence or Arguments）

- **核心发现：** 常规 S_LAND 高估主要来自土地覆盖反事实，而非单纯模型数值差异。**证据链：** 同一 BLUE 框架只改变土地覆盖假设，S_LAND 从 −3.0 变为 −3.7 GtC yr⁻¹，RSS 为 0.7 GtC yr⁻¹；同时 SI 将“土地覆盖影响”和“绝对碳密度影响”分开，显示二者在全球上会部分抵消 [p.5, SI p.13]。**仍不能证明：** 不能证明 BLUE 的绝对通量就是真值。
- **核心发现：** 环境效应同时改变人为排放项，而不只改变自然汇。**证据链：** E_LUC,trans 相对 E_LUC,pi 增加 0.34 GtC yr⁻¹，毁林和木材采伐贡献最大，再造林与恢复提供部分负贡献 [p.2, SI p.8]。**解释路径：** 碳密度上升 → 土地利用事件时释放或移除的碳量改变 → 静态碳密度产生时间依赖偏差。**仍不能证明：** DGVM 对 CO₂、气候和养分响应的幅度是否准确。
- **核心发现：** 一致性来自定义与模拟闭合，而非结果碰巧接近。**证据链：** 五种 BLUE 设置通过方程（1）–（10）分别定义 L、δL、S_LAND,trans、RSS 与 LASC；SI 的流程图用相同池差分关系展示 GCB 与本文设置的差别 [p.10-11, SI p.4]。这比仅比较最终净通量更有说服力，但复现仍受代码未公开限制。

## 最可复用的 takeaway（Most Reusable Takeaways）

- **先对齐定义再相加（硬结论）**：任何源—汇预算都应检查组成项是否共享相同基准状态、空间边界和驱动归属；适用于碳预算及其他守恒核算，失效条件是各项本来就由同一闭合体系直接观测。
- **用成对反事实拆分交互项（实践启发）**：分别运行前工业、现今和瞬态环境条件，并在固定与瞬态土地覆盖间对照，可把环境效应、土地利用效应及其交互分开 [p.10-11]。可迁移到其他过程模型，但依赖模型结构允许状态隔离。
- **净值相近不代表组件正确（硬结论）**：BLUE 与 TRENDY 的净通量接近，可能源于碳密度差异与土地覆盖影响相互抵消；因此必须审查组件级差异 [p.7-8, SI p.13]。
- **把反事实中已不存在的系统标为“replaced”项（可复用框架）**：RSS 这个命名能显式暴露“在已被移除生态系统上计算的假想汇”，适用于涉及历史状态替换的预算问题 [p.2, p.5]。
- **不确定性必须与结论强度联动（硬结论）**：累计净通量范围跨零时，应保留“源或汇仍不确定”，不能因中心值为正就写成历史净源 [p.5]。

## 局限与开放问题（Limits / Open Questions）

- DGVM 对生产力、碳分配、养分限制和周转时间的响应差异很大，这些误差经碳密度缩放传入 BLUE，尤其放大 S_LAND 的范围 [p.8]。
- PFT 碳密度被聚合为全球平均，局地火灾、干旱、纬度差异与自然气候变率可能被弱化；作者指出这可能解释区域上的 BLUE–TRENDY 差异 [p.8]。
- LULUCF 强迫数据本身可能高估部分地区的木材采伐和清理面积，且与观测碳密度数据存在偏差 [p.8]。
- 外部观测通常只能约束净陆地通量，不能无假设地拆分 E_LUC 与 S_LAND，因此外部验证并非组件级真值验证 [p.5]。
- 处理后的数据公开，但分析代码仅“upon request”，限制了完全复现 [p.12]。
- 原文未明确给出把该框架纳入下一版 GCB 后所有其他预算项如何协同重估；碳预算不平衡转正表明问题未完全闭合 [p.6]。

## 审稿人视角（Reviewer Lens）

- **taxonomy tagging：** 有用；可稳定标注 terrestrial carbon budget、LULUCF、DGVM、bookkeeping model、RSS/LASC。
- **literature mapping：** 很有用；它连接 GCB、BLUE、TRENDY、国家清单和大气反演之间的定义差异。
- **method adoption：** 中高价值；方程与 SI 足够清楚，但代码未公开降低复现性。
- **mechanism tracing：** 中等；可追踪的是核算因果链和模型状态转换，不是生态过程的直接机理证明。
- **follow-up search：** 很有用；可沿 RSS、LASC、transient carbon density、DGVM–bookkeeping reconciliation 检索。

本文主导证据高度一致，作用是 genuinely evidentiary 但仍属模型内证据。事实、作者解释和笔记推断已分层；正因为其方法论文轮廓非常清晰，generic 只应是当前模板缺口下的临时承接，而不是长期归宿。

## 主观打分

- **原创性（兼模板边界判断）：8/10。** 将瞬态 DGVM 碳密度系统嵌入空间显式记账模型并分离全部预算项，是明确的方法贡献；但 generic 适配仅属临时。
- **严谨性（证据强度）：8/10。** 有多模拟对照、区域结果、模型集合范围和外部估算比较，但核心仍依赖模型假设。
- **证据闭合度：7/10。** 方程、SI 和数据可用性较完整；代码仅按请求提供，且组件级直接观测验证不足。
- **工业应用潜力（兼跨域复用价值）：5/10。** 不是工业装置研究；价值主要在政策核算、碳市场与模型方法复用，而非直接工业部署。

## 核心结论总结

本文值得保留，因为它把一个看似“总量估算偏差”的问题还原为两个可检查的核算边界：环境条件是否瞬态、土地覆盖是否代表现实历史。最可复用的内容是“先统一组成项定义，再用成对反事实拆分交互项”的方法；最主要的边界是结果依赖 DGVM 碳密度、全球 PFT 聚合和 LULUCF 强迫，且代码未直接公开。当前 `generic-research-note` 能保存跨域证据，但并非理想终点；一旦有环境模型方法模板，应立即重路由。
