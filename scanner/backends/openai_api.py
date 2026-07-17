"""
OpenAI-compatible Chat Completions backend.

Works with:
  - OpenAI Inc. (GPT-4o, GPT-4-turbo, ...)
  - Any provider exposing the OpenAI protocol via a custom base_url:
    DeepSeek, Mistral, OpenRouter, Together, Groq, Qwen, vLLM, Ollama,
    LM Studio, etc.

PDF handling: this backend cannot send native PDF input the way Vertex
or Anthropic can. Instead, it extracts page text locally with pdfplumber
and includes that text in the user message. This is universally portable
across OpenAI-compatible providers but **loses figures, tables, and any
visual content** that the model would otherwise see. The notes will still
follow the structured schema, but figure-by-figure analysis sections may
be thinner than with Vertex / Anthropic.

Structured output: enforced via OpenAI tool calling. The JSON schema goes
into a tool's `parameters`, and `tool_choice` forces the call. Most OpenAI-
compatible providers support this. A text-mode JSON fallback is included
for providers that ignore `tool_choice`.

Required env (only when LOCALRAG_PROCESSOR_BACKEND=openai):
  OPENAI_API_KEY
  OPENAI_BASE_URL    optional; defaults to https://api.openai.com/v1
  OPENAI_FLASH_MODEL optional; default "gpt-4o-mini"
  OPENAI_PRO_MODEL   optional; default "gpt-4o"
  OPENAI_ORG_ID      optional
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .base import ProcessorBackend
from ._schema_compat import to_json_schema


_TOOL_NAME = "submit_structured_output"


class OpenAIBackend(ProcessorBackend):
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        organization: str | None = None,
        max_tokens: int | None = None,
        max_chars_per_pdf: int | None = 200_000,
    ):
        if not api_key:
            raise RuntimeError(
                "OpenAIBackend requires OPENAI_API_KEY. "
                "Set it in your environment or in .env."
            )
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError(
                "OpenAIBackend requires `openai>=1.40`. "
                'Install with: pip install "openai>=1.40"'
            ) from e
        try:
            import pdfplumber  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "OpenAIBackend extracts PDF text with `pdfplumber>=0.10`. "
                'Install with: pip install "pdfplumber>=0.10"'
            ) from e

        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url or None,
            organization=organization or None,
        )
        # max_tokens: explicit param > $OPENAI_MAX_TOKENS > 8192 default.
        # Some compatible providers (Groq, Together free tier) cap below 8192.
        if max_tokens is None:
            max_tokens = int(os.environ.get("OPENAI_MAX_TOKENS", "8192"))
        self.max_tokens = max_tokens
        # The official OpenAI API deprecated `max_tokens` in favor of
        # `max_completion_tokens` (reasoning models reject the old name).
        # Compatible providers (DeepSeek, Groq, vLLM, ...) mostly still
        # expect `max_tokens`, so branch on the endpoint.
        self._use_max_completion_tokens = not base_url or "api.openai.com" in base_url
        self.max_chars_per_pdf = max_chars_per_pdf
        self._pdf_text_blocks: list[dict] = []
        self._profiler_pdf_text_blocks: list[dict] | None = None

    # ------------------------------------------------------------------
    # PDF transport: local pdfplumber extraction
    # ------------------------------------------------------------------

    def _extract_text_blocks(self, pdf_paths, *, label):
        """pdfplumber-extract text from each PDF; return text blocks.

        `label` ("full" | "profiler") only feeds the truncation warning
        prefix so an operator can tell which attachment set raised it.
        """
        import pdfplumber
        import sys as _sys

        blocks = []
        for path in pdf_paths:
            with pdfplumber.open(path) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            text = text.strip()
            original_len = len(text)
            truncated = False
            if self.max_chars_per_pdf and original_len > self.max_chars_per_pdf:
                text = text[: self.max_chars_per_pdf]
                truncated = True
                print(
                    f"[OpenAI backend] WARNING ({label}): {Path(path).name} truncated "
                    f"from {original_len:,} to {self.max_chars_per_pdf:,} chars. "
                    "Set max_chars_per_pdf=None or raise the limit if the cut "
                    "loses essential content.",
                    file=_sys.stderr,
                )
            blocks.append({
                "filename": Path(path).name,
                "text": text,
                "truncated": truncated,
            })
        return blocks

    def attach_pdfs(self, pdf_paths, *, combined_hash="", profiler_pdf_paths=None):
        super().attach_pdfs(
            pdf_paths,
            combined_hash=combined_hash,
            profiler_pdf_paths=profiler_pdf_paths,
        )
        self._pdf_text_blocks = self._extract_text_blocks(pdf_paths, label="full")
        if profiler_pdf_paths is not None:
            # The profiler PDFs are already pre-sliced to first-N-pages by
            # the orchestrator. pdfplumber here just reads what's in those
            # short files — usually well under max_chars_per_pdf.
            self._profiler_pdf_text_blocks = self._extract_text_blocks(
                profiler_pdf_paths, label="profiler"
            )
        else:
            self._profiler_pdf_text_blocks = None

    # ------------------------------------------------------------------
    # Model invocation: tool-calling for schema enforcement
    # ------------------------------------------------------------------

    @staticmethod
    def _translate_model_id(model_id: str) -> str:
        """Map orchestrator tier names to OpenAI model ids; pass through everything else.

        The orchestrator's auto-routing emits Gemini-family model names like
        ``gemini-2.5-flash`` or ``gemini-2.5-pro``. When the OpenAI backend
        receives one, we map it to the user-configured ``OPENAI_FLASH_MODEL``
        or ``OPENAI_PRO_MODEL``. Anything that does **not** look like a
        Gemini tier name (e.g. ``gpt-4o-2024-08-06``, ``deepseek-chat``,
        ``deepseek-coder-pro``, ``qwen2.5:32b``) is treated as an explicit
        user model id and passed through verbatim.

        Env vars are read at call time so values loaded from ``.env`` after
        module import still take effect.
        """
        flash = os.environ.get("OPENAI_FLASH_MODEL", "gpt-4o-mini")
        pro = os.environ.get("OPENAI_PRO_MODEL", "gpt-4o")
        if not model_id:
            return flash
        m = model_id.lower()
        # Gemini-family tier names from the orchestrator's auto-routing.
        if "gemini" in m and "pro" in m:
            return pro
        if "gemini" in m and "flash" in m:
            return flash
        # Bare tier hints (some callers may pass just "pro" / "flash" / etc.)
        if m == "pro":
            return pro
        if m in ("flash", "mini", "haiku"):
            return flash
        # Anything else: explicit user-supplied id — pass through unchanged.
        return model_id

    def _build_messages(self, system_prompt: str, user_prompt: str, blocks) -> list[dict]:
        if blocks:
            pdf_section = "\n\n".join(
                f"=== PDF {i+1}: {b['filename']}"
                + (" (truncated)" if b["truncated"] else "")
                + f" ===\n{b['text']}"
                for i, b in enumerate(blocks)
            )
            user_content = (
                "The following is the text extracted from one or more PDF documents. "
                "Figures, tables, and images are not included — only the text the PDF "
                "library could extract.\n\n"
                f"{pdf_section}\n\n"
                "----- task -----\n"
                f"{user_prompt}"
            )
        else:
            user_content = user_prompt
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

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
        translated_model = self._translate_model_id(model_id)
        # Stage A reads the truncated text blocks when attached.
        if stage == "profiler" and self._profiler_pdf_text_blocks is not None:
            active_blocks = self._profiler_pdf_text_blocks
        else:
            active_blocks = self._pdf_text_blocks
        messages = self._build_messages(system_prompt, user_prompt, active_blocks)
        tool = {
            "type": "function",
            "function": {
                "name": _TOOL_NAME,
                "description": (
                    "Submit the structured output for this stage. "
                    "The arguments must conform to the provided JSON schema."
                ),
                # Packs author schemas in the Vertex dialect; OpenAI tools
                # expect standard JSON Schema.
                "parameters": to_json_schema(schema),
            },
        }

        token_kwargs = (
            {"max_completion_tokens": self.max_tokens}
            if self._use_max_completion_tokens
            else {"max_tokens": self.max_tokens}
        )
        response = self.client.chat.completions.create(
            model=translated_model,
            messages=messages,
            temperature=temperature,
            tools=[tool],
            tool_choice={"type": "function", "function": {"name": _TOOL_NAME}},
            **token_kwargs,
        )

        if not response.choices:
            raise RuntimeError(
                f"OpenAIBackend stage={stage}: model {translated_model} returned "
                "an empty choices list. Likely causes: provider content filter, "
                "memory pressure on a local quantized model, or upstream rate "
                "limiting. Check the provider's response status."
            )
        if getattr(response.choices[0], "finish_reason", None) == "length":
            raise RuntimeError(
                f"OpenAIBackend stage={stage}: response truncated at "
                f"max_tokens={self.max_tokens} (finish_reason=length); the "
                "structured output is incomplete and unusable. Raise "
                "OPENAI_MAX_TOKENS and retry."
            )
        choice = response.choices[0].message
        for tc in (getattr(choice, "tool_calls", None) or []):
            fn = getattr(tc, "function", None)
            if fn and getattr(fn, "name", None) == _TOOL_NAME:
                arguments = getattr(fn, "arguments", "")
                try:
                    return json.loads(arguments)
                except (ValueError, TypeError) as e:
                    raise RuntimeError(
                        f"OpenAIBackend stage={stage}: model {translated_model} "
                        f"returned tool arguments that are not valid JSON: {e}"
                    ) from e

        # Fallback: some compatible providers ignore tool_choice and respond
        # with a JSON object in the message content.
        content = getattr(choice, "content", None)
        if content:
            try:
                return json.loads(content)
            except (ValueError, TypeError):
                pass

        raise RuntimeError(
            f"OpenAIBackend stage={stage}: model {translated_model} produced no "
            "usable structured output (no tool_call and no JSON in content). "
            "If you are using an OpenAI-compatible provider, verify it supports "
            "tool calling or JSON-mode responses."
        )
