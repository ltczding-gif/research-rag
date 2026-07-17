"""
Local Dual-Library RAG Query Service.

Endpoints:
  GET  /health
  POST /search_notes              — vector search over `notes` collection
  POST /get_note                  — fetch a full note by source or zotero_parent_key
  POST /search_papers             — vector search over `papers` collection
  POST /write_query_log           — append-only Markdown query log (idempotent)
  POST /append_query_log_action   — append a follow-up action to an existing log

All paths, ports, and credentials come from environment variables — see
config.py and .env.example. Set LOCALRAG_SKIP_CHROMA_INIT=1 to skip startup
ChromaDB connection (useful for tests).
"""

from flask import Flask, request, jsonify
import chromadb
import json
import os
import re
import yaml
import urllib.request
from datetime import datetime
from uuid import uuid4

from config import (
    CHROMA_PATH,
    PAPERS_COLLECTION_NAME as COLLECTION_NAME,
    NOTES_COLLECTION_NAME,
    QUERY_LOG_ROOT,
    QUERY_LOG_SCHEMA_VERSION,
    QUERY_LOG_ALLOWED_STATUS,
    QUERY_LOG_REGISTRY_FILENAME,
    SKIP_CHROMA_INIT,
    HOST,
    PORT,
)
from embedding_client import (
    get_embedding,
    get_chromadb_embedding_function,
    healthcheck as embedding_healthcheck,
    detect_dim_mismatch,
)

app = Flask(__name__)

if SKIP_CHROMA_INIT:
    client = None
    ef = None
    pdf_col = None
    notes_col = None
    chroma_ready = False
    notes_ready = False
    print("[INIT] Skipping ChromaDB initialization (LOCALRAG_SKIP_CHROMA_INIT=1)")
else:
    # Create the shared ChromaDB client FIRST, independent of whether any
    # individual collection exists. A missing `papers` collection must not
    # nil out the client that `notes` also needs: notes and papers are built
    # by separate steps (build_notes_db.py / build_pdf_db.py), so a
    # notes-only install (or a papers-only one) is a normal state. Folding
    # client creation into the papers try-block meant an absent papers
    # collection dragged the notes collection down with it.
    try:
        client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    except Exception as e:
        print(f"[INIT ERROR] ChromaDB client failed: {e}")
        client = None

    try:
        ef = get_chromadb_embedding_function()
        pdf_col = (
            client.get_collection(name=COLLECTION_NAME, embedding_function=ef)
            if client else None
        )
        chroma_ready = pdf_col is not None
        if chroma_ready:
            print(f"[INIT] ChromaDB papers loaded: {pdf_col.count()} chunks")
        else:
            print("[INIT WARN] ChromaDB papers not available: client not initialized")
    except Exception as e:
        print(f"[INIT ERROR] ChromaDB papers failed: {e}")
        pdf_col = None
        chroma_ready = False
        ef = None

    try:
        # notes collection doesn't bind ef (build_notes_db.py passes embeddings
        # manually). Queries call get_embedding() directly.
        notes_col = client.get_collection(name=NOTES_COLLECTION_NAME) if client else None
        notes_ready = notes_col is not None
        if notes_ready:
            print(f"[INIT] ChromaDB notes loaded: {notes_col.count()} notes")
        else:
            print("[INIT WARN] ChromaDB notes not available: client not initialized")
    except Exception as e:
        print(f"[INIT WARN] ChromaDB notes not available: {e}")
        notes_col = None
        notes_ready = False

if not SKIP_CHROMA_INIT and 'notes_col' not in globals():
    notes_col = None
    notes_ready = False


# Dimension-mismatch guard: warn loudly if the configured embedding model
# produces a different vector dim than what the collection was built with.
# Most common cause: user changed LOCALRAG_EMBED_PROVIDER or OLLAMA_EMBED_MODEL
# without rebuilding the chroma store. Without this guard, queries fail with
# opaque chromadb / Rust-backend errors at request time.
_dim_warnings: list[str] = []
if not SKIP_CHROMA_INIT:
    for _name, _col in (("papers", pdf_col if chroma_ready else None),
                        ("notes", notes_col if notes_ready else None)):
        if _col is None:
            continue
        try:
            _mismatch, _msg = detect_dim_mismatch(_col)
        except Exception as _exc:
            print(f"[INIT WARN] dim check failed for {_name}: {_exc}")
            continue
        if _mismatch:
            warning = f"[INIT WARN] dim-mismatch on {_name}: {_msg}"
            print(warning)
            _dim_warnings.append(warning)
        else:
            print(f"[INIT] {_name} {_msg}")
