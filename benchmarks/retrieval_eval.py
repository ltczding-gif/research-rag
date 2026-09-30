"""Retrieval evaluation against a canonical papers generation.

Gold evidence is pinned to canonical source coordinates (file hash, physical
page, page-text hash and a character interval), never to chunk IDs, so the
same eval set can score any chunker, embedding model or retrieval strategy
that serves the same extracted pages.

Every run is appended to a score ledger. Ledger records contain IDs, settings
and numbers only: no query text, source text, paths or parent keys. A private
eval set can therefore stay on the owner's machine while its scores are kept
and compared over time.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
import statistics
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LEDGER = REPO_ROOT / "benchmarks" / "results" / "retrieval-ledger.jsonl"
SUITE_SCHEMA = "retrieval-eval-suite-v1"
RUN_SCHEMA = "retrieval-eval-run-v1"
DEFAULT_KS = (5, 10, 20)
PRIMARY_METRIC = "span_coverage@10"
SCORED_RELEVANCE = 2
FILTER_FIELDS = ("zotero_parent_key", "zotero_attachment_key", "source_role", "pdf_filename")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA_RE = re.compile(r"^[a-f0-9]{64}$")

# Retrieval strategies: keyword arguments for query_server.search_papers_chroma
# and prepare_answer_payload. Each pins its mode and reranking explicitly so
# server-side defaults (LOCALRAG_RETRIEVAL_MODE, LOCALRAG_RERANK_DEFAULT) can
# never change what a strategy measures.
STRATEGIES: dict[str, dict[str, Any]] = {
    "dense": {"retrieval_mode": "dense", "rerank": False},
    "lexical": {"retrieval_mode": "lexical", "rerank": False},
    "hybrid": {"retrieval_mode": "hybrid", "rerank": False},
    "dense-rerank": {"retrieval_mode": "dense", "rerank": True},
    "hybrid-rerank": {"retrieval_mode": "hybrid", "rerank": True},
}


class SuiteError(ValueError):
    pass


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Eval-set model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceSpan:
    evidence_id: str
    group: str
    file_hash: str
    pdf_page_index: int
    page_text_hash: str
    char_start: int
    char_end: int
    relevance: int = 3
    zotero_parent_key: str | None = None

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.file_hash, self.pdf_page_index, self.page_text_hash)

    @property
    def scored(self) -> bool:
        return self.relevance >= SCORED_RELEVANCE


@dataclass
class EvalQuery:
    query_id: str
    text: str
    second_query: str | None = None
    filters: dict[str, str] = field(default_factory=dict)
    slices: list[str] = field(default_factory=list)
    relevant_parents: list[str] = field(default_factory=list)
    evidence: list[EvidenceSpan] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)

    @property
    def scored_evidence(self) -> list[EvidenceSpan]:
        return [span for span in self.evidence if span.scored]

    @property
    def groups(self) -> dict[str, list[EvidenceSpan]]:
        groups: dict[str, list[EvidenceSpan]] = {}
        for span in self.scored_evidence:
            groups.setdefault(span.group, []).append(span)
        return groups

    @property
    def parents(self) -> list[str]:
        parents = list(self.relevant_parents)
        for span in self.scored_evidence:
            if span.zotero_parent_key and span.zotero_parent_key not in parents:
                parents.append(span.zotero_parent_key)
        return parents


@dataclass
class Suite:
    suite_id: str
    sha256: str
    queries: list[EvalQuery]
    source: str

    @property
    def evidence_count(self) -> int:
        return sum(len(query.scored_evidence) for query in self.queries)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SuiteError(message)


def _parse_span(raw: dict, query_id: str, line: int) -> EvidenceSpan | dict:
    where = f"line {line}, query {query_id}"
    _require(isinstance(raw, dict), f"{where}: evidence entries must be objects")
    evidence_id = raw.get("evidence_id")
    _require(isinstance(evidence_id, str) and bool(_ID_RE.match(evidence_id)),
             f"{where}: evidence_id must be a simple identifier")
    relevance = raw.get("relevance", 3)
    _require(isinstance(relevance, int) and not isinstance(relevance, bool) and 0 <= relevance <= 3,
             f"{where}, {evidence_id}: relevance must be an integer 0-3")
    coordinates = ("file_hash", "pdf_page_index", "page_text_hash", "char_start", "char_end")
    if not all(key in raw for key in coordinates):
        # Quote-only annotation: resolved to coordinates by `resolve`.
        _require(isinstance(raw.get("quote"), str) and raw["quote"].strip(),
                 f"{where}, {evidence_id}: give canonical coordinates or a quote to resolve")
        return dict(raw)
    _require(isinstance(raw["file_hash"], str) and bool(_SHA_RE.match(raw["file_hash"])),
             f"{where}, {evidence_id}: file_hash must be a lowercase sha256")
    _require(isinstance(raw["page_text_hash"], str) and bool(_SHA_RE.match(raw["page_text_hash"])),
             f"{where}, {evidence_id}: page_text_hash must be a lowercase sha256")
    start, end, page = raw["char_start"], raw["char_end"], raw["pdf_page_index"]
    _require(all(isinstance(v, int) and not isinstance(v, bool) for v in (start, end, page))
             and 0 <= start < end and page >= 0,
             f"{where}, {evidence_id}: invalid page or character interval")
    group = raw.get("group", evidence_id)
    _require(isinstance(group, str) and bool(group), f"{where}, {evidence_id}: group must be a string")
    parent = raw.get("zotero_parent_key")
    return EvidenceSpan(evidence_id, group, raw["file_hash"], page, raw["page_text_hash"],
                        start, end, relevance, parent if isinstance(parent, str) and parent else None)


def parse_query(raw: dict, line: int = 0) -> EvalQuery:
    _require(isinstance(raw, dict), f"line {line}: each line must be a JSON object")
    query_id = raw.get("query_id")
    _require(isinstance(query_id, str) and bool(_ID_RE.match(query_id)),
             f"line {line}: query_id must be a simple identifier")
    text = raw.get("text")
    _require(isinstance(text, str) and bool(text.strip()), f"line {line}, {query_id}: text is required")
    filters = raw.get("filters") or {}
    _require(isinstance(filters, dict) and set(filters) <= set(FILTER_FIELDS)
             and all(isinstance(v, str) and v for v in filters.values()),
             f"line {line}, {query_id}: filters may only contain {', '.join(FILTER_FIELDS)}")
    query = EvalQuery(query_id=query_id, text=text, second_query=raw.get("second_query") or None,
                      filters=dict(filters), slices=list(raw.get("slices") or []),
                      relevant_parents=list(raw.get("relevant_parents") or []),
                      candidates=list(raw.get("candidates") or []))
    seen = set()
    for item in raw.get("evidence") or []:
        parsed = _parse_span(item, query_id, line)
        identifier = parsed.evidence_id if isinstance(parsed, EvidenceSpan) else parsed["evidence_id"]
        _require(identifier not in seen, f"line {line}, {query_id}: duplicate evidence_id {identifier}")
        seen.add(identifier)
        if isinstance(parsed, EvidenceSpan):
            query.evidence.append(parsed)
        else:
            query.unresolved.append(parsed)
    return query


def load_suite(path: str | Path, suite_id: str | None = None) -> Suite:
    """Load a JSONL eval set: one query per line (see docs/RETRIEVAL_EVAL.md)."""
    path = Path(path)
    raw = path.read_bytes()
    queries, seen = [], set()
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SuiteError(f"line {line_number}: invalid JSON ({exc.msg})") from exc
        query = parse_query(record, line_number)
        _require(query.query_id not in seen, f"line {line_number}: duplicate query_id {query.query_id}")
        seen.add(query.query_id)
        queries.append(query)
    _require(bool(queries), f"{path}: no queries")
    identifier = suite_id or re.sub(r"[^a-z0-9._-]+", "-", path.stem.lower()).strip("-") or "suite"
    return Suite(identifier, hashlib.sha256(raw).hexdigest(), queries, source="jsonl")


def dump_query(query: EvalQuery) -> dict:
    record: dict[str, Any] = {"query_id": query.query_id, "text": query.text}
    if query.second_query:
        record["second_query"] = query.second_query
    for name in ("filters", "slices", "relevant_parents"):
        value = getattr(query, name)
        if value:
            record[name] = value
    evidence = []
    for span in query.evidence:
        item = {"evidence_id": span.evidence_id, "group": span.group, "relevance": span.relevance,
                "file_hash": span.file_hash, "pdf_page_index": span.pdf_page_index,
                "page_text_hash": span.page_text_hash, "char_start": span.char_start,
                "char_end": span.char_end}
        if span.zotero_parent_key:
            item["zotero_parent_key"] = span.zotero_parent_key
        evidence.append(item)
    evidence.extend(query.unresolved)
    if evidence:
        record["evidence"] = evidence
    if query.candidates:
        record["candidates"] = query.candidates
    return record


def load_official_suite(benchmark_root: str | Path, suite_id: str) -> Suite:
    """Adapt the official benchmark ledgers (queries, evidence units, qrels)."""
    import yaml

    root = Path(benchmark_root)
    suite_def = yaml.safe_load((root / "suites" / f"{suite_id}.yaml").read_text(encoding="utf-8"))
    files = {}
    for line in (root / "corpus" / "manifest.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            paper = json.loads(line)
            for item in [paper["main_pdf"], *paper.get("si", [])]:
                files[item["file_id"]] = item["sha256"]

    def rows(relative):
        path = root / relative
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    units = {unit["evidence_id"]: unit for unit in rows("gold/evidence_units.jsonl")}
    qrels: dict[str, list[dict]] = {}
    for qrel in rows("queries/evidence_qrels.jsonl"):
        qrels.setdefault(qrel["query_id"], []).append(qrel)
    wanted = set(suite_def.get("query_ids") or [])
    digest = hashlib.sha256()
    queries = []
    for record in rows("queries/queries.jsonl"):
        if record["query_id"] not in wanted:
            continue
        digest.update(json.dumps(record, sort_keys=True).encode())
        query = EvalQuery(query_id=record["query_id"], text=record["text"],
                          slices=list(record.get("slice_ids", [])))
        for qrel in sorted(qrels.get(record["query_id"], []), key=lambda q: q["evidence_id"]):
            unit = units[qrel["evidence_id"]]
            locator = unit["locator"]
            if "char_start" not in locator:
                continue  # bbox locators are not scorable against text spans
            digest.update(json.dumps([qrel, unit], sort_keys=True).encode())
            query.evidence.append(EvidenceSpan(
                unit["evidence_id"], unit["evidence_group_id"], files[unit["file_id"]],
                unit["pdf_page_index"], unit["canonical_page_hash"], locator["char_start"],
                locator["char_end"], qrel["relevance"]))
        queries.append(query)
    return Suite(suite_id, digest.hexdigest(), queries, source="official")


# --------------------------------------------------------------------------
# Canonical pages of the active generation
# --------------------------------------------------------------------------


def load_pages(reader, wanted: set[tuple[str, int]] | None = None) -> dict[tuple[str, int], dict]:
    """Canonical pages keyed by (file_hash, pdf_page_index)."""
    path = reader.artifact(reader.manifest["artifacts"]["pages"])
    pages = {}
    if wanted is not None and not wanted:
        return pages
    with path.open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            page = json.loads(line)
            key = (page["file_hash"], page["pdf_page_index"])
            if wanted is None or key in wanted:
                pages[key] = page
                if wanted is not None and len(pages) == len(wanted):
                    break
    return pages


def check_scorable(suite: Suite, pages: dict) -> list[dict]:
    """Evidence whose canonical page is absent or re-extracted differently."""
    problems = []
    for query in suite.queries:
        for span in query.scored_evidence:
            page = pages.get((span.file_hash, span.pdf_page_index))
            if page is None:
                reason = "page not in active generation"
            elif page["page_text_hash"] != span.page_text_hash:
                reason = "canonical page text changed"
            elif span.char_end > len(page["normalized_text"]):
                reason = "interval exceeds page text"
            else:
                continue
            problems.append({"query_id": query.query_id, "evidence_id": span.evidence_id, "reason": reason})
    return problems


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


Interval = tuple[tuple[str, int, str], int, int]


def evidence_intervals(evidence: dict) -> list[Interval]:
    """Canonical intervals of one verified hit or packet entry."""
    if not evidence or not evidence.get("verified"):
        return []
    file_hash = evidence["file_hash"]
    return [((file_hash, segment["pdf_page_index"], segment["page_text_hash"]),
             segment["char_start_in_normalized_page"], segment["char_end_in_normalized_page"])
            for segment in evidence.get("segments", [])]


def _merged(intervals: Iterable[Interval], key) -> list[tuple[int, int]]:
    ranges = sorted((start, end) for k, start, end in intervals if k == key)
    merged: list[list[int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def span_covered(span: EvidenceSpan, intervals: list[Interval]) -> bool:
    """True when the union of returned intervals contains the whole gold span."""
    return any(start <= span.char_start and span.char_end <= end
               for start, end in _merged(intervals, span.key))


def span_touched(span: EvidenceSpan, intervals: list[Interval]) -> bool:
    return any(key == span.key and start < span.char_end and span.char_start < end
               for key, start, end in intervals)


def score_query(query: EvalQuery, hits: list[dict], ks: Iterable[int],
                packet: dict | None = None) -> dict[str, float | int | None]:
    """Per-query metrics; spans below SCORED_RELEVANCE are ignored."""
    spans = query.scored_evidence
    groups = query.groups
    parents = query.parents
    per_hit = [evidence_intervals(hit.get("evidence") or {}) for hit in hits]
    result: dict[str, float | int | None] = {"evidence_spans": len(spans), "evidence_groups": len(groups)}
    for k in ks:
        top = [interval for hit in per_hit[:k] for interval in hit]
        covered = {span.evidence_id for span in spans if span_covered(span, top)}
        touched = {span.evidence_id for span in spans if span_touched(span, top)}
        groups_met = sum(1 for members in groups.values()
                         if any(span.evidence_id in covered for span in members))
        result[f"spans_covered@{k}"] = len(covered)
        result[f"spans_touched@{k}"] = len(touched)
        result[f"groups_covered@{k}"] = groups_met
        result[f"span_coverage@{k}"] = len(covered) / len(spans) if spans else None
        result[f"query_complete@{k}"] = (1.0 if groups_met == len(groups) else 0.0) if groups else None
        if parents:
            found = {hit.get("metadata", {}).get("zotero_parent_key") for hit in hits[:k]}
            result[f"doc_recall@{k}"] = sum(1 for parent in parents if parent in found) / len(parents)
        else:
            result[f"doc_recall@{k}"] = None
    first = None
    for rank, intervals in enumerate(per_hit, 1):
        if any(span_touched(span, intervals) for span in spans):
            first = rank
            break
    result["first_evidence_rank"] = first
    result["reciprocal_rank"] = (1.0 / first if first else 0.0) if spans else None
    if packet is not None:
        intervals = [i for item in packet.get("evidence", []) for i in evidence_intervals(item.get("source") or {})]
        covered = {span.evidence_id for span in spans if span_covered(span, intervals)}
        groups_met = sum(1 for members in groups.values()
                         if any(span.evidence_id in covered for span in members))
        result["packet_spans_covered"] = len(covered)
        result["packet_span_coverage"] = len(covered) / len(spans) if spans else None
        result["packet_complete"] = (1.0 if groups_met == len(groups) else 0.0) if groups else None
        result["packet_used_codepoints"] = packet.get("used_codepoints")
    return result


def _mean(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return round(sum(present) / len(present), 6) if present else None


def aggregate(per_query: dict[str, dict], ks: Iterable[int], packet: bool) -> dict[str, float | None]:
    """Micro span coverage (the headline metric) plus query-level means."""
    rows = list(per_query.values())
    spans = sum(row["evidence_spans"] for row in rows)
    groups = sum(row["evidence_groups"] for row in rows)
    metrics: dict[str, float | None] = {}
    for k in ks:
        metrics[f"span_coverage@{k}"] = round(sum(r[f"spans_covered@{k}"] for r in rows) / spans, 6) if spans else None
        metrics[f"span_touch@{k}"] = round(sum(r[f"spans_touched@{k}"] for r in rows) / spans, 6) if spans else None
        metrics[f"group_coverage@{k}"] = round(sum(r[f"groups_covered@{k}"] for r in rows) / groups, 6) if groups else None
        metrics[f"span_coverage_macro@{k}"] = _mean(r[f"span_coverage@{k}"] for r in rows)
        metrics[f"query_complete@{k}"] = _mean(r[f"query_complete@{k}"] for r in rows)
        metrics[f"doc_recall@{k}"] = _mean(r[f"doc_recall@{k}"] for r in rows)
    metrics["mrr"] = _mean(r["reciprocal_rank"] for r in rows)
    if packet:
        metrics["packet_span_coverage"] = (round(sum(r["packet_spans_covered"] for r in rows) / spans, 6)
                                           if spans else None)
        metrics["packet_complete"] = _mean(r["packet_complete"] for r in rows)
        metrics["packet_used_codepoints_mean"] = _mean(r["packet_used_codepoints"] for r in rows)
    return metrics


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(fraction * len(ordered)) - 1)], 4)


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


def git_state(root: Path = REPO_ROOT) -> dict:
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                                text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root,
                                    capture_output=True, text=True, check=True).stdout.strip())
        return {"commit": commit, "dirty": dirty}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": "unknown", "dirty": None}


def index_fingerprint(core) -> dict:
    reader = core.pdf_generation
    if reader is None:
        raise SuiteError("Retrieval evaluation requires a canonical papers generation")
    embedding = reader.manifest["contract"]["embedding"]
    return {
        "papers_generation_id": reader.manifest["generation_id"],
        "papers_item_count": reader.manifest["item_count"],
        "embedding": {key: embedding.get(key) for key in ("provider", "model", "revision", "dimensions")},
    }


def evidence_fingerprint(suite: Suite, excluded: set[tuple[str, str]]) -> str:
    """Identity of the exact gold spans a run scored; runs compare only when equal."""
    items = sorted((query.query_id, span.evidence_id, span.file_hash, span.pdf_page_index,
                    span.page_text_hash, span.char_start, span.char_end, span.group)
                   for query in suite.queries for span in query.scored_evidence
                   if (query.query_id, span.evidence_id) not in excluded)
    return hashlib.sha256(json.dumps(items).encode("utf-8")).hexdigest()


VECTORS_SCHEMA = "retrieval-eval-query-vectors-v1"


def effective_text(query: EvalQuery) -> str:
    """The text the server embeds: second_query when given, else the question."""
    return query.second_query or query.text


def embed_queries(suite: Suite, embed: Callable[[str], list], contract: Callable[..., dict]) -> dict:
    """Embed every query once, bound to the provider's embedding contract.

    Lets a machine that cannot hold the embedding model and the index at the
    same time compute query vectors first, then score retrieval separately.
    """
    vectors = {}
    for query in suite.queries:
        text = effective_text(query)
        vector = [float(value) for value in embed(text)]
        if not vector or not all(math.isfinite(value) for value in vector):
            raise SuiteError(f"{query.query_id}: embedding provider returned an invalid vector")
        vectors[query.query_id] = {"text_sha256": sha256_text(text), "vector": vector}
    dimensions = {len(item["vector"]) for item in vectors.values()}
    if len(dimensions) != 1:
        raise SuiteError("Query vectors have inconsistent dimensions")
    return {"schema": VECTORS_SCHEMA, "suite_id": suite.suite_id, "suite_sha256": suite.sha256,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "embedding": contract(dimensions=dimensions.pop()), "vectors": vectors}


def load_query_vectors(path: str | Path, suite: Suite, reader) -> tuple[dict[str, list], str]:
    """Validate precomputed vectors against the eval set and the active generation."""
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    _require(data.get("schema") == VECTORS_SCHEMA, f"{path}: not a {VECTORS_SCHEMA} file")
    _require(data.get("suite_sha256") == suite.sha256,
             f"{path}: vectors were computed for a different version of the eval set")
    expected = reader.manifest["contract"]["embedding"]
    _require(data.get("embedding") == expected,
             f"{path}: embedding contract differs from the active generation's; "
             "recompute the vectors with the model that built this index")
    vectors = {}
    for query in suite.queries:
        item = data.get("vectors", {}).get(query.query_id)
        _require(item is not None, f"{path}: no vector for {query.query_id}")
        _require(item.get("text_sha256") == sha256_text(effective_text(query)),
                 f"{path}: {query.query_id} text changed since its vector was computed")
        _require(len(item["vector"]) == expected["dimensions"], f"{path}: {query.query_id} has wrong dimensions")
        vectors[query.query_id] = item["vector"]
    return vectors, hashlib.sha256(raw).hexdigest()


def first_covered_ranks(query: EvalQuery, hits: list[dict]) -> list[int | None]:
    """Per scored span, the smallest k at which the top-k hits fully cover it."""
    ranks = []
    for span in query.scored_evidence:
        seen: list[Interval] = []
        found = None
        for rank, hit in enumerate(hits, 1):
            seen.extend(evidence_intervals(hit.get("evidence") or {}))
            if span_covered(span, seen):
                found = rank
                break
        ranks.append(found)
    return sorted(ranks, key=lambda r: (r is None, r or 0))


def run_strategy(core, suite: Suite, strategy: str, *, ks=DEFAULT_KS, repetitions: int = 1,
                 packet_budget: int | None = None, allow_unscorable: bool = False,
                 query_vectors: dict[str, list] | None = None, query_vectors_sha256: str | None = None,
                 diagnostic_depth: int | None = None,
                 clock: Callable[[], float] = time.perf_counter,
                 progress: Callable[[str], None] | None = None) -> dict:
    """Evaluate one strategy on every query; returns a ledger-ready record.

    Gold spans that the active generation cannot score (page missing or
    re-extracted) fail the run unless allow_unscorable is set; then they are
    excluded and the run's evidence fingerprint changes, so `compare` refuses
    to pair it with runs that scored a different evidence set.

    query_vectors (from load_query_vectors) replace provider calls; latency is
    then retrieval-only. diagnostic_depth adds a separate, deeper search that
    records the rank at which each gold span is first covered; it never
    changes the metrics at the requested k.
    """
    if strategy not in STRATEGIES:
        raise SuiteError(f"Unknown strategy {strategy!r}; known: {', '.join(sorted(STRATEGIES))}")
    unresolved = [q.query_id for q in suite.queries if q.unresolved]
    if unresolved:
        raise SuiteError("Resolve quote-only evidence first (retrieval_eval.py resolve): "
                         + ", ".join(unresolved[:5]))
    ks = tuple(sorted(set(ks)))
    depth = max(ks)
    params = STRATEGIES[strategy]
    if not suite.queries:
        raise SuiteError(f"Eval set {suite.suite_id!r} has no queries")
    wanted = {(span.file_hash, span.pdf_page_index)
              for query in suite.queries for span in query.scored_evidence}
    pages = load_pages(core.pdf_generation, wanted)
    unscorable = check_scorable(suite, pages)
    unscorable_ids = {(item["query_id"], item["evidence_id"]) for item in unscorable}
    if unscorable and not allow_unscorable:
        sample = ", ".join(f"{i['query_id']}/{i['evidence_id']} ({i['reason']})" for i in unscorable[:5])
        raise SuiteError(f"{len(unscorable)} gold spans are unscorable against this generation: {sample}. "
                         "Re-resolve them against the current extraction, or pass --allow-unscorable "
                         "(such runs are not comparable with runs that scored the full set).")
    if suite.evidence_count - len(unscorable) <= 0:
        raise SuiteError(f"Eval set {suite.suite_id!r} has no scorable gold evidence; nothing to measure")
    per_query, latencies, stability = {}, [], {"membership_identical": 0, "order_identical": 0}
    slices: dict[str, list[str]] = {}
    for index, query in enumerate(suite.queries, 1):
        scored = EvalQuery(**{**query.__dict__, "evidence": [
            span for span in query.evidence if (query.query_id, span.evidence_id) not in unscorable_ids]})
        vector = {"query_vector": query_vectors[query.query_id]} if query_vectors else {}
        runs = []
        for _ in range(max(1, repetitions)):
            started = clock()
            payload, status = core.search_papers_chroma(
                query=query.text, n=depth, second_query=query.second_query,
                include_context=False, **query.filters, **params, **vector)
            latencies.append(clock() - started)
            if status != 200:
                raise SuiteError(f"{query.query_id}: search failed ({status}): {payload.get('error')}")
            runs.append(payload["results"])
        packet = None
        if packet_budget:
            # Same filters as retrieval: packet and search scores share one scope.
            packet, status = core.prepare_answer_payload(
                query.text, n=10, budget_codepoints=packet_budget, second_query=query.second_query,
                **query.filters, **params, **vector)
            if status != 200:
                raise SuiteError(f"{query.query_id}: prepare_answer failed ({status}): {packet.get('error')}")
        per_query[query.query_id] = score_query(scored, runs[0], ks, packet)
        if diagnostic_depth and diagnostic_depth > depth:
            # Pin every pool the scored search used, so the deeper list extends
            # the scored ranking instead of re-ranking a larger pool.
            stage1 = core._rerank_pool_depth(depth) if params.get("rerank") else depth
            diagnostic_pool = {}
            if params.get("retrieval_mode") == "hybrid":
                diagnostic_pool["candidate_pool_depth"] = core._candidate_depth(stage1)
            if params.get("rerank"):
                diagnostic_pool["rerank_pool_depth"] = stage1
            deep, status = core.search_papers_chroma(
                query=query.text, n=diagnostic_depth, second_query=query.second_query,
                include_context=False, **query.filters, **params, **vector, **diagnostic_pool)
            if status != 200:
                raise SuiteError(f"{query.query_id}: diagnostic search failed ({status}): {deep.get('error')}")
            scored_ids = [hit["id"] for hit in runs[0]]
            if [hit["id"] for hit in deep["results"][:len(scored_ids)]] != scored_ids:
                raise SuiteError(f"{query.query_id}: diagnostic ranking diverges from the scored top-{depth}")
            ranks = first_covered_ranks(scored, deep["results"])
            per_query[query.query_id]["span_first_covered_ranks"] = ranks
            per_query[query.query_id]["diagnostic_results"] = len(deep["results"])
        ids = [[hit["id"] for hit in result] for result in runs]
        stability["membership_identical"] += all(set(i) == set(ids[0]) for i in ids)
        stability["order_identical"] += all(i == ids[0] for i in ids)
        for name in query.slices:
            slices.setdefault(name, []).append(query.query_id)
        if progress:
            progress(f"[{strategy}] {index}/{len(suite.queries)} {query.query_id}")
    overall = aggregate(per_query, ks, bool(packet_budget))
    diagnostic_returned = None
    if diagnostic_depth and diagnostic_depth > depth:
        scored_total = sum(row["evidence_spans"] for row in per_query.values())
        found = [rank for row in per_query.values() for rank in row.get("span_first_covered_ranks", [])]
        # Pinned pools (hybrid candidates, rerank pool) can return fewer hits than
        # requested, so this is coverage of the diagnostic list actually returned,
        # not "coverage@depth".
        overall["diagnostic_span_coverage"] = round(
            sum(r is not None for r in found) / scored_total, 6) if scored_total else None
        present = sorted(r for r in found if r is not None)
        overall["covered_rank_median"] = present[len(present) // 2] if present else None
        returned = [row["diagnostic_results"] for row in per_query.values() if "diagnostic_results" in row]
        diagnostic_returned = {"min": min(returned), "max": max(returned)} if returned else None
    metrics = {"overall": overall,
               "by_slice": {name: aggregate({q: per_query[q] for q in ids}, ks, bool(packet_budget))
                            for name, ids in sorted(slices.items())}}
    created = datetime.now(timezone.utc)
    short = hashlib.sha256(f"{created.isoformat()}{strategy}{suite.sha256}".encode()).hexdigest()[:6]
    first_calls = latencies[::max(1, repetitions)]
    return {
        "schema": RUN_SCHEMA,
        "run_id": f"{suite.suite_id}-{strategy}-{created.strftime('%Y%m%dT%H%M%SZ')}-{short}",
        "created_at": created.isoformat(timespec="seconds"),
        "git": git_state(),
        "suite": {"suite_id": suite.suite_id, "sha256": suite.sha256, "source": suite.source,
                  "queries": len(suite.queries), "evidence_spans": suite.evidence_count,
                  "scored_spans": suite.evidence_count - len(unscorable),
                  "unscorable_spans": len(unscorable),
                  "evidence_fingerprint": evidence_fingerprint(suite, unscorable_ids)},
        "index": index_fingerprint(core),
        "strategy": {"name": strategy, "params": params, "depth": depth,
                     "reranker": ({key: value for key, value in core.reranker.identity().items()
                                   if key != "endpoint"} if params.get("rerank") else None)},
        "settings": {"ks": list(ks), "repetitions": max(1, repetitions), "packet_budget": packet_budget,
                     "primary_metric": PRIMARY_METRIC,
                     "diagnostic_depth": diagnostic_depth if diagnostic_depth and diagnostic_depth > depth else None,
                     "diagnostic_results_returned": diagnostic_returned},
        "query_vectors": ({"precomputed": True, "sha256": query_vectors_sha256}
                          if query_vectors else {"precomputed": False}),
        "metrics": metrics,
        "latency_scope": "retrieval_only" if query_vectors else "end_to_end",
        "latency_seconds": {"p50": _percentile(latencies, 0.5), "p95": _percentile(latencies, 0.95),
                            "max": round(max(latencies), 4) if latencies else None,
                            "first_query": round(first_calls[0], 4) if first_calls else None},
        "stability": {**stability, "queries": len(suite.queries)},
        "unscorable": unscorable,
        "per_query": {qid: {key: value for key, value in row.items()
                            if key.startswith(("span_coverage@", "query_complete@", "packet_span_coverage",
                                               "packet_complete", "first_evidence_rank", "doc_recall@",
                                               "span_first_covered_ranks", "evidence_spans",
                                               "spans_covered@", "packet_spans_covered",
                                               "diagnostic_results"))}
                      for qid, row in per_query.items()},
    }


# --------------------------------------------------------------------------
# Ledger and comparison
# --------------------------------------------------------------------------


def append_ledger(record: dict, path: str | Path = DEFAULT_LEDGER) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def read_ledger(path: str | Path = DEFAULT_LEDGER) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def comparable(baseline: dict, candidate: dict) -> str | None:
    """Reason two runs cannot be paired, or None when they scored identical gold."""
    if baseline["suite"].get("sha256") != candidate["suite"].get("sha256"):
        return "different eval-set version"
    before = baseline["suite"].get("evidence_fingerprint")
    after = candidate["suite"].get("evidence_fingerprint")
    if not before or not after or before != after:
        return "different scored evidence"
    return None


def _span_counts(row: dict, metric: str) -> tuple[int, int] | None:
    """(covered, scored) spans of one query for a span-coverage metric, if recorded.

    Older records lack explicit counts; runs with diagnostic ranks still carry
    them implicitly, because ranks are prefix-consistent with the scored list.
    """
    ranks = row.get("span_first_covered_ranks")
    total = row.get("evidence_spans", len(ranks) if ranks is not None else None)
    if total is None:
        return None
    if metric == "packet_span_coverage":
        covered = row.get("packet_spans_covered")
    elif metric.startswith("span_coverage@"):
        k = int(metric.split("@", 1)[1])
        covered = row.get(f"spans_covered@{k}")
        if covered is None and ranks is not None:
            covered = sum(1 for rank in ranks if rank is not None and rank <= k)
    else:
        return None
    return None if covered is None else (int(covered), int(total))


def paired_delta(baseline: dict, candidate: dict, metric: str = PRIMARY_METRIC, *,
                 resamples: int = 2000, seed: int = 0) -> dict:
    """Paired comparison on the same questions, averaged the way the metric is.

    Span-coverage metrics are micro-averaged over gold spans, so their delta is
    the change in total covered spans / total spans, with a cluster bootstrap
    that resamples questions (spans of one question move together). Wins and
    losses count questions that gained or lost covered spans. The per-question
    mean is reported separately as mean_question_delta and never replaces it.
    Other metrics (e.g. query_complete@k) are per-question means. Runs that
    scored different gold evidence are never paired.
    """
    reason = comparable(baseline, candidate)
    if reason:
        return {"metric": metric, "queries": 0, "not_comparable": reason}
    shared = sorted(set(baseline["per_query"]) & set(candidate["per_query"]))
    rng = random.Random(seed)

    def interval(values: list[float]) -> list[float]:
        values = sorted(values)
        return [round(values[int(0.025 * resamples)], 6), round(values[int(0.975 * resamples) - 1], 6)]

    question_deltas = []
    for query_id in shared:
        before = baseline["per_query"][query_id].get(metric)
        after = candidate["per_query"][query_id].get(metric)
        if before is not None and after is not None:
            question_deltas.append(after - before)
    counts = [(_span_counts(baseline["per_query"][q], metric), _span_counts(candidate["per_query"][q], metric))
              for q in shared]
    counts = [(a, b) for a, b in counts if not (a is None and b is None)]
    if counts and all(a is not None and b is not None and a[1] == b[1] for a, b in counts):
        rows = [(b[0] - a[0], a[1]) for a, b in counts if a[1] > 0]
        if not rows:
            return {"metric": metric, "queries": 0}
        spans = sum(total for _, total in rows)
        samples = []
        for _ in range(resamples):
            draw = rng.choices(rows, k=len(rows))
            drawn_spans = sum(total for _, total in draw)
            samples.append(sum(gain for gain, _ in draw) / drawn_spans if drawn_spans else 0.0)
        gains = [gain for gain, _ in rows]
        return {"metric": metric, "averaging": "spans", "queries": len(rows), "spans": spans,
                "delta": round(sum(gains) / spans, 6), "delta_spans": sum(gains), "ci95": interval(samples),
                "wins": sum(g > 0 for g in gains), "losses": sum(g < 0 for g in gains),
                "ties": sum(g == 0 for g in gains),
                "mean_question_delta": round(statistics.fmean(question_deltas), 6) if question_deltas else None}
    if metric.startswith("span_coverage@") or metric == "packet_span_coverage":
        return {"metric": metric, "queries": 0,
                "not_comparable": "records lack per-question span counts; rerun with this harness"}
    if not question_deltas:
        return {"metric": metric, "queries": 0}
    samples = [statistics.fmean(rng.choices(question_deltas, k=len(question_deltas))) for _ in range(resamples)]
    return {"metric": metric, "averaging": "questions", "queries": len(question_deltas),
            "delta": round(statistics.fmean(question_deltas), 6), "ci95": interval(samples),
            "wins": sum(d > 0 for d in question_deltas), "losses": sum(d < 0 for d in question_deltas),
            "ties": sum(d == 0 for d in question_deltas)}


def compare_table(records: list[dict], metrics: list[str], baseline_id: str | None = None,
                  primary: str = PRIMARY_METRIC) -> str:
    """Markdown table of runs on one suite, with paired deltas against a baseline."""
    if not records:
        return "No runs recorded for this suite."
    baseline = next((r for r in records if r["run_id"] == baseline_id), records[0])
    lines = []
    hashes = {r["suite"]["sha256"] for r in records}
    if len(hashes) > 1:
        lines.append("> Warning: these runs used different versions of the eval set; "
                     "deltas across versions are not comparable.\n")
    if any(r["suite"].get("unscorable_spans") for r in records):
        lines.append("> Warning: some runs excluded unscorable gold spans; they are paired only with "
                     "runs that scored the same evidence.\n")
    header = ["run", "strategy", "generation", "embedding", *metrics, f"Δ {primary} (95% CI)", "W/L/T"]
    lines.append(f"Δ is paired against the baseline and averaged like {primary} itself: over gold spans "
                 "for span coverage (questions resampled in the interval), over questions otherwise. "
                 "W/L/T counts questions.\n")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))
    for record in records:
        embedding = record["index"]["embedding"]
        values = [record["metrics"]["overall"].get(name) for name in metrics]
        cells = [record["run_id"] + (" (baseline)" if record is baseline else ""),
                 record["strategy"]["name"], record["index"]["papers_generation_id"][:8],
                 f"{embedding.get('provider')}:{embedding.get('model')}",
                 *["–" if v is None else f"{v:.3f}" for v in values]]
        if record is baseline:
            cells += ["–", "–"]
        else:
            delta = paired_delta(baseline, record, primary)
            if delta.get("not_comparable"):
                cells += [f"not comparable: {delta['not_comparable']}", "–"]
            elif delta.get("queries"):
                low, high = delta["ci95"]
                spans = f", {delta['delta_spans']:+d} spans" if "delta_spans" in delta else ""
                cells += [f"{delta['delta']:+.3f}{spans} ({low:+.3f}, {high:+.3f})",
                          f"{delta['wins']}/{delta['losses']}/{delta['ties']}"]
            else:
                cells += ["n/a", "n/a"]
        lines.append("| " + " | ".join(str(cell) for cell in cells) + " |")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Building eval sets: candidate pooling and quote resolution
# --------------------------------------------------------------------------


def pool_candidates(core, suite: Suite, strategies: list[str], depth: int = 20) -> Suite:
    """Attach pooled, unjudged retrieval candidates to every query for annotation."""
    for query in suite.queries:
        pooled: dict[str, dict] = {c["candidate_id"]: c for c in query.candidates}
        for strategy in strategies:
            payload, status = core.search_papers_chroma(
                query=query.text, n=depth, second_query=query.second_query,
                include_context=False, **query.filters, **STRATEGIES[strategy])
            if status != 200:
                raise SuiteError(f"{query.query_id}: search failed ({status}): {payload.get('error')}")
            for rank, hit in enumerate(payload["results"], 1):
                evidence = hit.get("evidence") or {}
                if not evidence.get("verified"):
                    continue
                spans = [{"file_hash": key[0], "pdf_page_index": key[1], "page_text_hash": key[2],
                          "char_start": start, "char_end": end}
                         for key, start, end in evidence_intervals(evidence)]
                candidate_id = "c-" + sha256_text(json.dumps(spans, sort_keys=True))[:10]
                entry = pooled.setdefault(candidate_id, {
                    "candidate_id": candidate_id, "relevance": None, "quote": None,
                    "zotero_parent_key": evidence["zotero_parent_key"],
                    "source_role": evidence["source_role"],
                    "pages": sorted({span["pdf_page_index"] + 1 for span in spans}),
                    "text": hit["content"], "spans": spans, "found_by": {}})
                entry["found_by"].setdefault(strategy, rank)
        query.candidates = sorted(pooled.values(), key=lambda c: min(c["found_by"].values() or [depth + 1]))
    return suite


_WS = re.compile(r"\s+")


def _find_quote(text: str, quote: str) -> list[tuple[int, int]]:
    """Exact matches first; otherwise whitespace-insensitive matches."""
    exact = [(m.start(), m.start() + len(quote)) for m in re.finditer(re.escape(quote), text)]
    if exact:
        return exact
    tokens = [re.escape(token) for token in _WS.split(quote.strip()) if token]
    if not tokens:
        return []
    return [(m.start(), m.end()) for m in re.finditer(r"\s+".join(tokens), text)]


def resolve_suite(suite: Suite, pages: dict, *, min_relevance: int = SCORED_RELEVANCE,
                  allow_chunk_spans: bool = False) -> tuple[Suite, list[str]]:
    """Turn quote annotations and judged candidates into canonical evidence spans."""
    problems = []
    by_parent: dict[str, list[dict]] = {}
    for page in pages.values():
        by_parent.setdefault(page["zotero_parent_key"], []).append(page)

    def locate(query_id, item, candidate_pages):
        quote = item["quote"]
        matches = []
        for page in candidate_pages:
            if item.get("pdf_page_index") is not None and page["pdf_page_index"] != item["pdf_page_index"]:
                continue
            if item.get("source_role") and page["source_role"] != item["source_role"]:
                continue
            for start, end in _find_quote(page["normalized_text"], quote):
                matches.append((page, start, end))
        if len(matches) != 1:
            problems.append(f"{query_id}/{item['evidence_id']}: quote found {len(matches)} times"
                            + ("; add pdf_page_index or source_role" if matches else
                               "; quotes must lie on one page and match the extracted text"))
            return None
        page, start, end = matches[0]
        return EvidenceSpan(item["evidence_id"], item.get("group") or item["evidence_id"], page["file_hash"],
                            page["pdf_page_index"], page["page_text_hash"], start, end,
                            item.get("relevance", 3), page["zotero_parent_key"])

    for query in suite.queries:
        remaining = []
        for item in query.unresolved:
            if item.get("file_hash"):
                scope = [p for p in pages.values() if p["file_hash"] == item["file_hash"]]
            elif item.get("zotero_parent_key"):
                scope = by_parent.get(item["zotero_parent_key"], [])
            else:
                scope = list(pages.values())
            span = locate(query.query_id, item, scope)
            if span:
                query.evidence.append(span)
            else:
                remaining.append(item)
        query.unresolved = remaining
        # Lossless: only candidates converted into evidence leave the list.
        # Unjudged, non-relevant and unresolvable candidates stay for later rounds.
        kept = []
        for candidate in query.candidates:
            relevance = candidate.get("relevance")
            if relevance is None or relevance < min_relevance:
                kept.append(candidate)
                continue
            base = {"evidence_id": f"{query.query_id}-{candidate['candidate_id']}",
                    "group": candidate.get("group") or candidate["candidate_id"], "relevance": relevance}
            if candidate.get("quote"):
                keys = {(s["file_hash"], s["pdf_page_index"]) for s in candidate["spans"]}
                span = locate(query.query_id, {**base, "quote": candidate["quote"]},
                              [pages[key] for key in keys if key in pages])
                if span:
                    query.evidence.append(span)
                else:
                    kept.append(candidate)
            elif allow_chunk_spans:
                for index, raw in enumerate(candidate["spans"]):
                    query.evidence.append(EvidenceSpan(
                        f"{base['evidence_id']}-{index}", base["group"], raw["file_hash"],
                        raw["pdf_page_index"], raw["page_text_hash"], raw["char_start"], raw["char_end"],
                        relevance, candidate.get("zotero_parent_key")))
            else:
                problems.append(f"{query.query_id}/{candidate['candidate_id']}: judged relevant but has no "
                                "quote; add the minimal supporting quote or pass --allow-chunk-spans")
                kept.append(candidate)
        query.candidates = kept
    return suite, problems
