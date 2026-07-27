---
title_en: Combining data and theory for derivable scientific discovery with AI-Descartes
title_zh: AI-Descartes：结合数据与理论实现可推导的科学发现
authors:
  - Cristina Cornelio
  - Sanjeeb Dash
  - Vernon Austel
  - Tyler R. Josephson
  - Joao Goncalves
  - Kenneth L. Clarkson
  - Nimrod Megiddo
  - Bachir El Khadir
  - Lior Horesh
year: 2023
journal: Nature Communications
doi: 10.1038/s41467-023-37236-y
keywords:
  - AI-Descartes
  - symbolic regression
  - automated theorem proving
  - scientific discovery
  - background axioms
  - mixed-integer nonlinear programming
  - KeYmaera X
  - model derivability
topic:
  - neuro-symbolic scientific discovery
  - data-theory integration
  - derivable model selection
  - scientific law discovery
research_domain: other
document_type: research-article
note_template: generic-research-note
seed_terms:
  - AI-Descartes
  - symbolic regression
  - automated theorem proving
scope_hint: core
signal_quality: strong
routing_confidence: high
combined_hash: 12bffd0d84d55c39d5b5d42bf90cdc6a13c26f9442b1f6e0e2c77842af759b89
legacy_combined_hash: 53e6b559fe6e3e1dd99f163a3747b40240f027816c4d5bef4bf0c093bd90a45b
pdf_0_name: main.pdf
pdf_0_artifact_path: corpus/files/cornelio-2023-ai-descartes/main.pdf
pdf_1_name: si.pdf
pdf_1_artifact_path: corpus/files/cornelio-2023-ai-descartes/si.pdf
tags: []
candidate_tags_high: []
candidate_tags_medium: []
candidate_tags_low: []
human_reviewed: 0
---

这是一篇计算方法研究论文，提出AI-Descartes：先用基于混合整数非线性规划（MINLP）的符号回归从少量实验数据生成候选公式，再用形式逻辑和KeYmaera X判断候选能否由背景理论公理推导，或计算其相对理论的reasoning error。作者在开普勒第三定律、相对论时间膨胀和Langmuir吸附方程上展示该框架，并在带噪声的小样本Feynman符号回归基准上报告候选公式召回优势 [Main p.1-3; SI p.29-32]。最值得保留的不是某个物理公式，而是“经验拟合误差ε与理论可推导性β分开评价”的模型选择原则。

## 文献基本信息

- **推荐保存文件名**：`2023-NatCommun-LiorHoresh-AIDescartes科学发现-可推导符号回归-数据理论与自动定理证明-可解释模型筛选_review_note.md`
- **标题**：Combining data and theory for derivable scientific discovery with AI-Descartes
- **期刊与年份**：Nature Communications, 2023
- **DOI**：10.1038/s41467-023-37236-y [Main p.1]

## 文档类型与边界（Document Type / Boundary）

本文是原始AI/计算方法论文。直接证据包括算法定义、形式化公理、证明/反例结果、计算案例与基准比较；作者关于“加速科学发现”或未来完整自动化的表述属于前景判断 [Main p.2-8]。留在generic的原因是**现有模板缺少软件/ML与神经符号方法合同**：`foundational-theory`差点可用，但本文没有提出新的自然科学基础理论，而是提出连接符号回归、优化与自动定理证明的工程—方法框架；`methods-or-materials-synthesis`又偏向实验协议或材料制备。未来应迁往“AI methods / scientific machine learning / neuro-symbolic systems”模板。

## 重新路由触发条件（Re-routing Triggers）

若模板库新增以下任一结构，应离开generic：①按任务定义、算法、复杂度、基线、消融和复现性组织的软件/ML模板；②明确区分语法搜索、语义约束、定理证明与反例生成的神经符号模板；③面向科学机器学习、公式发现和物理先验的专用模板。本文从系统定义到SI基准均呈现稳定方法论文轮廓，generic只应是当前模板缺口下的临时路由。

## 核心问题（Main Question）

当多个公式都能拟合稀疏或有噪声的实验数据时，能否把一般科学背景知识写成公理，使系统不仅比较数据误差，还能证明候选公式可由理论推导、证明其不一致，或量化它距可推导公式还有多远 [Main p.1-3]？作者概括的目标是得到“derivable, and not merely empirically accurate”的模型 [Main p.2]。

## 主导证据类型（Dominant Evidence Type）

主导证据是**算法构造 + 形式证明 + 多案例计算实验 + 基准比较**。MINLP符号回归负责从数据生成候选；KeYmaera X与Mathematica负责可推导性、约束和reasoning error分析 [Main p.2-3, p.7; SI p.5-18]。形式证明在给定公理正确、完整且工具没有超时的前提下能强力回答“候选是否由B推出”，但不能证明公理本身真实或完备；基准准确率证明候选生成能力，不能单独证明整个科学发现循环在开放领域有效。

## 证据层级与混合处理（Evidence Hierarchy / Mixed Evidence）

