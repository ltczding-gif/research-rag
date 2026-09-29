---
name: literature-tagging-pipeline
description: Run and supervise the literature-tagging workflow for the $LOCALRAG_NOTES_DIR Obsidian vault. Use this when processing new or changed *_review_note.md files with Kimi batch execution, gate validation, state tracking, and navigation-hub refresh.
---

# Literature Tagging Pipeline

> ⚠️ **Not functional out-of-the-box.** This skill drives scripts and config
> files that live in the maintainer's vault (`$LOCALRAG_NOTES_DIR/scripts/*`,
> `$LOCALRAG_NOTES_DIR/wiki/*.yaml` — see "Canonical workspace files" below);
> **none of them ship with this repo**. Treat this SKILL.md as a reference
> template for building your own post-generation tagging pipeline, or skip it
> entirely — note generation and retrieval do not depend on it.

Use this skill for the `$LOCALRAG_NOTES_DIR` vault when you need to run the production tagging workflow end-to-end instead of ad hoc scripts.

This is the top-level workflow skill. It wraps the current queue selection, Kimi execution, per-batch gate validation, state bookkeeping, and Obsidian navigation refresh.

## What this skill covers

- incremental queue selection for new or changed `*_review_note.md`
- manual rerun modes for `errors-only`, explicit files, or ruleset retag
- duplicate-title companion notes are automatically co-batched so one title group is not split across batches
- Kimi batch execution through the existing pipeline scripts
- per-batch gate validation and rollback
- stable report generation under `progress/gate_reports` and `progress/pipeline_reports`
- status recording under `status/current-status.md` and `status/task-log.md`
- skill-local monitoring through:
  - `$REPO_ROOT/skills/literature-tagging-pipeline/scripts/watch_tagging_pipeline.ps1`

## Canonical workspace files

- `$LOCALRAG_NOTES_DIR\scripts\run_tagging_pipeline.ps1`
- `$LOCALRAG_NOTES_DIR\scripts\run_tagging_gate.ps1`
- `$LOCALRAG_NOTES_DIR\scripts\validate_tagging_batch.py`
- `$LOCALRAG_NOTES_DIR\scripts\tagging_state.py`
- `$LOCALRAG_NOTES_DIR\scripts\generate_navigation_hubs.py`
- `$LOCALRAG_NOTES_DIR\wiki\tagging_prompt.md`
- `$LOCALRAG_NOTES_DIR\wiki\taxonomy.yaml`
- `$LOCALRAG_NOTES_DIR\wiki\tag_aliases.yaml`
- `$LOCALRAG_NOTES_DIR\wiki\material_hierarchy.yaml`
- `$LOCALRAG_NOTES_DIR\progress\tagging_state.jsonl`
- `$LOCALRAG_NOTES_DIR\status\current-status.md`
- `$LOCALRAG_NOTES_DIR\status\task-log.md`

## When to Use

- new literature notes were added to the vault
- existing review notes were modified and may need retagging
- you want to run the default incremental tagging flow safely
- you need to retry failed notes only
- taxonomy or tagging rules changed and a controlled retag is needed
- you need a supervised Kimi batch instead of trusting manual edits

## Hard Rules

- Never run multiple `run_tagging_gate.ps1` or `run_tagging_pipeline.ps1` jobs that write to the same vault in parallel.
- Do not manually split duplicate-title companion notes into separate batches; the pipeline now expands duplicate-title closure automatically.
- In gate mode, do not manually edit `progress/*` files to fake completion.
- Trust the filesystem and JSON reports, not Kimi's plain-text summary alone.
- New concepts must be reported as `CANDIDATE_NEEDED`; do not append directly to `taxonomy_candidates.tsv` during gate execution.
- After each completed task, update `$LOCALRAG_NOTES_DIR\status\current-status.md` and append a record to `$LOCALRAG_NOTES_DIR\status\task-log.md`.
- Follow the vault AGENTS rules for Python: use `$LOCALRAG_MAIN_PYTHON` for general scripts.
- If a pipeline batch is actively running, avoid editing workspace `scripts/*` or `wiki/*` unless you intentionally want the current batch to fail validation.
- Prefer the skill-local watcher while a batch is active, because it does not touch monitored workspace files.

## Default Workflow

### Step 1: Read the current state

Read these files first:

- `$LOCALRAG_NOTES_DIR\status\current-status.md`
- `$LOCALRAG_NOTES_DIR\wiki\tagging_prompt.md`
- `$LOCALRAG_NOTES_DIR\progress\tagging_state.jsonl`

If the task is rule-sensitive, also read:

- `$LOCALRAG_NOTES_DIR\wiki\taxonomy.yaml`
- `$LOCALRAG_NOTES_DIR\wiki\tag_aliases.yaml`
- `$LOCALRAG_NOTES_DIR\wiki\material_hierarchy.yaml`

