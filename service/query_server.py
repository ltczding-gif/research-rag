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
from time import perf_counter
from datetime import datetime
from pathlib import Path
from threading import RLock
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
    embedding_contract,
    embed_index_text,
)
from index_generation import GenerationStore, atomic_write_json, atomic_write_text
from generation_query import open_generation
from answer_workflow import prepare_packet, check_answer as check_answer_sources

app = Flask(__name__)

# Query-log persistence has a single-process writer contract.  This lock covers
# each full read/check/write transaction, including recovery.  Deployments with
# multiple worker processes must not share QUERY_LOG_ROOT without a future
# cross-process transaction layer.
_QUERY_LOG_WRITE_LOCK = RLock()

# Each server process pins its active generation at startup. Restart after a
# publication to adopt it; retained previous generations keep in-flight reads valid.
client = None
pdf_col = notes_col = ef = None
pdf_generation = notes_generation = None
chroma_ready = notes_ready = False
_dim_warnings = []
_index_errors = {}


def _load_collection(logical_name, *, papers=False):
    collection, reader = open_generation(client, CHROMA_PATH, logical_name, embedding_contract)
    if reader is not None:
        return collection, reader
    # Legacy compatibility is explicitly unverified: it cannot supply canonical
    # evidence or prove same-dimensional model identity.
    legacy = client.get_collection(
        name=logical_name,
        embedding_function=get_chromadb_embedding_function() if papers else None,
    )
    if legacy.count() <= 0:
        raise ValueError("Legacy collection is empty")
    mismatch, message = detect_dim_mismatch(legacy)
    if mismatch:
        _dim_warnings.append(message)
        raise ValueError(message)
    return legacy, None


if not SKIP_CHROMA_INIT:
    try:
        client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    except Exception as exc:
        _index_errors["client"] = str(exc)
    if client is not None:
        for _logical, _papers in ((COLLECTION_NAME, True), (NOTES_COLLECTION_NAME, False)):
            try:
                _collection, _reader = _load_collection(_logical, papers=_papers)
                if _papers:
                    pdf_col, pdf_generation, chroma_ready = _collection, _reader, True
                else:
                    notes_col, notes_generation, notes_ready = _collection, _reader, True
            except Exception as exc:
                _index_errors[_logical] = str(exc)


def index_state():
    """Distinguish a usable active index from the latest build attempt."""
    result = {}
    for name, logical, collection, reader, ready in (
        ("papers", COLLECTION_NAME, pdf_col, pdf_generation, chroma_ready),
        ("notes", NOTES_COLLECTION_NAME, notes_col, notes_generation, notes_ready),
    ):
        item = {"ready": bool(ready and collection is not None), "logical_collection": logical,
                "mode": "canonical" if reader else "legacy_unverified" if ready else "unavailable"}
        try:
            item["latest_attempt"] = GenerationStore(CHROMA_PATH, logical).latest_attempt()
            if item["ready"]:
                if reader:
                    reader.store.validate_artifacts(reader.manifest)
                    reader.check_embedding(embedding_contract)
                    if collection.count() != reader.manifest["item_count"]:
                        raise ValueError("Active collection count changed")
                    item.update(generation_id=reader.manifest["generation_id"],
                                collection=reader.manifest["collection_name"],
                                previous_generation=reader.manifest.get("previous_generation"))
                else:
                    item["collection"] = logical
                item["count"] = collection.count()
        except Exception as exc:
            item.update(ready=False, error=str(exc))
        if logical in _index_errors:
            item["error"] = _index_errors[logical]
        result[name] = item
    return result


def _query_vector(query, reader):
    if reader is None:
        return get_embedding(query)
    reader.check_embedding(embedding_contract)
    vector = embed_index_text(query)
    if len(vector) != reader.manifest["contract"]["embedding"]["dimensions"]:
        raise ValueError("Query vector dimension differs from the active generation")
    return vector


