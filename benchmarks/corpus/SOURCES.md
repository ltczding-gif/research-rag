# S5 corpus source and license record

This document records the maintainer decision that admits the first five
papers to the public S5 benchmark. It is an evidence log, not legal advice.
The machine-readable source of truth is [`manifest.jsonl`](manifest.jsonl).

## Acceptance gate

Each S5 paper must satisfy all of the following:

1. one paper for each frozen S5 domain;
2. a main article PDF and at least one distinct SI PDF;
3. an article-level CC BY 4.0 notice with no paper-specific downgrade;
4. a stable publisher page, DOI, and direct PDF URLs;
5. valid PDF magic, readable text, page count, and frozen SHA-256;
6. enough main/SI cross-file evidence to support later retrieval queries.

Nature Communications describes its accepted SI as part of the paper and
requests a single Word or PDF SI file where possible. Its open-access
license-to-publish form defines the licensed “Contribution” as the paper text
and all supplementary information. The selected article pages each state CC
BY 4.0. Under that license, redistribution is allowed with attribution.

- [Nature Communications SI submission guidance](https://www.nature.com/ncomms/submit/how-to-submit)
- [Nature open-access license-to-publish form](https://mts-common.nature.com/rj_forms/NAI_LTP_OA_CCBY_Nov_2012.pdf)
- [Creative Commons Attribution 4.0 terms](https://creativecommons.org/licenses/by/4.0/)

Third-party material can carry a separate credit-line exception. The selected
main and SI PDFs were text-scanned for explicit contrary notices during the
2026-07-27 review; none was found. Re-check the article page and both files
before publishing a new corpus artifact version.

## Frozen five-paper selection

| Domain | Paper | Publisher record | Main / SI pages | Why it is useful |
|---|---|---|---:|---|
| Catalysis/materials | `liu-2024-single-atom-cobalt-orr` — *In situ modulating coordination fields of single-atom cobalt catalyst for enhanced oxygen reduction reaction* | [Article and rights](https://www.nature.com/articles/s41467-024-45990-w) · [DOI](https://doi.org/10.1038/s41467-024-45990-w) | 10 / 43 | Main/SI evidence chain spans synthesis, ORR conditions, in-situ spectroscopy, DFT, and device validation. |
| Biomedicine | `papier-2024-proteomic-cancer-risk` — *Identifying proteomic risk factors for cancer using prospective and exome analyses of 1463 circulating proteins and risk of 19 cancers in the UK Biobank* | [Article and rights](https://www.nature.com/articles/s41467-024-48017-6) · [DOI](https://doi.org/10.1038/s41467-024-48017-6) | 12 / 36 | Dense cohort definitions, multiple-testing rules, sensitivity analyses, and large SI tables create realistic biomedical retrieval boundaries. |
| CS/ML | `cornelio-2023-ai-descartes` — *Combining data and theory for derivable scientific discovery with AI-Descartes* | [Article and rights](https://www.nature.com/articles/s41467-023-37236-y) · [DOI](https://doi.org/10.1038/s41467-023-37236-y) | 10 / 37 | Algorithms, logical constraints, symbolic-regression cases, equations, and extended SI proofs test non-experimental technical literature. |
| Environment/energy/geoscience | `dorgeist-2024-terrestrial-carbon-fluxes` — *A consistent budgeting of terrestrial carbon fluxes* | [Article and rights](https://www.nature.com/articles/s41467-024-51126-x) · [DOI](https://doi.org/10.1038/s41467-024-51126-x) | 13 / 18 | Carbon-budget identities and multi-model comparisons require retrieval across definitions, equations, estimates, and SI sensitivity tests. |
| Social science/economics | `smith-2024-supply-chain-regulations` — *Stringent sustainability regulations for global supply chains are supported across middle-income democracies* | [Article and rights](https://www.nature.com/articles/s41467-024-45399-5) · [DOI](https://doi.org/10.1038/s41467-024-45399-5) | 12 / 22 | Multi-country survey experiments, treatment arms, subgroup analysis, and robustness checks test statistical and policy-language retrieval. |

The ten files total 213 pages and 19,908,380 bytes. Text extraction produced
535,534 characters, so none of the selected PDFs is an image-only scan.

All five S5 papers deliberately come from one publisher family. This keeps
license interpretation and acquisition stable while S5 checks the main/SI
wiring across domains. It also means S5 does **not** establish robustness to
publisher-layout diversity. D20 must add multiple publisher templates,
single-column and unusual-layout articles, and at least one genuinely
scan-like or extraction-hostile document before any broad parsing claim.

## File-level verification

| File ID | Bytes | Pages | Extracted characters | SHA-256 |
|---|---:|---:|---:|---|
| `liu-2024-single-atom-cobalt-orr-main` | 1,448,704 | 10 | 56,809 | `6162062b70c73d8df2d4f1890454dc5f0c737a7fadbd1829a989c398cac2fa32` |
| `liu-2024-single-atom-cobalt-orr-si-1` | 2,052,518 | 43 | 23,476 | `6d0b1fc0e9fba2af0dd53ca28fa75952d3ad743eb39cf643cfd90c512b7dbcd1` |
| `papier-2024-proteomic-cancer-risk-main` | 4,170,162 | 12 | 65,420 | `1f1c7607e38fb6f179df6b583a9659893993d4bfc0da977c974ee37c403bd448` |
| `papier-2024-proteomic-cancer-risk-si-1` | 4,411,574 | 36 | 40,053 | `71750a04c75de526de64e91d5443521b2f36e015bbacbb5e6415f50be90d7678` |
| `cornelio-2023-ai-descartes-main` | 1,196,909 | 10 | 53,754 | `852cc032d5713124b9d0f79113e50283d05cd38dbf5706c4bb4e1860c130ec89` |
| `cornelio-2023-ai-descartes-si-1` | 440,746 | 37 | 89,567 | `681230c8247c928cefedcef71634cf7b0d55c2d943b11336b67e4b1aad63d45b` |
| `dorgeist-2024-terrestrial-carbon-fluxes-main` | 1,870,470 | 13 | 77,446 | `0d00bc8ae49165d037b2f1dd8a6eb0baa5088a697c6eefd872a76754b2c4e207` |
| `dorgeist-2024-terrestrial-carbon-fluxes-si-1` | 2,495,155 | 18 | 27,179 | `7064ab924cee9ceffc4f659d9af69227bdd40b101820c95bd10286d744e55a74` |
| `smith-2024-supply-chain-regulations-main` | 1,406,716 | 12 | 73,325 | `ba673cf18a660add32d0e611b3f124c6b6b2c61611156cbab4bfbd06f6af25ea` |
| `smith-2024-supply-chain-regulations-si-1` | 415,426 | 22 | 28,505 | `5310f5e2d3ccd16e34f18e6d1ae22cd63aea609ebff9d0b28d35f7944d5b4da1` |

## Template coverage decision

S5 measures the repository as shipped; it does not pretend that five mature
domain packs already exist.

| Paper | Stage A route using the shipped `catalysis` pack | Interpretation |
|---|---|---|
| `liu-2024-single-atom-cobalt-orr` | `electrocatalysis-experimental` (`high` confidence) | Positive control for the existing specialized template. |
| `papier-2024-proteomic-cancer-risk` | `generic-research-note` | Exposes the missing biomedical template. |
| `cornelio-2023-ai-descartes` | `generic-research-note` | Exposes the missing CS/ML methods template. |
| `dorgeist-2024-terrestrial-carbon-fluxes` | `generic-research-note` | Exposes the missing environmental-modeling template. |
| `smith-2024-supply-chain-regulations` | `generic-research-note` | Exposes the missing social-science empirical template. |

The four generic routes are expected limitations, not annotation failures.
Generated-note fixtures preserve them. Human-reviewed reference notes live in
`benchmarks/gold/notes/` and must never be presented as current generation
quality.

## Acquisition boundary

Run the maintainer fetch/check command from the repository root:

```bash
python benchmarks/scripts/fetch_corpus.py --check-only
```

Omit `--check-only` to acquire the publisher files recorded in the manifest.
Downloaded PDFs land under the ignored `benchmarks/corpus/files/` directory.
Publisher downloads are not an ordinary PR-CI dependency. Before enabling the
offline S5 CI job, publish these exact hashes as a versioned dataset or GitHub
Release artifact and pin the CI cache to that artifact.
