# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Until 1.0, minor versions may
include breaking changes.

## [Unreleased]

### Changed

- The note-generation skill is renamed from `gemini-literature-processor` to
  `literature-processor`; its description and post-generation steps now reflect
  the backend-neutral pipeline and `scripts/build_indexes.py`. **Upgrade:**
  remove the old `gemini-literature-processor` copy from `~/.claude/skills/` or
  `~/.agents/skills/`.
- The plugin now publishes the four user-facing skills only. The reference
  skills `rag-engineer`, `vector-database-engineer`, `embedding-strategies` and
  the template-only `literature-tagging-pipeline` moved to `contrib/skills/`
  and are no longer copied by the setup walkthrough.
- Historical audits, investigations, plans, architecture snapshots and the
  staging `STATUS.md` moved to `docs/archive/`. The maintainer's validation
  reports moved to `docs/reports/`. `docs/README.md` indexes the current docs.
- README (English and Chinese): comparison with related tools, the complete
  MCP tool table, measured retrieval and parsing limitations, and a roadmap.

### Added

- Retrieval evaluation: `benchmarks/scripts/retrieval_eval.py` scores retrieval
  strategies against coordinate-pinned gold evidence (span coverage@k, query
  completeness, MRR, evidence-packet coverage, latency and stability), builds
  eval sets from quotes or pooled judgments, and appends every run to a
  text-free score ledger under `benchmarks/results/`. Method and decision rule:
  `docs/RETRIEVAL_EVAL.md`.
- `CONTRIBUTING.md`, `SECURITY.md`, this changelog, issue templates and a pull
  request template.

## [0.1.0] - Unreleased

First public release baseline.

- Zotero-aware discovery that groups main text and supporting information by
  parent item, with content-hash deduplication and resumable runs.
- Two-stage note generation driven by domain packs, with five backends:
  terminal sub-agent (default), Gemini API, Anthropic, OpenAI-compatible and
  Vertex AI.
- Immutable, verified index generations for notes and source-PDF passages with
  three embedding providers: FastEmbed (default), Ollama and OpenAI-compatible.
- Six stdio MCP tools: `search_notes`, `search_papers`, `get_note`,
  `index_status`, `prepare_answer` and `check_answer`.
- Catalysis domain pack and a template for new fields.
- Cross-platform guided setup, a synthetic demo, health checks and recovery
  tooling.

[Unreleased]: https://github.com/ltczding-gif/research-rag/commits/main
