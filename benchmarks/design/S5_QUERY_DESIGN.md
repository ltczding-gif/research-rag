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

Each paper contributes the same five query roles, but none is allowed to remain
a single-span factoid:

1. constrained quantitative reconciliation;
2. SI or methods reconstruction;
3. main-plus-SI multi-hop synthesis;
4. mechanism, inference, or validity boundary;
5. negative, ambiguity, or source-conflict handling.

## Difficulty contract

Difficulty is a property of the evidence path and required reasoning, not of
question length. Each candidate is scored from 0 to 2 on five independent
dimensions:

| Dimension | 0 | 1 | 2 |
|---|---|---|---|
| Evidence dispersion (`E`) | one local span | two non-adjacent spans in one file | main + SI, or at least three non-adjacent evidence regions |
| Constraint density (`C`) | one or two answer slots | three or four linked slots | at least five linked slots, conditions, denominators, or entity types |
| Reasoning depth (`R`) | copy or list | compare, type, normalize, or connect | reconcile, rule out a shortcut, or delimit a causal/validity claim |
| Answerability control (`A`) | direct positive | incomplete evidence or a tempting overclaim must be identified | conflict, ambiguity, false premise, or justified no-answer/abstention |
| Distractor risk (`D`) | answer-bearing wording is locally unique | near-duplicate values/terms or source paraphrase | competing values, denominators, entities, stages, or superficially plausible evidence |

The sum is a design-time difficulty score:

- `hard`: 6–7/10;
- `very-hard`: 8–10/10;
- 0–5/10 is rejected from S5 rather than labeled “easy”.

Every promoted S5 query must require at least two independently annotatable
evidence groups. A model that returns one correct number or one plausible span
must still fail answer completeness when another required group is missing.
Empirical baseline results may later expose a mislabeled item, but scores must
not be changed after looking at held-out query-level results.

The current query schema has no standalone difficulty property. On promotion,
the tier is serialized without a schema change as exactly one of
`difficulty-hard` or `difficulty-very-hard` in `slice_ids`; the numeric
five-axis score remains design and audit metadata.

## Public-benchmark design inputs

Public datasets are used as design references, not as substitutes for this
repository's five main-paper-plus-SI corpus:

- **QASPER** motivates information-seeking questions whose evidence can be
  distributed across a research paper, plus explicit unanswerable cases.
- **PeerQA** motivates reviewer-grade questions, evidence retrieval, and
  answerability classification over full scientific documents.
- **BRIGHT** motivates queries whose relevant evidence cannot be found through
  surface keyword or semantic overlap alone.
- **RGB** motivates noise robustness, information integration, negative
  rejection, and resistance to conflicting or counterfactual context.
- **SciFact / SciFact-Open** motivates claim-level rationale annotation and
  pooled evidence judgments instead of accepting a fluent answer as correct.

Primary references:

- QASPER: <https://aclanthology.org/2021.naacl-main.365/>
- PeerQA: <https://aclanthology.org/2025.naacl-long.22/>
- BRIGHT: <https://arxiv.org/abs/2407.12883>
- RGB: <https://arxiv.org/abs/2309.01431>
- SciFact: <https://aclanthology.org/2020.emnlp-main.609/>
- SciFact-Open: <https://aclanthology.org/2022.findings-emnlp.347/>

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
| Hard | 10 | Score 6–7; no candidate below 6 |
| Very hard | 15 | Score 8–10; dispersed evidence plus reasoning or answerability stress |

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

- **Query:** 请为 CR-Co/ClNC、Pt/C 与 Co/NC 建立一张可复核的 RDE 性能对照：先从方法与结果中锁定电解液、气氛、转速、扫描速率和电位基准，再报告三者在 0.85 V 的动力学电流密度，并仅对 CR-Co/ClNC 补充半波电位与 Tafel 斜率；说明哪些量处于相同归一化口径，哪些看似相关的质量活性或起始电位不能替代题中指标。
- **Difficulty:** `hard` (7/10)
- **Difficulty factors:** `E=2, C=2, R=1, A=0, D=2`
- **Answerability:** `answerable`
- **Slices:** `paper-specific`, `exact-token`, `cross-language`, `difficulty-hard`
- **Expected answer outline:** In `O2`-saturated `0.1 M KOH` at `1600 rpm`
  and `10 mV s^-1`, all potentials are reported versus RHE. At `0.85 V`,
  CR-Co/ClNC, Pt/C, and Co/NC have `Jk = 95.2`, `16.7`, and
  `4.7 mA cm^-2`; CR-Co/ClNC additionally has `E1/2 = 0.93 V` and a
  `66 mV dec^-1` Tafel slope. The within-paper geometric-current comparison is
  aligned, while mass activity and onset potential use different quantities
  and cannot replace the requested metrics.
