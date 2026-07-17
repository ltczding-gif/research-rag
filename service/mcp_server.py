"""
stdio MCP server for research-rag retrieval.

The terminal agent (Claude Code / Codex / any MCP client) spawns this
process per session and talks JSON-RPC over stdio — no port, no daemon,
nothing for the user to remember to start. The Flask sidecar
(query_server.py) remains available as a compatibility fallback; both are
thin shells over the same transport-free functions.

Registration: the setup walkthrough writes a local MCP entry using the
installed venv's absolute Python path. The repo also ships a project-scoped
`.mcp.json` fallback. Manual registration (replace both paths):

    claude mcp add --transport stdio --scope local research-rag -- /absolute/path/to/python /absolute/path/to/scripts/run_mcp_server.py

Requires: pip install "mcp>=1.0" (in requirements-rag.txt) plus the
usual service deps (chromadb, and your configured embedding provider).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make sibling-module imports work when spawned from any cwd.
_SERVICE_DIR = Path(__file__).resolve().parent
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "research-rag MCP server requires the `mcp` package. "
        'Install with: pip install "mcp>=1.0" '
        "(it is included in requirements-rag.txt)."
    ) from exc

# Importing query_server initializes the ChromaDB collections at module
# level (unless SKIP_CHROMA_INIT=1) — exactly the state the tools need.
# The Flask `app` object is created but never run.
import query_server as _core  # noqa: E402

mcp = FastMCP("research-rag")


@mcp.tool()
def search_notes(query: str, n: int = 5, zotero_parent_key: str = "") -> dict:
    """Semantic search over the whole-document research-notes collection.

    Returns the top-n notes with content previews and metadata
    (title_en/title_zh/year/journal/doi/zotero_parent_key/score).
    Ask in Chinese or English; notes are typically Chinese with English
    technical terms.
    """
    return _core.search_notes_chroma(
        query, limit=n, zotero_parent_key=zotero_parent_key or None
    )


@mcp.tool()
def search_papers(
    query: str,
    n: int = 3,
    zotero_parent_key: str = "",
    second_query: str = "",
    include_context: bool = False,
) -> dict:
    """Semantic search over the PDF chunk collection (original paper text).

    Use zotero_parent_key (from a search_notes hit) to restrict to one
    paper's main text + SI. second_query overrides the embedding query
    (e.g. English technical terms extracted from a Chinese note) while
    `query` is kept for logging. include_context=True stitches the
    previous/next chunks around each match, with the match wrapped in
    [MATCH]...[/MATCH].
    """
    payload, _status = _core.search_papers_chroma(
        query=query,
        n=n,
        zotero_parent_key=zotero_parent_key or None,
        second_query=second_query or None,
        include_context=include_context,
    )
    return payload


@mcp.tool()
def get_note(source: str = "", zotero_parent_key: str = "", summary_only: bool = False) -> dict:
    """Fetch the full content of one research note.

    Identify it by `source` (note filename) or `zotero_parent_key`.
    summary_only=True returns frontmatter + first 500 chars.
    """
    payload, _status = _core.get_note_payload(
        source=source or None,
        zotero_parent_key=zotero_parent_key or None,
        summary_only=summary_only,
    )
    return payload


@mcp.tool()
def index_status() -> dict:
    """Health/status of the local index: collection readiness and counts,
    plus which embedding provider/model is active."""
    from embedding_client import healthcheck, active_model_id

    status: dict = {
        "papers_ready": bool(_core.chroma_ready and _core.pdf_col is not None),
        "notes_ready": bool(_core.notes_ready and _core.notes_col is not None),
        "embedding": healthcheck(),
        "active_embed_model": active_model_id(),
    }
    try:
        if status["papers_ready"]:
            status["paper_chunks"] = _core.pdf_col.count()
        if status["notes_ready"]:
            status["note_count"] = _core.notes_col.count()
    except Exception as exc:  # count() can fail on a corrupt store
        status["count_error"] = str(exc)
    if getattr(_core, "_dim_warnings", None):
        status["dim_mismatch"] = _core._dim_warnings
    return status


if __name__ == "__main__":
    mcp.run()  # stdio transport
