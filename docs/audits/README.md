# Audit reports

Code review reports produced by sub-agents at specific commits. Each is a
**snapshot** of the system at that commit's HEAD — the issues flagged may
have already been fixed in subsequent commits. Cross-reference STATUS.md
or `git log` for current state.

| File | Subject | Commit reviewed | Disposition |
|---|---|---|---|
| [2026-05-08-backend-pluggability-review.md](2026-05-08-backend-pluggability-review.md) | Pluggable processor backends (Vertex / Gemini API / Anthropic / sub-agent) | `5560d92` | All Critical + most Important fixed in `aaa0441`; remaining items deferred to later commits or [POLISH-EVALUATION.md](../POLISH-EVALUATION.md). |
| [2026-05-08-openai-backend-review.md](2026-05-08-openai-backend-review.md) | OpenAI / OpenAI-compatible backend addition | `8d2aed8` | All Critical + Important + 2 of 4 Minor fixed in `e982d0a`. |

These reports are kept verbatim — *don't* edit them after the fact. The point
is to be able to look back and see what was actually found at that commit,
not the polished narrative of "everything got fixed." If a reviewer later
got something wrong, fix it in the *code*, not in the *audit*.

The disposition column above is the only place to update when issues land.
