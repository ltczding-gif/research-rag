"""
Shared configuration for the scanner layer (Zotero → Gemini note generator).

All paths and tunables read from environment variables with cross-platform
defaults. Override via .env or shell environment. See .env.example.
"""

from __future__ import annotations

import os
from pathlib import Path


# --- Repo layout ---

SCANNER_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCANNER_DIR.parent


def _hydrate_env_from_dotenv(path: Path) -> None:
    """Load $REPO_ROOT/.env if present. Existing env vars take precedence.

    The scanner is invoked across many hosts (Claude Code, Codex,
    OpenClaw, plain shell) and we cannot count on the user having
    sourced .env first. Without this, the .env-advertised default
    `LOCALRAG_PROCESSOR_BACKEND=subagent` silently doesn't apply and
    --backend falls back to "vertex" — which is the exact opposite of
    what the README promises ("a fresh clone runs with no credentials").

    We use a minimal parser instead of pulling python-dotenv to keep
    the dependency footprint tight. Format we support:
      KEY=value          # inline comment
      KEY="quoted value"
      KEY='single quotes'
    Lines starting with # are skipped. Anything more exotic is left to
    the user's shell or a real dotenv tool.
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            # Don't overwrite an explicit shell export.
            continue
        value = value.strip()
        # Strip surrounding quotes (single or double) and trailing inline
        # comment (only when the value isn't quoted).
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        else:
            comment_idx = value.find(" #")
            if comment_idx != -1:
                value = value[:comment_idx].rstrip()
        os.environ[key] = value


_hydrate_env_from_dotenv(REPO_ROOT / ".env")


def _env_path(key: str, default: Path) -> Path:
    raw = os.environ.get(key)
    if not raw:
        return default
    return Path(os.path.expandvars(os.path.expanduser(raw)))


# --- Roots ---

VAULT_ROOT: Path = _env_path("LOCALRAG_NOTES_DIR", Path.home() / "research-note")
LOCALRAG_HOME: Path = _env_path("LOCALRAG_HOME", Path.home() / ".localrag")


# --- Zotero ---

ZOTERO_DB_PATH: Path = _env_path("ZOTERO_DB_PATH", Path.home() / "Zotero" / "zotero.sqlite")
ZOTERO_DATA_DIR: Path = _env_path("ZOTERO_DATA_DIR", Path.home() / "Zotero")
# Empty default: scanner auto-detects from Zotero prefs.js when blank.
ZOTERO_ATTACHMENT_BASE_DIR: str = os.environ.get("ZOTERO_ATTACHMENT_BASE_DIR", "")


# --- Python interpreters used by post-publish actions ---
# Defaults rely on PATH lookup for cross-platform behavior; override
# with absolute paths via env vars when the host has multiple Pythons.
LOCALRAG_MAIN_PYTHON: str = os.environ.get("LOCALRAG_MAIN_PYTHON", "python3")
LOCALRAG_RAG_PYTHON: str = os.environ.get("LOCALRAG_RAG_PYTHON", "python3")


# --- Skill / pipeline roots ---

CANONICAL_SKILL_ROOT: Path = _env_path(
    "GEMINI_LITERATURE_SKILL_ROOT",
    REPO_ROOT / "skills" / "gemini-literature-processor",
)
PIPELINE_REPORT_ROOT: Path = _env_path(
    "GEMINI_INCREMENTAL_ALIGNMENT_REPORT_ROOT",
    VAULT_ROOT / "progress" / "pipeline_reports" / "gemini_incremental_alignment",
)
PROCESSED_HISTORY_PATH: Path = _env_path(
    "GEMINI_PROCESSED_HISTORY",
    SCANNER_DIR / "processed_history.txt",
)


# --- Domain pack ---
# A "domain pack" bundles the prompts, schemas, and templates that encode a
# specific research field's conventions. The pipeline reads from
# `domain-packs/<DOMAIN_PACK_NAME>/` for everything Stage A and Stage B need.
# Override via $LOCALRAG_DOMAIN_PACK. Bootstrap a new pack with
# `python scanner/bootstrap_domain_pack.py --name <field>`.
DOMAIN_PACK_NAME: str = os.environ.get("LOCALRAG_DOMAIN_PACK", "catalysis")
DOMAIN_PACK_ROOT: Path = _env_path(
    "LOCALRAG_DOMAIN_PACK_ROOT",
    REPO_ROOT / "domain-packs" / DOMAIN_PACK_NAME,
)

# Universal rules live at the repo root and are field-invariant. Every pack
# inherits them; do not duplicate this file inside a pack directory.
UNIVERSAL_RULES_PATH: Path = _env_path(
    "LOCALRAG_UNIVERSAL_RULES",
    REPO_ROOT / "prompts" / "_universal_rules.txt",
)

MODEL_ROUTING_POLICY_PATH: Path = _env_path(
    "GEMINI_MODEL_ROUTING_POLICY",
    DOMAIN_PACK_ROOT / "config" / "model_routing_policy.json",
)


# --- Processor backend selection ---
# Which LLM backend to use for note generation. See scanner/backends/.
# Options: "subagent" (default — needs no API key, uses your Claude Code
# session via Task tool) | "vertex" | "gemini-api" | "anthropic" | "openai".
# Why subagent is the fallback: a fresh-clone user who runs scanner before
# completing init_environment.py shouldn't accidentally land on the
# hardest-to-configure backend (Vertex/GCP). Production batches that need
# real API throughput override this via .env or --backend.
PROCESSOR_BACKEND: str = os.environ.get("LOCALRAG_PROCESSOR_BACKEND", "subagent")


# --- Vertex AI / GCS (used when PROCESSOR_BACKEND == "vertex") ---

GOOGLE_CLOUD_PROJECT: str = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
GOOGLE_CLOUD_LOCATION: str = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
GOOGLE_APPLICATION_CREDENTIALS: str = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
GEMINI_VERTEX_GCS_BUCKET: str = os.environ.get("GEMINI_VERTEX_GCS_BUCKET", "")
GEMINI_VERTEX_GCS_BUCKET_LOCATION: str = os.environ.get("GEMINI_VERTEX_GCS_BUCKET_LOCATION", "US")
GCS_UPLOAD_TIMEOUT_SECONDS: int = int(os.environ.get("GEMINI_GCS_UPLOAD_TIMEOUT_SECONDS", "900"))


# --- Direct API keys (used by non-Vertex backends) ---
# gemini-api backend reads GEMINI_API_KEY at runtime.
# anthropic backend reads ANTHROPIC_API_KEY at runtime.
# openai backend reads OPENAI_API_KEY (+ OPENAI_BASE_URL for compatible providers).
# Defaults blank; the scanner errors out clearly if a backend is selected
# without its required key.
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")

ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_FLASH_MODEL: str = os.environ.get("ANTHROPIC_FLASH_MODEL", "claude-haiku-4-5-20251001")
ANTHROPIC_PRO_MODEL: str = os.environ.get("ANTHROPIC_PRO_MODEL", "claude-sonnet-4-6")

OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")
# OPENAI_BASE_URL: leave blank for OpenAI Inc.; set to "https://api.deepseek.com/v1",
# "https://openrouter.ai/api/v1", "http://localhost:11434/v1" (Ollama OpenAI proxy), etc.
OPENAI_BASE_URL: str = os.environ.get("OPENAI_BASE_URL", "")
OPENAI_ORG_ID: str = os.environ.get("OPENAI_ORG_ID", "")
OPENAI_FLASH_MODEL: str = os.environ.get("OPENAI_FLASH_MODEL", "gpt-4o-mini")
OPENAI_PRO_MODEL: str = os.environ.get("OPENAI_PRO_MODEL", "gpt-4o")
# Some compatible providers (Groq, Together free tier) cap below the OpenAI
# default. Lower this if you hit "max_tokens too large" errors.
OPENAI_MAX_TOKENS: int = int(os.environ.get("OPENAI_MAX_TOKENS", "8192"))


# --- Vertex limits ---

VERTEX_PDF_MAX_SIZE_BYTES: int = int(
    os.environ.get("GEMINI_VERTEX_MAX_PDF_BYTES", str(50 * 1024 * 1024))
)
VERTEX_PDF_MAX_PAGES: int = int(os.environ.get("GEMINI_VERTEX_MAX_PDF_PAGES", "1000"))


# --- Companion service script paths (for restart_query / build_*_db etc.) ---

SERVICE_DIR: Path = _env_path("LOCALRAG_SERVICE_DIR", REPO_ROOT / "service")
BUILD_NOTES_DB_PATH: Path = SERVICE_DIR / "build_notes_db.py"
BUILD_PDF_DB_PATH: Path = SERVICE_DIR / "build_pdf_db.py"
QUERY_SERVER_PATH: Path = SERVICE_DIR / "query_server.py"


# --- Optional vault scripts (post-publish actions) ---
# These live in the user's vault, not the repo. They are optional.
RUN_TAGGING_PIPELINE_PATH: Path = VAULT_ROOT / "scripts" / "run_tagging_pipeline.ps1"
EXPORT_REVIEW_QUEUE_PATH: Path = VAULT_ROOT / "scripts" / "export_review_queue.py"
PREFILL_CANDIDATE_TAGS_PATH: Path = VAULT_ROOT / "scripts" / "prefill_candidate_tags.py"