### Step 2: Pick the narrowest safe mode

Preferred order:

1. `changed`
2. `unprocessed`
3. `errors-only`
4. explicit `-Files`
5. `retag-by-ruleset`
6. `retag-tagged-all`
7. `retag-all`

Default to `changed` unless the user clearly wants a broader rerun.

### Step 3: Run the pipeline

Default incremental run:

```powershell
& "$LOCALRAG_NOTES_DIR\scripts\run_tagging_pipeline.ps1" `
  -Workspace "$LOCALRAG_NOTES_DIR" `
  -Session "tagging-pipeline-<session-name>" `
  -Mode "changed" `
  -BatchSize 5
```

Retry only failures:

```powershell
& "$LOCALRAG_NOTES_DIR\scripts\run_tagging_pipeline.ps1" `
  -Workspace "$LOCALRAG_NOTES_DIR" `
  -Session "tagging-pipeline-errors" `
  -Mode "errors-only" `
  -BatchSize 5
```

Run explicit files only:

```powershell
& "$LOCALRAG_NOTES_DIR\scripts\run_tagging_pipeline.ps1" `
  -Workspace "$LOCALRAG_NOTES_DIR" `
  -Session "tagging-pipeline-explicit" `
  -Files @(
    "$LOCALRAG_NOTES_DIR\some_note_review_note.md"
  ) `
  -BatchSize 1
```

### Step 4: Validate the outcome

Always inspect:

- latest pipeline report in `$LOCALRAG_NOTES_DIR\progress\pipeline_reports\`
- related gate reports in `$LOCALRAG_NOTES_DIR\progress\gate_reports\`

Confirm:

- pipeline status is `completed` or expected `partial`
- gate did not report unauthorized file writes
- duplicate-title companion notes, if any, were processed in the same batch
- navigation refresh completed
- rollback happened if a batch failed

### Step 4.5: Monitor long-running sessions

Use the skill-local watcher when you need live status without mutating the vault.

Watch until the current batch finishes:

```powershell
& "$REPO_ROOT/skills/literature-tagging-pipeline/scripts/watch_tagging_pipeline.ps1" `
  -Workspace "$LOCALRAG_NOTES_DIR" `
  -Session "tagging-pipeline-full-20260407-r2" `
  -UntilBatchDone `
  -RefreshSeconds 20
```

Watch until the whole pipeline finishes:

```powershell
& "$REPO_ROOT/skills/literature-tagging-pipeline/scripts/watch_tagging_pipeline.ps1" `
  -Workspace "$LOCALRAG_NOTES_DIR" `
  -Session "tagging-pipeline-full-20260407-r2" `
  -UntilPipelineDone `
  -RefreshSeconds 30
```

One-shot JSON snapshot:

```powershell
& "$REPO_ROOT/skills/literature-tagging-pipeline/scripts/watch_tagging_pipeline.ps1" `
  -Workspace "$LOCALRAG_NOTES_DIR" `
  -Session "tagging-pipeline-full-20260407-r2" `
  -RefreshSeconds 5 `
  -MaxChecks 1 `
  -AsJson
```

### Step 5: Record state

After the task finishes:

- update `$LOCALRAG_NOTES_DIR\status\current-status.md`
- append the task summary to `$LOCALRAG_NOTES_DIR\status\task-log.md`

## When to drop to lower-level tools

Use `run_tagging_gate.ps1` directly only when:

- debugging one specific batch
- reproducing a validator failure
- testing rollback behavior

Use `validate_tagging_batch.py` directly only when:

- diagnosing a suspicious result outside the normal pipeline
- checking a known set of files without running Kimi

## Integration

- Pair with [`$kimi-supervision`]($KIMI_SUPERVISION_SKILL_PATH) when Kimi output is incomplete, suspicious, or needs native artifact inspection.
- Other agents should start from this skill for normal literature-tagging work in `$LOCALRAG_NOTES_DIR`.
- The pipeline is the default entrypoint; the gate is the focused diagnostic entrypoint.

## Quality Checklist

- [ ] Read current status before starting
- [ ] Used the narrowest safe queue mode
- [ ] Confirmed duplicate-title closure did not split a title group across batches
- [ ] Avoided parallel writes to the vault
- [ ] Verified pipeline and gate JSON reports
- [ ] Confirmed navigation refresh status
- [ ] Updated `status/current-status.md`
- [ ] Appended `status/task-log.md`

## Anti-Patterns

- Running `run_tagging_gate.ps1` manually for full-vault incremental work
- Trusting only Kimi's summary without checking JSON reports
- Manually editing `progress/*` to skip gate results
- Launching multiple pipeline sessions against the same vault at once
- Using broad retag modes when `changed` or explicit files would suffice