# We don't refuse to start the server — surface mismatches via /health
# so an operator can see them and decide. /health returns 503 while
# _dim_warnings is non-empty.


def parse_frontmatter(text):
    """Parse YAML frontmatter from markdown text. Returns (dict, body_str)."""
    if not text:
        return {}, text or ""
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
    if m:
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            fm = {}
        return fm, text[m.end():]
    return {}, text


def detect_query_language(text):
    """Best-effort query language detection for metadata."""
    if not text:
        return "unknown"
    if re.search(r'[\u4e00-\u9fff]', text):
        return "zh"
    return "en"


def slugify_query_title(query, max_len=32):
    """Generate a stable filesystem-safe short title from the original query."""
    text = (query or "").strip()
    text = re.sub(r'[\\/:*?"<>|]+', '', text)
    text = re.sub(r'\s+', '-', text)
    text = re.sub(r'-{2,}', '-', text)
    text = text.strip('-_. ')
    if not text:
        text = "query"
    text = text[:max_len].rstrip('-_. ')
    return text or "query"


def build_query_log_id(created_at, short_id):
    dt = datetime.fromisoformat(created_at)
    return f"ql-{dt.strftime('%Y%m%d-%H%M%S')}-{short_id.upper()}"


def build_query_log_filename(created_at, workflow_id, query, short_id):
    dt = datetime.fromisoformat(created_at)
    query_title = slugify_query_title(query)
    wf = (workflow_id or "WF").strip()
    return f"{dt.strftime('%Y%m%d-%H%M%S')}_{wf}_{query_title}_{short_id.upper()}.md"


def ensure_query_log_month_dir(created_at, root=None):
    dt = datetime.fromisoformat(created_at)
    active_root = root or QUERY_LOG_ROOT
    os.makedirs(active_root, exist_ok=True)
    month_dir = os.path.join(active_root, dt.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    return month_dir


def get_query_log_registry_path(root=None):
    active_root = root or QUERY_LOG_ROOT
    os.makedirs(active_root, exist_ok=True)
    return os.path.join(active_root, QUERY_LOG_REGISTRY_FILENAME)


def load_query_log_registry(root=None):
    path = get_query_log_registry_path(root)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_query_log_registry(registry, root=None):
    path = get_query_log_registry_path(root)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)


def ensure_nonempty_string(value, field_name):
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ValueError(f"Missing required field: {field_name}")
    return text


def ensure_nonempty_list(value, field_name):
    if not isinstance(value, list) or len(value) == 0:
        raise ValueError(f"Missing required non-empty list field: {field_name}")
    return value


def normalize_query_log_status(status):
    normalized = (status or "").strip()
    if normalized not in QUERY_LOG_ALLOWED_STATUS:
        raise ValueError(
            f"Invalid status '{status}'. Allowed: {sorted(QUERY_LOG_ALLOWED_STATUS)}"
        )
    return normalized


def infer_session_summary_title(final_response_snapshot, query, max_len=120):
    """Derive a readable title for frontmatter when the caller does not provide one."""
    base = (final_response_snapshot or "").strip().splitlines()
    candidate = next((line.strip() for line in base if line.strip()), "") if base else ""
    if not candidate:
        candidate = (query or "").strip()
    candidate = re.sub(r'\s+', ' ', candidate).strip()
    return candidate[:max_len].rstrip(" .,;:") if candidate else "Untitled session"


def normalize_angle_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def serialize_filters(filters):
    if filters is None or filters == "":
        return "none"
    if isinstance(filters, str):
        return filters
    return json.dumps(filters, ensure_ascii=False, sort_keys=True)


def unique_preserve_order(items):
    seen = set()
    ordered = []
    for item in items:
        if item and item not in seen:
            ordered.append(item)
            seen.add(item)
    return ordered


def collect_zotero_parent_keys(notes, papers, provided=None):
    keys = list(provided or [])
    for note in notes or []:
        meta = note.get("metadata", {}) if isinstance(note, dict) else {}
        keys.append(meta.get("zotero_parent_key"))
    for paper in papers or []:
        meta = paper.get("metadata", {}) if isinstance(paper, dict) else {}
        keys.append(meta.get("zotero_parent_key"))
    return unique_preserve_order(keys)


