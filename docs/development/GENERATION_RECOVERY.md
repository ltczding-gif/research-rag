# Generation publication and explicit recovery (Alpha)

The active-pointer replacement is the commit point. A complete generation manifest
is sealed: a later exception must never turn it into `failed`. A complete but
unpublished candidate may remain after an interrupted pointer replacement; it is
not automatically selected, deleted, or promoted.

`GenerationStore` now observes the actual persistent pointer after an exception.
Errors become separate best-effort events. An error recorder cannot overwrite a
newer `latest-attempt` entry. Failures recording diagnostics do not invalidate a
sealed manifest. Publication, rollback, and explicit recovery use this boundary.

## CLI outcomes

Exit `0` means the command completed normally. Exit `3` means an index operation
committed or completed, but subsequent output/processing raised a warning. Inspect
status before retrying. The aggregate builder stops rather than starting another
collection after exit `3`. An interruption with an unknown outcome also requires
status inspection. No exception handler claims that active is unchanged without
checking it. A success-output failure never rewrites a manifest.

The query service still pins its generation at startup. Restart after adopting a
new generation. This change adds neither hot reload nor online pruning.

## Safe replacement builds

Reuse requires a valid manifest, artifacts, collection count, and matching
collection generation identity. A valid manifest with damaged/missing collection
or artifacts can be rebuilt from a complete readable source snapshot. Original
files and the previous collection are not deleted before publication.

Use `python service/build_notes_db.py --rebuild` or
`python scripts/build_indexes.py --rebuild-notes` to force a new notes candidate.
The existing `--rebuild-papers` behavior remains. Neither flag bypasses an invalid
active manifest, unreadable sources, or the explicit source-withdrawal policy.

The previous regression test expecting an unrecoverable notes count error is
replaced by assertions for a verified replacement and retained previous collection.
Source withdrawal, rename, input-window, and full-note tests remain.

## Recover a corrupt or missing active pointer

Stop query processes first. Select a complete generation using a manifest
fingerprint retained in an independently trusted inventory. Never establish trust
merely by hashing a potentially damaged manifest at recovery time.

```text
python scripts/manage_index_generations.py --chroma-path <root> --logical-name notes status
python scripts/manage_index_generations.py --chroma-path <root> --logical-name notes recover --generation-id <32-hex-id> --manifest-fingerprint <trusted-64-hex-fingerprint>
```

Recovery validates namespace/path containment, identity, trusted manifest
fingerprint, embedding compatibility, successful sources, artifacts, collection
count, and collection generation identity. It saves the old pointer bytes and hash
in an intent record before replacement, then checks that the pointer did not
change. It refuses to replace a valid active manifest; use rollback or a replacement
build in that situation. Recovery does not rewrite the selected manifest.

A damaged generation itself cannot be repaired by changing its pointer. Recover to
another trusted complete generation or restore a trusted backup. There is no
implicit “pick the newest folder” rule. Reader shutdown and the existing writer
lock remain operational requirements; external writers bypassing those rules are
not supported.

## Evidence boundary

Fault-injection tests use real temporary files and original modified store/builder
functions, with fake collection/model interfaces. They do not prove power-loss
resilience, real Chroma durability, cross-platform integration, or research answer
quality. Complete installed-repository tests and real-provider smoke tests remain
required before merge. The project remains Alpha and dense retrieval is unchanged.
