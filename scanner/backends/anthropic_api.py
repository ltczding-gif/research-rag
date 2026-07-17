"""
Anthropic Claude API backend.

PDFs go inline as base64 content blocks. Structured output is enforced
via tool-use: the JSON schema becomes a single tool's `input_schema`,
and `tool_choice` forces Claude to call it. The tool input *is* the
parsed JSON output we want.

Required env:
  ANTHROPIC_API_KEY

Default model can be overridden via `ANTHROPIC_FLASH_MODEL` /
`ANTHROPIC_PRO_MODEL` env vars; falls back to a sensible Claude family.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from .base import ProcessorBackend
from ._schema_compat import to_json_schema


_DEFAULT_FLASH = os.environ.get("ANTHROPIC_FLASH_MODEL", "claude-haiku-4-5-20251001")
_DEFAULT_PRO = os.environ.get("ANTHROPIC_PRO_MODEL", "claude-sonnet-4-6")
_TOOL_NAME = "submit_structured_output"


class AnthropicBackend(ProcessorBackend):
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        max_tokens: int = 8192,
    ):
        if not api_key:
            raise RuntimeError(
                "AnthropicBackend requires ANTHROPIC_API_KEY. "
                "Set it in your environment or in .env."
            )
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError(
                "AnthropicBackend requires `anthropic`. "
                "Install with: pip install anthropic"
            ) from e

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.max_tokens = max_tokens
        self._pdf_blocks: list[dict] = []
        self._profiler_pdf_blocks: list[dict] | None = None

    @staticmethod
    def _build_pdf_blocks(pdf_paths):
        blocks = []
        for pdf_path in pdf_paths:
            data = Path(pdf_path).read_bytes()
            encoded = base64.standard_b64encode(data).decode("ascii")
            blocks.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": encoded,
                },
            })
        return blocks

    def attach_pdfs(self, pdf_paths, *, combined_hash="", profiler_pdf_paths=None):
        super().attach_pdfs(
            pdf_paths,
            combined_hash=combined_hash,
            profiler_pdf_paths=profiler_pdf_paths,
        )
        self._pdf_blocks = self._build_pdf_blocks(pdf_paths)
        self._profiler_pdf_blocks = (
            self._build_pdf_blocks(profiler_pdf_paths)
            if profiler_pdf_paths is not None
            else None
        )

    @staticmethod
    def _translate_model_id(model_id: str) -> str:
        """Map Gemini model IDs to Claude equivalents.

        The orchestration layer thinks in Gemini terms (flash vs pro). We
        translate at the boundary so the rest of the pipeline doesn't need
        to know about Anthropic. A user can override the mapping with
        ANTHROPIC_FLASH_MODEL / ANTHROPIC_PRO_MODEL.
        """
        m = (model_id or "").lower()
        # An explicit Claude model id is honored verbatim — checked FIRST so
        # ids like "claude-haiku-4-5" aren't hijacked by the tier keywords.
        if m.startswith("claude"):
            return model_id
        if "pro" in m:
            return _DEFAULT_PRO
        if "flash" in m or "haiku" in m:
            return _DEFAULT_FLASH
        return model_id

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
        claude_model = self._translate_model_id(model_id)
        tool = {
            "name": _TOOL_NAME,
            "description": (
                "Return the structured output for this stage. "
                "The input arguments must conform to the provided schema."
            ),
            # Packs author schemas in the Vertex dialect; Anthropic expects
            # standard JSON Schema.
            "input_schema": to_json_schema(schema),
        }
        # Stage A → truncated profiler PDFs when attached; Stage B → full set.
        if stage == "profiler" and self._profiler_pdf_blocks is not None:
            active_blocks = self._profiler_pdf_blocks
        else:
            active_blocks = self._pdf_blocks
        content_blocks = list(active_blocks) + [
            {"type": "text", "text": user_prompt}
        ]
        response = self.client.messages.create(
            model=claude_model,
            max_tokens=self.max_tokens,
            temperature=temperature,
            system=system_prompt,
            tools=[tool],
            tool_choice={"type": "tool", "name": _TOOL_NAME},
            messages=[{"role": "user", "content": content_blocks}],
        )

        if getattr(response, "stop_reason", None) == "max_tokens":
            raise RuntimeError(
                f"AnthropicBackend stage={stage}: response truncated at "
                f"max_tokens={self.max_tokens}; the structured output is "
                "incomplete and unusable. Raise the backend max_tokens and retry."
            )

        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == _TOOL_NAME:
                # block.input is already a parsed dict
                return dict(block.input)

        # Fallback: try to extract JSON from any text block.
        for block in response.content:
            if getattr(block, "type", None) == "text":
                try:
                    return json.loads(block.text)
                except (ValueError, AttributeError):
                    continue

        raise RuntimeError(
            f"AnthropicBackend stage={stage}: model returned no usable structured output."
        )
