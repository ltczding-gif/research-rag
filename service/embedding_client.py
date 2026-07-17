"""
Embedding provider abstraction.

Three providers supported:

    EMBED_PROVIDER=fastembed      (default — in-process ONNX via `fastembed`;
                                   zero daemons, zero keys; model downloads
                                   on first use)
    EMBED_PROVIDER=ollama         (local Ollama daemon — quality upgrade tier)
    EMBED_PROVIDER=openai-compat  (any OpenAI-compatible /v1/embeddings endpoint —
                                   OpenAI Inc., Voyage AI, Jina, Mistral,
                                   Aliyun DashScope, self-hosted TEI / Infinity /
                                   vLLM / LM Studio)

Two surfaces:

  get_embedding(text) -> list[float]
      Single-text manual embedding. Used by build_notes_db.py (writing
      whole-note vectors) and query_server.py (embedding incoming queries).

  get_chromadb_embedding_function() -> chromadb.EmbeddingFunction
      Embedder compatible with chromadb's collection-bound interface. Used
      by build_pdf_db.py (chunked PDF ingest) and query_server.py for the
      `papers` collection.

ChromaDB locks dimensionality at first `add()`. Switching the provider or
the model against an existing collection will cause query-time failures.
See README "Why two venvs?" / Authoring Guide migration notes.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from config import (
    EMBED_MODEL,
    EMBED_PROVIDER,
    FASTEMBED_MODEL,
    MAX_EMBED_CHARS,
    OLLAMA_URL,
    OPENAI_EMBED_API_KEY,
    OPENAI_EMBED_BASE_URL,
    OPENAI_EMBED_MODEL,
)

_FASTEMBED_IMPORT_HINT = (
    "EMBED_PROVIDER=fastembed requires the `fastembed` package. "
    'Install with: pip install "fastembed>=0.4" '
    "(or switch LOCALRAG_EMBED_PROVIDER to ollama / openai-compat in .env)."
)

_fastembed_model_singleton = None


def _get_fastembed_model():
    """Lazy per-process TextEmbedding singleton (model load is expensive)."""
    global _fastembed_model_singleton
    if _fastembed_model_singleton is None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise RuntimeError(_FASTEMBED_IMPORT_HINT) from exc
        _fastembed_model_singleton = TextEmbedding(model_name=FASTEMBED_MODEL)
    return _fastembed_model_singleton


def _fastembed_embed(text: str) -> list[float]:
    model = _get_fastembed_model()
    vector = next(iter(model.embed([text])))
    return [float(v) for v in vector]


def _truncate(text: str) -> str:
    if len(text) > MAX_EMBED_CHARS:
        return text[:MAX_EMBED_CHARS]
    return text


def _ollama_embed(text: str) -> list[float]:
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps({"model": EMBED_MODEL, "prompt": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())["embedding"]


def _openai_compat_embed(text: str) -> list[float]:
    if not OPENAI_EMBED_API_KEY:
        raise RuntimeError(
            "EMBED_PROVIDER=openai-compat but OPENAI_EMBED_API_KEY is empty. "
            "Set it in .env."
        )
    url = f"{OPENAI_EMBED_BASE_URL.rstrip('/')}/embeddings"
    req = urllib.request.Request(
        url,
        data=json.dumps({"model": OPENAI_EMBED_MODEL, "input": text}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_EMBED_API_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read())
    return payload["data"][0]["embedding"]


def active_model_id() -> str:
    """The model id the current provider will embed with. Stamped into
    collection metadata at creation so a later provider/model switch is
    diagnosable instead of a silent dimension mismatch."""
    if EMBED_PROVIDER == "fastembed":
        return FASTEMBED_MODEL
    if EMBED_PROVIDER == "openai-compat":
        return OPENAI_EMBED_MODEL
    return EMBED_MODEL


def get_embedding(text: str) -> list[float]:
    """Embed a single text via the configured provider."""
    text = _truncate(text)
    if EMBED_PROVIDER == "fastembed":
        return _fastembed_embed(text)
    if EMBED_PROVIDER == "openai-compat":
        return _openai_compat_embed(text)
    if EMBED_PROVIDER != "ollama":
        raise RuntimeError(
            f"Unknown LOCALRAG_EMBED_PROVIDER={EMBED_PROVIDER!r}; "
            f"expected 'fastembed', 'ollama' or 'openai-compat'."
        )
    return _ollama_embed(text)


class _FastembedEmbeddingFunction:
    """chromadb-compatible EmbeddingFunction over the fastembed singleton."""

    def __call__(self, input):  # noqa: A002 — chromadb mandates the name `input`
        model = _get_fastembed_model()
        return [[float(v) for v in vec] for vec in model.embed(list(input))]

    @staticmethod
    def name():
        return "research-rag-fastembed"


def get_chromadb_embedding_function():
    """Return an EmbeddingFunction instance for chromadb collection binding."""
    if EMBED_PROVIDER == "fastembed":
        return _FastembedEmbeddingFunction()
    if EMBED_PROVIDER == "openai-compat":
        if not OPENAI_EMBED_API_KEY:
            raise RuntimeError(
                "EMBED_PROVIDER=openai-compat but OPENAI_EMBED_API_KEY is empty. "
                "Set it in .env."
            )
        from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
        return OpenAIEmbeddingFunction(
            api_key=OPENAI_EMBED_API_KEY,
            api_base=OPENAI_EMBED_BASE_URL,
            model_name=OPENAI_EMBED_MODEL,
        )
    if EMBED_PROVIDER != "ollama":
        raise RuntimeError(
            f"Unknown LOCALRAG_EMBED_PROVIDER={EMBED_PROVIDER!r}; "
            f"expected 'fastembed', 'ollama' or 'openai-compat'."
        )
    from chromadb.utils.embedding_functions import OllamaEmbeddingFunction
    return OllamaEmbeddingFunction(model_name=EMBED_MODEL, url=OLLAMA_URL)


def healthcheck() -> dict:
    """Return a dict describing whether the configured provider is reachable.

    Used by the query_server's /health endpoint. Doesn't raise — returns
    `{"ok": False, "reason": "..."}` so the caller can decide what to do.

    For openai-compat, prefers a free `GET {base}/models` listing (cheap,
    no per-token billing) and only falls back to a billable embedding
    probe if /models is unavailable.
    """
    if EMBED_PROVIDER == "fastembed":
        # Import check only — loading the model here would trigger a
        # multi-hundred-MB first-time download inside a health probe.
        try:
            import fastembed  # noqa: F401
        except ImportError:
            return {
                "ok": False,
                "provider": "fastembed",
                "reason": _FASTEMBED_IMPORT_HINT,
            }
        return {
            "ok": True,
            "provider": "fastembed",
            "model": FASTEMBED_MODEL,
            "note": "in-process ONNX; model downloads on first embed",
        }
    if EMBED_PROVIDER == "openai-compat":
        if not OPENAI_EMBED_API_KEY:
            return {
                "ok": False,
                "provider": "openai-compat",
                "reason": "OPENAI_EMBED_API_KEY is empty",
            }
        # Try the free /models endpoint first.
        try:
            req = urllib.request.Request(
                f"{OPENAI_EMBED_BASE_URL.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {OPENAI_EMBED_API_KEY}"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                # Status 200 is enough — we don't introspect the model list.
                if resp.status == 200:
                    return {
                        "ok": True,
                        "provider": "openai-compat",
                        "base_url": OPENAI_EMBED_BASE_URL,
                        "model": OPENAI_EMBED_MODEL,
                        "probe": "models-list",
                    }
        except urllib.error.HTTPError as exc:
            # 401 = bad key (real failure); 404 = no /models endpoint (fall
            # back to embedding probe); other = soft fail, try probe.
            if exc.code == 401:
                return {
                    "ok": False,
                    "provider": "openai-compat",
                    "base_url": OPENAI_EMBED_BASE_URL,
                    "reason": "401 Unauthorized — check OPENAI_EMBED_API_KEY",
                }
            # else: fall through to embedding probe
        except Exception:
            # Network blip — fall through to embedding probe
            pass

        # Fallback: tiny billable probe.
        try:
            _ = _openai_compat_embed("ok")
            return {
                "ok": True,
                "provider": "openai-compat",
                "base_url": OPENAI_EMBED_BASE_URL,
                "model": OPENAI_EMBED_MODEL,
                "probe": "embedding",
            }
        except Exception as exc:
            return {
                "ok": False,
                "provider": "openai-compat",
                "base_url": OPENAI_EMBED_BASE_URL,
                "reason": str(exc),
            }
    # ollama
    base = OLLAMA_URL.rsplit("/api/", 1)[0] if "/api/" in OLLAMA_URL else OLLAMA_URL
    tags_url = f"{base}/api/tags"
    try:
        with urllib.request.urlopen(tags_url, timeout=3) as resp:
            data = json.loads(resp.read())
        return {
            "ok": True,
            "provider": "ollama",
            "url": base,
            "models_count": len(data.get("models", [])),
        }
    except Exception as exc:
        return {
            "ok": False,
            "provider": "ollama",
            "url": base,
            "reason": str(exc),
        }


def detect_dim_mismatch(collection) -> tuple[bool, str]:
    """Probe the configured embedding model and compare its output dim to
    the collection's stored dim. Returns (mismatch_detected, message).

    Called at query_server startup for both `notes` and `papers` collections.
    Catches the most common silent foot-gun: user changes OLLAMA_EMBED_MODEL
    or LOCALRAG_EMBED_PROVIDER, restarts the server, and gets opaque
    chromadb errors at first query because the new vector dimensionality
    doesn't match what the collection was built with.

    Failure modes in order:
      • collection has 0 entries → cannot compare; returns (False, info-msg)
      • model probe fails → returns (False, warn-msg) so we don't false-flag
      • dims match → returns (False, ok-msg)
      • dims differ → returns (True, actionable rebuild instructions)
    """
    try:
        if collection.count() == 0:
            return False, "collection empty; dim check skipped"
    except Exception as exc:
        return False, f"collection.count() failed: {exc}"

    try:
        peek = collection.peek(limit=1)
        embeddings = peek.get("embeddings") if isinstance(peek, dict) else getattr(peek, "embeddings", None)
        # NB: chromadb 1.x returns a numpy array here — `not embeddings`
        # raises "truth value of an array is ambiguous", which the except
        # below used to swallow, silently disabling this check forever.
        if embeddings is None or len(embeddings) == 0 or embeddings[0] is None:
            return False, "no stored embeddings to inspect; dim check skipped"
        stored_dim = len(embeddings[0])
    except Exception as exc:
        return False, f"could not read stored embedding dim: {exc}"

    try:
        probe_dim = len(get_embedding("research-rag dim probe"))
    except Exception as exc:
        return False, f"embedding model probe failed: {exc}"

    if stored_dim == probe_dim:
        return False, f"dim ok ({stored_dim})"

    msg = (
        f"DIMENSION MISMATCH: collection stored at {stored_dim}-dim, "
        f"current embedding model produces {probe_dim}-dim. Queries will "
        f"return errors or wrong results.\n"
        f"\n"
        f"OPTION 1 (recommended for existing users): revert your embedding\n"
        f"  model env var to whatever produces {stored_dim}-dim vectors. No\n"
        f"  data loss; queries work immediately.\n"
        f"\n"
        f"OPTION 2: rebuild the collections under the new model. WARNING —\n"
        f"  this deletes your indexed chroma store. Re-running the builders\n"
        f"  re-embeds all your notes and PDFs (minutes-to-hours depending on\n"
        f"  corpus size). Note generation is NOT re-run; existing Markdown\n"
        f"  notes are preserved.\n"
        f"\n"
        f"  Backup first if you care about the existing index:\n"
        f"    macOS/Linux: cp -r $LOCALRAG_HOME/chroma $LOCALRAG_HOME/chroma.bak\n"
        f"    Windows:     Copy-Item -Recurse $env:LOCALRAG_HOME/chroma $env:LOCALRAG_HOME/chroma.bak\n"
        f"\n"
        f"  Then rebuild:\n"
        f"    macOS/Linux:\n"
        f"      rm -rf $LOCALRAG_HOME/chroma\n"
        f"      : > $LOCALRAG_HOME/processed_groups.txt\n"
        f"      : > $LOCALRAG_HOME/processed_notes.txt\n"
        f"      python service/build_notes_db.py\n"
        f"      python service/build_pdf_db.py\n"
        f"    Windows (PowerShell):\n"
        f"      Remove-Item -Recurse -Force $env:LOCALRAG_HOME\\chroma\n"
        f"      Clear-Content $env:LOCALRAG_HOME\\processed_groups.txt\n"
        f"      Clear-Content $env:LOCALRAG_HOME\\processed_notes.txt\n"
        f"      python service\\build_notes_db.py\n"
        f"      python service\\build_pdf_db.py"
    )
    return True, msg


__all__ = [
    "get_embedding",
    "get_chromadb_embedding_function",
    "healthcheck",
    "detect_dim_mismatch",
]