- **Proposed claims:** `cl.s5.cat.01.metrics`
- **Required evidence groups:** `eg.s5.cat.01.main-performance`,
  `eg.s5.cat.01.si-comparison-conditions`
- **Source targets for annotation:** Main physical page 4; SI physical page 13.
  The separate
  `95.2`/`92.2` conflict belongs to `s5.cat.05` and must not be silently hidden
  in this key.

### `s5.cat.02`

- **Query:** 分别还原 RRDE 与 Koutecky–Levich 两条“四电子路径”证据链：RRDE 计算采用什么环收集效率，得到的电子转移数和 H2O2 产率范围是什么；K-L 分析覆盖哪些转速并得到什么电子数结论；最后判断两条链共享哪些电化学假设或误差来源，为什么不能把它们表述为彼此完全独立的化学产物验证。
- **Difficulty:** `hard` (7/10)
- **Difficulty factors:** `E=2, C=2, R=2, A=0, D=1`
- **Answerability:** `answerable`
- **Slices:** `method`, `si`, `exact-token`, `multi-hop`, `cross-language`, `difficulty-hard`
- **Expected answer outline:** Pt ring collection efficiency `N = 0.37`;
  RRDE gives `n ≈ 3.96` and `H2O2 < 3%`; K-L spans `400–2500 rpm` and
  yields an approximately four-electron interpretation. Both chains depend on
  electrochemical current separation and transport/model assumptions, so they
  are mutually supportive rather than an independent chemical peroxide assay.
- **Proposed claims:** `cl.s5.cat.02.selectivity`,
  `cl.s5.cat.02.limit`
- **Required evidence groups:** `eg.s5.cat.02.main-rrde`,
  `eg.s5.cat.02.si-kl`
- **Source targets for annotation:** Main physical pages 5 and 8; SI physical
  pages 20–22.

### `s5.cat.03`