def collect_source_note_files(notes, provided=None):
    files = list(provided or [])
    for note in notes or []:
        meta = note.get("metadata", {}) if isinstance(note, dict) else {}
        files.append(meta.get("source_file") or note.get("source"))
    return unique_preserve_order(files)


def render_frontmatter(payload):
    """Render ordered YAML frontmatter for a query log."""
    search_runs = payload.get("search_runs", [])
    fm = {
        "schema_version": QUERY_LOG_SCHEMA_VERSION,
        "log_id": payload.get("log_id"),
        "idempotency_key": payload.get("idempotency_key"),
        "created_at": payload.get("created_at"),
        "month": payload.get("month"),
        "workflow_id": payload.get("workflow_id"),
        "workflow_name": payload.get("workflow_name"),
        "status": payload.get("status"),
        "query": payload.get("query"),
        "query_title": payload.get("query_title"),
        "session_summary_title": payload.get("session_summary_title"),
        "query_language": payload.get("query_language"),
        "anchor_query": payload.get("anchor_query"),
        "anchor_query_source": payload.get("anchor_query_source"),
        "saved_by": payload.get("saved_by", "search-literature"),
        "search_runs": len(search_runs),
        "planned_angles": payload.get("planned_angles", []),
        "executed_angles": payload.get("executed_angles", []),
        "expansion_reason": payload.get("expansion_reason"),
        "stop_reason": payload.get("stop_reason"),
        "notes_hits": len(payload.get("notes", [])),
        "papers_hits": len(payload.get("papers", [])),
        "zotero_parent_keys": payload.get("zotero_parent_keys", []),
        "source_note_files": payload.get("source_note_files", []),
        "effective_queries": payload.get("effective_queries", {}),
        "second_queries": payload.get("second_queries", []),
        "search_run_details": search_runs,
        "log_path": payload.get("log_path"),
    }
    yaml_text = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{yaml_text}\n---"


def render_search_plan_section(payload):
    lines = ["## Search Plan", ""]
    anchor_query = payload.get("anchor_query") or payload.get("query") or "Not recorded"
    planned_angles = payload.get("planned_angles", [])
    initial_exploratory = next(
        (angle for angle in planned_angles if str(angle).strip() != "anchor"),
        "not recorded"
    )
    executed_angles = payload.get("executed_angles", [])
    expansion_reason = payload.get("expansion_reason")
    stop_reason = payload.get("stop_reason") or "not recorded"

    lines.append(f"- Anchor angle: {anchor_query}")
    lines.append(f"- Initial exploratory angle: {initial_exploratory}")
    lines.append(f"- Expansion triggered: {'yes' if expansion_reason else 'no'}")
    if expansion_reason:
        lines.append(f"- Expansion reason: {expansion_reason}")
    lines.append(f"- Executed angles: {', '.join(executed_angles) if executed_angles else 'not recorded'}")
    lines.append(f"- Stop reason: {stop_reason}")
    return "\n".join(lines)


