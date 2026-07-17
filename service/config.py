"""
Shared configuration for the service layer.

All paths, ports, and credentials are read from environment variables with
sensible defaults. Defaults assume Unix-style layout under the user's home
directory; on Windows, ~/ resolves to %USERPROFILE%.

Override individual values via .env or shell environment. See .env.example
at the repo root for the full contract.
"""

from __future__ import annotations

import os
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent


def _hydrate_env_from_dotenv(path: Path) -> None:
    """Mirror of scanner/config.py:_hydrate_env_from_dotenv.

    Service modules can be invoked outside a shell that sourced .env
    (Claude Code skill subprocess, query_server background process,
    cron, etc.). Loading .env here ensures LOCALRAG_PROCESSOR_BACKEND
    and the other env-driven knobs actually take effect. Existing
    environment variables win — never clobber an explicit shell export.
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        else:
            comment_idx = value.find(" #")
            if comment_idx != -1:
                value = value[:comment_idx].rstrip()
        os.environ[key] = value


_hydrate_env_from_dotenv(_REPO_ROOT / ".env")


def _env_path(key: str, default: Path) -> Path:
    """Read a path from env var; expand ~ and env vars; fall back to default."""
    raw = os.environ.get(key)
    if not raw:
        return default
    return Path(os.path.expandvars(os.path.expanduser(raw)))


# --- Root directories ---

LOCALRAG_HOME: Path = _env_path("LOCALRAG_HOME", Path.home() / ".localrag")
NOTES_DIR: Path = _env_path("LOCALRAG_NOTES_DIR", Path.home() / "research-note")


# --- ChromaDB ---

CHROMA_PATH: Path = _env_path("LOCALRAG_CHROMA_PATH", LOCALRAG_HOME / "chroma")
NOTES_COLLECTION_NAME: str = os.environ.get("LOCALRAG_NOTES_COLLECTION", "notes")
PAPERS_COLLECTION_NAME: str = os.environ.get("LOCALRAG_PAPERS_COLLECTION", "papers")


# --- Ledger files (track which content has been ingested) ---

PDF_LEDGER: Path = _env_path("LOCALRAG_PDF_LEDGER", LOCALRAG_HOME / "processed_groups.txt")
NOTES_LEDGER: Path = _env_path("LOCALRAG_NOTES_LEDGER", LOCALRAG_HOME / "processed_notes.txt")
TEXTBOOK_LEDGER: Path = _env_path("LOCALRAG_TEXTBOOK_LEDGER", LOCALRAG_HOME / "textbook_ledger.txt")


# --- Embedding provider selection ---
#
# `fastembed` (default): in-process ONNX embeddings via the `fastembed`
#                     library — zero daemons, zero API keys. The model
#                     (~0.2 GB, multilingual zh+en) downloads on first use.
# `ollama`:           local Ollama daemon at OLLAMA_URL. Quality upgrade
#                     tier (qwen3-embedding family) for users who run Ollama.
# `openai-compat`:    any provider exposing the OpenAI /v1/embeddings shape —
#                     OpenAI Inc., Voyage AI, Jina, Mistral, Aliyun DashScope,
#                     self-hosted TEI / Infinity / vLLM / LM Studio.
#
# NB: switching provider/model against an existing ChromaDB fails at query
# time (dimension mismatch) — rebuild the collections after switching.
EMBED_PROVIDER: str = os.environ.get("LOCALRAG_EMBED_PROVIDER", "fastembed")

# --- fastembed (used when EMBED_PROVIDER=fastembed) ---

FASTEMBED_MODEL: str = os.environ.get(
    "LOCALRAG_FASTEMBED_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)

# --- Ollama embedding service (used when EMBED_PROVIDER=ollama) ---

OLLAMA_URL: str = os.environ.get("OLLAMA_EMBED_URL", "http://localhost:11434/api/embeddings")
EMBED_MODEL: str = os.environ.get("OLLAMA_EMBED_MODEL", "qwen3-embedding:0.6b")
# qwen3-embedding:0.6b takes up to 8192 tokens; 12000 chars is a safe ceiling
# for Chinese-English mixed text (~2 chars/token). Larger models (4b, 8b)
# accommodate more — see .env.example for tier-specific values.
MAX_EMBED_CHARS: int = int(os.environ.get("LOCALRAG_MAX_EMBED_CHARS", "12000"))


# --- OpenAI-compatible embedding (used when EMBED_PROVIDER=openai-compat) ---

OPENAI_EMBED_BASE_URL: str = os.environ.get(
    "OPENAI_EMBED_BASE_URL",
    "https://api.openai.com/v1",
)
OPENAI_EMBED_API_KEY: str = os.environ.get("OPENAI_EMBED_API_KEY", "")
# Reference defaults; override per-provider:
#   OpenAI:     text-embedding-3-small (1536-dim) / text-embedding-3-large (3072)
#   Voyage:     voyage-3.5-lite / voyage-3.5 / voyage-3-large (1024)
#   DashScope:  text-embedding-v3 (1024, configurable 64-1024)
#   Jina:       jina-embeddings-v3 (1024)
#   Mistral:    mistral-embed (1024)
OPENAI_EMBED_MODEL: str = os.environ.get(
    "OPENAI_EMBED_MODEL",
    "text-embedding-3-small",
)


# --- Query server ---

HOST: str = os.environ.get("LOCALRAG_HOST", "127.0.0.1")
PORT: int = int(os.environ.get("LOCALRAG_PORT", "18810"))
SKIP_CHROMA_INIT: bool = os.environ.get("LOCALRAG_SKIP_CHROMA_INIT") == "1"


# --- Query log ---

QUERY_LOG_ROOT: Path = _env_path("LOCALRAG_QUERY_LOG_ROOT", NOTES_DIR / "_query_logs")
QUERY_LOG_SCHEMA_VERSION: int = 1
QUERY_LOG_ALLOWED_STATUS = {"success", "no_hits", "partial", "error"}
QUERY_LOG_REGISTRY_FILENAME: str = "_registry.json"


# --- Zotero ---

ZOTERO_DB_PATH: Path = _env_path("ZOTERO_DB_PATH", Path.home() / "Zotero" / "zotero.sqlite")


# --- Chunking (PDF papers) ---

CHUNK_SIZE: int = int(os.environ.get("LOCALRAG_CHUNK_SIZE", "800"))
CHUNK_STEP: int = int(os.environ.get("LOCALRAG_CHUNK_STEP", "700"))
MIN_CHUNK_LEN: int = int(os.environ.get("LOCALRAG_MIN_CHUNK_LEN", "100"))
TEXTBOOK_BATCH_SIZE: int = int(os.environ.get("LOCALRAG_TEXTBOOK_BATCH_SIZE", "50"))
NOTE_SUFFIX: str = os.environ.get("LOCALRAG_NOTE_SUFFIX", "_review_note.md")
