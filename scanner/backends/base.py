"""
Abstract processor backend.

A backend encapsulates two responsibilities:
  1. PDF transport for a particular LLM provider (GCS upload, inline bytes,
     base64 content blocks, or just a file reference).
  2. Structured-output model invocation: given a system prompt, user prompt,
     and JSON schema, return a parsed dict that conforms.

The orchestration in `gemini_analyze_pdf.py` is provider-agnostic; it asks
the backend for model calls and trusts the backend to handle PDF transport
and JSON parsing internally.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class ProcessorBackend(ABC):
    """Abstract base for all processor backends."""

    #: Short backend name (e.g. "vertex"), used in logs.
    name: str = "base"

    def attach_pdfs(
        self,
        pdf_paths: list[Path],
        *,
        combined_hash: str = "",
        profiler_pdf_paths: list[Path] | None = None,
    ) -> None:
        """Prepare PDFs for the upcoming model calls.

        Called once per paper before any `call_model` invocation. Each
        backend handles its own transport: GCS upload, byte read + inline
        Part, base64 encoding, or just storing the paths for a manifest.

        Args:
            pdf_paths: full PDF set used by Stage B (note generator).
            combined_hash: identifier for run-dir / GCS prefix.
            profiler_pdf_paths: optional, smaller PDF set used by Stage A
                (document profiler). When None, Stage A reuses
                ``pdf_paths`` (backwards-compatible). When provided, the
                backend prepares both attachment sets and ``call_model``
                dispatches by ``stage``.

        Subclasses overriding this method must call ``super().attach_pdfs``
        first so the base attributes are populated.
        """
        self._pdf_paths = list(pdf_paths)
        self._combined_hash = combined_hash
        self._profiler_pdf_paths = (
            list(profiler_pdf_paths) if profiler_pdf_paths is not None else None
        )

    @abstractmethod
    def call_model(
        self,
        *,
        stage: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict,
        model_id: str,
        temperature: float = 0.0,
    ) -> dict:
        """Run one structured-output call. Returns the parsed JSON dict.

        Args:
            stage: "profiler" or "note_generator". Backends may use this to
                differentiate retry behaviour or logging; most don't care.
            system_prompt: Provider-neutral system instructions string.
            user_prompt: Provider-neutral user prompt string. The backend
                must combine this with its prepared PDF references.
            schema: JSON Schema describing the expected output structure.
                For providers with native structured output (Gemini), this
                is passed directly. For others (Anthropic), it goes into a
                tool definition or is enforced via post-parse validation.
            model_id: Provider-specific model identifier
                (e.g. "gemini-2.5-flash", "claude-opus-4-7").
            temperature: Sampling temperature. Default 0 for determinism.

        Returns:
            A dict matching `schema`. Backends are responsible for parsing
            JSON from text responses if the provider doesn't deliver
            pre-parsed objects.
        """

    def cleanup(self) -> None:
        """Optional per-paper cleanup. Default is a no-op."""


class SubagentManifestPending(Exception):
    """Sentinel raised by ``SubagentBackend`` from ``call_model``.

    The orchestration loop catches this and emits instructions to the
    user's Claude Code session. No external model call has happened — the
    sub-agent is expected to fulfill the manifest and write results back
    to the path declared inside it.

    Attributes:
        manifest_path: where the manifest JSON was written.
        run_dir: parent directory containing manifest + expected output paths.
    """

    def __init__(self, manifest_path: Path, run_dir: Path):
        self.manifest_path = Path(manifest_path)
        self.run_dir = Path(run_dir)
        super().__init__(
            f"Sub-agent manifest written: {self.manifest_path}. "
            f"The Python pipeline cannot continue until the sub-agent "
            f"writes outputs into {self.run_dir}/."
        )
