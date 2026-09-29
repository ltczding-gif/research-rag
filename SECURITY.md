# Security policy

## Supported versions

research-rag is alpha software. Security fixes are made on the `main` branch
and included in the next release.

## Reporting a vulnerability

Please do **not** open a public issue for a security problem.

Report it privately through GitHub's
[private vulnerability reporting](https://github.com/ltczding-gif/research-rag/security/advisories/new)
(repository **Security** tab → **Report a vulnerability**). Include the affected
version or commit, reproduction steps and the impact you observed. Do not include
private papers, notes or credentials.

You should receive an acknowledgement within a week. Once a fix is available,
the advisory will be published with credit unless you ask otherwise.

## Scope

Relevant reports include, for example:

- reading or writing files outside the configured notes, Zotero and index
  directories (for example through `get_note` or query-log paths);
- credentials from `.env` or service-account files leaking into logs, notes,
  indexes or model requests;
- document content being sent to a model provider other than the configured
  generation backend or embedding provider;
- instructions embedded in PDFs or notes causing tools to act beyond returning
  evidence (prompt injection that crosses a tool boundary).

Out of scope: the behavior of third-party model providers and MCP clients, and
the scientific accuracy of generated notes. See the data and trust boundaries in
the [README](README.md#data-and-trust-boundaries).
