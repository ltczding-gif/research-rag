# Deploy a verified local library

The normal product interface is the six-tool stdio MCP server. It uses the same
query core as the optional HTTP sidecar; an always-running HTTP process is not
required. The client supplies the answer model and reviews scientific support.

## Build a separate candidate

Keep the previous data root and its configuration. Use a new directory on a disk
with enough room for the source snapshot, canonical artifacts and vectors. Do not
overwrite a legacy store to migrate it.

Reconcile source identity before building. Current notes, archived note versions,
Zotero attachments and legacy indexed files are different inventories. Preserve
original note bytes; put any proven frontmatter correction in a staged copy and
record the original and corrected hashes. Notes without a proven parent remain
outside the canonical input set, with their original files and exceptions listed.

PDF input declarations must bind exact parent/attachment identities and actual
file bytes. Preserve main/SI roles; a second main-paper version is not an SI.
Deduplicate exact attachment aliases, record relocated sources, and resolve
same-attachment role conflicts before declaring them ready. An existing filename
alone is not proof of attachment identity.

The notes and PDF builders may use different input directories. This allows
byte-preserving note snapshots and separate generated PDF-declaration stubs:

```text
LOCALRAG_CHROMA_PATH=<new-candidate>/chroma
LOCALRAG_HOME=<new-candidate>
LOCALRAG_EMBED_PROVIDER=ollama
OLLAMA_EMBED_MODEL=qwen3-embedding:4b
OLLAMA_EMBED_URL=http://127.0.0.1:11434/api/embeddings
ZOTERO_DB_PATH=<actual-zotero-database>
```

Run `service/build_notes_db.py` with `LOCALRAG_NOTES_DIR` set to the staged notes,
then `service/build_pdf_db.py` with it set to the reconciled PDF-declaration
directory, using the approved Chroma environment. Both publish into the candidate
root only. Never aim a partial input directory at an existing production root.

The builders use at most 16 inputs per embedding batch. Ollama submits them in one
request, verifies the owned model alias before and after, validates returned
vectors and preserves input order. Other providers retain their existing serial
embedding behavior. Notes writes are also bounded to 16 sections; PDF writes
retain the 100-chunk boundary. These changes reduce calls, not source coverage.

The embedding receipt's `request_count` remains an input count for compatibility;
`request_count_unit` makes that explicit. Ollama separately records
`embedding_http_request_count` for `/api/embed` calls. Numerical equivalence
between single-input and batched embeddings is not assumed.

An explicit local runner connection reset, refusal or unexpected EOF may retry
the same owned alias. Correctly sized vectors containing nonfinite values or
only zeros may also retry. These failures share a maximum of three HTTP attempts,
one second apart. Each attempt rechecks the alias identity; input, identity,
vector type/dimension and response-count errors are not retried. The whole batch
must validate before recording any successful input. The HTTP count includes
failed attempts; `transient_retry_count` and `invalid_vector_retry_count` record
the two recovery paths. Persistently invalid output still stops publication.

On the audited Windows machine, `localhost` incurred a roughly two-second delay
for every local HTTP request, while `127.0.0.1` did not. Use the actual measured
loopback address consistently during build and serving. Changing it changes the
recorded endpoint contract even when the model/daemon stays the same.

## Validate before selecting the candidate

Verify both complete manifests, source coverage and exceptions, artifact hashes,
embedding identity and collection counts. Then use a real MCP client against the
candidate for all six tools, including valid and invalid citation cases. Record
source budgets, startup and query latency separately. Test the real product flow
with host review; a passed citation check does not establish scientific accuracy.

Keep the corpus, questions, source labels, model, top-k and evidence budget fixed
when comparing results. A larger corpus changes no-answer judgments. Report
unmappable gold coordinates as unscorable. See
[full-library validation](FULL_LIBRARY_VALIDATION.md) and the
[answer workflow](ANSWER_WORKFLOW.md).

## Select the accepted runtime

Bind the local `.env` to the accepted data root, actual source-note root, model
and endpoint. Keep personal paths and acceptance data out of Git. Save the prior
configuration before changing it.

For a managed external Python environment, configure the MCP client to invoke
that interpreter and `service/mcp_server.py` directly with absolute paths and the
repository working directory. The portable `scripts/run_mcp_server.py` launcher
deliberately prefers a repository virtual environment; it should not be used to
force an unrelated external interpreter.

Codex supports a project-scoped `.codex/config.toml` in trusted projects, with
`command`, `args`, `cwd`, and startup/tool timeout settings. Set startup timeout
from measured full-library initialization, rather than assuming the default ten
seconds is enough. [Official MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)

Set `required = true` for this project's Research-RAG server so the initial tool
catalog waits for it. Increasing `startup_timeout_sec` alone is insufficient:
optional servers have a separate default one-second initial-catalog grace. A
real client run returned no tools while the accepted full library needed about
ten seconds to initialize. Keep this setting scoped to the project server.

Test the exact persisted client command in two fresh MCP sessions. Confirm the
accepted generation IDs and repeat discovery, retrieval and citation checking.
Existing server processes pin the generation they loaded, so reconnect the client
after selecting another root/generation. Application tool discovery must be
observed separately from an SDK transport smoke test.

## Recovery and normal operation

- Retain the old data root. To revert an external-root migration, restore its
  saved client/environment configuration and reconnect. Do not delete the new
  candidate while a reader may still hold it.
- For rollback within a canonical data root, follow
  [generation recovery](development/GENERATION_RECOVERY.md) and use the trusted
  prior manifest fingerprint. Source files and legacy collections are retained.
- New source material needs a fresh reconciled input snapshot and candidate
  build. Updating a note file alone does not change a pinned index.
- A failed build remains a failed candidate; do not rerun blindly or select it
  because some rows exist. Inspect its recorded error and coverage first. Local
  Ollama HTTP failures now retain the daemon's error detail for diagnosis.
- Generation publication is atomic, but embedding builds do not yet resume a
  partially written generation automatically. Preserve diagnostic artifacts and
  any independently verified extraction cache before deciding to rebuild.
- Large PDF collections verify final ID coverage in bounded queries. If an
  older build failed only at that final check, complete stored vectors may be
  recoverable through a separately audited new-generation copy. This requires
  exact fresh source/page/text/metadata agreement and full stored-vector
  readback. Preserve the failed seal; do not relabel it complete or invent a
  missing embedding-session receipt. This is an operator recovery, not automatic
  partial-build resume; see the [recorded release](CANONICAL_RELEASE.md).
- Clean-install/launcher CI and real-data acceptance have different purposes.
  Both must pass before reporting a release as complete.
