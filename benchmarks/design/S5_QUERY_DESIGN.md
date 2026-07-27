# S5 query design candidate

**Status:** proposed for owner review; not gold and not yet part of
`queries/queries.jsonl`.

**Benchmark version:** `0.1.0`

## Boundary

This document designs the 25 S5 queries required by ADR-002. The generated-note
fixtures were used only as an audited navigation aid. Before promotion, every
answer, claim, and evidence target must be independently re-annotated from the
main and SI PDFs. No candidate-note sentence may be copied into gold without
source verification.

All 25 queries use:

- `partition: d20`, because `S5` is a strict subset of D20;
- `language: zh`;
- `corpus_language: en`;
- one target S5 paper;
- stable proposed claim and evidence-group IDs that do not depend on chunk
  boundaries.

Each paper contributes the same five query roles:

1. main-paper exact lookup;
2. SI or methods lookup;
3. main-plus-SI multi-hop synthesis;
4. mechanism, inference, or validity boundary;
5. negative, ambiguity, or source-conflict handling.

## Coverage

| Dimension | Count | Design intent |
|---|---:|---|
| Total queries | 25 | Five per paper and domain |
| Chinese query to English corpus | 25 | Exercises the release-critical cross-language direction |
| Answerable | 20 | Positive retrieval and synthesis |
| Negative or boundary-sensitive | 5 | Two conflicting, one ambiguous, one false-premise, one no-answer |
| Exact-token | 18 | Values, units, sample counts, solver states, or named configurations |
| SI | 20 | At least two SI-dependent queries per paper |
| Multi-hop | 14 | At least one required main-plus-SI pair per paper |

Slice counts overlap. S5 is a wiring and boundary smoke suite, not a
statistically powered estimate of per-slice quality.

## Proposed qrel skeleton

- Each query's named target paper is the initial positive document judgment
  with proposed relevance `3`, including negative queries whose correct answer
  is a source-bounded conflict, correction, or abstention.
- Evidence units that directly establish an expected claim receive proposed
  relevance `3`; evidence that establishes only one part of a multi-hop answer
  receives `2`; useful method or limitation background receives `1`.
- No evidence unit is created from generated-note prose. Exact PDF quotes and
  locators are required before any proposed evidence group can become a qrel
  target.
- Relevance `0` and unjudged `-1` candidates are added only after pooling
  lexical, dense, hybrid, reranker, and human candidate runs. They are not
  guessed during query design.
- `assessor_id` and `adjudication_status` remain unset in this proposal. They
  are filled by the annotation and second-person review workflow.

## Catalysis and materials

Target paper: `liu-2024-single-atom-cobalt-orr`

### `s5.cat.01`

- **Query:** 在 O2 饱和 0.1 M KOH、1600 rpm 和 10 mV s^-1 条件下，CR-Co/ClNC 的半波电位、0.85 V 动力学电流密度和 Tafel 斜率分别是多少？同条件下 Pt/C 与 Co/NC 的 0.85 V 动力学电流密度是多少？
- **Answerability:** `answerable`
- **Slices:** `paper-specific`, `exact-token`, `cross-language`
- **Expected answer outline:** `0.93 V vs RHE`; main-text `Jk = 95.2 mA cm^-2`; `66 mV dec^-1`; Pt/C `16.7 mA cm^-2`; Co/NC `4.7 mA cm^-2`.
- **Proposed claims:** `cl.s5.cat.01.metrics`
- **Required evidence groups:** `eg.s5.cat.01.main-performance`
- **Source targets for annotation:** Main physical page 4. The separate
  `95.2`/`92.2` conflict belongs to `s5.cat.05` and must not be silently hidden
  in this key.

### `s5.cat.02`

- **Query:** RRDE 与 Koutecky-Levich 两条证据链如何共同支持 CR-Co/ClNC 主要走四电子 ORR 路径？请给出环收集效率、电子转移数、H2O2 产率和变转速范围。
- **Answerability:** `answerable`
- **Slices:** `method`, `si`, `exact-token`, `multi-hop`, `cross-language`
- **Expected answer outline:** Pt ring collection efficiency `N = 0.37`;
  `n ≈ 3.96`; `H2O2 < 3%`; K-L rotation range `400–2500 rpm`; the two
  methods are mutually supportive but do not provide an independent chemical
  peroxide assay.
