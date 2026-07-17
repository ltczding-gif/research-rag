"""
Pluggable processor backends.

Each backend implements `ProcessorBackend` and handles model invocation +
PDF transport for one provider. The orchestration logic
(`gemini_analyze_pdf.py`) is backend-agnostic: it preflights PDFs, picks
a model tier from `model_routing_policy.json`, and then asks the backend
to run the two structured-output calls (profiler + note generator).

Available backends (chosen by `LOCALRAG_PROCESSOR_BACKEND` or `--backend`):

    vertex      Vertex AI Gemini (PDFs uploaded to GCS).
                Default. Production path.
    gemini-api  Direct Google AI Studio API key (PDFs inline as bytes).
                Cheaper to set up; no GCP project needed.
    anthropic   Anthropic Claude API (PDFs inline as base64 content blocks).
    openai      OpenAI Chat Completions protocol (PDFs as locally-extracted
                text via pdfplumber). Works with OpenAI Inc. and any compatible
                provider via OPENAI_BASE_URL: DeepSeek, Mistral, OpenRouter,
                Together, Groq, Qwen, vLLM, Ollama, LM Studio, etc.
    subagent    No external API — writes a manifest for a Claude Code
                sub-agent to process. The user's Claude Code session runs
                the actual model calls via the Task / Agent tool.

Two factory entry points:

    make_backend(name, **kwargs)
        Low-level: takes already-resolved kwargs (api_key, project_id, ...).
        Useful for tests and programmatic use.

    make_backend_from_env(name, **overrides)
        High-level: resolves env vars per-backend, applies any overrides,
        and constructs the backend. This is what the scanner CLI uses.

Adding a new backend: implement ProcessorBackend in a new module under
this package, then register both `make_backend()` (constructor) and
`make_backend_from_env()` (env-var resolution) below. Document required
env vars in your backend's module docstring and in `.env.example`.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from .base import ProcessorBackend, SubagentManifestPending


# Canonical list of backend names. argparse `choices=` should match this.
BACKEND_NAMES = ("vertex", "gemini-api", "anthropic", "openai", "subagent")


def _normalize_name(name: str | None) -> str:
    return (name or "vertex").lower().replace("_", "-")


def make_backend(name: str, **kwargs) -> ProcessorBackend:
    """Construct a backend by name. Late imports keep optional deps optional."""
    name = _normalize_name(name)
    if name == "vertex":
        from .vertex import VertexBackend
        return VertexBackend(**kwargs)
    if name in ("gemini-api", "gemini"):
        from .gemini_api import GeminiAPIBackend
        return GeminiAPIBackend(**kwargs)
    if name == "anthropic":
        from .anthropic_api import AnthropicBackend
        return AnthropicBackend(**kwargs)
    if name == "openai":
        from .openai_api import OpenAIBackend
        return OpenAIBackend(**kwargs)
    if name == "subagent":
        from .subagent import SubagentBackend
        return SubagentBackend(**kwargs)
    raise ValueError(
        f"Unknown processor backend: {name!r}. "
        f"Choose one of: {', '.join(BACKEND_NAMES)}."
    )


def make_backend_from_env(name: str, **overrides: Any) -> ProcessorBackend:
    """Construct a backend, reading per-backend env vars at call time.

    Env-var resolution is centralized here so every entry point (CLI,
    notebook, test) shares the same contract. Pass `overrides` to inject
    constructor kwargs (e.g. `run_dir_provider` for the subagent backend,
    or to override an env-var-resolved value for a specific call).

    Errors on missing required env vars: prints a friendly message to
    stderr and calls `sys.exit(1)` so the orchestrator gets a clean exit
    rather than a stack trace.
    """
    name = _normalize_name(name)

    def _need(env_var: str, backend_label: str) -> str:
        value = os.environ.get(env_var, "").strip()
        if not value:
            print(
                f"❌ {backend_label} backend needs {env_var} in the environment.",
                file=sys.stderr,
            )
            sys.exit(1)
        return value

    if name == "vertex":
        # Lazy: importing project-id resolution helpers from the scanner
        # entry point so we don't duplicate the GOOGLE_APPLICATION_CREDENTIALS
        # JSON-parsing logic. The scanner is expected to set GOOGLE_CLOUD_PROJECT
        # itself (or have the credentials JSON expose it).
        project_id = (
            os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
            or _resolve_project_id_from_credentials()
        )
        if not project_id:
            print(
                "❌ Vertex backend requires GOOGLE_CLOUD_PROJECT (or a service-account "
                "JSON at GOOGLE_APPLICATION_CREDENTIALS that exposes a project_id).",
                file=sys.stderr,
            )
            sys.exit(1)
        location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global").strip() or "global"
        bucket_name = (
            (overrides.pop("bucket_name", None) or "").strip()
            or os.environ.get("GEMINI_VERTEX_GCS_BUCKET", "").strip()
            or f"{project_id}-gemini-literature-temp"
        )
        bucket_location = os.environ.get("GEMINI_VERTEX_GCS_BUCKET_LOCATION", "US")
        upload_timeout_seconds = int(os.environ.get("GEMINI_GCS_UPLOAD_TIMEOUT_SECONDS", "900"))
        kwargs = {
            "project_id": project_id,
            "location": location,
            "bucket_name": bucket_name,
            "bucket_location": bucket_location,
            "upload_timeout_seconds": upload_timeout_seconds,
            **overrides,
        }
        return make_backend("vertex", **kwargs)

    if name in ("gemini-api", "gemini"):
        api_key = _need("GEMINI_API_KEY", "gemini-api")
        return make_backend("gemini-api", api_key=api_key, **overrides)

    if name == "anthropic":
        api_key = _need("ANTHROPIC_API_KEY", "anthropic")
        return make_backend("anthropic", api_key=api_key, **overrides)

    if name == "openai":
        api_key = _need("OPENAI_API_KEY", "openai")
        base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or None
        organization = os.environ.get("OPENAI_ORG_ID", "").strip() or None
        return make_backend(
            "openai",
            api_key=api_key,
            base_url=base_url,
            organization=organization,
            **overrides,
        )

    if name == "subagent":
        # subagent has no env vars; just forward overrides
        # (run_dir_provider, resume_dir) from the orchestrator.
        return make_backend("subagent", **overrides)

    raise ValueError(
        f"Unknown processor backend: {name!r}. "
        f"Choose one of: {', '.join(BACKEND_NAMES)}."
    )


def _resolve_project_id_from_credentials() -> str:
    """Best-effort read of `project_id` from a service-account JSON.

    Scanner CLI also has its own `resolve_project_id()` that does the same
    thing; this is a duplicate so backends/__init__.py can be self-sufficient
    without circular imports.
    """
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not creds_path:
        return ""
    try:
        import json
        from pathlib import Path
        data = json.loads(Path(creds_path).read_text(encoding="utf-8"))
        return str(data.get("project_id", "") or "")
    except Exception:
        return ""


__all__ = [
    "ProcessorBackend",
    "SubagentManifestPending",
    "BACKEND_NAMES",
    "make_backend",
    "make_backend_from_env",
]
