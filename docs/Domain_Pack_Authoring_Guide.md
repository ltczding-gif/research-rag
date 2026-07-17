# Domain Pack Authoring Guide

How to adapt this literature-note pipeline to a new research field by writing a
**domain pack** — a directory bundling the prompts, schemas, and templates that
encode your field's conventions.

**Audience**: a researcher cloning the repo who works in a field other than
catalysis (the reference pack). You should be comfortable editing Markdown and
text files; the JSON Schema edits are guided line-by-line.

**Time budget**: 1-3 hours for a workable first pack. The bootstrap CLI handles
the mechanical parts in 5 minutes; the rest is genuine writing — your trap
scan checklist, your filename slot semantics, and one template body.

---

## Skip-the-theory: see a worked biology pack first

Before reading the principles, look at what a biologist actually writes.
This walkthrough takes ~10 minutes and grounds everything else in the guide.

**1. Bootstrap the pack** (the CLI asks 6 questions):

```
$ python scanner/bootstrap_domain_pack.py --name cell-biology

1. Description: Cell biology research papers covering wet-lab assays,
   omics pipelines, and clinical-translational studies.

2. Language: zh-CN (or "en" if you want English notes)

3. research_domain enum: molecular-cell-biology, developmental-biology,
   immunology, neuroscience, cancer-biology

4. Field-specific template ids: wet-lab-experimental, omics-computational,
   clinical-translational

5. primary_routing_key field name: primary_paradigm

6. Axis-4 name: biological / clinical relevance
```

**2. The CLI generates the pack and prints a numbered TODO list.** Edit the
files in this order:

**a. `domain-packs/cell-biology/templates/_domain_quality_rules.txt`** —
write your field's filename slot semantics, trap-scan checklist, and
scoring axis 4. Example:

```
### Recommended Filename Contract
年份 - 期刊 - 作者 - 生物体系或细胞类型 - 实验范式或问题类型 - 关键发现或机制 - 贡献焦点 _review_note.md

Author slot rule (cell biology specifically — DON'T copy catalysis blindly):
- Single corresponding author: use that author's last name.
- Co-corresponding authors (~30% of high-impact biology): use the first
  listed corresponding author's last name. Do NOT default to "last listed
  corresponding author" — that's an electrochem convention that breaks here.

### Trap Scan
- 样本量是否充分论证（n per group, biological vs technical replicates）
- 多重检验校正（Bonferroni / BH-FDR present where >1 hypothesis tested）
- 动物或临床数据是否有 blinding / randomization
- 效应量是否与 p 值同时报告（不能只 p<0.05）
- -omics work 是否承认 batch effects（sequencing run, day, animal litter）
- 抗体 / 试剂的可追溯性（catalog number, validation reference）

### Domain-Specific Scoring Axis (axis 4)
biological / clinical relevance:
  9-10: in-vivo demonstration in disease model + 跨多组样本独立复现
  7-8: strong in-vivo or organoid demonstration, single cohort
  5-6: in-vitro only with plausible disease relevance
  3-4: cell-line phenotype with broad disease claims unsupported
```

**b. `domain-packs/cell-biology/templates/wet-lab-experimental.txt`** —
write the body section structure. Biology-shaped sections:

```
## 文献基本信息
## 客观摘要
## 研究问题
## 实验体系                     (cell line / model organism / IRB+IACUC)
## 关键方法与材料                (CRISPR design, antibody panel, statistical framework)
## 核心结果                      (with effect sizes, n, p-values, CIs)
## 逐图证据路径                  (panel-level: image / quantification / control)
## 关键主张-证据路径图
## 机理 / 通路解释               (pathway-level synthesis)
## 方法亮点与陷阱扫描
## 主观打分                      (4 axes, axis 4 = biological/clinical relevance)
## 核心结论总结
```

**c. `domain-packs/cell-biology/prompts/routing_disambiguation_hints.txt`** —
3 routing tie-breakers:

```
- Wet-lab vs computational vs clinical:
  - Primary independent variable is bench manipulation (knockdown, drug,
    mutation) → wet-lab-experimental
  - Primary independent variable is dataset, cohort, or computational
    pipeline → omics-computational
  - Primary subject is patient cohort, RCT, or biomarker validation →
    clinical-translational

- Methods paper vs performance paper:
  - Headline = new technique (e.g. new sequencing protocol) →
    methods-or-materials-synthesis (you may need to add this template)
  - Headline = biological finding using established methods → research-article

- Single-cell atlas vs hypothesis-driven biology:
  - Atlas papers often look like reviews but are actually data-generation
    papers. Route to omics-computational, NOT review-or-perspective.
```