1. **最高层：机器核验的逻辑结果**。给定明确背景理论，KeYmaera成功证明Langmuir单站点方程；移除必要公理后不能再证明，并可构造数值反例 [SI p.15-16]。
2. **直接计算证据**。三类真实数据案例报告候选公式、数值误差、reasoning error、变量依赖与可推导性 [Main p.4-8]。
3. **比较证据**。81个Feynman问题与四个基线比较候选公式命中率；另在15个两变量问题上加入TuringBot [SI p.19-33]。
4. **方法解释**。gentree、L-monomial、剪枝和并行MINLP描述算法为何能搜索较丰富表达式 [SI p.5-11]。
5. **作者前景判断**。自动抽取公理、引入abduction或optimal experimental design尚未实现，应与已完成系统分开 [Main p.7-8]。

## 核心内容（Core Content）

- **系统合同**：输入为背景知识`B`、候选函数语法/约束`C`、数据`D`和建模者偏好`M`。SR模块产生候选并计算数据误差ε，reasoning模块评估相对背景理论的距离β、可推导性与反例；可推导候选被选中，否则返回质量评估并要求更多数据或约束 [Main p.2-3]。
- **符号回归引擎**：作者枚举运算符已确定、叶节点为L-monomial的generalized expression trees，再为每棵树建立MINLP；并行求解、剪枝和lower-bound终止降低搜索负担 [SI p.6-9]。实验设置限制树深`d=3`、常数范围`[-100,100]`、整数幂`[-2,2]`、误差容限`10^-4` [SI p.11]。
- **开普勒案例**：太阳系数据的低误差公式可忽略质量变量，reasoning-domain扩张暴露了缺失依赖；二元星候选`√(d³/(0.9967m1+m2))`在三个变量上均显示正确依赖，广义reasoning error为0.0020 [Main p.4-5]。本文没有声称SR在所有开普勒数据上直接恢复精确真式。
- **时间膨胀案例**：系统未恢复爱因斯坦原式，但能界定候选公式的可验证外推区间，并判定这些候选与替代的“Newtonian light”公理系统不兼容，从而让数据间接参与理论系统辨别 [Main p.5-6; SI p.3-4, p.15]。
- **Langmuir案例**：作者把站点平衡、吸附/解吸速率、平衡和质量守恒写成公理；对SR中的数值常数先改写为存在量化变量，再检验公式形状是否由理论推出 [Main p.6-7; SI p.15-18]。拟合更好的公式不一定可推导：Langmuir数据上`f1`的相对误差低于可证明的`f2`，正好说明ε和可推导性不能合并成一个排名 [Main p.7-8]。
- **基准结果**：在81个去除三角函数的Feynman问题上，每题取10个点并加入1%误差，AI-Descartes的SR候选命中49/81（60.49%），高于AI Feynman的33/81、PySR的40/81和BMS的39/81；作者将相对下一名的绝对提升报告为11.11个百分点 [SI p.19, p.29-31]。在手工形式化背景理论的5个问题上，reasoning模块对正确候选均能直接或通过存在量化完成推导 [SI p.32]。

## 最强证据或论证（Strongest Evidence or Arguments）

**核心发现1：可推导性可以改变仅按拟合误差得到的选择。** Langmuir原始数据中，`f1`的相对`l2`数值误差为0.0631，但推理超时且只满足2/5个热力学约束；`f2`误差为0.1799，却可由单站点理论证明并满足5/5个约束 [Main p.7-8]。这是一组直接的同数据对照，说明更低ε不等于更可信的科学模型；它仍依赖所写公理确实正确且完备。

**核心发现2：reasoning error能揭示训练数据未激发的变量依赖。** 太阳系候选`√(0.1319d³)`在观测范围内误差小，却完全忽略两颗天体质量；把变量域扩展一个数量级后，依赖诊断将`m1`和`m2`标为缺失 [Main p.5]。证据链为：窄域拟合良好 → 公理定义跨域可推导关系 → 扩域β上升 → 暴露错误变量依赖。该方法需要可计算的背景理论域，不能自动用于没有公理的任务。

**核心发现3：形式推理可区分“未找到原式”与“与理论不兼容”。** 时间膨胀案例没有恢复原公式，但候选在相对论公理下存在可验证外推区间，而在替代Newtonian公理下连数据域内的绝对广义reasoning error也大于1 [Main p.6]。这比只报告测试误差多出理论一致性信息，但尚不是对全部可能理论的开放式选择。

**核心发现4：候选生成基准有量化优势但范围受控。** 49/81的候选命中率高于三个基线，且15题子集中为13/15（86.67%） [SI p.29-32]。比较使用各工具默认设置，作者也承认逐数据集调参可能改变结果；因此它支持“当前设置下更强的候选召回”，不支持全面支配所有SR方法 [SI p.20, p.33]。

## 最可复用的 takeaway（Most Reusable Takeaways）

