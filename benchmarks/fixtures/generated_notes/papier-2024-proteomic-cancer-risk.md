---
title_en: Identifying proteomic risk factors for cancer using prospective and exome analyses of 1463 circulating proteins and risk of 19 cancers in the UK Biobank
title_zh: 利用英国生物样本库中1463种循环蛋白、19种癌症的前瞻性与外显子分析识别蛋白质组风险因子
authors:
  - Keren Papier
  - Joshua R. Atkins
  - Tammy Y. N. Tong
  - Kezia Gaitskell
  - Trishna Desai
  - Chibuzor F. Ogamba
  - Mahboubeh Parsaeian
  - Gillian K. Reeves
  - Ian G. Mills
  - Tim J. Key
  - Karl Smith-Byrne
  - Ruth C. Travis
year: 2024
journal: Nature Communications
doi: 10.1038/s41467-024-48017-6
keywords:
  - UK Biobank
  - plasma proteomics
  - cancer risk
  - prospective cohort
  - cis-pQTL
  - exome-wide genetic score
  - reverse causality
  - Olink
topic:
  - circulating protein-cancer associations
  - multi-omics triangulation
  - cancer risk biomarkers
  - genetic epidemiology
research_domain: other
document_type: research-article
note_template: generic-research-note
seed_terms:
  - UK Biobank
  - Olink plasma proteomics
  - protein-cancer risk association
  - cis-pQTL
  - exome-wide genetic score
scope_hint: core
signal_quality: strong
routing_confidence: high
combined_hash: 8de16fddd303c9322ac947b3f0a32abbeae46baf3f4821a9fce33f5b5ee7f5dc
pdf_0_name: main.pdf
pdf_0_artifact_path: corpus/files/papier-2024-proteomic-cancer-risk/main.pdf
pdf_1_name: si.pdf
pdf_1_artifact_path: corpus/files/papier-2024-proteomic-cancer-risk/si.pdf
tags: []
candidate_tags_high: []
candidate_tags_medium: []
candidate_tags_low: []
human_reviewed: 0
---

这是一篇以英国生物样本库为基础的前瞻性蛋白质组—遗传流行病学研究。作者在44,645名参与者中联合分析1463种血浆蛋白、19类癌症及9个亚型，并以距诊断时间、cis-pQTL和外显子范围遗传评分（exGS）对观察性关联进行三角验证；核心结果是发现618个蛋白—癌症关联，并将SFTPA2、TNFRSF1B、CD74和ADAM8收敛为得到多条方向一致证据支持的候选因子 [Main p.2, p.4-6]。它不适合现有催化专用模板，但其“观察关联 → 时间分层 → 遗传支持 → 生物学语境”的证据分层非常适合作为跨域检索与证据判级样本。

## 文献基本信息

- **推荐保存文件名**：`2024-NatCommun-RuthCTravis-UKBiobank血浆蛋白组-泛癌风险-前瞻队列与外显子三角验证-筛选病因候选蛋白_review_note.md`
- **标题**：Identifying proteomic risk factors for cancer using prospective and exome analyses of 1463 circulating proteins and risk of 19 cancers in the UK Biobank
- **期刊与年份**：Nature Communications, 2024
- **DOI**：10.1038/s41467-024-48017-6 [Main p.1]

## 文档类型与边界（Document Type / Boundary）

本文是原始研究论文，主导材料是一手队列数据与遗传关联分析；引言中的既往标志物、讨论中的生物学解释以及药物靶点映射只承担语境和解释功能，不能与原始风险估计同权 [Main p.1, p.4-10]。其结构其实相当稳定，留在generic的原因主要是**主题不匹配**：现有模板没有生物医学队列、多组学或遗传流行病学合同，而非文档本身混乱。`methods-or-materials-synthesis`是最近的备选，但本文贡献重心是跨癌种实证发现而不是提出可独立复用的新实验协议；未来最合理的迁移目标是“biomedical multi-omics / prospective epidemiology”专用模板。

## 重新路由触发条件（Re-routing Triggers）

若模板库新增以下任一合同，应离开generic：①以样本、暴露、结局、协变量、效应量和敏感性分析为骨架的前瞻性队列模板；②明确区分观察关联、Mendelian/genetic support与因果主张的多组学模板；③面向生物标志物发现、验证和临床转化的模板。本文全文均由上述结构主导，generic只应承担当前的跨域兜底说明，而不应长期替代专用流行病学模板。

## 核心问题（Main Question）

在基线血浆中测得的1463种蛋白里，哪些与后续19种癌症及9个亚型的发生风险相关；其中哪些关联能在超过7年的诊断滞后、cis-pQTL和exGS分析中获得方向一致支持，从而比单纯的近期诊断标志物更接近潜在病因候选因子 [Main p.1-2, p.10]？

## 主导证据类型（Dominant Evidence Type）