def _where_and(**values):
    filters = [{key: value} for key, value in values.items() if value is not None and value != ""]
    return {"$and": filters} if len(filters) > 1 else filters[0] if filters else None


def _with_timings(payload, started, embedding_seconds, retrieval_seconds):
    # Measures JSON preparation separately; actual stdio/HTTP wire latency is
    # measured by the client and must not be presented as this server duration.
    serialize_started = perf_counter()
    json.dumps(payload, ensure_ascii=False)
    payload["timings_seconds"] = {
        "query_embedding": embedding_seconds, "retrieval": retrieval_seconds,
        "rerank": 0.0, "serialization": perf_counter() - serialize_started,
        "total": perf_counter() - started,
    }
    return payload


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


class QueryLogRegistryError(RuntimeError):
    """Raised when registry state cannot be read or recovered safely."""


def load_query_log_registry(root=None):
    path = get_query_log_registry_path(root)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            registry = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QueryLogRegistryError(
            "Query-log registry is unreadable; refusing to discard idempotency state"
        ) from exc
    required_entry_fields = ("log_id", "log_path", "month", "created_at")
    if not isinstance(registry, dict) or any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(entry, dict)
        or any(
            not isinstance(entry.get(field), str) or not entry[field].strip()
            for field in required_entry_fields
        )
        for key, entry in registry.items()
    ):
        raise QueryLogRegistryError(
            "Query-log registry has an invalid structure; refusing to write"
        )
    return registry


def save_query_log_registry(registry, root=None):
    path = get_query_log_registry_path(root)
    atomic_write_json(path, registry)


def recover_query_log_registry_entry(idempotency_key, root=None):
    """Recover one orphaned Markdown log after a registry-write interruption."""
    active_root = Path(root or QUERY_LOG_ROOT)
    if not active_root.exists():
        return None
    matches = []
    try:
        candidates = tuple(active_root.rglob("*.md"))
    except OSError as exc:
        raise QueryLogRegistryError(
            "Could not scan query logs for idempotency recovery"
        ) from exc
    for path in candidates:
        if not is_path_within_root(path, active_root):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise QueryLogRegistryError(
                "Could not read query logs for idempotency recovery"
            ) from exc
        frontmatter, _ = parse_frontmatter(content)
        if str(frontmatter.get("idempotency_key") or "") != idempotency_key:
            continue
        log_id = str(frontmatter.get("log_id") or "").strip()
        created_at = str(frontmatter.get("created_at") or "").strip()
        month = str(frontmatter.get("month") or path.parent.name).strip()
        if not log_id or not created_at or not month:
            raise QueryLogRegistryError(
                "Matching orphaned query log has incomplete frontmatter"
            )
        matches.append({
            "log_id": log_id,
            "log_path": str(path.resolve()),
            "month": month,
            "created_at": created_at,
        })
    if len(matches) > 1:
        raise QueryLogRegistryError(
            "Multiple query logs use the same idempotency key; refusing to guess"
        )
    return matches[0] if matches else None


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
    """Search note sections, optionally keeping the best section per note."""
    if not notes_ready or notes_col is None:
        return {"error": "Notes collection not initialized. Run build_notes_db.py first."}
    if not query or not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return {"error": "query and positive integer n are required"}
    started = perf_counter()
    try:
        where = _where_and(zotero_parent_key=zotero_parent_key)
        embedding_started = perf_counter()
        query_emb = _query_vector(query, notes_generation)
        embedding_seconds = perf_counter() - embedding_started
        retrieval_started = perf_counter()
        # Sections can occupy several ranks. Fetch in increasing prefixes until
        # enough distinct notes are found, preserving the backend's ranking.
        count = notes_col.count()
        requested = min(count, limit)
        while True:
            results = notes_col.query(query_embeddings=[query_emb], n_results=max(1, requested), where=where)
            formatted, seen = [], set()
            for i, doc in enumerate(results["documents"][0]):
                meta = results["metadatas"][0][i]
                identity = meta.get("note_id", results["ids"][0][i])
                if dedupe and identity in seen:
                    continue
                seen.add(identity)
                if notes_generation:
                    full_note = notes_generation.full_note(meta)
                    if full_note[meta["start"]:meta["end"]] != doc:
                        raise ValueError("Note section differs from stored full note")
                    fm, _ = parse_frontmatter(full_note)
                else:
                    fm, _ = parse_frontmatter(doc)
                distance = results.get("distances", [[None] * len(results["ids"][0])])[0][i]
                metadata = {key: fm[key] for key in ("title_en", "title_zh", "year", "journal", "authors", "doi") if fm.get(key) is not None}
                metadata.update(meta)
                metadata.update(score=round(1 - distance, 4) if distance is not None else None,
                                note_rank=len(formatted) + 1)
                formatted.append({"id": results["ids"][0][i], "content": doc,
                                  "metadata": metadata, "distance": distance})
                if len(formatted) == limit:
                    break
            if (not dedupe or len(formatted) >= limit or requested >= count
                    or len(results["ids"][0]) < requested):
                break
            requested = min(count, max(requested + 1, requested * 2))
        payload = {"results": formatted, "filters": where,
                   "index_mode": "canonical" if notes_generation else "legacy_unverified"}
        return _with_timings(payload, started, embedding_seconds, perf_counter() - retrieval_started)
    except Exception as exc:
        return error_payload(exc)


