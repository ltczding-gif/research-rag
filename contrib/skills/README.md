# Contributor and reference skills

These skills are **not** published by the plugin and are **not** copied by the
setup walkthrough. They are kept for contributors and as starting points for
your own workflows.

| Skill | Status | Use |
|---|---|---|
| `rag-engineer` | reference | Guidance for debugging chunking, recall and evaluation in this repository. |
| `vector-database-engineer` | reference | Guidance for Chroma collection design, filters and index operations. |
| `embedding-strategies` | reference | Guidance for choosing and comparing embedding models and chunk policies. |
| `literature-tagging-pipeline` | template only | Drives scripts from the maintainer's own vault that are not shipped here. It does not work out of the box. |

To use one, copy its directory into your agent's skill directory, for example
`~/.claude/skills/` for Claude Code or `~/.agents/skills/` for Codex.

The user-facing skills live in [`../../skills/`](../../skills/).
