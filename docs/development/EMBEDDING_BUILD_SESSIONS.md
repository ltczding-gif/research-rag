# Generation-scoped embedding sessions (candidate implementation)

Both builders share `service/embedding_session.py`. The session holds the expected
embedding contract, validates output dimensions and finite/nonzero vectors, checks
identity at relevant boundaries, and closes before the active pointer is published.
A hashed `embedding-session.json` artifact records the assurance class, input and
returned-vector fingerprints, request count, and cleanup warnings. It contains
neither API keys nor input text. The vector fingerprint is of adapter-returned
Python float values, not independently read-back Chroma bytes.

## Provider boundaries

FastEmbed captures the loaded model and tokenizer objects. Inputs are split using
the captured tokenizer window and checked before embedding. Replacing either object
or changing the expected contract rejects the build. Mutation of internal model
state by an external administrator is outside this local-process contract.

Ollama creates a random `research-rag-build-<uuid>:latest` alias using `/api/copy` and
verifies its digest against the expected source model. Every embedding request goes
to that owned alias with `truncate=false`, with response identity and digest checks.
Ordinary A → B → A updates to the user's source tag therefore do not route different
inputs to different source revisions. The source contract is checked again at
finish because the resulting generation must match the serving configuration.

The alias is cleaned up before publishing, only if its identity still matches the
session. The user's original tag is never overwritten or deleted. Existing alias
collisions, changed alias identity, or an uncertain copy outcome fail closed.
Uncertain copies/cleanup can leave an owned alias; its name is included in the
failure or receipt for explicit inspection. Do not globally delete build-prefixed
aliases: another build may still own one. Administrators must not mutate a running
session's alias. Digest checks are not cryptographic per-response weight attestation.
A real local Ollama copy/embed/delete smoke test is still required.

OpenAI-compatible remote APIs capture endpoint, model and credential, and retain the
existing `operator_declared` revision limitation. The provider must enforce its
input limits and deployment pinning. This patch cannot independently prove remote
weights or prevent a provider changing the deployment behind a stable API name.
Injected test adapters use before/after observed-contract checks, not stronger
identity guarantees than their interfaces can supply.

PDF writes now materialize at most the current 100-chunk embedding batch before each
database write rather than embedding the entire corpus into a second Python list.
This does not remove the prepared PDF IR from memory or claim an end-to-end speedup.
PDF embedding-window subdivision and vector creation run inside the same open
session. The final contract uses that session's expected identity; a healthy PDF
reuse also opens and validates the session rather than consulting an unbound splitter.
Canonical extraction and base chunk preparation still precede the embedding session.

## Compatibility and unresolved matters

No retrieval model, dense default, evidence-scoring rule, or research result changes.
Existing generation readers accept the extra artifact inventory. New pipeline code
fingerprints cause the next requested build to produce a fresh candidate. Previous
source artifacts and immutable generations remain available; there is no automatic
personal-index switch outside an explicit build operation.

The private 23/550 vector variation reported before this patch is NOT attributed to
model-tag replacement, and is NOT declared fixed. The receipt helps separate future
input, identity and numerical-output differences. Bitwise reproducibility is not
claimed. A stale source model configuration after publication can still make query
preflight reject the index; this is not silently bypassed.

## API references used in implementation review

- Ollama copy: https://docs.ollama.com/api/copy
- Ollama embed, response model and `truncate=false`: https://docs.ollama.com/api/embed
- Ollama model digests: https://docs.ollama.com/api/tags
- Ollama delete: https://docs.ollama.com/api/delete

The API shapes above were verified on 2026-09-17. Offline fakes test contract logic;
source documentation does not substitute for exercising the actual installed model.