def render_search_runs_section(search_runs):
    lines = ["## Search Runs", ""]
    if not search_runs:
        lines.append("No search runs recorded.")
        return "\n".join(lines)

    for idx, run in enumerate(search_runs, 1):
        lines.append(f"### Run {idx}")
        role = run.get("role")
        if role:
            lines.append(f"- Role: {role}")
        lines.append(f"- Purpose: {run.get('purpose', 'not recorded')}")
        lines.append(f"- Endpoint: `{run.get('endpoint', 'not recorded')}`")
        lines.append(f"- Query: {run.get('query', 'not recorded')}")
        lines.append(f"- Filters: {serialize_filters(run.get('filters'))}")
        lines.append(f"- Hits: {run.get('hits', 'not recorded')}")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_notes_hits_section(notes):
    lines = ["## Notes Hits", ""]
    if not notes:
        lines.append("No note hits recorded.")
        return "\n".join(lines)

    for idx, note in enumerate(notes, 1):
        meta = note.get("metadata", {})
        lines.append(f"### N{idx}")
        lines.append(f"- Source: {meta.get('source_file', note.get('source', 'unknown'))}")
        if meta.get("note_rank") is not None:
            lines.append(f"- Rank: {meta.get('note_rank')}")
        if meta.get("score") is not None:
            lines.append(f"- Score: {meta.get('score')}")
        if meta.get("year"):
            lines.append(f"- Year: {meta.get('year')}")
        if meta.get("journal"):
            lines.append(f"- Journal: {meta.get('journal')}")
        if meta.get("zotero_parent_key"):
            lines.append(f"- Zotero key: {meta.get('zotero_parent_key')}")
        lines.append("")
        lines.append("摘录：")
        excerpt = (note.get("content") or "").strip()[:800]
        lines.append(f"> {excerpt}" if excerpt else "> ")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_paper_hits_section(papers):
    lines = ["## Paper Hits", ""]
    if not papers:
        lines.append("No paper hits recorded.")
        return "\n".join(lines)

    for idx, paper in enumerate(papers, 1):
        meta = paper.get("metadata", {})
        lines.append(f"### P{idx}")
        lines.append(f"- Source: {meta.get('pdf_filename', 'unknown')}")
        if meta.get("is_main") is not None or meta.get("is_si") is not None:
            source_type = "主文" if meta.get("is_main", False) else "SI"
            lines.append(f"- Type: {source_type}")
        if paper.get("distance") is not None:
            lines.append(f"- Distance: {paper.get('distance')}")
        if meta.get("chunk_index") is not None:
            lines.append(f"- Chunk index: {meta.get('chunk_index')}")
        if meta.get("zotero_parent_key"):
            lines.append(f"- Zotero key: {meta.get('zotero_parent_key')}")
        lines.append("")
        lines.append("原文：")
        excerpt = (paper.get("content") or "").strip()[:1200]
        lines.append(f"> {excerpt}" if excerpt else "> ")
        translation = (
            paper.get("translation")
            or paper.get("translation_zh")
            or paper.get("translated_content")
        )
        if translation:
            lines.append("")
            lines.append("译：")
            lines.append(f"> {translation.strip()}")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_followup_block(action_payload):
    """Render one append-only follow-up block."""
    lines = [f"### {action_payload.get('timestamp', iso_now())}"]
    lines.append(f"- Action: {action_payload.get('action', 'unknown')}")
    lines.append(f"- Result: {action_payload.get('result', 'not recorded')}")
    details = action_payload.get("details") or {}
    if isinstance(details, dict):
        for key, value in details.items():
            if value is not None and value != "":
                label = str(key).replace("_", " ").title()
                lines.append(f"- {label}: {value}")
    elif details:
        lines.append(f"- Details: {details}")
    return "\n".join(lines)


def render_query_log_markdown(payload):
    """Render the full Markdown document for one query log."""
    sections = [
        render_frontmatter(payload),
        "",
        "# Query Record",
        "",
        "## User Query",
        "",
        payload.get("query", ""),
        "",
        "## Workflow Decision",
        "",
        f"- Workflow: {payload.get('workflow_id', 'unknown')} · {payload.get('workflow_name', 'unknown')}",
        f"- Reason: {payload.get('workflow_reason', 'not recorded')}",
        "",
        render_search_plan_section(payload),
        "",
        render_search_runs_section(payload.get("search_runs", [])),
        "",
        "## Result Summary",
        "",
        payload.get("result_summary", "No result summary recorded."),
        "",
        render_notes_hits_section(payload.get("notes", [])),
        "",
        render_paper_hits_section(payload.get("papers", [])),
        "",
        "## Final Response Snapshot",
        "",
        payload.get("final_response_snapshot", ""),
        "",
        "## Follow-up Actions",
        "",
    ]
    return "\n".join(sections).rstrip() + "\n"


DEBUG_ERRORS = os.environ.get("LOCALRAG_DEBUG_ERRORS", "") == "1"


def error_payload(exc):
    """Body for 500 responses. Tracebacks (absolute local paths, code
    context) are only included when LOCALRAG_DEBUG_ERRORS=1 — the server is
    localhost-only, but log files and pasted client output travel."""
    payload = {"error": str(exc)}
    if DEBUG_ERRORS:
        import traceback
        payload["traceback"] = traceback.format_exc()
    return payload


def iso_now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_followup_header(text):
    if "## Follow-up Actions" in text:
        return text.rstrip() + "\n\n"
    return text.rstrip() + "\n\n## Follow-up Actions\n\n"