@app.route('/health', methods=['GET'])
def health():
    states = index_state()
    embed_status = embedding_healthcheck()
    ready = any(item["ready"] for item in states.values()) and embed_status.get("ok", False)
    payload = {"status": "ok" if ready else "unavailable", **states, "embedding": embed_status}
    if _dim_warnings:
        payload["dim_mismatch"] = _dim_warnings
    return jsonify(payload), 200 if ready else 503


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
        where = _where_and(zotero_parent_key=zotero_parent_key,
                           source_file=os.path.basename(source) if source else None)
        # All sections of a matching note are collapsed into its original entity.
        results = notes_col.get(where=where)
        if not results["ids"]:
            return {"error": "笔记未找到"}, 404
        note_list, seen = [], set()
        for i in range(len(results["ids"])):
            doc = results["documents"][i]
            meta = results["metadatas"][i]
            identity = meta.get("note_id", results["ids"][i])
            if identity in seen:
                continue
            seen.add(identity)
            if notes_generation:
                doc = notes_generation.full_note(meta)
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
    for marker in ("_chunk_", "_c"):
        prefix, separator, number = hit_id.rpartition(marker)
        if separator and number.isdigit():
            # The stored ID is authoritative; retain chunk_index in the public
            # helper signature for callers of the older ordinal schema.
            index = int(number)
            previous = f"{prefix}{marker}{index - 1}" if index > 0 else None
            return previous, f"{prefix}{marker}{index + 1}"
    raise ValueError(f"unrecognized chunk id: {hit_id}")


def _same_legacy_source(hit: dict, neighbor: dict) -> bool:
    """Legacy ordinal prefixes can collide; require matching source metadata."""
    if not hit.get("pdf_path") or hit["pdf_path"] != neighbor.get("pdf_path"):
        return False
    return all(
        not hit.get(key) or not neighbor.get(key) or hit[key] == neighbor[key]
        for key in ("zotero_parent_key", "zotero_attachment_key", "source_sha256", "file_hash", "file_id")
    )


