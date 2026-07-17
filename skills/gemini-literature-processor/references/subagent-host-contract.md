# Sub-Agent Host Contract (host-agnostic)

This document specifies how a parent LLM agent — running in **Claude Code**,
**OpenAI Codex CLI**, **OpenClaw**, or any other tool-using LLM — drives the
note-generation pipeline **without configuring any external API key**, by
delegating model calls to a freshly-dispatched sub-agent.

If you only care about the Claude Code happy path, see SKILL.md. This page
exists for everyone else.

---

## Why this exists

The literature processor's note generator is a structured-output LLM call. The
default backends (`vertex`, `gemini-api`, `anthropic`, `openai`) call a remote
API directly. The `subagent` backend takes a different shape:

1. The Python pipeline writes a **manifest JSON** describing the work.
2. The pipeline exits without making any model call.
3. The host agent dispatches a **sub-agent** that reads the manifest, processes
   the PDFs, and writes the structured JSON back to disk.
4. The host agent re-invokes the pipeline. The pipeline picks up the JSON,
   advances to the next stage, and repeats.

This means: **whichever LLM agent you're already chatting with becomes the
"model"** for the pipeline. No additional API key, no additional service
account, no additional cost.

---

## Two-actor protocol

Every sub-agent run involves **exactly two actors** in the host platform:

### The **parent agent** (orchestrator)

The agent the user is talking to. Its job:

1. Run the scanner (`gemini_analyze_pdf.py` or `zotero_batch_scanner.py`) with
   `--backend subagent`.
2. When the scanner exits with code **200**, read the manifest(s) it wrote.
3. **Dispatch a sub-agent** for each pending manifest. (Tool name varies by
   host — see "Mapping to your platform" below.)
4. Wait for every sub-agent to finish (i.e. each `expected_output_path`
   becomes a non-empty valid JSON file).
5. Advance the scanner. **Batch mode** (`zotero_batch_scanner.py`):
   re-run the same batch command — it auto-resumes each group's run_dir.
   **Solo mode** (`gemini_analyze_pdf.py`): run the manifest's
   `parent_agent_task.resume_command` (pre-interpolated with
   `--resume <run_dir>`); re-running the bare solo command without
   `--resume` starts a fresh run instead of advancing.
6. Repeat until the scanner exits **0**. Each paper takes **3 passes**
   (Stage A manifest → fill → Stage B manifest → fill → final render).

### The **sub-agent** (worker)

A freshly-dispatched, isolated agent whose only input is the manifest path.
Its job — and **only its job**:

1. Read the manifest JSON at the path given.
2. Read every PDF listed in `manifest.pdf_paths`.
3. Apply `manifest.system_prompt` + `manifest.user_prompt` to those PDFs.
4. Produce a single JSON object that strictly conforms to
   `manifest.response_schema`.
5. Write that JSON to `manifest.expected_output_path`.
6. **Stop.** Do not re-invoke the scanner. Do not touch any other file.

Keeping these roles separate prevents the most common failure mode: a
confused sub-agent trying to drive the loop itself, mutating the ledger,
or recursing into the scanner.

The manifest's `subagent_task` and `parent_agent_task` fields encode the
above split machine-readably.

---

## Manifest contract (`schema_version: 3`)

```jsonc
{
  "schema_version": 3,
  "stage": "profiler" | "note_generator",
  "model_hint": "gemini-2.5-flash",          // advisory only
  "temperature": 0.0,
  "combined_hash": "...",                     // stable id for the paper
  "pdf_paths": ["/abs/path/main.pdf", ...],   // what the sub-agent reads
  "system_prompt": "...",                     // pass verbatim
  "user_prompt": "...",                       // pass verbatim
  "response_schema": { ... },                 // strict JSON schema
  "expected_output_path": "/abs/.../01-document-profile.json",
  "run_dir": "/abs/.../runs/<combined_hash>",

  "subagent_task": {
    "role": "Fresh sub-agent. Has only this manifest as input.",
    "steps": [
      "Read every PDF listed in pdf_paths.",
      "Apply system_prompt + user_prompt to those PDFs.",
      "Produce a single JSON object that strictly conforms to response_schema.",
      "Write that JSON to expected_output_path. No other files, no logs, no scanner re-invocation."
    ],
    "must_not": [
      "Re-invoke the scanner.",
      "Touch the ledger or any file outside expected_output_path.",
      "Read PDFs beyond pdf_paths (the parent already truncated for Stage A)."
    ]
  },

  "parent_agent_task": {
    "role": "The orchestrator that called the scanner. Picks up here.",
    "steps": [
      "Wait for the sub-agent to finish (i.e. expected_output_path becomes non-empty valid JSON).",
      "After ... exists in run_dir, run: `python scanner/gemini_analyze_pdf.py <pdf_paths> --backend subagent --resume \"<run_dir>\"` ..."
    ]
  },

  // Legacy mirrors of the above for older sub-agent prompts; will be
  // removed in schema_version 4.
  "instructions": "...",
  "next_step": "..."
}
```

**The contract for the sub-agent is the entire `subagent_task` block plus the
six top-level fields it references** (`pdf_paths`, `system_prompt`,
`user_prompt`, `response_schema`, `expected_output_path`, `combined_hash`).

Everything else is for the parent agent.

---

## Exit-code contract

Both `gemini_analyze_pdf.py` and `zotero_batch_scanner.py` use a 3-way exit
code so the parent agent can branch deterministically without parsing logs:

| Exit code | Meaning |
|-----------|---------|
| **0**     | Done. Note(s) generated and ledger updated. |
| **200**   | Sub-agent manifest pending. The host must dispatch a sub-agent and re-run the same command. |
| **1** (or other non-zero) | Real error. Read stderr, fix the underlying issue, retry. |

`scanner/list_pending_subagent_runs.py` follows the same convention:
exit `0` if the queue is empty, exit `200` if anything is pending.

This means a parent agent can write a portable guard loop:

```bash
# Pseudo-bash; adapt to your host's sub-agent dispatch syntax.
while true; do
    python scanner/zotero_batch_scanner.py --limit 5 --backend subagent
    rc=$?
    if [ $rc -eq 0 ]; then break; fi
    if [ $rc -ne 200 ]; then echo "real failure"; exit $rc; fi

    # Fan out one sub-agent per pending manifest.
    python scanner/list_pending_subagent_runs.py --json |
        jq -r '.[].manifest_path' |
        while read mp; do dispatch_subagent_against "$mp"; done
