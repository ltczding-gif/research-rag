# Reliable local indexes (Alpha)

New notes and papers builds publish complete, immutable generations. A failed or
interrupted candidate cannot replace the active generation. The two collections
publish independently: if notes succeeds and papers fails, the command fails while
the newly published notes and previous papers remain available.

## Build and source identity

Run `python scripts/build_indexes.py` with your configured service interpreter.
`--notes-only` builds only notes. `--rebuild-papers` forces a fresh papers candidate;
it does not delete the current index. `--allow-removals` explicitly permits sources
missing from the current full snapshot to be withdrawn from both requested indexes.
An unavailable root, empty scan, invalid note, missing declared PDF, empty PDF text,
or unresolved PDF identity fails the build instead of silently shrinking coverage.

Notes require a nonempty `zotero_parent_key`. Their original UTF-8 bytes are stored
with each generation; headings and the actual embedding window determine retrieval
sections. All source characters remain represented. `get_note` returns the complete
stored entity, even when `search_notes` returns a section near its end. Identical
content renamed to a new note filename is matched one-to-one; removing another copy
still requires `--allow-removals`.

PDFs require an exact Zotero parent/attachment pair. The resolver accepts an explicit
`pdf_N_attachment_key` paired with the note's parent or an unambiguous, read-only
Zotero storage/linked-path lookup. A basename is not an identity. `pdf_0_path` is main;
every other declared index is SI, including sparse indices. Missing entries never
renumber surviving attachments. `pdf_N_source_path` preserves an original path when
`pdf_N_path` points to a copied input. Changing an attachment key withdraws the old
identity and therefore requires `--allow-removals`, even if its bytes are identical.

Canonical PDF pages use the fixed pdfplumber extraction contract. Base 800-character
windows (step 700) are further split when required by the embedding tokenizer. The
default multilingual FastEmbed model tested here has a 128-token input window.
Offsets and real chunk adjacency survive this subdivision. Inter-page separators
have no fabricated page spans; a unit containing only derived separators needs no
embedding. Source-bearing text remains represented. The legacy final-reference
truncation policy still applies; this is a text-extraction index, not OCR.

## Readiness and query contract

Restart the MCP/query process after publishing: a process pins the generation it
loaded. `index_status` separates its usable active generation from `latest_attempt`.
A failed build can coexist with a ready previous generation; a first failed build
has no ready canonical index. Notes-only installation is supported.

Manifests bind sources, extractor/chunker code and settings, dependency versions,
provider, model revision, dimensions, cosine distance, and stored artifact hashes.
Model identity is checked even if dimensions match. Ollama uses its model digest;
FastEmbed hashes local model/tokenizer artifacts. OpenAI-compatible deployments
must set `OPENAI_EMBED_REVISION` to an operator-pinned model version. A remote alias
is not independently verifiable; operators must also enforce the remote provider's
input-limit behavior. Such providers were not part of the local acceptance run.

`search_papers` accepts `zotero_parent_key`, `zotero_attachment_key`, `source_role`
(`main` or `si`), and `pdf_filename`. Every supplied filter is ANDed. A known
attachment conflicting with the supplied parent, role, or filename returns HTTP 400;
an unknown attachment returns no hits. Invalid roles or legacy numeric `paper_group`
on canonical indexes return explicit errors. HTTP additionally retains `source_type="pdf"`.

Canonical hits keep `content`, `metadata`, and cosine `distance`, and add verified
`evidence`. Each segment has a zero-based physical `pdf_page_index`, one-based display
`page_number`, half-open character offsets, page hash, exact quote, and quote hash.
Cross-page quotes are separate segments. Missing or inconsistent evidence fails the
request; it is never returned as verified. Context uses actual adjacent IDs and
returns their evidence as well. Old fixed-name collections remain readable as
`legacy_unverified`; they cannot prove canonical provenance or same-dimensional
model identity. Rebuilding is required for those guarantees.

Server timings separate query embedding, retrieval/evidence preparation, reranking
(zero when disabled), JSON preparation, and total. They exclude transport. MCP client
wall time and process initialization must be recorded separately. The default remains
dense Chroma retrieval; research NumPy ranking is a different backend, so its kernel
timing is not a serving latency promise.

## Offline rollback and retention

Stop query processes before maintenance. The utility requires an explicit index
root and logical collection name:

```text
python scripts/manage_index_generations.py --chroma-path <index-root> --logical-name papers status
python scripts/manage_index_generations.py --chroma-path <index-root> --logical-name papers rollback
python scripts/manage_index_generations.py --chroma-path <index-root> --logical-name papers prune
```

Rollback validates the bound previous manifest, artifacts, collection identity/count,
and current embedding contract before atomically changing the pointer. Select the
previous model configuration first if its embedding contract differs. No manifest is
rewritten. Prune is explicit and retains active, previous, and the latest attempt;
it only removes generations/collections owned by the chosen logical namespace. It
does not touch original notes/PDFs, legacy collections, or another namespace. Older
generations are retained until this offline maintenance is requested.

## HTTP log boundary

The optional HTTP writer serializes the complete read/check/write transaction in
one process and atomically replaces files. Retry can recover an orphan log whose
registry update failed. Damaged registries and different-key filename collisions
fail explicitly. This is a single-process writer contract, not multi-worker or
cross-process transactional logging. Stdio retrieval does not write these logs.