**d. Validate + dry-run on 5 PDFs**:

```
python scanner/bootstrap_domain_pack.py --validate cell-biology
echo "LOCALRAG_DOMAIN_PACK=cell-biology" >> .env
python scanner/zotero_batch_scanner.py --limit 5
```

That's the whole pack. Eyeball the 5 generated notes; iterate on the
template based on what the model got wrong.

The catalysis pack at `domain-packs/catalysis/` is the same shape with
electrochemistry filling the slots. Compare them to see what's universal
and what's pack-owned.

---

## Why packs exist

The pipeline has three layers:

| Layer | What | Field-specific? |
|---|---|---|
| Architecture (Python code) | scanner, service, dedup, backends, retrieval | No — universal |
| Universal prompt rules | evidence citation, anti-hallucination, scoring | No — `prompts/_universal_rules.txt` |
| Domain pack | research_domain enums, trap-scan checklist, template body structures | **Yes** — `domain-packs/<your-field>/` |

The first two are the project core. The third is what makes notes feel
written-by-a-domain-expert instead of generic. Packs let you have full
control over field semantics without forking the codebase.

The reference pack (`domain-packs/catalysis/`) was the entire system before
this abstraction landed. It's a worked example, not the universal default.

---

## The six universal patterns your pack must respect

These are constants your pack inherits automatically. **Do not duplicate them
into your pack**; they're loaded from the repo root for every pack.

### 1. Two-stage classify → generate

Stage A (Document Profiler) classifies the paper and picks a template.
Stage B (Note Generator) writes the structured note using the chosen template.

Why two stages: classification needs only first-3-pages context; generation
needs the full document. Splitting saves ~45% input tokens per paper. See
`docs/Project_Architecture_Blueprint.md` ADR-1.

### 2. Evidence citation contract

Every claim is anchored: `[p.X]`, `[p.X-Y]`, `[p.X, Fig.Y]`. Quantitative
statements preserve `value + unit + condition`. No `(p.X)` or `第X页` or bare
figure numbers without explanation.

### 3. Anti-hallucination guardrails