- **双距离模型选择（硬框架）**：将数据误差`ε(f,D)`与理论距离`β(f,B)`分开，候选应同时回答“拟合吗”和“由已知理论推出吗” [Main p.2-3]。
- **变量依赖诊断（实践启发）**：在观测数据包围盒之外逐变量扩展定义域；若β只沿某变量方向恶化，可定位遗漏变量或错误指数 [Main p.5]。只适用于背景理论足够明确、证明器能处理该域的情况。
- **数值常数抽象（可复用方法）**：将SR候选中的数值常数替换为存在量化变量，以判断公式结构能否对应理论参数，而不要求数据拟合常数与符号公理同名 [Main p.6-7; SI p.17-18]。
- **结果状态必须分开（硬边界）**：`proved`、`counterexample/disproved`、`not provable with current axioms`和`timeout`不是同一结论；两站点Langmuir候选超时后需要人工推导，说明超时不能当作错误 [SI p.17-18]。
- **物理/领域约束可作为二级筛选器（实践启发）**：Langmuir的零压、正值、单调、低压斜率和高压饱和五项约束能剔除数值上好看但物理上异常的表达式 [SI p.2-3]。
- **公理供应是系统瓶颈（暂定pattern）**：背景理论需要机器可读、正确且足够完整；自动知识抽取、abduction与实验设计仍是后续模块，而非当前闭环 [Main p.7-8]。

## 局限与开放问题（Limits / Open Questions）

- 核心推理假设背景理论正确、完整且一致；真实科学问题中公理缺失或错误会使“不可推导”难以解释 [Main p.2, p.7]。
- 机器可读科学公理稀缺，现有技术文本知识抽取质量被作者判断为不足，当前形式化高度依赖人工 [Main p.7]。
- gentree数量随深度爆炸：使用`+, -, ×, /, √`时，深度3为60棵、深度4为4485棵、深度5超过1000万棵；MINLP自身也可能指数级增长 [SI p.8-10]。
- 实验把深度限制为3、幂限制为`[-2,2]`并使用剪枝，因此并未穷尽所有该深度可表达的公式；例如需要三次幂的结构可能被遗漏 [SI p.11]。
- KeYmaera X只返回最终可证/不可证状态而不提供完整证明步骤；对存在量化常数也不能自动给出实例值，且大量结果会超时 [SI p.12, p.17-18]。
- 三个主案例规模小且领域受限；Feynman基准采用每题10点、1%人工噪声，并排除19个含三角函数的问题，外推到更高维、强噪声或未知理论任务仍属开放问题 [SI p.19, p.29-31]。
- 时间膨胀案例没有恢复目标原式；Langmuir两站点候选需要人工证明；自动公理获取、abduction与optimal experimental design尚未集成 [Main p.6-8]。
- SI比较主要评估候选列表是否包含真式，而不是统一端到端成本、证明成功率或现实科学发现效用；完整运行时间和硬件归一化比较原文未明确提供。

## 审稿人视角（Reviewer Lens）

- **taxonomy tagging**：高价值；适合标注symbolic regression、formal reasoning、scientific ML和neuro-symbolic AI。
- **literature mapping**：高价值；清楚定位于“用一般背景公理检验候选”，区别于只约束函数形状的方法 [SI p.18-20]。
- **method adoption**：中等；代码和数据公开 [Main p.8]，但BARON/KeYmaera工具链、手工公理和指数级搜索成本提高复现门槛。
- **mechanism tracing**：对“算法决策链”价值高，对自然科学机制本身价值有限；系统验证的是公理蕴含，而不是自动发现公理真实性。
- **follow-up search**：高价值；可沿reasoning error、counterexample-guided SR、machine-readable axioms、abduction和OED继续检索。

论文有清晰且一致的方法证据基础，主要作用是genuinely evidentiary的方法论原型，而不是宏观AI愿景的说服文。generic已把算法事实、案例解释和未来判断分开，但其主体明显应由软件/ML方法模板接管。

## 主观打分

- **originality：8/10**——把一般背景理论公理、符号回归候选与自动定理证明连接成端到端选择框架，问题定义鲜明。
- **rigor：7/10**——有形式化合同、三类案例、基线比较和公开代码，但调参公平性、手工公理与有限案例降低普遍性。
- **evidence closure：6/10**——关键概念得到可证明案例支持，超时、未恢复目标公式、人工推导和未集成模块表明闭环仍是原型级。
- **工业应用潜力：1/10**——该催化领域轴对AI方法论文不适用；此数值仅记录模板轴错配，不代表软件或科学发现影响力。

## 核心结论总结

AI-Descartes最强的贡献是把模型选择从单一拟合误差扩展为“数据一致性 + 理论可推导性”双标准，并用开普勒变量依赖、相对论公理辨别和Langmuir可证明公式给出互补示例 [Main p.4-8]。它最主要的边界同样清晰：系统只在公理被正确形式化、搜索空间可计算且证明器不超时时成立，现实世界的公理获取、复杂度和实验闭环尚未解决。generic目前可以保存这份跨数学优化、AI和自然科学案例的笔记，但文档已呈现稳定的软件/ML方法论文轮廓；未来最应迁往scientific ML或neuro-symbolic methods模板。