- **Proposed claims:** `cl.s5.cat.02.selectivity`,
  `cl.s5.cat.02.limit`
- **Required evidence groups:** `eg.s5.cat.02.main-rrde`,
  `eg.s5.cat.02.si-kl`
- **Source targets for annotation:** Main physical pages 5 and 8; SI physical
  pages 20–22.

### `s5.cat.03`

- **Query:** 哪些主文与 SI 证据支持活性位由静态 Cl-Co-N4 在工作电位下转变为配位不饱和的 Cl-Co-N2，并进一步关联到 *O/*OOH 中间体差异？这一机理链还缺少什么直接验证？
- **Answerability:** `answerable`
- **Slices:** `mechanism-causal`, `si`, `multi-hop`, `cross-language`
- **Expected answer outline:** ex situ Co-N/Co-Cl coordination near `4/1`;
  at `1.00 V`, Co-N drops to about `2.2` while Co-Cl stays near `1`; at
  `0.90/0.75 V`, about `1.1` Co-O coordination appears; d-electron changes and
  the `895 cm^-1` versus `1080 cm^-1` SRIR candidates support the interpretation.
  Missing isotope shifts, direct bond-breaking kinetics, or adsorption-energy
  validation prevent a complete causal closure.
- **Proposed claims:** `cl.s5.cat.03.reconstruction`,
  `cl.s5.cat.03.intermediate-limit`
- **Required evidence groups:** `eg.s5.cat.03.main-operando`,
  `eg.s5.cat.03.si-fits`
- **Source targets for annotation:** Main physical pages 2–6 and 8; SI
  physical pages 34 and 40.

### `s5.cat.04`

- **Query:** 半电池性能转移到水系 Zn-air 电池后，开路电压、最大功率密度、10 mA cm^-2 比容量和耐久窗口分别是多少？这些结果为什么仍不足以证明工业寿命或通用器件可迁移性？
- **Answerability:** `answerable`
- **Slices:** `paper-specific`, `si`, `exact-token`, `multi-hop`, `cross-language`
- **Expected answer outline:** `1.50 V`; `176.6 mW cm^-2`;
  `745 mA h gZn^-1`; main cycling window about `30 h` and supplementary
  curves about `48 h`. Missing cathode loading, repeat uncertainty, long-term
  failure analysis, water/salt/air management, and broader device conditions
  bound the claim.
- **Proposed claims:** `cl.s5.cat.04.device`,
  `cl.s5.cat.04.device-limit`
- **Required evidence groups:** `eg.s5.cat.04.main-zab`,
  `eg.s5.cat.04.si-durability`
- **Source targets for annotation:** Main physical pages 6–8; SI physical
  pages 35–37.

### `s5.cat.05`

- **Query:** 该论文在 0.85 V 对 CR-Co/ClNC 的动力学电流密度究竟报告为 95.2 还是 92.2 mA cm^-2？能否只选择其中一个作为无条件答案？
- **Answerability:** `conflicting`
- **Slices:** `negative`, `exact-token`, `si`, `cross-language`
- **Expected answer outline:** Main physical page 4 reports
  `95.2 mA cm^-2`; SI Table S2 on physical page 39 reports
  `92.2 mA cm^-2`. The corpus does not reconcile them, so a faithful answer
  must preserve both values and their locations rather than choose one.
- **Proposed claims:** `cl.s5.cat.05.main-value`,
  `cl.s5.cat.05.si-value`,
  `cl.s5.cat.05.unresolved`
- **Required evidence groups:** `eg.s5.cat.05.main-95-2`,
  `eg.s5.cat.05.si-92-2`
- **Source targets for annotation:** Main physical page 4; SI physical page 39.
- **Abstention policy:** A bare refusal is insufficient. A conflict-preserving
  answer is expected.

## Biomedicine

Target paper: `papier-2024-proteomic-cancer-risk`

### `s5.bio.01`

- **Query:** 这项 UK Biobank 蛋白质组研究发现了多少种独特蛋白、多少个经癌种内多重校正的蛋白-癌症关联，其中多少关联属于血液恶性肿瘤？诊断间隔超过 7 年后还剩多少关联和独特蛋白？
- **Answerability:** `answerable`
- **Slices:** `paper-specific`, `exact-token`, `cross-language`
- **Expected answer outline:** `371` proteins; `618` associations;
  `320` hematologic-malignancy associations; after more than seven years,
  `107` associations involving `72` unique proteins remain ENT-significant.
- **Proposed claims:** `cl.s5.bio.01.discovery-map`,
  `cl.s5.bio.01.long-lag`
- **Required evidence groups:** `eg.s5.bio.01.main-discovery`,
  `eg.s5.bio.01.main-time-stratification`
- **Source targets for annotation:** Main physical pages 2–5.

### `s5.bio.02`

- **Query:** 该研究如何用蛋白主成分定义每个癌种的有效检验数和显著性阈值？Cox 模型的时间尺度与癌种特异协变量在哪里说明，这一做法没有控制哪一层面的总体错误率？
- **Answerability:** `answerable`
- **Slices:** `method`, `si`, `exact-token`, `multi-hop`, `cross-language`
- **Expected answer outline:** `639` protein principal components explain
  95% of variance; each cancer uses `p < 0.05/639`; age is the Cox time scale;
  cancer-specific covariates are specified in SI. The paper does not clearly
  state an additional family-wise correction across all 19 cancers and nine
  sub-sites.
- **Proposed claims:** `cl.s5.bio.02.ent`,
  `cl.s5.bio.02.global-testing-limit`
- **Required evidence groups:** `eg.s5.bio.02.main-statistics`,
  `eg.s5.bio.02.si-covariates`
- **Source targets for annotation:** Main physical pages 8–9; SI physical
  pages 3–4.

### `s5.bio.03`

- **Query:** 哪四个蛋白-癌症候选在主观察分析、超过 7 年诊断间隔、cis-pQTL 和 exGS 中方向一致？为什么这种四柱汇合仍只能用于候选优先级，而不能直接视为目标蛋白的因果证明？
- **Answerability:** `answerable`
- **Slices:** `mechanism-causal`, `si`, `multi-hop`, `cross-language`
- **Expected answer outline:** `SFTPA2–lung cancer`,
  `TNFRSF1B–non-Hodgkin lymphoma`, `CD74–non-Hodgkin lymphoma`, and
  `ADAM8–leukemia`. The extra analyses use conventional significance rather
  than all passing their own multiplicity thresholds; observational and
  genetic analyses share UK Biobank, and pleiotropy/linkage/proteoform issues
  remain.
- **Proposed claims:** `cl.s5.bio.03.four-candidates`,
  `cl.s5.bio.03.causal-limit`
- **Required evidence groups:** `eg.s5.bio.03.main-convergence`,
  `eg.s5.bio.03.main-genetic-limit`
- **Source targets for annotation:** Main physical pages 5–7 and 10; SI
  evidence should be used only where it directly supports a candidate or
  method detail.

### `s5.bio.04`

- **Query:** 药物与组织表达映射中，304、83、38 和 9 分别计数什么实体？为什么“9”不能写成九种药物，也不能据此断言这些蛋白是可安全预防癌症的靶点？
- **Answerability:** `answerable`
- **Slices:** `exact-token`, `si`, `multi-hop`, `cross-language`
- **Expected answer outline:** `304` and `83` are protein-cancer association
  counts whose encoded genes exceed the stated expression proportions;
  `38` associated proteins are targets of approved drugs; `9` associated
  proteins are targets of drugs indicated for the corresponding cancer.
  These mappings neither count nine drugs nor establish causal, preventive,
  directionally safe intervention.
- **Proposed claims:** `cl.s5.bio.04.entity-counts`,
  `cl.s5.bio.04.translation-limit`
- **Required evidence groups:** `eg.s5.bio.04.main-drug-map`,
  `eg.s5.bio.04.si-expression-method`
- **Source targets for annotation:** Main physical pages 2, 4–5, and 8–9; SI
  physical pages 5 and 8–11.

### `s5.bio.05`

- **Query:** UKB-PPP 蛋白测量流程的起始人数到底是 54,306 还是 54,221？这两个数字与最终最大观察分析样本 44,645 分别代表什么，85 人差值能否由论文确定解释？
- **Answerability:** `ambiguous`
- **Slices:** `negative`, `exact-token`, `si`, `cross-language`
- **Expected answer outline:** Main physical page 8 states that measurements
  were generated for `54,306` selected participants; the exclusion flow on
  main physical page 9 starts from `54,221` Olink-measured participants and
  leads to a maximum analysis sample of `44,645`. The supplied source set does
  not explain the `85`-participant difference.
- **Proposed claims:** `cl.s5.bio.05.54306`,
  `cl.s5.bio.05.54221`,
  `cl.s5.bio.05.unexplained`
- **Required evidence groups:** `eg.s5.bio.05.main-program-count`,
  `eg.s5.bio.05.main-flow-count`,
  `eg.s5.bio.05.si-flow`
- **Source targets for annotation:** Main physical pages 8–9; SI physical
  page 6.
- **Abstention policy:** The answer must retain both contexts. Choosing one
  denominator or inventing the missing flow step is an error.

## Computer science and machine learning

Target paper: `cornelio-2023-ai-descartes`

### `s5.cs.01`

- **Query:** AI-Descartes 的符号回归组件在修改后的 Feynman Symbolic Regression Database 上用了多少个任务、每题多少数据点和多大噪声？它与 AI Feynman、PySR、BMS 的命中数分别是多少？
- **Answerability:** `answerable`
- **Slices:** `paper-specific`, `si`, `exact-token`, `cross-language`
- **Expected answer outline:** `81` non-trigonometric tasks; `10` points per
  task; `1%` error; AI-Descartes `49/81`, AI Feynman `33/81`, PySR `40/81`,
  BMS `39/81`.
- **Proposed claims:** `cl.s5.cs.01.benchmark`
- **Required evidence groups:** `eg.s5.cs.01.si-benchmark`
- **Source targets for annotation:** SI physical pages 19–20 and 29–33.

### `s5.cs.02`

- **Query:** 该 MINLP 符号回归实现的表达式树深度、常数范围、幂范围、tau 参数和平方误差停止阈值分别是什么？剪枝和限时求解对“没有找到公式”的解释有什么限制？
- **Answerability:** `answerable`
- **Slices:** `method`, `si`, `exact-token`, `cross-language`
- **Expected answer outline:** depth `3`; constants `[-100,100]`; powers
  `[-2,2]`; `tau = 6`; squared-error threshold `10^-4`. Bounded-power pruning
  can remove otherwise nonredundant trees, and time limits do not prove that a
  target expression does not exist or that a global optimum was returned.
- **Proposed claims:** `cl.s5.cs.02.search-config`,
  `cl.s5.cs.02.search-limit`
- **Required evidence groups:** `eg.s5.cs.02.si-minlp`,
  `eg.s5.cs.02.si-pruning`
- **Source targets for annotation:** SI physical pages 5–11.

### `s5.cs.03`

- **Query:** 在开普勒和时间膨胀案例中，数据误差 epsilon 与理论距离 beta 如何帮助识别低训练误差公式的外推问题？回答时要区分数据拟合、bounded-distance 和理论选择各自能支持到什么程度。
- **Answerability:** `answerable`
- **Slices:** `mechanism-causal`, `si`, `multi-hop`, `cross-language`
- **Expected answer outline:** Low empirical error alone can retain formulas
  with the wrong variable dependence or extrapolation. Theory distance and
  bounded generalization checks expose those differences in the tested domain;
  the time-dilation case compares one relativity axiom set with one constructed
  Newtonian alternative and therefore does not establish general theory
  selection.
- **Proposed claims:** `cl.s5.cs.03.dual-distance`,
  `cl.s5.cs.03.scope-limit`
- **Required evidence groups:** `eg.s5.cs.03.main-cases`,
  `eg.s5.cs.03.si-bounds`
- **Source targets for annotation:** Main physical pages 4–6; SI physical
  pages 1–4 and 12–15.

### `s5.cs.04`

- **Query:** Langmuir 案例中 f2、g2、f4、f5、g1、g5 和 g7 的证明状态分别是什么？哪些是自动证明、明确 No、timeout，以及 timeout 后由人工参数实例化完成的证明？
- **Answerability:** `answerable`
- **Slices:** `paper-specific`, `si`, `exact-token`, `multi-hop`, `cross-language`
- **Expected answer outline:** `f2/g2 = Yes`; `f4/f5/g1 = No`;
  `g5/g7 = Timeout` in the automatic run and are later closed through manual
  parameter instantiation. `No`, timeout, counterexample, and manual closure
  must remain distinct.
- **Proposed claims:** `cl.s5.cs.04.states`,
  `cl.s5.cs.04.manual-closure`
- **Required evidence groups:** `eg.s5.cs.04.main-table3`,
  `eg.s5.cs.04.si-proof-details`
- **Source targets for annotation:** Main physical page 8; SI physical
  pages 15–18.

### `s5.cs.05`

- **Query:** KeYmaera X 是否已经证伪了双站点 Langmuir 候选 g5 和 g7，因此可以断言它们与背景理论不一致？
- **Answerability:** `false-premise`
- **Slices:** `negative`, `si`, `exact-token`, `cross-language`
- **Expected answer outline:** No. The automatic runs timed out; timeout is
  neither disproof nor a preserved counterexample. The authors subsequently
  supplied parameter instances and manually proved equivalence for `g5/g7`.
- **Proposed claims:** `cl.s5.cs.05.timeout`,
  `cl.s5.cs.05.manual-proof`
- **Required evidence groups:** `eg.s5.cs.05.main-timeout`,
  `eg.s5.cs.05.si-manual-proof`
- **Source targets for annotation:** Main physical page 8; SI physical
  page 18.
- **Abstention policy:** Correcting the premise is preferred. Repeating the
  premise or treating timeout as `No` is a false answer.

## Environment, energy, and geoscience

Target paper: `dorgeist-2024-terrestrial-carbon-fluxes`

### `s5.env.01`

- **Query:** 2012–2021 年，瞬态碳密度相对工业化前和近似现今碳密度分别使 ELUC 增加多少？使用工业化前土地覆盖估算 SLAND 又把陆地汇高估多少？请保留百分比、范围和 GtC yr^-1 数值。
- **Answerability:** `answerable`
- **Slices:** `paper-specific`, `exact-token`, `cross-language`
- **Expected answer outline:** Relative to `ELUC,pi`, `ELUC,trans` is
  `28% (21%–38%)` or `0.34 (0.18–0.56) GtC yr^-1` higher; relative to
  `ELUC,pd`, it is `14% (8%–23%)` higher. The preindustrial-cover assumption
  overestimates the sink by `23% (8%–33%)`, with `RSS = 0.7 (0.3–1.3)
  GtC yr^-1`.
- **Proposed claims:** `cl.s5.env.01.eluc`,
  `cl.s5.env.01.sland`
- **Required evidence groups:** `eg.s5.env.01.main-recent`,
  `eg.s5.env.01.si-window-table`
- **Source targets for annotation:** Main physical pages 2–5; SI physical
  page 17.

### `s5.env.02`

- **Query:** BLUEpi、BLUEpd、BLUEtrans、BLUEtrans+m 和 BLUES2 五种设置各自承担什么反事实角色？delta-L、SLAND,trans、RSS 和 LASC 分别由哪些运行差分定义？
- **Answerability:** `answerable`
- **Slices:** `method`, `si`, `exact-token`, `multi-hop`, `cross-language`
- **Expected answer outline:** The answer must map all five scenarios rather
  than only list their names, and reconstruct the main difference equations:
  `delta-L = DeltaCA,BLUEtrans - DeltaCA,BLUEpi`;
  `SLAND,trans = (DeltaCL,BLUEtrans+m - DeltaCL,BLUEpi) - delta-L`;
  `RSS = SLAND,trans - SLAND,pi`; `LASC = delta-L + RSS`.
- **Proposed claims:** `cl.s5.env.02.scenarios`,
  `cl.s5.env.02.equations`
- **Required evidence groups:** `eg.s5.env.02.main-equations`,
  `eg.s5.env.02.si-flowchart`
- **Source targets for annotation:** Main physical pages 10–12; SI physical
  page 4.

### `s5.env.03`

- **Query:** 瞬态植被和土壤碳密度分别由多少个 DGVM 构造，为什么 JSBACH 改用 TRENDYv12？全球碳密度时间序列还经过了什么平滑与末端外推处理？
- **Answerability:** `answerable`
- **Slices:** `method`, `si`, `exact-token`, `cross-language`
- **Expected answer outline:** `8` DGVMs for vegetation trends and `5` for
  soil; JSBACH uses TRENDYv12 because of a v11 PFT-output setup error; the
  globally aggregated series uses a `20-year` moving average and linearly
  extrapolates the final `9 years`.
- **Proposed claims:** `cl.s5.env.03.inputs`,
  `cl.s5.env.03.processing`
- **Required evidence groups:** `eg.s5.env.03.main-dgvm`,
  `eg.s5.env.03.si-processing`
- **Source targets for annotation:** Main physical pages 9–10; SI physical
  pages 2–3 and 16.

### `s5.env.04`

- **Query:** 为什么 BLUE 的近期净陆地通量与 TRENDY、反演和 O2 约束在量级上相容，仍不能验证 ELUC、SLAND 与 RSS 的现实唯一拆分？哪些共享输入或误差抵消会制造“总量对、分量未必对”？
- **Answerability:** `answerable`
- **Slices:** `mechanism-causal`, `multi-hop`, `cross-language`
- **Expected answer outline:** Total estimates overlap in magnitude, but
  component attribution is counterfactual and RSS is not directly observable.
  Shared TRENDY/LUH2 information, different windows and uncertainty meanings,
  and compensating component errors can yield total agreement without
  identifying the split.
- **Proposed claims:** `cl.s5.env.04.total-agreement`,
  `cl.s5.env.04.nonidentification`
- **Required evidence groups:** `eg.s5.env.04.main-total-comparison`,
  `eg.s5.env.04.main-attribution-limit`
- **Source targets for annotation:** Main physical pages 4–6, 8, and 12.

### `s5.env.05`

- **Query:** 根据这组论文与 SI，现实世界中唯一正确的 ELUC、SLAND 和 RSS 数值拆分是什么？DGVM 趋势缩放碳密度与 LUH2 土地利用强迫中，哪一个已经被独立观测证明为正确输入？
- **Answerability:** `no-answer`
- **Slices:** `negative`, `si`, `multi-hop`, `cross-language`
- **Expected answer outline:** The supplied corpus cannot identify a unique
  real-world component split or certify either input branch as correct. It
  provides model-internal definitions, total-flux comparisons, and sensitivity
  evidence; the components are not independently observed and the input
  alternatives are not jointly discriminated.
- **Proposed claims:** `cl.s5.env.05.nonidentifiable`,
  `cl.s5.env.05.input-uncertainty`
- **Required evidence groups:** `eg.s5.env.05.main-observability-limit`,
  `eg.s5.env.05.main-input-sensitivity`,
  `eg.s5.env.05.si-model-differences`
- **Source targets for annotation:** Main physical pages 5–8 and 12; SI
  physical pages 11 and 13–15.
- **Abstention policy:** An evidence-bounded “not identifiable from this
  source set” is correct. Inventing a single observed split is a false answer.

## Social science and economics

Target paper: `smith-2024-supply-chain-regulations`

### `s5.soc.01`

- **Query:** 巴西、印度和印度尼西亚在线样本对全部供应链监管方案的平均支持率分别是多少？三国对低、中、高三个理想型方案的模式说明了什么，又不能外推成什么？
- **Answerability:** `answerable`
- **Slices:** `paper-specific`, `exact-token`, `cross-language`
- **Expected answer outline:** Brazil `64.5%`, India `64.1%`, Indonesia
  `50.9%`; medium/high packages retain majority or near-majority support,
  while low-intensity packages can be less popular. These are model-predicted
  hypothetical vote probabilities in online quota samples, not actual
  referenda, policy adoption, or compliance.
- **Proposed claims:** `cl.s5.soc.01.support`,
  `cl.s5.soc.01.external-limit`
- **Required evidence groups:** `eg.s5.soc.01.main-marginal-means`,
  `eg.s5.soc.01.main-design-limit`
- **Source targets for annotation:** Main physical pages 3–4 and 9–10.

### `s5.soc.02`

- **Query:** 正文与 SI 的 respondent counts、proposal-response counts，以及三个 ideal-type/group analyses 的 analytical sample/observations 分别是多少？为什么这三层计数不能相互替换？
- **Answerability:** `answerable`
- **Slices:** `method`, `si`, `exact-token`, `multi-hop`, `cross-language`
- **Expected answer outline:** Main respondents `2,000` per country and
  `6,000` total; SI respondents Brazil `2,000`, Indonesia `2,000`, India
  `2,001`; proposal responses `20,000/20,000/20,010`; ideal-type analytical
  sample/observations `1,761/1,615/1,723`. They count different entities and
  analysis stages.
- **Proposed claims:** `cl.s5.soc.02.respondents`,
  `cl.s5.soc.02.responses`,
  `cl.s5.soc.02.analysis-observations`
- **Required evidence groups:** `eg.s5.soc.02.main-counts`,
  `eg.s5.soc.02.si-counts`
- **Source targets for annotation:** Main physical pages 8 and 10–11; SI
  physical pages 15–19.

### `s5.soc.03`

- **Query:** 随机信息提示实验实际支持“没有发现合并样本平均显著效应”还是“公众偏好已经被证明具有韧性”？要把后者成立还缺哪些国别、等效性和处理忠实度证据？
- **Answerability:** `answerable`
- **Slices:** `mechanism-causal`, `si`, `multi-hop`, `cross-language`
- **Expected answer outline:** The randomized experiment supports only the
  reported pooled null-significance result under the tested messages. It does
  not prove equivalence or immutable preferences. Country-level effects,
  preregistered equivalence bounds/power, and manipulation checks are needed.
- **Proposed claims:** `cl.s5.soc.03.null-result`,
  `cl.s5.soc.03.resilience-limit`
- **Required evidence groups:** `eg.s5.soc.03.main-information-result`,
  `eg.s5.soc.03.si-treatment-wording`
- **Source targets for annotation:** Main physical pages 5, 7, and 10–11; SI
  physical page 9.

### `s5.soc.04`

- **Query:** 为什么三项收益评分普遍高于三项成本评分，只能支持“评分次序”，不能单独证明“收益预期因果驱动政策支持”？什么实验或中介设计才能区分这两种主张？
- **Answerability:** `answerable`
- **Slices:** `mechanism-causal`, `si`, `multi-hop`, `cross-language`
- **Expected answer outline:** The vignette randomizes policy intensity and
  measures six 1–7 ratings, but it does not randomize a specific belief and
  identify belief-to-support mediation; item wording, acquiescence, and
  non-comparable constructs remain alternatives. A validated belief
  manipulation followed by support measurement and a preregistered mediation
  estimand would test the stronger claim.
- **Proposed claims:** `cl.s5.soc.04.rating-order`,
  `cl.s5.soc.04.mediation-limit`
- **Required evidence groups:** `eg.s5.soc.04.main-ratings`,
  `eg.s5.soc.04.si-vignette`
- **Source targets for annotation:** Main physical pages 3–5, 7, and 10; SI
  physical page 8.

### `s5.soc.05`

- **Query:** 论文用于排除过快完成者的阈值究竟是中位时长的 45%、7 分 03 秒，还是 50%、7 分 08 秒？能否把其中一个写成没有争议的唯一标准？
- **Answerability:** `conflicting`
- **Slices:** `negative`, `exact-token`, `si`, `cross-language`
- **Expected answer outline:** Main physical page 9 reports `45%` and
  `7:03`; the SI table on physical page 11 reports `50% Median Time` and
  `7:08`. The supplied source set does not reconcile the discrepancy, so both
  must be retained.
- **Proposed claims:** `cl.s5.soc.05.main-threshold`,
  `cl.s5.soc.05.si-threshold`,
  `cl.s5.soc.05.unresolved`
- **Required evidence groups:** `eg.s5.soc.05.main-threshold`,
  `eg.s5.soc.05.si-threshold`
- **Source targets for annotation:** Main physical page 9; SI physical page 11.
- **Abstention policy:** A conflict-preserving answer is required; silently
  choosing one threshold is incorrect.

## Promotion checklist

The proposal can enter official benchmark files only after all of the
following are complete:

1. The owner approves or edits the 25 natural-language query texts without
   seeing retrieval results.
2. A source annotator independently writes the reference answer and atomic
   claims from the PDFs, not from generated-note prose.
3. Each proposed evidence group receives at least one exact source quote,
   zero-based `pdf_page_index`, canonical-page hash, quote hash, and canonical
   character offsets or PDF bounding box.
4. A second person reviews every evidence unit and claim; at least 20% are
   independently recreated blind.
5. Negative items are adjudicated as `no-answer`, `false-premise`,
   `ambiguous`, or `conflicting`; these labels are not collapsed.
6. Official `queries.jsonl`, `answers.jsonl`, `claims.jsonl`,
   `evidence_units.jsonl`, document qrels, evidence qrels, and `s5.yaml`
   membership are updated atomically and pass `validate_benchmark.py` without
   `--allow-empty`.
7. Query wording and gold data are frozen before any retriever output is
   inspected. Retrieval failures may diagnose the system but may not be used
   to rewrite a difficult S5 query.