Ground every factual statement in the attached document set only. When data
is missing, write `原文未提及` (or your locale's equivalent). Avoid
speculative phrases unless the source itself frames the point that way.

### 4. Frontmatter contract

15 fixed-order bibliographic fields, then content-addressing block (combined
hash, PDF paths), then tagging shell. The order is enforced by
`scanner/note_render.py:_FRONTMATTER_FIELD_ORDER`. Coordinate any change
with `service/build_notes_db.py` metadata extraction.

### 5. Filename naming skeleton (universal slot count + slot order)

7 hyphen-separated slots, ending in `_review_note.md`:

```
year - source - author - <slot4> - <slot5> - <slot6> - contribution _review_note.md
```

The skeleton is universal. The **slot semantics** (what `<slot4>` etc. mean)
are domain-specific and live in your pack's `_domain_quality_rules.txt`.

### 6. Subjective scoring four-axis pattern

Three universal axes: originality, rigor, evidence closure. The fourth is
domain-specific and named in your pack's `_domain_quality_rules.txt`
(catalysis: industrial application potential; biology: clinical relevance;
ML: real-world reproducibility).

---

## What a domain pack contains

```
domain-packs/<your-field>/
├── pack.yaml                                  # manifest + versioning
├── prompts/
│   ├── document_profiler.system.txt           # universal (don't edit)
│   ├── note_generator.system.txt              # universal (don't edit)
│   ├── seed_terms_guidance.txt                # FIELD-SPECIFIC
│   └── routing_disambiguation_hints.txt       # FIELD-SPECIFIC
├── schemas/
│   ├── document_profile.vertex.schema.json    # FIELD-SPECIFIC enums
│   └── structured_note.vertex.schema.json     # mostly universal
├── templates/
│   ├── _domain_quality_rules.txt              # FIELD-SPECIFIC (most important)
│   ├── research-article.txt                   # FIELD-SPECIFIC body
│   ├── review-or-perspective.txt              # universal copy
│   ├── phd-dissertation.txt                   # universal copy
│   ├── foundational-theory.txt                # universal copy
│   └── generic-research-note.txt              # universal copy (fallback)
└── config/
    └── model_routing_policy.json              # mostly universal (tune for paper sizes)
```

---

## Quick start: bootstrap a new pack

```bash
python scanner/bootstrap_domain_pack.py --name <your-field-slug>
```

The CLI asks 6 questions, copies `_template/` to `domain-packs/<your-field>/`,
and patches in the answers. You then hand-edit the prose files.

---

## The recommended authoring order

### Step 1 — Schemas first

Edit `schemas/document_profile.vertex.schema.json`:

- **`research_domain` enum**: 5-10 sub-areas of your field. The bootstrap
  CLI fills these in from your answers; review and refine.
- **`recommended_template` enum**: must match your `pack.yaml` template
  list AND your `templates/*.txt` filenames. Bootstrap keeps these aligned.
- **`primary_routing_key_value`**: stays as a generic stable string field.
  The semantics are documented in `pack.yaml:primary_routing_key.field_name`.

Why first: these enums propagate everywhere. A late rename of
`primary_paradigm` → `primary_pathway` means re-touching every template.

### Step 2 — `_domain_quality_rules.txt`

This is the soul of your pack. Three things:

**Filename slot semantics** — your field's equivalents to catalysis's
`核心研究对象 / 核心反应类型 / 关键机制 / 解决的瓶颈`.

**Trap scan checklist** — 5-10 quality checks specific to your field's
recurring methodological failure modes. Examples:
- Catalysis: 误差棒 / 碳平衡 / 对照实验 / 进料一致性 / SI 关键数据
- Biology: sample-size justification / multiple-testing correction /
  blinding / effect-size + p-value / batch effects in -omics work
- ML: train/test contamination / reproducibility (code+seed) /
  compute-budget baselines / statistical significance of benchmark deltas /
  data licensing
- Materials: phase purity / grain-size distribution / anneal-cycle
  reproducibility / contamination controls / cross-lab replication
- Clinical: pre-registration / intention-to-treat / loss-to-follow-up /
  conflict-of-interest / effect-size + CI

**Scoring axis-4 name** — your field's relevance dimension. Bootstrap fills
this in; the anchors are field-grounded.

### Step 3 — One template

Edit `templates/<your-primary-template>.txt`. **Do not write all templates
upfront.** Pick the one that covers ~70% of your library; for catalysis
that was `electrocatalysis-experimental`. The body sections you'll write
are field-specific (catalysis has `核心测试条件`, `核心性能指标`,
`逐图证据路径`; biology might have `实验设计`, `统计分析`, `效应量与显著性`).

### Step 4 — Dry-run validate

Pick 5 PDFs from your library, ideally one per `recommended_template` enum
value. Run:

```bash
python scanner/bootstrap_domain_pack.py --validate <your-field-slug>
LOCALRAG_DOMAIN_PACK=<your-field-slug> python scanner/zotero_batch_scanner.py --limit 5
```

Eyeball the 5 generated notes. The validator catches structural problems
(missing files, enum/file mismatch); the dry-run catches *semantic*
problems (routing wrong, seed_terms ungrounded, body sections empty).

### Step 5 — Iterate on the template

Real outputs reveal what your prompts didn't say clearly enough. Fix the
template; re-run on the same 5 PDFs.

### Step 6 — `seed_terms_guidance.txt` AFTER you've seen real outputs

Calibration is much better with real examples in hand. Pick 3-5 actual
seed_terms from your dry-run notes, classify them as good/bad, and write
the guidance prose.

### Step 7 — Remaining domain-specific templates

Now write the other field-specific templates with the lessons learned. The
4 universal templates (review, dissertation, theory, generic) usually need
no changes; if they do, you're probably overfitting.

---

## Worked example 1 — Catalysis (the reference pack)

The catalysis pack predates this abstraction; it's what got refactored INTO
the pack layout. Browse `domain-packs/catalysis/` for the full content.

### Decisions catalysis made

| Question | Answer |
|---|---|
| `research_domain` enum | electrocatalysis, thermocatalysis, electrochemical-separations, materials-synthesis, surface-science, fundamental-theory, multidomain, other |
| `primary_routing_key.field_name` | `primary_reaction_or_system` |
| Domain templates (besides 4 universal) | electrocatalysis-experimental, thermocatalysis-experimental, methods-or-materials-synthesis |
| Trap-scan items | 误差棒 / 碳平衡 / 对照实验 / 进料一致性 / SI 关键数据 |
| Filename slot order | year-journal-author-object-reaction-mechanism-bottleneck |
| Author slot rule | last-listed corresponding author, full Romanized spelling |
| Scoring axis 4 | 工业应用潜力 (industrial application potential) |
| Routing tie-breakers | electro vs thermo vs photo (by primary independent variable); methods vs performance (by headline contribution); review vs research article (by % original data) |

### What you'd notice browsing the pack

`templates/electrocatalysis-experimental.txt` is 268 lines and covers
catalysis-specific body sections like:

- `## 核心测试条件与定量方法` (electrolyte, MEA params, gas/liquid product
  detection)
- `## 核心性能指标` (current density, FE, carbon balance, Tafel slope,
  stability, benchmark comparability)
- `## 逐图证据路径总结 (Figure-by-Figure)` (panel-level analysis with
  catalysis-specific roles: structural baseline, mechanistic trigger,
  operando validation, degradation support, etc.)

Most of those section names won't translate to other fields — but the
**pattern** (a body that goes evidence → interpretation → boundary) does.
Use it as a template for your field's analogous sections.

---

## Worked example 2 — Cell biology (synthetic walkthrough)

Suppose you run a wet lab + computational mix and want to adopt this
pipeline. Here's what your pack might look like.

### Bootstrap session

```
$ python scanner/bootstrap_domain_pack.py --name cell-biology

1. Description: Cell biology research papers covering wet-lab assays, 
   omics pipelines, and clinical-translational studies.

2. Language: zh-CN

3. research_domain enum: molecular-cell-biology, developmental-biology, 
   immunology, neuroscience, cancer-biology

4. Field-specific template ids: wet-lab-experimental, omics-computational, 
   clinical-translational

5. primary_routing_key field name: primary_paradigm

6. Axis-4 name: biological / clinical relevance
```

### Then hand-edit `_domain_quality_rules.txt`

Filename slot semantics:

```
年份 - 期刊 - 作者 - 生物体系或细胞类型 - 实验范式或问题类型 - 关键发现或机制 - 贡献焦点 _review_note.md
```

Author slot rule (cell biology has co-corresponding authors often):

```
- For papers with single corresponding author: use that author's last name.
- For papers with co-corresponding authors (≥30% of high-impact biology): 
  use the first listed corresponding author's last name. If both PIs are 
  on the cover, document the co-authorship in the body, not the filename.
- Do NOT default to "last listed author" — that rule is electrochem-specific 
  and breaks for biology.
```

Trap scan:

```
- 样本量是否充分论证（n per group, biological vs technical replicates）
- 多重检验校正（Bonferroni / BH-FDR present where >1 hypothesis tested）
- 动物或临床数据是否有 blinding / randomization
- 效应量是否与 p 值同时报告（不能只 p<0.05）
- -omics work 是否承认 batch effects（sequencing run, day, animal litter）
- 抗体 / 试剂的可追溯性（catalog number, validation reference）
```

Scoring axis 4 — biological / clinical relevance:

```
- 9-10: in-vivo demonstration in disease model + 跨多组样本独立复现 + 
        明确的 mechanistic causal chain to phenotype
- 7-8: strong in-vivo or organoid demonstration, single cohort
- 5-6: in-vitro only with plausible disease relevance
- 3-4: cell-line phenotype with broad disease claims unsupported by 
        the actual measurement context
```

### Then write `templates/wet-lab-experimental.txt`

Following the catalysis-experimental pattern but with biology-shaped
sections:

- `## 文献基本信息`
- `## 客观摘要`
- `## 研究问题`
- `## 实验体系`（cell line / model organism / sample source / IRB+IACUC if applicable）
- `## 关键方法与材料`（CRISPR design, antibody panel, instrument, statistical framework）
- `## 核心结果`（with effect sizes, n's, p-values, confidence intervals）
- `## 逐图证据路径`（panel-level: representative image / quantification / control / mechanistic trigger）
- `## 关键主张-证据路径图`
- `## 机理 / 通路解释`（pathway-level synthesis）
- `## 方法亮点与陷阱扫描`（pull from `_domain_quality_rules.txt` trap scan）
- `## 主观打分`（4 axes, axis 4 = biological/clinical relevance）
- `## 核心结论总结`

### Then `routing_disambiguation_hints.txt`

```
- Wet-lab vs computational vs clinical:
  - Primary independent variable is bench manipulation (knockdown, drug, mutation) → wet-lab-experimental
  - Primary independent variable is dataset, cohort, or computational pipeline → omics-computational
  - Primary subject is patient cohort, RCT, or biomarker validation → clinical-translational
  - Coupled (e.g. CRISPR screen + scRNA-seq): route by which contribution the abstract leads with.

- Methods paper vs performance paper:
  - Headline = new technique (e.g. new sequencing protocol, new microscopy 
    method) → methods-or-materials-synthesis (you may need to add this 
    template back, or reuse research-article).
  - Headline = biological finding using established methods → research-article.

- Single-cell atlas vs hypothesis-driven biology:
  - Atlas papers often look like reviews but are actually data-generation papers. 
    Route to omics-computational, not review-or-perspective.
```

### Then `seed_terms_guidance.txt`

```
A good seed_terms entry in cell biology is:
- A gene/protein/pathway name central to the paper:
  - GOOD: TP53-R175H, MAPK pathway, IL-6/JAK/STAT3, BRCA1-PALB2 complex
  - BAD: signaling, expression, regulation (too generic)
- An experimental system identity:
  - GOOD: HCT116, K562, primary CD8+ T cells, ApoE-/- mice, organoid 3D-culture
  - BAD: cell lines, animal models, tissue
- A technique that IS the paper's central methodological novelty:
  - GOOD: prime editing, CITE-seq, CUT&RUN, in-situ Hi-C
  - BAD: PCR, Western blot, microscopy (unless the paper invents a new variant)

A bad seed_terms entry is:
- A treatment dose mentioned once but not central (those go in body).
- A side cell line used for controls.
- "scRNA-seq" UNLESS the paper's contribution is a new scRNA-seq method.
```

---

## Hidden risks (warnings from the design phase)

These came out of pre-implementation sub-agent reviews. Internalize them.

### "All three at once" papers

ML papers are often method + benchmark + review simultaneously. Your
profiler will route them inconsistently across runs. Fix: in
`routing_disambiguation_hints.txt`, write a "route by **primary
contribution**, not **content present**" rule. Without this, the
classifier oscillates between templates run-to-run on the same paper.

### Co-corresponding authors break the catalysis filename rule

Catalysis assumes one corresponding author and uses their last name. ~30%
of biology / ML / multi-PI fields ship co-corresponding authorship. Don't
copy the catalysis author-slot rule blindly; pick a rule that produces
deterministic output for your field.

### Frontmatter is shared across packs

The frontmatter shape lives at the pipeline core, not in the pack. If
catalysis emits `primary_reaction` and biology emits `primary_paradigm`,
the ChromaDB metadata schema fragments and cross-pack search dies.
Solution: every pack populates the stable `primary_routing_key_value`
field with its own semantics. Display labels can vary; the key is stable.

### Universal templates need light touch-ups

`review-or-perspective.txt`, `phd-dissertation.txt`,
`foundational-theory.txt`, `generic-research-note.txt` are field-agnostic
**enough** for most fields, but they retain a few catalysis-leaning lines
(e.g. `if writing about CO2RR, ...`). Skim them before shipping; replace
catalysis examples with field-neutral ones if they bother you.

### Zotero `itemType` filter is implicit

The scanner queries Zotero SQLite for items with `path LIKE '%.pdf'`. For
science papers (`journalArticle`, `book`, `thesis`, `preprint`) this is
fine. For humanities (`manuscript`, `archival-document`, `case`) it might
filter out 80% of your library. Audit
`scanner/zotero_batch_scanner.py:get_zotero_pdf_groups` if your field
uses non-default Zotero item types.

---

## Validation checklist before you go live

```bash
# 1. Pack invariants
python scanner/bootstrap_domain_pack.py --validate <your-pack>

# 2. Module imports
LOCALRAG_DOMAIN_PACK=<your-pack> python -c "import gemini_analyze_pdf; print('ok')"

# 3. Prompt rendering
LOCALRAG_DOMAIN_PACK=<your-pack> python -c "
from gemini_analyze_pdf import load_augmented_system_prompt
print(load_augmented_system_prompt('document_profiler.system.txt'))
print('---')
print(load_augmented_system_prompt('note_generator.system.txt'))
"

# 4. Dry-run on 5 PDFs
LOCALRAG_DOMAIN_PACK=<your-pack> python scanner/zotero_batch_scanner.py --limit 5

# 5. Eyeball the 5 generated notes
ls "$LOCALRAG_NOTES_DIR"/*_review_note.md | head -5
```

---

## Sharing your pack

If your pack is generally useful, open a PR adding it under
`domain-packs/<your-field>/`. PRs are reviewed for:

- Does the pack pass `--validate`?
- Does `_domain_quality_rules.txt` actually describe field-specific quality
  (vs. generic platitudes)?
- Are the templates' body sections evidence-grounded (vs. encouraging
  speculation)?
- Does `seed_terms_guidance.txt` have real positive AND negative examples?

The repo welcomes packs from any research field. The catalysis pack
benefits from drift-correction every time another field's pack lands —
universal-vs-domain boundaries get sharper with more examples.

---

## Where to find more

- `domain-packs/catalysis/` — full reference pack (browse the source)
- `domain-packs/_template/README.md` — what each file does in one page
- `docs/Project_Architecture_Blueprint.md` — system architecture if you
  need to understand how the pack is loaded
- `prompts/_universal_rules.txt` — the field-invariant baseline
- `scanner/bootstrap_domain_pack.py` — the CLI source if you want to
  understand exactly what gets patched
