# Open Source Readiness Review

Date: 2026-05-09
Reviewer: Codex
Target: `<repo>`
Scope: product/readiness review for publishing `research-rag` as a GitHub open-source repository.

## Verdict

**Request changes before stable public release. Acceptable as an alpha/preview release.**

The repository is already substantially better than a personal script dump: it has a clear README, environment-based configuration, setup scripts, a doctor command, domain packs, multiple LLM backends, Claude Code plugin metadata, skills, and a meaningful smoke-test suite.

However, the first-run story still has enough inconsistencies that a new user could hit avoidable confusion. The main blockers are not architectural; they are release hygiene, documentation accuracy, and clean-room validation.

## Validation Performed

- `git status --short --branch`
  - Result: clean `main` before this review document was added.
- `<python-3.11> -m pytest tests/ -q`
  - Result: `144 passed, 1 skipped in 1.24s`.
- `<python-3.11> scanner\doctor.py`
  - Result on this machine: reported missing local service dependencies in the current interpreter, missing `.env`, Ollama not reachable, and query server not running. This is useful evidence that `doctor.py` explains failures well, but it is not a clean-room install test.
- Static inspection of README, setup scripts, plugin metadata, skills, config files, and status docs.

## Findings

### P0 - License is still TBD, blocking public open-source release

Evidence:
- `README.md:387-389` has a License section but says `TBD before public release`.
- `.claude-plugin/plugin.json:6` sets `"license": "TBD"`.

Why it matters:
GitHub users cannot safely adopt, redistribute, package, or contribute to the project without a real license. Plugin metadata also exports the ambiguity directly to install surfaces.

Recommendation:
Choose and commit a license before public release. For broad adoption, use MIT or Apache-2.0. If patent protection matters, prefer Apache-2.0. Add a root `LICENSE` file and update README/plugin metadata.

### P1 - Default backend story is inconsistent across README, config, and setup

Evidence:
- `README.md:22` says `.env.example` defaults to `subagent` so a fresh clone runs with no credentials.
- `scanner/config.py:144` falls back to `LOCALRAG_PROCESSOR_BACKEND` default `"vertex"` when no `.env` or env var is present.
- `scanner/init_environment.py:203` and `scanner/init_environment.py:249` also use `"vertex"` as fallback when no value is found.
- `skills/gemini-literature-processor/SKILL.md:75` presents `vertex` as the default example, while subagent is documented later.

Why it matters:
The project is trying to make literature processing feel easy. The first path in README says "no credentials"; the raw code path and parts of the skill docs still lean toward Vertex/GCP. A new user running a script before completing `init_environment.py` can land in the hardest backend by accident.

Recommendation:
Make `subagent` the default everywhere intended for first-run UX, or explicitly state that `subagent` is only the `.env.example`/guided-setup default while raw CLI fallback remains `vertex`. The cleaner release posture is to default to `subagent` in `scanner/config.py` and `init_environment.py`, then describe Vertex as the high-fidelity production option.

### P1 - No clean-room end-to-end bootstrap has been recorded

Evidence:
- `STATUS.md:214` says the fresh-clone verification sequence has not yet been tested end-to-end.
- `tests/README.md` explicitly says there is no end-to-end test for `build_pdf_db.py` / `query_server.py`.
- Existing automated tests pass, but they intentionally avoid real Zotero, ChromaDB, Ollama, and LLM provider calls.

Why it matters:
The unit/smoke tests prove many internals are solid, but they do not prove the promise users care about: clone repo, configure paths, scan a few PDFs, build indexes, query literature. For an open-source tool whose value is workflow simplification, this is the core acceptance test.

Recommendation:
Before stable release, run and document a clean-room E2E on a fresh account or VM:

1. Clone repository.
2. Run `setup.ps1` or `setup.sh`.
3. Run `scanner/init_environment.py`.
4. Use 3-5 public sample PDFs or a tiny fixture Zotero DB.
5. Run `scanner/zotero_batch_scanner.py --limit 5 --backend subagent` or a low-cost API backend.
6. Run `service/build_notes_db.py`, `service/build_pdf_db.py`, and `service/query_server.py`.
7. Execute a search workflow and record expected output.

### P1 - README/plugin naming and count metadata are inconsistent

Evidence:
- `README.md:52` says the plugin registers `research-literature`, but the actual skill is `skills/search-literature/SKILL.md`.
- `skills/search-literature/SKILL.md:2` declares `name: search-literature`.
- `.claude-plugin/plugin.json:4` says the plugin exposes `8 retrieval skills + 10 named workflows`.
- The `skills/` directory contains 8 skills total, but several are infrastructure/advisory skills rather than retrieval skills: `rag-engineer`, `vector-database-engineer`, `embedding-strategies`.
- `.claude-plugin/marketplace.json:12` says "10 named workflows", while README and skills should be the source of truth for whether that count is still current.