主导证据是大样本前瞻性数据分析：Olink相对定量蛋白、癌症登记结局和Cox模型构成第一证据柱；距诊断时间分层用于诊断反向因果风险；cis-pQTL与exGS提供偏倚结构不同的遗传支持 [Main p.8-10]。这套组合擅长筛出“值得继续验证”的候选蛋白，但单凭关联与方向一致性仍不能证明蛋白改变会导致癌症。组织/细胞mRNA表达、GO富集和药物靶点数据库属于间接语境证据 [Main p.4, p.8-9; SI p.5, p.8-11]。

## 证据层级与混合处理（Evidence Hierarchy / Mixed Evidence）

1. **直接证据**：44,645人的基线蛋白浓度与随访癌症结局、风险因子校正后的HR及95% CI [Main p.2, p.8-9]。
2. **诊断性支持**：按`<3年`、`3-7年`、`>7年`分层，检查近期临床前疾病是否驱动关联 [Main p.4, p.9]。
3. **正交但仍间接的遗传支持**：cis-pQTL和exGS在最多约33.7万名欧洲血统参与者中评估遗传预测蛋白与癌症风险的关系 [Main p.6, p.10]。
4. **解释性材料**：细胞/组织表达、GO通路、现有药物靶点以及讨论中的生物学合理性帮助排序，但不能升级为机制证明 [Main p.4, p.7-8; SI p.5, p.8-11]。
5. **补充材料角色**：SI给出癌种特异协变量合同和各癌种完整火山图；例如肺癌多种蛋白在充分校正后效应明显衰减，说明吸烟等混杂会改变风险估计 [SI p.3-4, p.7]。

## 核心内容（Core Content）

- **队列与测量**：UK Biobank共503,317名成人，Olink项目测量54,306人；经质控及排除基线癌症、糖尿病和部分激素用药等后，最大观察分析样本为44,645人。1463种蛋白由四个384-plex面板测量，NPX为log2尺度并经标准化处理 [Main p.8-9]。
- **泛癌关联图谱**：平均随访约12年、4921例恶性肿瘤中，371种蛋白形成618个经有效检验数（ENT，639个独立检验）校正后的蛋白—癌症关联；其中320个关联来自血液系统恶性肿瘤 [Main p.2-4]。
- **时间分层**：618个关联中有107个在采血后超过7年才诊断的病例分析中仍达ENT显著；398个也在3年内诊断分析中显著，后者可能包含较强的反向因果成分 [Main p.4-5]。
- **遗传分析**：939个cis-pQTL覆盖294种蛋白；三个提高TNFRSF14的cis-pQTL与较低NHL风险相关。exGS在533个可分析关联中得到28个多重校正后关联，但部分评分由trans变异主导，解释时不能简单等同于目标蛋白的直接因果效应 [Main p.5, p.7, p.10]。
- **证据收敛**：SFTPA2—肺癌（观察HR 1.24，95% CI 1.14-1.35）、TNFRSF1B—NHL（1.28，1.19-1.37）、CD74—NHL（1.68，1.49-1.90）及ADAM8—白血病（1.87，1.69-2.06）同时获得长期诊断间隔、cis-pQTL和exGS的方向一致、常规显著支持 [Main p.5-6]。
- **转化语境**：38种风险相关蛋白也是已上市药物靶点，其中9种已有方向一致遗传支持；作者同时明确指出，治疗性预防仍需功能实验、毒性评估和更多研究 [Main p.4-5, p.8]。

## 最强证据或论证（Strongest Evidence or Arguments）

**核心发现1：候选关联并非只来自单一癌种或单一统计模型。** 618个ENT显著关联来自跨19种癌症的统一分析，且癌种特异SI图保留了效应量与显著性分布；例如肾癌HAVCR1的HR为2.88，NHL中PDCD1为1.99，多发性骨髓瘤中SLAMF7为3.07 [Main p.2-4; SI p.27, p.30, p.32]。统一框架和多重检验校正增强筛选可信度，但不同癌种病例数与混杂结构不等，因此不能横向把HR大小直接解释成同等生物学效应。

**核心发现2：时间分层能把“临近诊断信号”与“长期先行信号”初步分开。** 107个关联在`>7年`诊断组仍显著，而398个在`<3年`诊断组显著 [Main p.4-5]。该对照比横断面关联更有说服力，但作者指出慢性淋巴细胞白血病等可在临床诊断前多年存在，肝肾基础病也可长期扰动血浆蛋白，因此`>7年`并不自动排除反向因果 [Main p.8]。

**核心发现3：四个蛋白获得三路方向一致支持。** 观察风险、长期诊断间隔、cis-pQTL与exGS的收敛把SFTPA2、TNFRSF1B、CD74和ADAM8置于最高优先级 [Main p.5-7]。这是本文最强的“多条偏倚不同证据”论证，但仍是病因候选排序，不是干预实验意义上的因果闭环。