def is_path_within_root(path, root):
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    except ValueError:
        return False




def search_notes_chroma(query, limit=5, dedupe=True, zotero_parent_key=None):
    """Search notes collection in ChromaDB (整篇笔记入库，不切块)"""
    if not notes_ready or notes_col is None:
        return {"error": "Notes collection not initialized. Run build_notes_db.py first."}

    try:
        where = {}
        if zotero_parent_key:
            where["zotero_parent_key"] = zotero_parent_key

        # 手动获取查询向量（notes_col 无绑定 ef）
        query_emb = get_embedding(query)

        # dedupe 对整篇入库无意义（每篇只有一条记录），直接取 top-n
        results = notes_col.query(
            query_embeddings=[query_emb],
            n_results=limit,
            where=where if where else None,
        )

        formatted = []
        for i in range(len(results["documents"][0])):
            doc = results["documents"][0][i]
            meta = results["metadatas"][0][i]
            dist = results["distances"][0][i] if "distances" in results else None

            # 解析 frontmatter 补充缺失字段
            fm, body = parse_frontmatter(doc)

            metadata = {
                "source_file": meta.get("source_file", ""),
                "zotero_parent_key": meta.get("zotero_parent_key", ""),
                "title_en": meta.get("title_en", fm.get("title_en", "")),
                "title_zh": meta.get("title_zh", fm.get("title_zh", "")),
                "year": meta.get("year", fm.get("year", "")),
                "journal": meta.get("journal", fm.get("journal", "")),
                "authors": meta.get("authors", fm.get("authors", "")),
                "doi": meta.get("doi", fm.get("doi", "")),
                "score": round(1 - dist, 4) if dist is not None else None,
                "note_rank": i + 1,
            }
            metadata = {k: v for k, v in metadata.items() if v is not None and v != ""}

            formatted.append({
                "id": results["ids"][0][i],
                "content": doc[:3000],  # 返回前 3000 字符，够看结论和摘要
                "metadata": metadata,
            })

        return {"results": formatted}

    except Exception as e:
        return error_payload(e)


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    status = {
        "status": "ok",
        "papers": {
            "ready": chroma_ready,
            "path": str(CHROMA_PATH),
            "collection": COLLECTION_NAME,
        },
        "notes": {
            "ready": notes_ready,
            "collection": NOTES_COLLECTION_NAME,
        },
    }

    if chroma_ready and pdf_col:
        try:
            status["papers"]["chunks"] = pdf_col.count()
        except Exception as e:
            status["papers"]["error"] = str(e)

    if notes_ready and notes_col:
        try:
            status["notes"]["count"] = notes_col.count()
        except Exception as e:
            status["notes"]["error"] = str(e)
    
    # Check the configured embedding provider — branches automatically
    # between Ollama and openai-compat. Field name is "embedding" rather
    # than "ollama" so consumers don't assume a specific provider.
    embed_status = embedding_healthcheck()
    status["embedding"] = embed_status
    # Backwards-compat: legacy clients keyed off "ollama". Mirror the
    # boolean health into that field too when ollama is the active provider.
    if embed_status.get("provider") == "ollama":
        status["ollama"] = "ready" if embed_status.get("ok") else f"error: {embed_status.get('reason', 'unknown')}"

    if _dim_warnings:
        status["dim_mismatch"] = _dim_warnings

    http_code = 200 if (
        status["papers"]["ready"]
        and status["notes"]["ready"]
        and embed_status.get("ok")
        and not _dim_warnings
    ) else 503
    return jsonify(status), http_code


@app.route('/search_notes', methods=['POST'])
def search_notes():
    """
    Query md notes library (SQLite vector similarity)
    Input: {"query": "...", "n": 5, "dedupe": true, "zotero_parent_key": "..."}
    """
    data = request.json or {}
    query = data.get('query', '')
    n = data.get('n', 5)
    dedupe = data.get('dedupe', True)
    zotero_parent_key = data.get('zotero_parent_key')
    
    if not query:
        return jsonify({"error": "Missing required field: query"}), 400
    
    result = search_notes_chroma(query, limit=n, dedupe=dedupe,
                                  zotero_parent_key=zotero_parent_key)
    
    if "error" in result:
        return jsonify(result), 500
    
    return jsonify(result)


