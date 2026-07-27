"""
Sub-agent manifest backend (no external API).

Instead of calling a remote LLM, this backend emits one manifest JSON
file per stage describing exactly what needs to be done. The user's
Claude Code session is expected to dispatch a Task sub-agent against
each manifest. The sub-agent:

  1. Reads the manifest at `manifest_path`.
  2. Reads the PDFs listed in it.
  3. Applies `system_prompt` + `user_prompt` to those PDFs.
  4. Produces a JSON object that strictly conforms to `response_schema`.
  5. Writes that JSON to `expected_output_path`.

Because Stage B (`note_generator`)'s prompt depends on Stage A's output
(`document_profile.recommended_template` chooses the template_rules
file), the orchestrator runs the two stages in separate invocations:

  Invocation 1: Stage A manifest written. Exits.
  Sub-agent fills runs/<hash>/01-document-profile.json.
  Invocation 2 (--resume <run_dir>): Stage A loaded from disk; Stage B
                manifest written using the loaded profile. Exits.
  Sub-agent fills runs/<hash>/02-note-draft.json.
  Invocation 3 (--resume <run_dir>): Both stages loaded from disk; the
                Markdown note is rendered, written, and the ledger is
                updated. Done.

In `--resume` mode, `call_model` reads the expected output JSON instead
of writing a manifest. If the JSON is missing, it writes the manifest
and raises (i.e. it always advances to the "next" pending stage).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import ProcessorBackend, SubagentManifestPending


_STAGE_OUTPUT_FILE = {
    "profiler": "01-document-profile.json",
    "note_generator": "02-note-draft.json",
}


def _is_output_filled(path: Path) -> bool:
    """Treat a non-empty, JSON-parseable file as filled.

    Mirrors `scanner/list_pending_subagent_runs.py:_is_output_filled`. The
    invariant is: if the helper considers a stage "still pending", the
    resume path must NOT pretend it's done. Otherwise a partial/garbage
    sub-agent write would crash the renderer with a misleading error
    instead of being re-dispatched.

    UnicodeDecodeError is treated as "not filled" too: a sub-agent that
    crashed mid-stream may leave a partial multi-byte sequence on disk.
    """
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if not text.strip():
        return False
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


class SubagentBackend(ProcessorBackend):
    name = "subagent"

    def __init__(
        self,
        *,
        run_dir_provider=None,
        resume_dir: Path | None = None,
        resume_cli_args: list[str] | tuple[str, ...] | None = None,
    ):
        """
        Args:
            run_dir_provider: callable returning the per-paper run directory
                (e.g. ``runs/<combined_hash>/``). The orchestrator passes
                this so manifests land alongside other run artifacts. Only
                used in non-resume mode.
            resume_dir: when set, `call_model` reads existing stage outputs
                from this directory instead of raising. The directory must
                be the same `run_dir` that prior invocations wrote into.
            resume_cli_args: non-secret scanner arguments that must survive
                into the manifest's ready-to-run resume command.
        """
        self._run_dir_provider = run_dir_provider
        self._resume_dir = Path(resume_dir) if resume_dir else None
        self._run_dir: Path | None = self._resume_dir
        self._resume_cli_args = tuple(str(arg) for arg in (resume_cli_args or ()))

    def attach_pdfs(self, pdf_paths, *, combined_hash="", profiler_pdf_paths=None):
        super().attach_pdfs(
            pdf_paths,
            combined_hash=combined_hash,
            profiler_pdf_paths=profiler_pdf_paths,
        )
        if self._resume_dir is not None:
            # In resume mode, the run_dir is fixed. Don't re-derive it.
            return
        if self._run_dir_provider is not None:
            self._run_dir = Path(self._run_dir_provider())
        else:
            base = Path.cwd() / "subagent_runs"
            self._run_dir = base / (combined_hash or "default")
        self._run_dir.mkdir(parents=True, exist_ok=True)

    def call_model(
        self,
        *,
        stage,
        system_prompt,
        user_prompt,
        schema,
        model_id,
        temperature=0.0,
    ):
        if self._run_dir is None:
            raise RuntimeError(
                "SubagentBackend.attach_pdfs() must be called before call_model()."
            )

        expected_output_filename = _STAGE_OUTPUT_FILE.get(stage, f"{stage}.json")
        manifest_path = self._run_dir / f"manifest-{stage}.json"
        expected_output_path = self._run_dir / expected_output_filename

        # --- Resume path: load existing output if it's there and valid. -----
        # If the file exists but is empty / partial / not valid JSON, fall
        # through to the manifest-emission branch instead of crashing. The
        # parent agent will see exit 200 again and re-dispatch the sub-agent.
        if self._resume_dir is not None and _is_output_filled(expected_output_path):
            return json.loads(expected_output_path.read_text(encoding="utf-8"))

        # --- Manifest path: write the stage manifest, then bail. -------------
        # Stage A (profiler) gets the truncated PDF set when one was attached;
        # Stage B always sees the full set. The manifest's `pdf_paths` field
        # is the hard contract: if a sub-agent reads only what's listed, it
        # cannot accidentally consume the full PDF during classification.
        if stage == "profiler" and self._profiler_pdf_paths is not None:
            pdf_paths_for_manifest = self._profiler_pdf_paths
        else:
            pdf_paths_for_manifest = self._pdf_paths
        # (quarantine_invalid_output below relies on the same
        # _STAGE_OUTPUT_FILE mapping used here.)

        # Two-actor protocol: the manifest is consumed by TWO different
        # agents in the host platform (Claude Code, Codex, OpenClaw, ...):
        #   • subagent_task   → executed by a freshly-dispatched sub-agent
        #     that has only the manifest as its input. Must NOT re-invoke
        #     the scanner, must NOT touch the ledger.
        #   • parent_agent_task → executed by the orchestrator (the agent
        #     that called the scanner). After the sub-agent writes the
        #     expected output, the parent re-invokes the scanner with
        #     `--resume <run_dir>` to advance to the next stage.
        # Keeping these separate prevents the common failure where a
        # confused sub-agent tries to drive the loop itself.
        # Build the resume command with REAL pdf paths interpolated, so a
        # literal-minded LLM agent can shell-execute it without first
        # filling in placeholders. Quote each path so paths with spaces
        # survive sh / cmd / pwsh.
        full_pdf_paths = [str(Path(p).resolve()) for p in self._pdf_paths]
        quoted_pdf_args = " ".join(f'"{p}"' for p in full_pdf_paths)
        # Absolute interpreter + script paths: the parent agent's cwd is NOT
        # guaranteed to be the repo root, and `python` may not resolve to the
        # interpreter (or venv) the scanner is running under.
        analyzer_script = Path(__file__).resolve().parent.parent / "gemini_analyze_pdf.py"
        quoted_resume_args = " ".join(
            arg
            if arg.startswith("--")
            else f'"{arg}"'
            for arg in self._resume_cli_args
        )
        resume_cmd = (
            f'"{sys.executable}" "{analyzer_script}" {quoted_pdf_args} '
            f'--backend subagent --resume "{self._run_dir}"'
        )
        if quoted_resume_args:
            resume_cmd = f"{resume_cmd} {quoted_resume_args}"
        if stage == "profiler":
            parent_followup = (
                "After 01-document-profile.json exists in run_dir, run: "
                f"`{resume_cmd}` to emit the Stage B (note_generator) manifest."
            )
        elif stage == "note_generator":
            parent_followup = (
                "After 02-note-draft.json exists in run_dir, run: "
                f"`{resume_cmd}` to render the final Markdown note and update "
                "the ledger."
            )
        else:
            parent_followup = (
                f"After {expected_output_filename} exists in run_dir, run: "
                f"`{resume_cmd}` to advance to the next stage."
            )

        manifest = {
            "schema_version": 3,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "model_hint": model_id,
            "temperature": temperature,
            "combined_hash": self._combined_hash,
            "pdf_paths": [str(Path(p).resolve()) for p in pdf_paths_for_manifest],
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response_schema": schema,
            "expected_output_path": str(expected_output_path),
            "run_dir": str(self._run_dir),
            "subagent_task": {
                "role": "Fresh sub-agent. Has only this manifest as input.",
                "steps": [
                    "Read every PDF listed in pdf_paths.",
                    "Apply system_prompt + user_prompt to those PDFs.",
                    "Produce a single JSON object that strictly conforms to response_schema.",
                    "Write that JSON to expected_output_path atomically (write to a sibling .tmp file, then rename). This prevents the parent from reading a half-written file on its next polling pass.",
                    "No other files, no logs, no scanner re-invocation.",
                ],
                "must_not": [
                    "Re-invoke the scanner.",
                    "Touch the ledger or any file outside expected_output_path.",
                    "Read PDFs beyond pdf_paths (the parent already truncated for Stage A).",
                ],
            },
            "parent_agent_task": {
                "role": "The orchestrator that called the scanner. Picks up here.",
                "steps": [
                    "Wait for the sub-agent to finish (i.e. expected_output_path becomes non-empty valid JSON).",
                    parent_followup,
                ],
                # Pre-interpolated, ready-to-execute shell line. Use this
                # verbatim from the host's shell-exec tool — no placeholder
                # substitution needed.
                "resume_command": resume_cmd,
            },
            # Legacy fields kept for older sub-agent prompts that reference
            # them directly; remove in schema_version 4.
            "instructions": (
                "Read each PDF in pdf_paths. Apply system_prompt + user_prompt. "
                "Produce a JSON object that strictly conforms to response_schema. "
                f"Write the result to expected_output_path."
            ),
            "next_step": parent_followup,
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        raise SubagentManifestPending(manifest_path=manifest_path, run_dir=self._run_dir)

    def quarantine_invalid_output(self, *, stage) -> Path | None:
        """Move a schema-invalid stage output aside so the stage re-dispatches.

        Called by the orchestrator when a sub-agent wrote syntactically valid
        JSON that fails schema validation. Without this, resume mode reloads
        the same bad payload forever (crash-loop) while
        list_pending_subagent_runs.py reports zero pending work. Renaming
        (rather than deleting) keeps the bad payload on disk for debugging
        while making the "is it filled?" predicate report pending again.

        Returns the quarantined path, or None if there was nothing to move.
        """
        if self._run_dir is None:
            return None
        expected = self._run_dir / _STAGE_OUTPUT_FILE.get(stage, f"{stage}.json")
        if not expected.exists():
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        quarantined = expected.with_name(f"{expected.name}.invalid-{stamp}")
        expected.replace(quarantined)
        return quarantined