**核心发现4：充分校正可显著改变部分肺癌关联。** SI显示PLAUR的HR由1.95降至1.37、LAMP3由2.28降至1.37、SFTPA2由1.64降至1.24 [SI p.7]；这直接展示混杂控制的重要性，也说明漂亮的最小校正结果不能单独承担病因主张。

## 最可复用的 takeaway（Most Reusable Takeaways）

- **证据排序规则（实践启发）**：观察关联先经多重检验，再依次检查距诊断时间、cis遗传工具和更宽的遗传评分；只有方向一致且跨偏倚结构重复的候选才进入最高优先级。适用于组学筛选，不等于因果证明 [Main p.10]。
- **反向因果诊断（可复用但非充分条件）**：近期诊断增强、长期诊断减弱的蛋白更像临床前疾病读出；长期仍在的信号更值得病因追踪，但仍需排除长期潜伏疾病与基础器官病 [Main p.4, p.8]。
- **区分三种角色（硬边界）**：风险标志物、早期检测标志物和病因因子不能互换；同一蛋白可能因时间窗口和遗传工具而落入不同角色 [Main p.6-8]。
- **trans驱动警报（诊断规则）**：若exGS主要由其他基因中的trans变异解释，应优先考虑通路或疾病过程读出，而不是把评分直接解释为目标蛋白效应；SRP14—JAK2例子明确展示了这一失败模式 [Main p.7]。
- **组织特异性边界（实践启发）**：循环蛋白更容易捕获血液、肝、肾和肺等与血液组成或交换紧密组织的信号，对局部器官效应可能灵敏度不足 [Main p.7]。

## 局限与开放问题（Limits / Open Questions）

- 只有一次基线蛋白测量，无法估计随时间变化，且回归稀释可能低估风险 [Main p.8]。
- UK Biobank参与者以白人、总体健康风险较低的人群为主，遗传决定因素和蛋白—癌症关联能否跨祖源推广仍未闭合 [Main p.8]。
- 较少见癌症和亚型统计功效有限；当前Olink面板也只覆盖人类蛋白质组的一个子集 [Main p.7-8]。
- `>7年`关联仍可能来自长期潜伏疾病、肝硬化或慢性肾病等基础过程；时间分层降低但不消除反向因果 [Main p.8]。
- 癌种特异协变量选择基于可用数据，SI明确显示不同癌种采用不同调整集；残余混杂无法由当前分析完全排除 [SI p.3-4, p.7]。
- 四个最高优先级蛋白尚缺外部前瞻队列复制、组织/细胞功能实验、正式Mendelian randomization和干预证据；临床阈值、判别性能与增量预测价值原文未明确讨论 [Main p.8]。

## 审稿人视角（Reviewer Lens）

- **taxonomy tagging**：高价值；可稳定标为前瞻性队列、血浆蛋白组、遗传流行病学和泛癌风险。
- **literature mapping**：高价值；提供跨癌种候选地图和证据等级。
- **method adoption**：中高价值；三角验证流程可迁移，但协变量合同、工具变量质量与祖源范围需重建。
- **mechanism tracing**：中等；组织表达和遗传证据提供路径线索，却没有功能机制闭环。
- **follow-up search**：高价值；四个收敛候选、trans驱动案例和38个已上市药物靶点均形成明确检索入口。

本文主要作用是genuinely evidentiary的候选排序，而非说服性机制论文。事实、作者解释与笔记推断已分层；其高度一致的队列/遗传结构也构成清晰重路由信号，未来应迁往biomedical multi-omics或prospective epidemiology模板。

## 主观打分

- **originality：8/10**——把1463种蛋白、19种癌症、时间分层与两类外显子遗传分析放入同一前瞻框架，整合广度强。
- **rigor：8/10**——有统一多重检验、癌种特异协变量、时间敏感性分析和遗传三角验证，但仍依赖单一主队列。
- **evidence closure：7/10**——候选排序闭环较完整，外部复制、功能实验和干预因果尚未闭合。
- **工业应用潜力：1/10**——该催化领域轴对生物医学队列不适用；此数值只标记模板轴错配，不应被解释为临床价值判断。

## 核心结论总结

这篇论文值得保留的首要原因不是“发现了很多显著蛋白”，而是给出了可复用的证据分级流程：先做统一泛癌筛选，再用诊断滞后辨别反向因果风险，并用cis-pQTL与exGS寻找方向一致的正交支持 [Main p.4-6, p.10]。四个收敛候选是最强结果，但单队列、单次蛋白测量、祖源局限和缺少功能/干预验证决定了它们仍是候选而非已证实病因。generic在当前有限模板库中可用，但只是主题兜底；一旦出现生物医学多组学或前瞻性流行病学模板，应立即重路由。