def get_note_payload(source=None, zotero_parent_key=None, summary_only=False):
    """Transport-free /get_note implementation. Returns (payload, status).

    Shared by the Flask route below and service/mcp_server.py.
    """
    if not source and not zotero_parent_key:
        return {"error": "需要 source 或 zotero_parent_key"}, 400

    if not notes_ready or notes_col is None:
        return {"error": "Notes collection not initialized"}, 503

    try:
        # 通过 zotero_parent_key 或 source_file 精确查找
        where = {}
        if zotero_parent_key:
            where["zotero_parent_key"] = zotero_parent_key
        elif source:
            # source 可能是完整路径或文件名
            filename = os.path.basename(source)
            where["source_file"] = filename

        results = notes_col.get(where=where if where else None, limit=5)

        if not results["ids"]:
            return {"error": "笔记未找到"}, 404

        note_list = []
        for i in range(len(results["ids"])):
            doc = results["documents"][i]
            meta = results["metadatas"][i]
            fm, body = parse_frontmatter(doc)
            content = doc[:500] + "\n..." if summary_only else doc
            note_list.append({
                "source": meta.get("source_file", ""),
                "content": content,
                "summary_only": summary_only,
                "metadata": {
                    "title_en": meta.get("title_en", fm.get("title_en")),
                    "title_zh": meta.get("title_zh", fm.get("title_zh")),
                    "year": meta.get("year", fm.get("year")),
                    "journal": meta.get("journal", fm.get("journal")),
                    "zotero_parent_key": meta.get("zotero_parent_key", fm.get("zotero_parent_key")),
                    "doi": meta.get("doi", fm.get("doi")),
                },
            })

        return {"notes": note_list}, 200

    except Exception as e:
        return error_payload(e), 500


@app.route('/get_note', methods=['POST'])
def get_note():
    """
    返回指定笔记的完整内容
    Input: {"source": "$LOCALRAG_NOTES_DIR/xxx.md"} 或 {"zotero_parent_key": "ABC12345"}
    可选: {"summary_only": true}  # 只返回frontmatter+前500字符
    """
    data = request.json or {}
    payload, status = get_note_payload(
        source=data.get('source'),
        zotero_parent_key=data.get('zotero_parent_key'),
        summary_only=data.get('summary_only', False),
    )
    return jsonify(payload), status


def _neighbor_chunk_ids(hit_id: str, chunk_index: int) -> tuple[str | None, str]:
    """Derive neighbor IDs from the actual hit ID for every supported schema."""
    marker = "_chunk_"
    if marker not in hit_id:
        raise ValueError(f"unrecognized chunk id: {hit_id}")
    prefix = hit_id.rsplit(marker, 1)[0]
    previous = f"{prefix}{marker}{chunk_index - 1}" if chunk_index > 0 else None
    following = f"{prefix}{marker}{chunk_index + 1}"
    return previous, following


def search_papers_chroma(
    query,
    n=3,
    zotero_parent_key=None,
    paper_group=None,
    pdf_filename=None,
    second_query=None,
    include_context=False,
):
    """Transport-free /search_papers implementation. Returns (payload, status).

    Shared by the Flask route below and service/mcp_server.py.
    """
    if not chroma_ready or pdf_col is None:
        return {"error": "ChromaDB not initialized"}, 503

    effective_query = second_query if second_query else query

    if not query:
        return {"error": "Missing required field: query"}, 400

    where = {}
    if zotero_parent_key:
        where["zotero_parent_key"] = zotero_parent_key
    elif paper_group is not None:
        where["paper_group"] = paper_group
    elif pdf_filename:
        where["pdf_filename"] = pdf_filename

    try:
        results = pdf_col.query(
            query_texts=[effective_query],
            n_results=n,
            where=where if where else None
        )

        formatted_results = []
        for i in range(len(results['documents'][0])):
            meta = results['metadatas'][0][i]
            content = results['documents'][0][i]  # 原始匹配 chunk，约800字符

            result_item = {
                "content": content,           # 默认只返回匹配段
                "metadata": meta,
                "distance": results['distances'][0][i] if 'distances' in results else None
            }

            # 仅当 include_context=true 时才拼接前后 chunk
            if include_context:
                try:
                    chunk_idx = meta.get('chunk_index', 0)
                    hit_id = results['ids'][0][i]
                    prev_id, next_id = _neighbor_chunk_ids(hit_id, chunk_idx)
                    neighbor_ids = [item for item in (prev_id, next_id) if item]

                    neighbors = pdf_col.get(ids=neighbor_ids)
                    neighbor_docs = {
                        nid: ndoc for nid, ndoc in
                        zip(neighbors['ids'], neighbors['documents'])
                    }

                    prev_text = neighbor_docs.get(prev_id, '') if prev_id else ''
                    next_text = neighbor_docs.get(next_id, '')

                    # 匹配段用标记包裹，agent 据此加粗展示
                    marked = f"[MATCH]{content}[/MATCH]"
                    context = ' '.join(filter(None, [prev_text, marked, next_text]))
                    result_item["context"] = context

                except Exception:
                    result_item["context"] = content  # 拼接失败退回原始

            formatted_results.append(result_item)

        return {
            "results": formatted_results,
            "query": query,
            "effective_query": effective_query,
            "filters": where if where else None
        }, 200

    except Exception as e:
        return error_payload(e), 500