- **Query:** 按静态、1.00 V、0.90 V 和 0.75 V 四个状态重建 Co 位点的配位演化：分别列出 Co–N、Co–Cl、Co–O 配位和 3d 电子占据的证据，再把 895 cm^-1 与 1080 cm^-1 的 SRIR 信号接入 *O/*OOH 解释；逐项区分直接观测、拟合结果和作者归属，并指出哪一种缺失实验最能检验“Cl–Co–N4 断键重构导致四电子选择性”这一完整因果链。
- **Difficulty:** `very-hard` (8/10)
- **Difficulty factors:** `E=2, C=1, R=2, A=1, D=2`
- **Answerability:** `answerable`
- **Slices:** `mechanism-causal`, `si`, `multi-hop`, `cross-language`, `difficulty-very-hard`
- **Expected answer outline:** Ex situ Co-N/Co-Cl coordination is near `4/1`.
  At `1.00 V`, Co-N falls to about `2.2`, Co-Cl stays near `1`, and the
  formalized 3d occupancy changes from about `d5.80` to `d5.28`; at
  `0.90/0.75 V`, about `1.1` Co-O coordination appears. XAFS coordination and
  electron counts are fits/interpretations, while the `895 cm^-1` versus
  `1080 cm^-1` SRIR candidates are author assignments to `*O`/`*OOH`.
  Isotope-sensitive operando spectroscopy is the most direct missing
  discriminator; bond-breaking kinetics or adsorption-energy validation would
  also be needed for complete causal closure.
- **Proposed claims:** `cl.s5.cat.03.reconstruction`,
  `cl.s5.cat.03.intermediate-limit`
- **Required evidence groups:** `eg.s5.cat.03.main-static-structure`,
  `eg.s5.cat.03.main-operando-xafs`,
  `eg.s5.cat.03.main-srir`,
  `eg.s5.cat.03.si-fits`
- **Source targets for annotation:** Main physical pages 2–6 and 8; SI
  physical pages 34 and 40.

### `s5.cat.04`

- **Query:** 把该工作的耐久性证据按三种不可互换的工况分开：0.7 V 半电池恒电位测试、正文 Zn–air 循环和 SI Zn–air 循环；分别报告时长及性能变化，再补充开路电压、最大功率密度和 10 mA cm^-2 下的比容量。基于每项实验实际控制的变量，判断它们分别支持“半电池稳定”“器件可运行”和“工业寿命”中的哪一级，不能支持哪一级。
- **Difficulty:** `very-hard` (9/10)
- **Difficulty factors:** `E=2, C=2, R=2, A=1, D=2`
- **Answerability:** `answerable`
- **Slices:** `paper-specific`, `si`, `exact-token`, `multi-hop`, `cross-language`, `difficulty-very-hard`
- **Expected answer outline:** The `0.7 V` half-cell chronoamperometry runs
  `120 h` with less than `8%` current loss and supports bounded half-cell
  stability. Zn-air results include `1.50 V`, `176.6 mW cm^-2`, and
  `745 mA h gZn^-1` at `10 mA cm^-2`; the main cycling window is about `30 h`
  without obvious decay, while supplementary curves extend to about `48 h`
  without reporting a separately quantified loss, supporting limited device
  operation.
  Missing repeat uncertainty, long-term failure analysis, and water/salt/air
  management prevent an industrial-lifetime claim.
- **Proposed claims:** `cl.s5.cat.04.device`,
  `cl.s5.cat.04.device-limit`
- **Required evidence groups:** `eg.s5.cat.04.main-half-cell-durability`,
  `eg.s5.cat.04.main-zab`,
  `eg.s5.cat.04.si-zab-cycling`
- **Source targets for annotation:** Main physical pages 5–8; SI physical
  pages 35–37.

### `s5.cat.05`

- **Query:** 为元分析抽取 CR-Co/ClNC 在 0.85 V 的动力学电流密度时，正文性能段或图表与 SI Table S2 分别报告了什么值、单位和比较条件？两处是否提供了校正、舍入、样品批次或其他可消解差异的解释；若没有，数据表应如何编码，为什么不能平均或无条件择一？
- **Difficulty:** `very-hard` (9/10)
- **Difficulty factors:** `E=2, C=1, R=2, A=2, D=2`
- **Answerability:** `conflicting`
- **Slices:** `negative`, `exact-token`, `si`, `cross-language`, `difficulty-very-hard`
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

- **Query:** 构造这项泛癌蛋白质组研究的嵌套计数图：依次给出独特蛋白数、经癌种内校正的蛋白–癌症关联数、其中血液恶性肿瘤关联数，以及诊断间隔超过 7 年后保留的关联数与独特蛋白数；为每个数字标明计数实体和筛选阶段，并解释为什么长期分层后的缩减不能直接换写成相同比例的因果确认。
- **Difficulty:** `hard` (7/10)
- **Difficulty factors:** `E=2, C=2, R=1, A=0, D=2`
- **Answerability:** `answerable`
- **Slices:** `paper-specific`, `exact-token`, `cross-language`, `difficulty-hard`
- **Expected answer outline:** `371` proteins; `618` associations;
  `320` hematologic-malignancy associations; after more than seven years,
  `107` associations involving `72` unique proteins remain ENT-significant.
  Protein, association, cancer family, and time-stratified association are
  different counting entities/stages; persistence after seven years reduces
  reverse-causation concern but is not a proportional causal confirmation.
- **Proposed claims:** `cl.s5.bio.01.discovery-map`,
  `cl.s5.bio.01.long-lag`
- **Required evidence groups:** `eg.s5.bio.01.main-discovery`,
  `eg.s5.bio.01.main-time-stratification`
- **Source targets for annotation:** Main physical pages 2–5.

### `s5.bio.02`

- **Query:** 从正文统计方法和 SI 癌种特异模型共同还原显著性控制：639 个主成分如何产生每个癌种的阈值，Cox 模型使用什么时间尺度，各癌种协变量在哪里定义；随后把“每个癌种内的有效检验控制”与“19 种癌症及 9 个亚部位组成的全研究终点家族”分开，明确论文控制了哪一层、没有报告哪一层，而不是把未报告误写成统计方法错误。
- **Difficulty:** `very-hard` (8/10)
- **Difficulty factors:** `E=2, C=2, R=2, A=1, D=1`
- **Answerability:** `answerable`
- **Slices:** `method`, `si`, `exact-token`, `multi-hop`, `cross-language`, `difficulty-very-hard`
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

- **Query:** 对四个最强蛋白–癌症候选逐一建立四列证据矩阵：主体观察分析、诊断间隔超过 7 年、cis-pQTL 和 exGS；逐格判断方向是否一致、采用的是癌种内 ENT 还是常规显著性，并列出不能由“方向一致”替代的多重校正、共享 UK Biobank 样本、多效性、连锁和蛋白形式问题。只有四个候选全部核对后，才能判断其证据上限是候选优先级还是因果确认。
- **Difficulty:** `very-hard` (8/10)
- **Difficulty factors:** `E=2, C=1, R=2, A=1, D=2`
- **Answerability:** `answerable`
- **Slices:** `mechanism-causal`, `si`, `multi-hop`, `cross-language`, `difficulty-very-hard`
- **Expected answer outline:** `SFTPA2–lung cancer`,
  `TNFRSF1B–non-Hodgkin lymphoma`, `CD74–non-Hodgkin lymphoma`, and
  `ADAM8–leukemia`. For every row, the main association is cancer-specific
  ENT-significant; the `>7 year`, cis-pQTL, and exGS columns are reported as
  directionally consistent with conventional significance, not as each
  independently passing its full multiplicity threshold. Observational and
  genetic analyses share UK Biobank, and pleiotropy/linkage/proteoform issues
  remain. Gold annotation must record all four rows across all four columns;
  partial candidate recall is incomplete.
- **Proposed claims:** `cl.s5.bio.03.four-candidates`,
  `cl.s5.bio.03.causal-limit`
- **Required evidence groups:** `eg.s5.bio.03.main-observational`,
  `eg.s5.bio.03.main-long-lag`,
  `eg.s5.bio.03.main-cis-pqtl`,
  `eg.s5.bio.03.main-exgs`
- **Source targets for annotation:** Main physical pages 5–7 and 10; SI
  evidence should be used only where it directly supports a candidate or
  method detail.

### `s5.bio.04`

- **Query:** 建立 304、83、38 和 9 的四层“实体—关系—门槛”表：前两项各以什么为计数单位并对应什么组织表达比例门槛，后两项分别计数什么实体及其与已批准药物、对应癌症适应证的关系；每项还要注明它不是哪一种相邻实体类型。最后说明为什么这张映射表不能推出“九种药物可安全用于癌症预防”。
- **Difficulty:** `hard` (7/10)
- **Difficulty factors:** `E=2, C=2, R=1, A=0, D=2`
- **Answerability:** `answerable`
- **Slices:** `exact-token`, `si`, `multi-hop`, `cross-language`, `difficulty-hard`
- **Expected answer outline:** `304` and `83` are protein-cancer association
  counts whose encoded genes exceed `>10%` and `>50%` expression proportions;
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

- **Query:** 把 UK Biobank 样本流按“总体招募→UKB-PPP 生成蛋白测量→Olink 实测排除起点→最大观察分析→exGS 可投射上限→最大遗传分析”排列并给出人数；指出其中两组最容易被误并的分母各差多少、分别对应什么阶段，以及论文能否完整解释这些差值。不得仅凭算术自行补造排除步骤。
- **Difficulty:** `very-hard` (9/10)
- **Difficulty factors:** `E=2, C=1, R=2, A=2, D=2`
- **Answerability:** `ambiguous`
- **Slices:** `negative`, `exact-token`, `si`, `cross-language`, `difficulty-very-hard`
- **Expected answer outline:** UK Biobank recruited `503,317`; UKB-PPP
  generated measurements for `54,306`; the exclusion flow starts from
  `54,221` Olink-measured participants and reaches a maximum observational
  sample of `44,645`; exGS projection covers up to `337,543`, while the
  maximum genetic analysis sample is `336,823`. The `85`-person and
  `720`-person gaps belong to different stages; the former is not explained
  by the supplied source set, and neither gap may be filled with invented
  exclusions.
- **Proposed claims:** `cl.s5.bio.05.observational-flow`,
  `cl.s5.bio.05.genetic-flow`,
  `cl.s5.bio.05.unresolved-denominators`
- **Required evidence groups:** `eg.s5.bio.05.main-program-count`,
  `eg.s5.bio.05.main-flow-count`,
  `eg.s5.bio.05.si-flow`
- **Source targets for annotation:** Main physical pages 2, 6, and 8–10; SI physical
  page 6.
- **Abstention policy:** The answer must retain both contexts. Choosing one
  denominator or inventing the missing flow step is an error.

## Computer science and machine learning

Target paper: `cornelio-2023-ai-descartes`

### `s5.cs.01`

- **Query:** 把 AI-Descartes 的组件级符号回归评测与完整管线案例分开审计：组件评测包含多少任务、每题多少点、何种噪声，四个系统的命中数和对应比例是多少；完整管线又只运行了多少个被选任务、结果如何，论文未报告哪些选择或排除信息。说明为什么组件领先和少量完整管线成功不能合并成一个端到端成功率。
- **Difficulty:** `hard` (7/10)
- **Difficulty factors:** `E=2, C=2, R=1, A=0, D=2`
- **Answerability:** `answerable`
- **Slices:** `paper-specific`, `si`, `exact-token`, `cross-language`, `difficulty-hard`
- **Expected answer outline:** `81` non-trigonometric tasks; `10` points per
  task; `1%` error; AI-Descartes `49/81`, AI Feynman `33/81`, PySR `40/81`,
  BMS `39/81`, corresponding to `60.49%`, `40.74%`, `49.38%`, and `48.15%`.
  The complete pipeline ran on `5` selected tasks and succeeded on all five,
  but selection timing, criteria, blinding, and exclusions are unreported.
  Component accuracy and selected-task success therefore cannot be merged into
  an end-to-end rate.
- **Proposed claims:** `cl.s5.cs.01.benchmark`,
  `cl.s5.cs.01.e2e-limit`
- **Required evidence groups:** `eg.s5.cs.01.si-benchmark`,
  `eg.s5.cs.01.si-evaluation-protocol`
- **Source targets for annotation:** SI physical pages 19–20 and 29–33.

### `s5.cs.02`

- **Query:** 复现 MINLP 搜索空间与停止条件：表达式树深度、常数范围、幂范围、tau 和平方误差阈值分别是什么；再从剪枝规则和限时求解说明一个候选“未找到”可能由哪些搜索空间或计算预算因素造成。回答必须区分“在受限运行中未返回”“目标表达式不存在”和“已证明全局最优”三种结论。
- **Difficulty:** `hard` (7/10)
- **Difficulty factors:** `E=2, C=2, R=2, A=0, D=1`
- **Answerability:** `answerable`
- **Slices:** `method`, `si`, `exact-token`, `cross-language`, `difficulty-hard`
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

- **Query:** 为开普勒和时间膨胀两个案例各建立“数据误差 epsilon—理论距离 beta—测试域外行为”三列表：指出低 epsilon 候选怎样仍具有错误变量依赖或外推，beta 与 bounded-distance 检查在哪个有限域中排除什么；最后说明时间膨胀案例实际比较了哪两套公理，以及为什么该案例不能证明系统具有一般性的理论选择能力。
- **Difficulty:** `very-hard` (8/10)
- **Difficulty factors:** `E=2, C=1, R=2, A=1, D=2`
- **Answerability:** `answerable`
- **Slices:** `mechanism-causal`, `si`, `multi-hop`, `cross-language`, `difficulty-very-hard`
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

- **Query:** 对 f2、g2、f4、f5、g1、g5 和 g7 建立逐项证明状态矩阵：主文自动运行分别是 Yes、No 还是 Timeout，是否存在论文保留的反例或 witness，SI 又对哪些式子通过人工参数实例化完成闭合。最后说明为什么自动证明、明确 No、timeout、反例和人工证明五类状态不能压缩成“通过/失败”二分类。
- **Difficulty:** `very-hard` (8/10)
- **Difficulty factors:** `E=2, C=2, R=2, A=0, D=2`
- **Answerability:** `answerable`
- **Slices:** `paper-specific`, `si`, `exact-token`, `multi-hop`, `cross-language`, `difficulty-very-hard`
- **Expected answer outline:** `f2/g2 = Yes`; `f4/f5/g1 = No`;
  `g5/g7 = Timeout` in the automatic run and are later closed through manual
  parameter instantiation. The paper does not report preserved counterexample
  artifacts for every `No`, and the manual instances are not automatic
  witnesses. `Yes`, `No`, timeout, counterexample, and manual closure must
  remain distinct.
- **Proposed claims:** `cl.s5.cs.04.states`,
  `cl.s5.cs.04.manual-closure`
- **Required evidence groups:** `eg.s5.cs.04.main-table3`,
  `eg.s5.cs.04.si-proof-details`
- **Source targets for annotation:** Main physical page 8; SI physical
  pages 15–18.

### `s5.cs.05`

- **Query:** KeYmaera X 为双站点 Langmuir 候选 g5 和 g7 分别返回了哪组反例参数？这些反例如何证明两式与背景理论不一致？
- **Difficulty:** `very-hard` (9/10)
- **Difficulty factors:** `E=2, C=1, R=2, A=2, D=2`
- **Answerability:** `false-premise`
- **Slices:** `negative`, `si`, `exact-token`, `cross-language`, `difficulty-very-hard`
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

- **Query:** 在同一 2012–2021 窗口内，对照瞬态、工业化前和近似现今碳密度三种假设：分别给出 ELUC,trans 相对 ELUC,pi 与 ELUC,pd 的百分比差异、范围和可用的 GtC yr^-1 差值，再给出工业化前土地覆盖假设导致的 SLAND 偏差及 RSS；说明每个范围表示模型集合最小—最大、统计置信区间还是其他不确定性，不能混写。
- **Difficulty:** `hard` (7/10)
- **Difficulty factors:** `E=2, C=2, R=1, A=0, D=2`
- **Answerability:** `answerable`
- **Slices:** `paper-specific`, `exact-token`, `cross-language`, `difficulty-hard`
- **Expected answer outline:** Relative to `ELUC,pi`, `ELUC,trans` is
  `28% (21%–38%)` or `0.34 (0.18–0.56) GtC yr^-1` higher; relative to
  `ELUC,pd`, it is `14% (8%–23%)` higher. The preindustrial-cover assumption
  overestimates the sink by `23% (8%–33%)`, with `RSS = 0.7 (0.3–1.3)
  GtC yr^-1`. These ranges are model-ensemble minimum–maximum ranges, not
  statistical confidence intervals.
- **Proposed claims:** `cl.s5.env.01.eluc`,
  `cl.s5.env.01.sland`
- **Required evidence groups:** `eg.s5.env.01.main-recent`,
  `eg.s5.env.01.si-window-table`
- **Source targets for annotation:** Main physical pages 2–5; SI physical
  page 17.

### `s5.env.02`

- **Query:** 把 BLUEpi、BLUEpd、BLUEtrans、BLUEtrans+m 和 BLUES2 按“碳密度是否瞬态、土地利用强迫、事件前后环境效应、对应反事实角色”四列完整映射；随后按依赖顺序从运行差分推导 delta-L、SLAND,trans、RSS 和 LASC，说明每一步为什么要扣除上一项，以及这些等式为何是模型内归因定义而不是四个独立观测量。
- **Difficulty:** `very-hard` (8/10)
- **Difficulty factors:** `E=2, C=2, R=2, A=0, D=2`
- **Answerability:** `answerable`
- **Slices:** `method`, `si`, `exact-token`, `multi-hop`, `cross-language`, `difficulty-very-hard`
- **Expected answer outline:** `BLUEpi` fixes preindustrial carbon density with
  land-use change; `BLUEpd` fixes approximately present-day carbon density;
  `BLUEtrans` uses transient DGVM-scaled density with land-use change;
  `BLUEtrans+m` additionally updates post-event natural/managed carbon pools;
  `BLUES2` uses transient environmental effects without land-use change. The
  answer must then reconstruct the dependent difference equations:
  `delta-L = DeltaCA,BLUEtrans - DeltaCA,BLUEpi`;
  `SLAND,trans = (DeltaCL,BLUEtrans+m - DeltaCL,BLUEpi) - delta-L`;
  `RSS = SLAND,trans - SLAND,pi`; `LASC = delta-L + RSS`. These are
  model-internal counterfactual definitions, not independently observed fluxes.
- **Proposed claims:** `cl.s5.env.02.scenarios`,
  `cl.s5.env.02.equations`
- **Required evidence groups:** `eg.s5.env.02.main-equations`,
  `eg.s5.env.02.si-flowchart`
- **Source targets for annotation:** Main physical pages 10–12; SI physical
  page 4.

### `s5.env.03`

- **Query:** 审计瞬态碳密度输入的完整构建链：植被与土壤趋势各由多少个 DGVM 构造，JSBACH 为什么替换 TRENDY 版本，全球时间序列随后采用什么平滑和末端外推；分别指出哪些决定属于模型集合组成、源数据纠错和时间序列后处理，并说明为何不能把某个局地表示误差单独归因给 20 年平滑。
- **Difficulty:** `hard` (7/10)
- **Difficulty factors:** `E=2, C=2, R=1, A=0, D=2`
- **Answerability:** `answerable`
- **Slices:** `method`, `si`, `exact-token`, `cross-language`, `difficulty-hard`
- **Expected answer outline:** `8` DGVMs for vegetation trends and `5` for
  soil; JSBACH uses TRENDYv12 because of a v11 PFT-output setup error; the
  globally aggregated series uses a `20-year` moving average and linearly
  extrapolates the final `9 years`. Ensemble membership, source-data
  correction, and temporal post-processing are distinct stages; the source
  does not identify the smoothing step alone as the cause of local
  representation error.
- **Proposed claims:** `cl.s5.env.03.inputs`,
  `cl.s5.env.03.processing`
- **Required evidence groups:** `eg.s5.env.03.main-dgvm`,
  `eg.s5.env.03.si-processing`
- **Source targets for annotation:** Main physical pages 8–10; SI physical
  pages 2–3 and 16.

### `s5.env.04`

- **Query:** 逐项比较 BLUE 近期净陆地通量与 TRENDY、反演及 O2 约束使用的时间窗、总量定义和不确定性含义；在此基础上说明总量相容为何不能验证 ELUC、SLAND 与 RSS 的现实唯一拆分。答案必须至少指出共享 TRENDY/LUH2 输入、反事实定义、窗口差异和分量误差抵消中的具体路径，而不是只给出“相关不等于因果”的泛化判断。
- **Difficulty:** `very-hard` (8/10)
- **Difficulty factors:** `E=2, C=1, R=2, A=1, D=2`
- **Answerability:** `answerable`
- **Slices:** `mechanism-causal`, `multi-hop`, `cross-language`, `difficulty-very-hard`
- **Expected answer outline:** For `2012–2021`, BLUE's internally consistent
  `ELUC,trans + SLAND,trans` total is `-1.2 (-2.1 to -0.5) GtC yr^-1`
  using the five-model min–max range; TRENDY S3 is
  `-1.4 ± 0.7 GtC yr^-1` with a model standard deviation; the eight-inversion
  range is `-1.4 (-2.0 to -0.3) GtC yr^-1`. The atmospheric O2 constraint uses
  `2013–2022` and reports `-1.2 ± 0.8 GtC yr^-1`. All constrain a total net
  land–atmosphere flux, not a unique ELUC/SLAND/RSS split. Shared TRENDY/LUH2
  information, different windows and uncertainty meanings, counterfactual
  component definitions, and compensating component errors can yield total
  agreement without identifying the split.
- **Proposed claims:** `cl.s5.env.04.total-agreement`,
  `cl.s5.env.04.nonidentification`
- **Required evidence groups:** `eg.s5.env.04.main-total-comparison`,
  `eg.s5.env.04.main-attribution-limit`
- **Source targets for annotation:** Main physical pages 4–6, 8, and 12.

### `s5.env.05`

- **Query:** 请基于论文给出的观测约束，为 2012–2021 年现实世界的 ELUC、SLAND 和 RSS 制作一张唯一真值表，并为每个分量列出独立观测来源；同时判定 DGVM 趋势缩放碳密度与 LUH2 土地利用强迫中哪一个已被独立证实为正确输入。
- **Difficulty:** `very-hard` (10/10)
- **Difficulty factors:** `E=2, C=2, R=2, A=2, D=2`
- **Answerability:** `no-answer`
- **Slices:** `negative`, `si`, `multi-hop`, `cross-language`, `difficulty-very-hard`
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

- **Query:** 先报告巴西、印度和印度尼西亚对全部随机监管方案的边际平均支持率，再比较文中明确报告的低强度与中高强度理想型模式；随后区分模型预测的假想公投 yes 概率、conjoint 强制选择和现实投票或政策执行。基于这种区分，判断“中高强度方案更受欢迎”与“现实中会通过并得到遵守”各自能否由研究支持。
- **Difficulty:** `hard` (7/10)
- **Difficulty factors:** `E=2, C=2, R=1, A=1, D=1`
- **Answerability:** `answerable`
- **Slices:** `paper-specific`, `exact-token`, `cross-language`, `difficulty-hard`
- **Expected answer outline:** Brazil `64.5%`, India `64.1%`, Indonesia
  `50.9%`; Brazil and India are about `65–70%` on the reported high-intensity
  pattern, Indonesia is about `50–55%` on the reported medium/high patterns,
  while reported low-intensity support is only `49%` in Brazil and `39%` in
  Indonesia. These are model-predicted
  hypothetical-referendum yes probabilities, distinct from the conjoint
  forced-choice outcome, in online quota samples; neither outcome establishes
  actual referenda, policy adoption, or compliance.
- **Proposed claims:** `cl.s5.soc.01.support`,
  `cl.s5.soc.01.external-limit`
- **Required evidence groups:** `eg.s5.soc.01.main-marginal-means`,
  `eg.s5.soc.01.main-design-limit`
- **Source targets for annotation:** Main physical pages 3–4 and 9–10.

### `s5.soc.02`

- **Query:** 按国家重建三层计数：正文与 SI 的 respondent counts、每人十次任务产生的 proposal-response counts，以及三个 ideal-type/group analyses 报告的 analytical sample/observations；计算正文与 SI 总受访者口径的差异，并判断最后一层能否写成独特受访者。若来源未给出从起始样本到分析样本的完整排除流，不得自行补齐。
- **Difficulty:** `very-hard` (8/10)
- **Difficulty factors:** `E=2, C=2, R=2, A=0, D=2`
- **Answerability:** `answerable`
- **Slices:** `method`, `si`, `exact-token`, `multi-hop`, `cross-language`, `difficulty-very-hard`
- **Expected answer outline:** Main respondents `2,000` per country and
  `6,000` total; SI respondents Brazil `2,000`, Indonesia `2,000`, India
  `2,001`; proposal responses `20,000/20,000/20,010`; ideal-type analytical
  sample/observations `1,761/1,615/1,723`. They count different entities and
  analysis stages. The main/SI total differs by `1`; the final numbers cannot
  be relabeled as unique respondents, and the source does not provide a
  complete exclusion flow that reconciles all stages.
- **Proposed claims:** `cl.s5.soc.02.respondents`,
  `cl.s5.soc.02.responses`,
  `cl.s5.soc.02.analysis-observations`
- **Required evidence groups:** `eg.s5.soc.02.main-counts`,
  `eg.s5.soc.02.si-counts`
- **Source targets for annotation:** Main physical pages 8 and 10–11; SI
  physical pages 15–19.

### `s5.soc.03`

- **Query:** 结合随机信息提示的具体处理文本、合并样本估计和研究设计，判断来源直接支持的是“未发现合并平均显著效应”“效应等于零”还是“公众偏好具有韧性”；要把最强主张成立，还需逐项补足哪些国别异质性结果、等效性界限与功效、处理忠实度或操纵检查证据。
- **Difficulty:** `very-hard` (8/10)
- **Difficulty factors:** `E=2, C=1, R=2, A=1, D=2`
- **Answerability:** `answerable`
- **Slices:** `mechanism-causal`, `si`, `multi-hop`, `cross-language`, `difficulty-very-hard`
- **Expected answer outline:** The randomized experiment supports only the
  reported pooled null-significance result under the four positive/negative
  policy-consequence messages versus the no-information control. It does not
  prove equivalence or immutable preferences. Country-level effects,
  preregistered equivalence bounds/power, and treatment-fidelity/manipulation
  checks are needed.
- **Proposed claims:** `cl.s5.soc.03.null-result`,
  `cl.s5.soc.03.resilience-limit`
- **Required evidence groups:** `eg.s5.soc.03.main-information-result`,
  `eg.s5.soc.03.si-treatment-wording`
- **Source targets for annotation:** Main physical pages 5, 7, and 10–11; SI
  physical page 9.

### `s5.soc.04`

- **Query:** 还原实验中真正被随机化的政策属性、随后测量的六项收益/成本评分及其量表，再说明“收益评分普遍高于成本评分”为什么只建立评分次序，不能识别收益信念对政策支持的中介因果效应。请给出一个能区分两种主张的最小实验设计，并明确需要随机化的变量、结果变量和预注册的中介估计量。
- **Difficulty:** `hard` (7/10)
- **Difficulty factors:** `E=2, C=1, R=2, A=1, D=1`
- **Answerability:** `answerable`
- **Slices:** `mechanism-causal`, `si`, `multi-hop`, `cross-language`, `difficulty-hard`
- **Expected answer outline:** The vignette randomizes policy intensity and
  its scope/transparency/enforcement package, then measures information
  improvement, production-condition improvement, employment creation,
  enterprise cost, consumer cost, and sovereignty loss on 1–7 scales. It does
  not randomize a specific belief and
  identify belief-to-support mediation; item wording, acquiescence, and
  non-comparable constructs remain alternatives. A validated randomized
  benefit belief, subsequent support outcome, and preregistered mediation
  estimand would test the stronger claim.
- **Proposed claims:** `cl.s5.soc.04.rating-order`,
  `cl.s5.soc.04.mediation-limit`
- **Required evidence groups:** `eg.s5.soc.04.main-ratings`,
  `eg.s5.soc.04.si-vignette`
- **Source targets for annotation:** Main physical pages 3–5, 7, and 10; SI
  physical page 8.

### `s5.soc.05`

- **Query:** 若要严格复现该研究的数据质量排除，正文方法与 SI 排除表对“完成过快”的百分比阈值和对应时长分别怎样定义？它们能否由舍入、不同国家样本或同一规则的不同表达解释；注意力检查排除又是否属于同一标准？若来源没有消解差异，复现协议应如何保留这一冲突。
- **Difficulty:** `very-hard` (9/10)
- **Difficulty factors:** `E=2, C=1, R=2, A=2, D=2`
- **Answerability:** `conflicting`
- **Slices:** `negative`, `exact-token`, `si`, `cross-language`, `difficulty-very-hard`
- **Expected answer outline:** Main physical page 9 reports `45%` and
  `7:03`; the SI table on physical page 11 reports `50% Median Time` and
  `7:08`. Neither rounding nor a country-specific denominator is documented as
  a reconciliation. Failing at least `2/3` attention checks is a separate
  exclusion rule. The source conflict must be retained in the reproduction
  protocol.
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