Why it matters:
Plugin install text is one of the first things external users see. Wrong command names or inflated counts create immediate distrust, even if the core system works.

Recommendation:
Normalize language around:

- Skill count: "8 skills total" or "3 retrieval/search skills plus supporting skills".
- Primary command: `search-literature`, not `research-literature`, unless the repo intentionally renames the skill.
- Workflow count: keep `WF1a-WF10` only if `skills/search-literature/SKILL.md` actually documents all ten and they remain maintained.

### P1 - Documentation has stale architecture snapshots that contradict current code

Evidence:
- `STATUS.md:46` says port conflicts were resolved to `18810`.
- `docs/ARCHITECTURE.md:182` still records `18800` as the old port in the comparison table.
- `README.md:344` and `.claude-plugin/plugin.json:43` say the default embedding model is `qwen3-embedding:0.6b`.
- `docs/ARCHITECTURE.md:35` and `docs/Project_Architecture_Blueprint.md` still present `qwen3-embedding:4b` as the stack default/baseline in several places.
- `scanner/init_environment.py:394` falls back to `qwen3-embedding:4b` if the env file has no model value, even though `.env.example` and README now position `0.6b` as the default.

Why it matters:
The repo contains a lot of documentation, which is good, but stale docs increase the cognitive load for exactly the users this tool wants to help. Port and embedding-model drift are especially painful because they cause real runtime failures or Chroma dimension mismatch confusion.

Recommendation:
Create a single "current defaults" table and update README, `.env.example`, `doctor.py`, `init_environment.py`, plugin metadata, and architecture docs from it. At minimum, align:

- Query port: `18810`.
- Default embedding model: either `qwen3-embedding:0.6b` for first-run laptop friendliness or `qwen3-embedding:4b` for quality, but not both as "default".
- Backend default: see P1 above.

### P2 - User-facing skills are still Chinese-first, which limits public adoption

Evidence:
- `skills/search-literature/SKILL.md` description and workflow text are primarily Chinese.
- `STATUS.md:175` already tracks English skill mirrors as a remaining public-release task.

Why it matters:
Chinese-first skills are great for the original workflow, but GitHub users will expect English-first installable behavior. The README is English-first, so the actual command behavior should match.

Recommendation:
For public release, use:

- `SKILL.md` as English canonical.
- `SKILL.zh.md` as Chinese mirror.
- Keep examples in both languages where useful.

### P2 - The repo needs a tiny public sample corpus or fixture path

Evidence:
- README explains how to scan a real Zotero library.
- Tests intentionally avoid real Zotero/PDF/ChromaDB integration.
- No obvious sample corpus or fixture Zotero DB is present.

Why it matters:
Asking users to point a new tool at their personal Zotero library as the first validation step raises trust and privacy friction. A tiny public sample lets users validate the pipeline before touching private data.

Recommendation:
Add `examples/sample-corpus/` with 3-5 public-domain/open-access PDFs or generated fixture PDFs, plus a documented dry-run path. If real PDFs are legally awkward, ship synthetic PDFs that exercise metadata, chunking, and note rendering.

## Positive Notes

- The domain-pack architecture is the strongest product idea. It makes the project extensible across fields without forking core code.
- The backend abstraction is directionally right: Vertex/Gemini/Anthropic/OpenAI-compatible/subagent gives users real deployment choice.
- `doctor.py` is exactly the right kind of open-source affordance: it makes failure states explainable rather than mysterious.
- The current smoke test suite is meaningful and fast: `144 passed, 1 skipped` is a strong baseline for an alpha release.
- The repository has already removed obvious personal/Feishu credential surfaces and has a reasonable `.gitignore` for local state.

## Suggested Release Plan

### Alpha release checklist

- Add a real license and root `LICENSE` file.
- Fix README/plugin command names and skill/workflow counts.
- Decide and align defaults for backend and embedding model.
- Add a "Known limitations" section that clearly says no full Docker/E2E harness yet.
- Keep the alpha label explicit in README and GitHub release notes.

### Stable release checklist

- Record a clean-room E2E bootstrap on Windows and one Unix-like environment.
- Add a tiny sample corpus or fixture path.
- Add English-first skill mirrors.
- Add CI for tests plus import checks.
- Add a minimal query-server integration test, even if it runs in `LOCALRAG_SKIP_CHROMA_INIT=1` or against a tiny temporary Chroma collection.

## Recommendation

Publish as **alpha/preview** if the goal is to invite collaborators and feedback.

Do **not** market it yet as a polished "literature processing made simple" tool until the default path, docs, plugin metadata, and clean-room E2E story are aligned. The underlying idea is strong; the remaining work is release hardening rather than a fundamental redesign.
