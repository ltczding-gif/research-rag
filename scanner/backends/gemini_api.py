"""
Direct Google AI Studio (Gemini API) backend.

Uses an API key instead of GCP service-account auth. PDFs go inline as
bytes — no GCS bucket needed. Cheaper to set up than Vertex; gives up
some Vertex features (e.g. private project residency, audit logs).

Required env:
  GEMINI_API_KEY
"""

from __future__ import annotations

import json
from pathlib import Path

from .base import ProcessorBackend


class GeminiAPIBackend(ProcessorBackend):
    name = "gemini-api"

    def __init__(self, *, api_key: str):
        if not api_key:
            raise RuntimeError(
                "GeminiAPIBackend requires GEMINI_API_KEY. "
                "Set it in your environment or in .env."
            )
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:
            raise RuntimeError(
                "GeminiAPIBackend requires `google-genai`. "
                "Install with: pip install google-genai"
            ) from e

        self._genai = genai
        self._types = types
        self.client = genai.Client(api_key=api_key)
        self._parts: list = []
        self._profiler_parts: list | None = None

    def _build_parts(self, pdf_paths):
        return [
            self._types.Part.from_bytes(
                data=Path(p).read_bytes(), mime_type="application/pdf"
            )
            for p in pdf_paths
        ]

    def attach_pdfs(self, pdf_paths, *, combined_hash="", profiler_pdf_paths=None):
        super().attach_pdfs(
            pdf_paths,
            combined_hash=combined_hash,
            profiler_pdf_paths=profiler_pdf_paths,
        )
        self._parts = self._build_parts(pdf_paths)
        self._profiler_parts = (
            self._build_parts(profiler_pdf_paths)
            if profiler_pdf_paths is not None
            else None
        )

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
        # Stage A uses the truncated PDFs when attached; Stage B always full.
        if stage == "profiler" and self._profiler_parts is not None:
            active_parts = self._profiler_parts
        else:
            active_parts = self._parts
        contents = list(active_parts) + [user_prompt]
        response = self.client.models.generate_content(
            model=model_id,
            contents=contents,
            config=self._types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=schema,
                temperature=temperature,
            ),
        )
        if hasattr(response, "parsed") and response.parsed:
            return response.parsed
        text = getattr(response, "text", None)
        if not text:
            # Blocked/empty responses surface as text=None; json.loads(None)
            # would raise an opaque TypeError.
            raise RuntimeError(
                f"GeminiAPIBackend stage={stage}: model returned an empty or "
                "blocked response (no text). Check safety filters / quota, "
                "then retry."
            )
        return json.loads(text)
