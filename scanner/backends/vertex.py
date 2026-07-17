"""
Vertex AI Gemini backend.

Uploads PDFs to a GCS bucket and references them via gs:// URIs in the
model call. This is the production path the original system used.

Required env / config:
  GOOGLE_APPLICATION_CREDENTIALS (service account JSON path)
  GOOGLE_CLOUD_PROJECT
  GOOGLE_CLOUD_LOCATION              (default: "global")
  GEMINI_VERTEX_GCS_BUCKET           (default: "<project>-gemini-literature-temp")
  GEMINI_VERTEX_GCS_BUCKET_LOCATION  (default: "US")
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .base import ProcessorBackend


class VertexBackend(ProcessorBackend):
    name = "vertex"

    def __init__(
        self,
        *,
        project_id: str,
        location: str = "global",
        bucket_name: str | None = None,
        bucket_location: str = "US",
        upload_timeout_seconds: int = 900,
    ):
        try:
            from google import genai
            from google.cloud import storage
            from google.genai import types
        except ImportError as e:
            raise RuntimeError(
                "VertexBackend requires `google-genai` and `google-cloud-storage`. "
                "Install with: pip install google-genai google-cloud-storage"
            ) from e

        self._genai = genai
        self._storage = storage
        self._types = types

        self.project_id = project_id
        self.location = location
        self.bucket_location = bucket_location
        self.upload_timeout_seconds = upload_timeout_seconds
        self.bucket_name = bucket_name or f"{project_id}-gemini-literature-temp"

        self.client = genai.Client(
            vertexai=True,
            project=project_id,
            location=location,
            http_options=types.HttpOptions(api_version="v1"),
        )
        self.storage_client = storage.Client(project=project_id)
        self._bucket = None
        self._parts: list = []
        self._archived: list[dict] = []
        # Stage A profiler attachments — populated only when the
        # orchestrator passes profiler_pdf_paths to attach_pdfs.
        self._profiler_parts: list | None = None

    # ------------------------------------------------------------------
    # PDF transport
    # ------------------------------------------------------------------

    def _ensure_bucket(self):
        if self._bucket is not None:
            return self._bucket
        bucket = self.storage_client.lookup_bucket(self.bucket_name)
        if bucket is None:
            bucket = self.storage_client.bucket(self.bucket_name)
            bucket.storage_class = "STANDARD"
            bucket = self.storage_client.create_bucket(
                bucket, location=self.bucket_location
            )
        self._bucket = bucket
        return bucket

    def _upload_and_build_parts(self, pdf_paths, bucket, combined_hash, *, prefix=""):
        """Upload `pdf_paths` to GCS and return (parts, archive entries)."""
        parts = []
        archived = []
        for idx, pdf_path in enumerate(pdf_paths):
            safe_name = "".join(
                c if c.isascii() and c not in r'\/:*?"<>|' else "_"
                for c in os.path.basename(pdf_path)
            )
            object_name = f"pdf-inputs/{combined_hash}/{prefix}{idx:02d}_{safe_name}"
            blob = bucket.blob(object_name)
            blob.upload_from_filename(
                str(pdf_path),
                content_type="application/pdf",
                timeout=self.upload_timeout_seconds,
            )
            file_uri = f"gs://{bucket.name}/{object_name}"
            archived.append({
                "index": idx,
                "object_name": object_name,
                "gcs_uri": file_uri,
                "original_name": os.path.basename(pdf_path),
                "local_path": os.path.abspath(str(pdf_path)),
            })
            parts.append(
                self._types.Part.from_uri(file_uri=file_uri, mime_type="application/pdf")
            )
        return parts, archived

    def attach_pdfs(self, pdf_paths, *, combined_hash="", profiler_pdf_paths=None):
        super().attach_pdfs(
            pdf_paths,
            combined_hash=combined_hash,
            profiler_pdf_paths=profiler_pdf_paths,
        )
        bucket = self._ensure_bucket()
        self._parts, self._archived = self._upload_and_build_parts(
            pdf_paths, bucket, combined_hash, prefix=""
        )
        if profiler_pdf_paths is not None:
            # Profiler PDFs land under a `profiler/` prefix so the full and
            # truncated copies don't collide. The slice is small (typically
            # 3 pages, ~100 KB) so the extra upload is cheap; the savings
            # come at model-process time, not transport time.
            self._profiler_parts, _ = self._upload_and_build_parts(
                profiler_pdf_paths, bucket, combined_hash, prefix="profiler/"
            )
        else:
            self._profiler_parts = None

    @property
    def archived_files(self):
        """List of dicts describing each uploaded GCS object. Used for run manifests."""
        return list(self._archived)

    @property
    def uploaded_uris(self):
        return [a["gcs_uri"] for a in self._archived]

    @property
    def gcs_bucket(self):
        """The live GCS bucket object. Available after `attach_pdfs()` has run."""
        return self._bucket

    # ------------------------------------------------------------------
    # Model invocation
    # ------------------------------------------------------------------

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
        # Stage A uses the truncated profiler PDFs when available so the
        # model only sees the first few pages of the primary document.
        # Stage B always sees the full set.
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
                f"VertexBackend stage={stage}: model returned an empty or "
                "blocked response (no text). Check safety filters / quota, "
                "then retry."
            )
        return json.loads(text)