@app.route('/search_papers', methods=['POST'])
def search_papers():
    """
    Query PDF source library (ChromaDB)
    Input: {
        "query": "...",
        "n": 3,
        "zotero_parent_key": "ABC12345",  // 推荐：覆盖主文+SI
        "paper_group": 1-6,               // 向后兼容
        "pdf_filename": "..."             // 向后兼容
    }
    """
    data = request.json or {}
    payload, status = search_papers_chroma(
        query=data.get('query', ''),
        n=data.get('n', 3),
        zotero_parent_key=data.get('zotero_parent_key'),
        paper_group=data.get('paper_group'),
        pdf_filename=data.get('pdf_filename'),
        second_query=data.get('second_query'),  # WF4：笔记结论的英文版
        include_context=data.get('include_context', False),
    )
    return jsonify(payload), status


@app.route('/write_query_log', methods=['POST'])
def write_query_log():
    """
    Write one session-level Markdown query log.
    """
    data = request.json or {}

    required_fields = [
        "workflow_id",
        "workflow_name",
        "status",
        "query",
        "anchor_query",
        "final_response_snapshot",
        "idempotency_key",
    ]
    missing = [field for field in required_fields if not data.get(field)]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    try:
        created_at = data.get("created_at") or iso_now()
        status = normalize_query_log_status(data.get("status"))
        workflow_id = ensure_nonempty_string(data.get("workflow_id"), "workflow_id")
        workflow_name = ensure_nonempty_string(data.get("workflow_name"), "workflow_name")
        query = ensure_nonempty_string(data.get("query"), "query")
        anchor_query = ensure_nonempty_string(data.get("anchor_query"), "anchor_query")
        final_response_snapshot = ensure_nonempty_string(
            data.get("final_response_snapshot"), "final_response_snapshot"
        )
        idempotency_key = ensure_nonempty_string(data.get("idempotency_key"), "idempotency_key")
        planned_angles = normalize_angle_list(ensure_nonempty_list(data.get("planned_angles"), "planned_angles"))
        executed_angles = normalize_angle_list(ensure_nonempty_list(data.get("executed_angles"), "executed_angles"))
        search_runs = ensure_nonempty_list(data.get("search_runs"), "search_runs")
        registry = load_query_log_registry()
        existing_entry = registry.get(idempotency_key)
        if existing_entry:
            existing_path = existing_entry.get("log_path")
            existing_log_id = existing_entry.get("log_id")
            if existing_path and os.path.exists(existing_path) and is_path_within_root(existing_path, QUERY_LOG_ROOT):
                return jsonify({
                    "success": True,
                    "created": False,
                    "deduplicated": True,
                    "log_id": existing_log_id,
                    "log_path": existing_path,
                    "month": existing_entry.get("month"),
                })

        short_id = data.get("short_id") or uuid4().hex[:4].upper()
        month_dir = ensure_query_log_month_dir(created_at)
        filename = build_query_log_filename(
            created_at=created_at,
            workflow_id=workflow_id,
            query=query,
            short_id=short_id,
        )
        log_path = os.path.join(month_dir, filename)
        # workflow_id / short_id flow into the filename unsanitized — refuse
        # anything that would escape QUERY_LOG_ROOT (path separators, "..").
        if filename != os.path.basename(filename) or not is_path_within_root(log_path, QUERY_LOG_ROOT):
            return jsonify({"error": "workflow_id/short_id produced an unsafe log filename"}), 400
        notes = data.get("notes") or []
        papers = data.get("papers") or []
        payload = {
            "log_id": data.get("log_id") or build_query_log_id(created_at, short_id),
            "idempotency_key": idempotency_key,
            "created_at": created_at,
            "month": datetime.fromisoformat(created_at).strftime("%Y-%m"),
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "workflow_reason": data.get("workflow_reason"),
            "status": status,
            "query": query,
            "query_title": data.get("query_title") or slugify_query_title(query),
            "session_summary_title": data.get("session_summary_title")
            or infer_session_summary_title(final_response_snapshot, query),
            "query_language": data.get("query_language") or detect_query_language(query),
            "anchor_query": anchor_query,
            "anchor_query_source": data.get("anchor_query_source", "original_user_query"),
            "saved_by": data.get("saved_by", "search-literature"),
            "planned_angles": planned_angles,
            "executed_angles": executed_angles,
            "expansion_reason": data.get("expansion_reason"),
            "stop_reason": data.get("stop_reason"),
            "search_runs": search_runs,
            "notes": notes,
            "papers": papers,
            "zotero_parent_keys": collect_zotero_parent_keys(
                notes, papers, data.get("zotero_parent_keys")
            ),
            "source_note_files": collect_source_note_files(
                notes, data.get("source_note_files")
            ),
            "effective_queries": data.get("effective_queries") or {},
            "second_queries": normalize_angle_list(data.get("second_queries")),
            "result_summary": data.get("result_summary") or "No result summary recorded.",
            "final_response_snapshot": final_response_snapshot,
            "log_path": log_path,
        }

        markdown = render_query_log_markdown(payload)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(markdown)
        registry[idempotency_key] = {
            "log_id": payload["log_id"],
            "log_path": log_path,
            "month": payload["month"],
            "created_at": payload["created_at"],
        }
        save_query_log_registry(registry)

        return jsonify({
            "success": True,
            "created": True,
            "deduplicated": False,
            "log_id": payload["log_id"],
            "log_path": log_path,
            "month": payload["month"],
        })

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify(error_payload(e)), 500