def search_papers_chroma(
    query, n=3, zotero_parent_key=None, paper_group=None, pdf_filename=None,
    second_query=None, include_context=False, zotero_attachment_key=None,
    source_role=None, source_type=None,
):
    """Search one pinned generation; all supplied source filters are ANDed."""
    if not chroma_ready or pdf_col is None:
        return {"error": "ChromaDB not initialized"}, 503
    if not query or not isinstance(n, int) or isinstance(n, bool) or n < 1:
        return {"error": "query and positive integer n are required"}, 400
    if source_role not in (None, "", "main", "si"):
        return {"error": "source_role must be main or si"}, 400
    if source_type not in (None, "", "pdf"):
        return {"error": "source_type must be pdf; use source_role for main/si"}, 400
    if pdf_generation and paper_group is not None:
        return {"error": "paper_group is a legacy ordinal; use zotero_parent_key for canonical indexes"}, 400
    if pdf_generation and zotero_attachment_key:
        known_sources = [source for source in pdf_generation.manifest["sources"]
                         if source.get("zotero_attachment_key") == zotero_attachment_key]
        constraints = {"zotero_parent_key": zotero_parent_key,
                       "source_role": source_role, "pdf_filename": pdf_filename}
        if known_sources and not any(
            all(not value or source.get(field) == value for field, value in constraints.items())
            for source in known_sources
        ):
            return {"error": "Attachment identity conflicts with the supplied source filters"}, 400
    effective_query = second_query or query
    where = _where_and(zotero_parent_key=zotero_parent_key, paper_group=paper_group,
                       pdf_filename=pdf_filename, zotero_attachment_key=zotero_attachment_key,
                       source_role=source_role, source_type=source_type)
    started = perf_counter()
    try:
        embedding_started = perf_counter()
        if pdf_generation:
            query_args = {"query_embeddings": [_query_vector(effective_query, pdf_generation)]}
        else:
            # Preserve the embedding function bound to old unverified collections.
            query_args = {"query_texts": [effective_query]}
        embedding_seconds = perf_counter() - embedding_started
        retrieval_started = perf_counter()
        results = pdf_col.query(**query_args, n_results=n, where=where)
        formatted_results = []
        for i, content in enumerate(results["documents"][0]):
            meta = results["metadatas"][0][i]
            hit_id = results["ids"][0][i]
            item = {"id": hit_id, "content": content, "metadata": meta,
                    "distance": results["distances"][0][i] if "distances" in results else None}
            if pdf_generation:
                item["evidence"] = pdf_generation.evidence(meta, content, hit_id)
            else:
                item["evidence"] = {"verified": False, "reason": "legacy index has no canonical provenance"}
            if include_context:
                if pdf_generation:
                    item.update(pdf_generation.context(meta, content, hit_id))
                else:
                    previous, following = _neighbor_chunk_ids(hit_id, meta.get("chunk_index", 0))
                    neighbor_ids = [identifier for identifier in (previous, following) if identifier]
                    neighbors = pdf_col.get(ids=neighbor_ids) if neighbor_ids else {"ids": [], "documents": []}
                    neighbor_docs = {
                        identifier: document
                        for identifier, document, neighbor_meta in zip(
                            neighbors["ids"], neighbors["documents"], neighbors.get("metadatas") or []
                        )
                        if _same_legacy_source(meta, neighbor_meta or {})
                    }
                    item["context"] = " ".join(filter(None, [neighbor_docs.get(previous, ""),
                                          f"[MATCH]{content}[/MATCH]", neighbor_docs.get(following, "")]))
            formatted_results.append(item)
        payload = {"results": formatted_results, "query": query, "effective_query": effective_query,
                   "filters": where, "index_mode": "canonical" if pdf_generation else "legacy_unverified"}
        if not pdf_generation:
            payload["timing_note"] = "Legacy embedding is included in retrieval; stages are not separated."
        return _with_timings(payload, started, embedding_seconds, perf_counter() - retrieval_started), 200
    except ValueError as exc:
        return error_payload(exc), 409
    except Exception as exc:
        return error_payload(exc), 500


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
        zotero_attachment_key=data.get('zotero_attachment_key'),
        source_role=data.get('source_role'),
        source_type=data.get('source_type'),
    )
    return jsonify(payload), status