done
```

---

## Discovering pending work

Two equivalent shapes:

### Human-readable
```bash
python scanner/list_pending_subagent_runs.py
```

### Machine-readable
```bash
python scanner/list_pending_subagent_runs.py --json
```

returns:

```json
[
  {
    "run_dir": "/abs/.../runs/<hash>",
    "stage": "profiler",
    "manifest_path": "/abs/.../runs/<hash>/manifest-profiler.json",
    "expected_output_path": "/abs/.../runs/<hash>/01-document-profile.json",
    "combined_hash": "<hash>",
    "pdf_paths": ["/abs/path/main.pdf"]
  }
]
```

Iterate over this list, dispatch one sub-agent per entry, wait for every
`expected_output_path` to be filled, then re-run the scanner.

---

## Mapping to your platform

The protocol above is host-agnostic. Below are the host-specific bindings.

### Claude Code

- **Sub-agent dispatch:** `Task` tool with `subagent_type: "general-purpose"`
  (or another fitting subagent if you have one).
- **Prompt to give the sub-agent:**
  > Read the manifest JSON at `<manifest_path>`. Read every PDF in
  > `manifest.pdf_paths`. Apply `manifest.system_prompt` and
  > `manifest.user_prompt` to those PDFs. Produce a single JSON object that
  > strictly conforms to `manifest.response_schema`. Write that JSON (no
  > extra text, no code fences) to `manifest.expected_output_path`. Do not
  > re-invoke any scanner script. Do not touch any other file.
- **Auto-routing:** the `gemini-literature-processor` skill in this repo
  triggers on Chinese phrases like "处理新增论文" / "批量生成笔记".

### OpenAI Codex CLI

- **Sub-agent dispatch:** Codex doesn't have a dedicated Task tool; instead,
  use a fresh `codex exec` invocation as the sub-agent. The parent reads the
  manifest path, then runs:
  ```bash
  codex exec --skip-git-repo-check \
    "Read $MANIFEST_PATH, then follow its subagent_task. Produce JSON
     conforming to response_schema. Write it to expected_output_path
     (do not write to stdout; the parent reads expected_output_path
     directly). Do not re-invoke any scanner script."
  ```
- The PDFs are local files Codex can read directly via its read tool.
- Codex returns when the sub-invocation completes; the parent then re-runs
  the scanner.

### OpenClaw / other tool-using LLMs

Anything with the following shape works:
- Read a local file (the manifest).
- Read PDFs (most modern LLMs accept PDF input either as file uploads or via
  a wrapping CLI like `pdftotext` for `openai`-style providers).
- Write a local file (the JSON output).
- Run a shell command (the re-invocation of the scanner).

The contract is intentionally narrow so the binding stays trivial: the
parent does step 1 (run scanner) and step 4 (re-run scanner); the sub-agent
does step 2-3 (consume manifest, write JSON). Steps 2-3 are described
entirely inside the manifest, in plain English under `subagent_task`.

---

## What this **does not** do

- It does not auto-retry failed sub-agent outputs. There are two failure
  shapes the parent must handle:
  - **Empty / partial / non-JSON write** (sub-agent crashed mid-write):
    the next scanner pass treats the file as not-yet-filled and emits the
    *same* manifest again. Re-dispatching the sub-agent is sufficient.
  - **Valid JSON that doesn't match `response_schema`** (sub-agent
    hallucinated): the next scanner pass loads the file, then crashes
    inside the renderer or schema validator with exit ≠ 200. Recovery is
    manual: delete the offending `01-document-profile.json` /
    `02-note-draft.json` from the run_dir, then re-run the same scanner
    command — the helper will see the run_dir as pending again and the
    parent re-dispatches.
  See `references/workflow-runbook.md` for diagnostics.
- It does not parallelize within a single paper's stages (Stage B depends on
  Stage A). It does parallelize across papers within one batch invocation.
- It does not work for Stage A truncation: the truncated PDF is generated by
  the parent's `pdf_slicer` before manifest emission. The sub-agent never
  sees the full PDF for Stage A by design.

---

## Single-paper flow (no batch)

For one PDF, the loop runs against `gemini_analyze_pdf.py` directly:

```text
1. python scanner/gemini_analyze_pdf.py paper.pdf --backend subagent
   → exit 200, manifest-profiler.json written
   → host dispatches sub-agent → 01-document-profile.json filled

2. python scanner/gemini_analyze_pdf.py paper.pdf --backend subagent --resume <run_dir>
   → exit 200, manifest-note_generator.json written
   → host dispatches sub-agent → 02-note-draft.json filled

3. python scanner/gemini_analyze_pdf.py paper.pdf --backend subagent --resume <run_dir>
   → exit 0, final note rendered, ledger updated
```

For batches, use `zotero_batch_scanner.py` — it handles `--resume` automatically
and emits the same exit-code contract.