@app.route('/append_query_log_action', methods=['POST'])
def append_query_log_action():
    """
    Append a follow-up action block to an existing query log.
    """
    data = request.json or {}
    log_path = data.get("log_path")
    log_id = data.get("log_id")
    action = data.get("action")
    result = data.get("result")

    if not log_path or not log_id or not action or not result:
        return jsonify({"error": "Missing required fields: log_path, log_id, action, result"}), 400

    if not os.path.exists(log_path):
        return jsonify({"error": f"Log file not found: {log_path}"}), 404

    if not is_path_within_root(log_path, QUERY_LOG_ROOT):
        return jsonify({"error": "Target log_path is outside QUERY_LOG_ROOT"}), 400

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read()
        frontmatter, _ = parse_frontmatter(content)
        existing_log_id = frontmatter.get("log_id")
        if existing_log_id != log_id:
            return jsonify({
                "error": f"log_id mismatch: expected {existing_log_id}, got {log_id}"
            }), 400

        content = ensure_followup_header(content)
        block = render_followup_block({
            "timestamp": data.get("timestamp") or iso_now(),
            "action": action,
            "result": result,
            "details": data.get("details"),
        })

        with open(log_path, "w", encoding="utf-8") as f:
            f.write(content + block + "\n")

        return jsonify({"success": True, "log_path": log_path, "log_id": log_id})

    except Exception as e:
        return jsonify(error_payload(e)), 500


if __name__ == '__main__':
    print("=" * 50)
    print("Local Dual-Library RAG Query Service")
    print("=" * 50)
    print(f"ChromaDB: {CHROMA_PATH}")
    print(f"Papers: {COLLECTION_NAME} ({pdf_col.count() if chroma_ready else 'N/A'} chunks)")
    print(f"Notes:  {NOTES_COLLECTION_NAME} ({notes_col.count() if notes_ready else 'N/A'} notes)")
    print(f"Endpoint: http://{HOST}:{PORT}")
    print("=" * 50)
    print(f"Starting server on {HOST}:{PORT}...")
    app.run(host=HOST, port=PORT, debug=False)