def prepare_answer_payload(query, n=10, budget_codepoints=8000, zotero_parent_key=None,
                           second_query=None, zotero_attachment_key=None, source_role=None):
    """Search then prepare bounded canonical evidence for the host's answer model."""
    if not isinstance(query, str) or not query.strip():
        return {'error': 'query must be a nonempty string'}, 400
    if not pdf_generation:
        return {'error': 'Answer preparation requires a canonical PDF generation'}, 409
    if isinstance(budget_codepoints, bool) or not isinstance(budget_codepoints, int) or not 256 <= budget_codepoints <= 8000:
        return {'error': 'budget_codepoints must be an integer between 256 and 8000'}, 400
    payload, status = search_papers_chroma(query, n=n, zotero_parent_key=zotero_parent_key,
        second_query=second_query, zotero_attachment_key=zotero_attachment_key, source_role=source_role)
    if status != 200:
        return payload, status
    try:
        packet = prepare_packet(query, payload['results'], pdf_generation, budget_codepoints)
        packet['effective_query'] = payload['effective_query']
        return packet, 200
    except ValueError as exc:
        return error_payload(exc), 409


def check_answer_payload(packet, answer):
    if not pdf_generation:
        return {'error': 'Answer checking requires a canonical PDF generation'}, 409
    try:
        return check_answer_sources(packet, answer, pdf_generation), 200
    except (ValueError, KeyError, TypeError) as exc:
        return error_payload(exc), 400


@app.route('/prepare_answer', methods=['POST'])
def prepare_answer_route():
    data = request.get_json() or {}
    if not isinstance(data, dict):
        return jsonify({'error': 'JSON object required'}), 400
    payload, status = prepare_answer_payload(data.get('query', ''), n=data.get('n', 10),
        budget_codepoints=data.get('budget_codepoints', 8000),
        zotero_parent_key=data.get('zotero_parent_key'), second_query=data.get('second_query'),
        zotero_attachment_key=data.get('zotero_attachment_key'), source_role=data.get('source_role'))
    return jsonify(payload), status


@app.route('/check_answer', methods=['POST'])
def check_answer_route():
    data = request.get_json() or {}
    if not isinstance(data, dict):
        return jsonify({'error': 'JSON object required'}), 400
    payload, status = check_answer_payload(data.get('packet'), data.get('answer'))
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
        with _QUERY_LOG_WRITE_LOCK:
            registry = load_query_log_registry()
            existing_entry = registry.get(idempotency_key)
            if existing_entry:
                existing_path = existing_entry.get("log_path")
                existing_log_id = existing_entry.get("log_id")
                if (
                    existing_path
                    and existing_log_id
                    and os.path.exists(existing_path)
                    and is_path_within_root(existing_path, QUERY_LOG_ROOT)
                ):
                    return jsonify({
                        "success": True,
                        "created": False,
                        "deduplicated": True,
                        "log_id": existing_log_id,
                        "log_path": existing_path,
                        "month": existing_entry.get("month"),
                    })

            recovered_entry = recover_query_log_registry_entry(idempotency_key)
            if recovered_entry:
                registry[idempotency_key] = recovered_entry
                save_query_log_registry(registry)
                return jsonify({
                    "success": True,
                    "created": False,
                    "deduplicated": True,
                    "recovered": True,
                    "log_id": recovered_entry["log_id"],
                    "log_path": recovered_entry["log_path"],
                    "month": recovered_entry["month"],
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
            if os.path.exists(log_path):
                return jsonify({"error": "A different query log already owns this filename; use a different short_id"}), 409
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
            atomic_write_text(log_path, markdown)
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

    if not is_path_within_root(log_path, QUERY_LOG_ROOT):
        return jsonify({"error": "Target log_path is outside QUERY_LOG_ROOT"}), 400

    try:
        with _QUERY_LOG_WRITE_LOCK:
            if not os.path.exists(log_path):
                return jsonify({"error": f"Log file not found: {log_path}"}), 404
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
            atomic_write_text(log_path, content + block + "\n")

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
