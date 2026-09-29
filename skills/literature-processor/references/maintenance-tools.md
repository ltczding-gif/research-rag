# Maintenance Tools

Scripts in this directory plus optional vault-side helpers. All paths
read from `.env` — substitute `$REPO_ROOT` for the cloned repository
location and `$LOCALRAG_NOTES_DIR` for your vault root.

## Verify History vs Notes

Use when `processed_history.txt` and note files may have drifted:

```bash
python scanner/verify_and_clean.py
python scanner/verify_and_clean.py --clean
```

This checks:

1. `combined_hash` from note frontmatter or recomputed PDF paths
2. ghost records: history exists, note missing
3. orphan records: note exists, history missing
4. `--clean` removes ghosts after backing up the ledger

## Backfill Missing `combined_hash`

Use for notes generated before the `combined_hash` frontmatter field was introduced:

```bash
python scanner/backfill_hash.py            # preview
python scanner/backfill_hash.py --write    # actually write
```

## Clean GCS PDF Archive

Default archive layout (Vertex backend only):

```
gs://${GEMINI_VERTEX_GCS_BUCKET}/pdf-inputs/<combined_hash>/
```

Examples:

```bash
python scanner/cleanup_gcs_archive.py --days 30
python scanner/cleanup_gcs_archive.py --hash <COMBINED_HASH> --delete
python scanner/cleanup_gcs_archive.py --days 60 --limit 20 --delete
```

Dry-run is the default. Deletion requires `--delete`.

## Refresh Stale PDF Paths From Zotero

Use when a note still has `zotero_parent_key` but its `pdf_0_path` /
`pdf_1_path` is stale (e.g. after Zotero or ZotMoov reclassification).

This script lives in your vault's `scripts/` directory rather than the
scanner repo — it's per-installation, not generally useful:

```bash
python "$LOCALRAG_NOTES_DIR/scripts/refresh_note_pdf_paths_from_zotero.py" \
    --vault-root "$LOCALRAG_NOTES_DIR" \
    --zotero-db "$ZOTERO_DB_PATH" \
    --zotero-data-dir "$ZOTERO_DATA_DIR"

# Add --apply to actually rewrite the notes; default is dry-run.
```

If the script doesn't exist locally, it's safe to skip — manual
frontmatter editing or re-generation via `--force` works as a fallback.

## Batch Candidate Tagging On Existing Notes

These scripts live in your vault's `scripts/` directory (`$LOCALRAG_NOTES_DIR/scripts/`),
not in this skill bundle. They're per-installation and reference the
catalysis pack's tagging schema. Adapt or skip for non-catalysis fields.

### Layer 1 — deterministic prefill (no API, fast)

`prefill_candidate_tags.py` matches note frontmatter and body text against
the local taxonomy/rules and writes `candidate_tags_high/medium/low` in place.

```bash
PREFILL="$LOCALRAG_NOTES_DIR/scripts/prefill_candidate_tags.py"

# Preview without writing
python "$PREFILL" --dry-run

# Fill only notes where candidate fields are still empty (default mode)
python "$PREFILL" --mode fill-empty

# Merge new suggestions into notes that already have candidates
python "$PREFILL" --mode merge

# Recompute all candidate fields from scratch
python "$PREFILL" --mode recompute

# Target specific files
python "$PREFILL" --mode merge --files "$LOCALRAG_NOTES_DIR/SomeNote_review_note.md"

# Limit to N notes (useful for spot-checking)
python "$PREFILL" --mode fill-empty --limit 20
```

### Layer 2 — full pipeline with Kimi API

`run_tagging_pipeline.ps1` orchestrates state selection → prefill → Kimi
candidate tagger → validation in batches.

```powershell
$PIPELINE = "$env:LOCALRAG_NOTES_DIR\scripts\run_tagging_pipeline.ps1"

# Process only notes flagged as changed or unprocessed (default)
& $PIPELINE

# Explicit mode selection
& $PIPELINE -Mode unprocessed
& $PIPELINE -Mode retag-all

# Control batch size (default 10) and max batches (0 = unlimited)
& $PIPELINE -Mode unprocessed -BatchSize 20 -MaxBatches 5

# Skip Kimi API call (deterministic prefill only, equivalent to Layer 1)
& $PIPELINE -Mode unprocessed -SkipKimi

# Target explicit files
& $PIPELINE -Files "$env:LOCALRAG_NOTES_DIR\SomeNote_review_note.md"
```

Available `-Mode` values: `changed`, `unprocessed`, `errors-only`, `retag-all`,
`retag-tagged-all`, `retag-by-ruleset`.

## Common Failure Map

| Symptom | What to do |
| --- | --- |
| `database is locked` | Zotero is still open; close it and retry |
| `quota exceeded` / `429` | wait and retry, or inspect provider quota |
| `FileNotFoundError` | log the missing PDF and continue the batch |
| empty YAML metadata | treat as model-format failure and consider `--force` rerun |
| garbled note filename | verify `$env:PYTHONUTF8 = "1"` (Windows) or terminal locale (Unix) |
| missing `google-cloud-storage` | `pip install google-cloud-storage` |
| bucket / `storage.objects.create` 403 | grant the service account storage permissions |
| Vertex AI API disabled | enable `aiplatform.googleapis.com` in your `GOOGLE_CLOUD_PROJECT` |
