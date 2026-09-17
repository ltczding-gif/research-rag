"""Adapt one canonical ``search_papers`` response to the strict scorer.

This module deliberately does not query Chroma or construct gold identity.  It
accepts an already-validated ``EvidenceMapping`` and one already-recorded service
response, then either creates scorer-ready source spans or rejects the trace.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
import hashlib
import json
import math
from numbers import Real
from typing import Any, Literal, Mapping, Sequence


class ServiceEvidenceAdapterError(ValueError):
    """The recorded service result cannot support a strict score."""


_SHA256_LENGTH = 64
_PAPER_FILTER_KEYS = frozenset(
    {"paper_id", "zotero_parent_key", "zotero_attachment_key", "pdf_filename",
     "paper_group"}
)
_SUPPORTED_FILTER_KEYS = frozenset(
    {"zotero_parent_key", "paper_group", "pdf_filename", "zotero_attachment_key",
     "source_role", "source_type"}
)
_SERVER_TIMING_KEYS = (
    "query_embedding", "retrieval", "rerank", "serialization", "total"
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ServiceEvidenceAdapterError(f"{label} must be a non-empty string")
    return value


def _require_sha256(value: object, label: str) -> str:
    value = _require_string(value, label)
    if (
        len(value) != _SHA256_LENGTH
        or value.lower() != value
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ServiceEvidenceAdapterError(f"{label} must be a lowercase SHA-256")
    return value


def _as_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ServiceEvidenceAdapterError(f"{label} must be an object")
    return value


def _as_sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ServiceEvidenceAdapterError(f"{label} must be an array")
    return value


def _filter_keys(value: object) -> set[str]:
    """Return every leaf field name in a Chroma ``where`` expression."""
    if isinstance(value, Mapping):
        return {
            key for key, child in value.items()
            for key in ({key} if not key.startswith("$") else set()) | _filter_keys(child)
        }
    if isinstance(value, list):
        return set().union(*(_filter_keys(child) for child in value)) if value else set()
    return set()


@dataclass(frozen=True)
class ServiceRequest:
    """Request facts captured by the client before calling ``search_papers``."""

    query: str
    effective_query: str
    scope: Literal["paper", "corpus"]
    expected_filters: Mapping[str, Any] | None
    n: int
    paper_id: str | None = None

    def __post_init__(self) -> None:
        _require_string(self.query, "request.query")
        _require_string(self.effective_query, "request.effective_query")
        if self.scope not in {"paper", "corpus"}:
            raise ServiceEvidenceAdapterError("request.scope must be paper or corpus")
        if self.scope == "paper":
            _require_string(self.paper_id, "request.paper_id")
        elif self.paper_id is not None:
            raise ServiceEvidenceAdapterError("corpus scope cannot carry a paper_id")
        keys = _filter_keys(self.expected_filters)
        if self.scope == "corpus" and keys & _PAPER_FILTER_KEYS:
            raise ServiceEvidenceAdapterError(
                "corpus scope cannot carry paper or attachment filter leakage"
            )
        if isinstance(self.n, bool) or not isinstance(self.n, int) or self.n < 1:
            raise ServiceEvidenceAdapterError("request.n must be a positive integer")

    @classmethod
    def from_tool_call(
        cls, tool_call: Mapping[str, Any], *, scope: Literal["paper", "corpus"], paper_id: str | None,
    ) -> "ServiceRequest":
        """Build the expected service contract from recorded tool arguments only."""
        raw = _as_mapping(tool_call, "tool_call")
        allowed = {
            "query", "n", "zotero_parent_key", "paper_group", "pdf_filename",
            "zotero_attachment_key", "source_role", "source_type", "second_query",
        }
        unsupported = set(raw) - allowed
        if unsupported:
            raise ServiceEvidenceAdapterError(
                f"unsupported search_papers tool field(s): {sorted(unsupported)}"
            )
        query = _require_string(raw.get("query"), "tool_call.query")
        second_query = raw.get("second_query")
        if second_query is not None and not isinstance(second_query, str):
            raise ServiceEvidenceAdapterError("tool_call.second_query must be a string when supplied")
        values = {
            key: raw.get(key)
            for key in (
                "zotero_parent_key", "paper_group", "pdf_filename",
                "zotero_attachment_key", "source_role", "source_type",
            )
        }
        filters = [{key: value} for key, value in values.items() if value is not None and value != ""]
        expected_filters = {"$and": filters} if len(filters) > 1 else filters[0] if filters else None
        return cls(
            query=query, effective_query=second_query or query, scope=scope,
            expected_filters=expected_filters, n=raw.get("n"), paper_id=paper_id,
        )


@dataclass(frozen=True)
class CanonicalGeneration:
    """Manifest plus the actual canonical pages bytes used to validate a response."""

    manifest: Mapping[str, Any]
    manifest_sha256: str
    pages_bytes: bytes

    def __post_init__(self) -> None:
        _require_sha256(self.manifest_sha256, "manifest_sha256")
        if self.manifest_fingerprint != self.manifest_sha256:
            raise ServiceEvidenceAdapterError("manifest_sha256 does not match manifest")
        _require_string(self.generation_id, "manifest.generation_id")
        if not self.embedding:
            raise ServiceEvidenceAdapterError("manifest.contract.embedding is required")
        artifacts = _as_mapping(_as_mapping(self.manifest, "manifest").get("artifacts"), "manifest.artifacts")
        hashes = _as_mapping(_as_mapping(self.manifest, "manifest").get("artifact_hashes"), "manifest.artifact_hashes")
        pages_name = _require_string(artifacts.get("pages"), "manifest.artifacts.pages")
        if hashlib.sha256(self.pages_bytes).hexdigest() != _require_sha256(hashes.get(pages_name), "manifest artifact_hashes.pages"):
            raise ServiceEvidenceAdapterError("pages bytes do not match manifest artifact_hashes")
        self.pages

    @property
    def manifest_fingerprint(self) -> str:
        encoded = json.dumps(
            self.manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
        return _sha256_text(encoded)

    @property
    def generation_id(self) -> str:
        return _as_mapping(self.manifest, "manifest").get("generation_id")

    @property
    def embedding(self) -> Mapping[str, Any]:
        contract = _as_mapping(self.manifest, "manifest").get("contract")
        return _as_mapping(contract, "manifest.contract").get("embedding")

    @cached_property
    def pages(self) -> Mapping[tuple[str, int], Mapping[str, Any]]:
        pages: dict[tuple[str, int], Mapping[str, Any]] = {}
        try:
            lines = self.pages_bytes.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ServiceEvidenceAdapterError("canonical pages bytes are not UTF-8") from exc
        for line_number, line in enumerate(lines, 1):
            if not line:
                continue
            try:
                page = _as_mapping(json.loads(line), f"pages line {line_number}")
            except json.JSONDecodeError as exc:
                raise ServiceEvidenceAdapterError(f"pages line {line_number} is invalid JSON") from exc
            file_id = _require_string(page.get("file_id"), f"pages line {line_number}.file_id")
            page_index = page.get("pdf_page_index")
            if isinstance(page_index, bool) or not isinstance(page_index, int) or page_index < 0:
                raise ServiceEvidenceAdapterError(f"pages line {line_number}.pdf_page_index is invalid")
            key = (file_id, page_index)
            if key in pages:
                raise ServiceEvidenceAdapterError("canonical pages contain a duplicate page identity")
            pages[key] = page
        if not pages:
            raise ServiceEvidenceAdapterError("canonical pages artifact has no pages")
        return pages


@dataclass(frozen=True)
class AnswerContext:
    """Exact answer input under the sole supported rank-prefix serializer."""

    selected_item_ids: tuple[str, ...]
    text: str
    budget_limit: int = 8000

    def __post_init__(self) -> None:
        if len(set(self.selected_item_ids)) != len(self.selected_item_ids):
            raise ServiceEvidenceAdapterError("context IDs must be unique")
        if not isinstance(self.text, str):
            raise ServiceEvidenceAdapterError("context.text must be a string")
        if self.budget_limit != 8000:
            raise ServiceEvidenceAdapterError("context budget is fixed at 8000 unicode code points")
        if self.measured_budget > self.budget_limit:
            raise ServiceEvidenceAdapterError("actual answer context exceeds declared budget")

    @property
    def measured_budget(self) -> int:
        return len(self.text)


@dataclass(frozen=True)
class ServiceScore:
    """Serialized, audit-ready result of adapting and scoring a service trace."""

    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def _validated_timings(value: object, label: str, *, required: Sequence[str]) -> dict[str, float]:
    timings = _as_mapping(value, label)
    output: dict[str, float] = {}
    for key in required:
        raw = timings.get(key)
        if isinstance(raw, bool) or not isinstance(raw, Real) or not math.isfinite(raw) or raw < 0:
            raise ServiceEvidenceAdapterError(f"{label}.{key} must be a non-negative number")
        output[key] = float(raw)
    return output


def _span_key(value: Mapping[str, Any]) -> tuple[object, ...]:
    return tuple(value.get(key) for key in (
        "file_id", "pdf_page_index", "char_start_in_normalized_page",
        "char_end_in_normalized_page", "page_text_hash",
    ))


def _filter_predicates(value: object) -> tuple[tuple[str, object], ...]:
    if value is None:
        return ()
    expression = _as_mapping(value, "request.expected_filters")
    predicates: list[tuple[str, object]] = []
    for key, expected in expression.items():
        if key == "$and":
            children = _as_sequence(expected, "request.expected_filters.$and")
            for child in children:
                predicates.extend(_filter_predicates(child))
            continue
        if key.startswith("$") or key not in _SUPPORTED_FILTER_KEYS:
            raise ServiceEvidenceAdapterError(f"unsupported service filter key/operator: {key}")
        if isinstance(expected, (Mapping, list)):
            raise ServiceEvidenceAdapterError(f"unsupported non-exact service filter: {key}")
        predicates.append((key, expected))
    return tuple(predicates)


def _assert_filters_match(metadata: Mapping[str, Any], predicates: Sequence[tuple[str, object]], item_id: str) -> None:
    for key, expected in predicates:
        if metadata.get(key) != expected:
            raise ServiceEvidenceAdapterError(f"{item_id}: result fails requested filter {key}")


def _assert_content_reconstructs(
    content: str, source_spans: Sequence[Mapping[str, Any]], generation: CanonicalGeneration, item_id: str,
) -> None:
    """Equivalent to ``locate_canonical_text`` without importing the PDF layer."""
    file_id = _require_string(source_spans[0].get("file_id"), f"{item_id}.source span file_id")
    pages = sorted(
        (page for (candidate_file_id, _), page in generation.pages.items() if candidate_file_id == file_id),
        key=lambda page: page["pdf_page_index"],
    )
    if not pages or [page["pdf_page_index"] for page in pages] != list(range(len(pages))):
        raise ServiceEvidenceAdapterError(f"{item_id}: canonical page sequence is incomplete")
    offsets, cursor = {}, 0
    for page in pages:
        offsets[page["pdf_page_index"]] = cursor
        cursor += len(page["normalized_text"]) + 1
    joined = "\n".join(page["normalized_text"] for page in pages)
    first = source_spans[0]
    anchor = offsets[first["pdf_page_index"]] + first["char_start_in_normalized_page"]
    for prefix in range(len(content) - len(content.lstrip("\n")) + 1):
        start = anchor - prefix
        if start < 0 or joined[start : start + len(content)] != content:
            continue
        expected = []
        for page in pages:
            page_start = offsets[page["pdf_page_index"]]
            left, right = max(start, page_start), min(start + len(content), page_start + len(page["normalized_text"]))
            if left < right:
                expected.append({
                    "file_id": page["file_id"], "pdf_page_index": page["pdf_page_index"],
                    "char_start_in_normalized_page": left - page_start,
                    "char_end_in_normalized_page": right - page_start,
                    "page_text_hash": page["page_text_hash"],
                })
        if [_span_key(value) for value in expected] == [_span_key(value) for value in source_spans]:
            return
    raise ServiceEvidenceAdapterError(f"{item_id}: content does not reconstruct from canonical spans")


def _spans_for_result(result: Mapping[str, Any], generation: CanonicalGeneration, scorer: Any) -> tuple[Any, ...]:
    item_id = _require_string(result.get("id"), "result.id")
    content = _require_string(result.get("content"), f"{item_id}.content")
    metadata = _as_mapping(result.get("metadata"), f"{item_id}.metadata")
    evidence = _as_mapping(result.get("evidence"), f"{item_id}.evidence")
    if evidence.get("verified") is not True:
        raise ServiceEvidenceAdapterError(f"{item_id}: service evidence is not verified")
    for field in (
        "generation_id", "file_id", "file_hash", "zotero_parent_key",
        "zotero_attachment_key", "source_role",
    ):
        if metadata.get(field) != evidence.get(field):
            raise ServiceEvidenceAdapterError(f"{item_id}: metadata/evidence {field} mismatch")
    if metadata.get("generation_id") != generation.generation_id:
        raise ServiceEvidenceAdapterError(f"{item_id}: result belongs to another generation")
    if evidence.get("generation_id") != generation.generation_id or evidence.get("chunk_id") != item_id:
        raise ServiceEvidenceAdapterError(f"{item_id}: evidence generation or chunk identity mismatch")
    if _sha256_text(content) != _require_sha256(metadata.get("text_hash"), f"{item_id}.metadata.text_hash"):
        raise ServiceEvidenceAdapterError(f"{item_id}: content does not match metadata.text_hash")
    file_id = _require_string(metadata.get("file_id"), f"{item_id}.metadata.file_id")
    file_hash = _require_sha256(metadata.get("file_hash"), f"{item_id}.metadata.file_hash")
    try:
        source_spans = json.loads(_require_string(metadata.get("source_spans_json"), f"{item_id}.metadata.source_spans_json"))
    except json.JSONDecodeError as exc:
        raise ServiceEvidenceAdapterError(f"{item_id}: source_spans_json is invalid JSON") from exc
    source_spans = _as_sequence(source_spans, f"{item_id}.source_spans_json")
    segments = _as_sequence(evidence.get("segments"), f"{item_id}.evidence.segments")
    if not source_spans or len(source_spans) != len(segments):
        raise ServiceEvidenceAdapterError(f"{item_id}: source span/segment count mismatch")
    source_keys = [_span_key(_as_mapping(span, "source span")) for span in source_spans]
    segment_keys = [_span_key(_as_mapping(segment, "evidence segment")) for segment in segments]
    if len(set(source_keys)) != len(source_keys) or len(set(segment_keys)) != len(segment_keys):
        raise ServiceEvidenceAdapterError(f"{item_id}: duplicate metadata/evidence page span")
    if source_keys != segment_keys:
        raise ServiceEvidenceAdapterError(f"{item_id}: metadata/evidence page span mismatch")
    output = []
    for segment in segments:
        segment = _as_mapping(segment, f"{item_id}.evidence.segment")
        if segment.get("file_id") != file_id:
            raise ServiceEvidenceAdapterError(f"{item_id}: segment file_id mismatch")
        page_hash = _require_sha256(segment.get("page_text_hash"), f"{item_id}.segment.page_text_hash")
        quote = _require_string(segment.get("quote"), f"{item_id}.segment.quote")
        if _sha256_text(quote) != _require_sha256(segment.get("quote_hash"), f"{item_id}.segment.quote_hash"):
            raise ServiceEvidenceAdapterError(f"{item_id}: quote hash mismatch")
        page = segment.get("pdf_page_index")
        start, end = segment.get("char_start_in_normalized_page"), segment.get("char_end_in_normalized_page")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (page, start, end)):
            raise ServiceEvidenceAdapterError(f"{item_id}: page/span coordinates must be integers")
        if page < 0 or start < 0 or end <= start or segment.get("page_number") != page + 1:
            raise ServiceEvidenceAdapterError(f"{item_id}: invalid page/span coordinates")
        canonical_page = generation.pages.get((file_id, page))
        if canonical_page is None:
            raise ServiceEvidenceAdapterError(f"{item_id}: canonical page is missing")
        for field in (
            "generation_id", "file_id", "file_hash", "zotero_parent_key",
            "zotero_attachment_key", "source_role",
        ):
            if canonical_page.get(field) != metadata.get(field):
                raise ServiceEvidenceAdapterError(f"{item_id}: canonical page {field} mismatch")
        normalized_text = _require_string(canonical_page.get("normalized_text"), f"{item_id}.canonical normalized_text")
        if _sha256_text(normalized_text) != page_hash or canonical_page.get("page_text_hash") != page_hash:
            raise ServiceEvidenceAdapterError(f"{item_id}: canonical page hash mismatch")
        if end > len(normalized_text) or normalized_text[start:end] != quote:
            raise ServiceEvidenceAdapterError(f"{item_id}: quote does not match canonical page slice")
        output.append(scorer.RetrievedEvidenceSpan(
            file_id=file_id, file_hash=file_hash, pdf_page_index=page,
            char_start_in_normalized_page=start, char_end_in_normalized_page=end,
            page_text_hash=page_hash,
        ))
    _assert_content_reconstructs(content, source_spans, generation, item_id)
    return tuple(output)


def score_service_response(
    *,
    mapping: Any,
    request: ServiceRequest,
    response: Mapping[str, Any],
    generation: CanonicalGeneration,
    scorer: Any,
    service_revision: str,
    trace_classification: str,
    recorded_generation_id: str,
    recorded_embedding: Mapping[str, Any],
    answer_context: AnswerContext | None = None,
    client_total_seconds: float | None = None,
) -> ServiceScore:
    """Strictly adapt one real response, then call the existing ``score_ranking``.

    ``mapping`` is intentionally opaque except for B's public EvidenceMapping
    fields.  Its identity and gold spans are not recomputed here.
    """
    _require_string(service_revision, "service_revision")
    _require_string(trace_classification, "trace_classification")
    if recorded_generation_id != generation.generation_id:
        raise ServiceEvidenceAdapterError("recorded trace generation_id differs from trusted manifest")
    if _as_mapping(recorded_embedding, "recorded_embedding") != generation.embedding:
        raise ServiceEvidenceAdapterError("recorded trace embedding differs from trusted manifest")
    if request.scope == "paper" and request.paper_id != mapping.paper_id:
        raise ServiceEvidenceAdapterError("paper scope does not match B mapping.paper_id")
    response = _as_mapping(response, "response")
    if response.get("index_mode") != "canonical":
        raise ServiceEvidenceAdapterError("strict scoring requires a canonical service response")
    if response.get("query") != request.query or response.get("effective_query") != request.effective_query:
        raise ServiceEvidenceAdapterError("response query/effective_query differs from recorded request")
    if response.get("filters") != request.expected_filters:
        raise ServiceEvidenceAdapterError("response filters differ from recorded request; refusing silent filter loss")
    predicates = _filter_predicates(request.expected_filters)
    server_timings = _validated_timings(response.get("timings_seconds"), "response.timings_seconds", required=_SERVER_TIMING_KEYS)
    results = _as_sequence(response.get("results"), "response.results")
    if len(results) > request.n:
        raise ServiceEvidenceAdapterError("service returned more results than requested n")
    ranked_item_ids: list[str] = []
    ranked_items: list[dict[str, Any]] = []
    item_source_spans: dict[str, tuple[Any, ...]] = {}
    for rank, raw_result in enumerate(results, 1):
        result = _as_mapping(raw_result, f"response.results[{rank - 1}]")
        item_id = _require_string(result.get("id"), f"response.results[{rank - 1}].id")
        if item_id in item_source_spans:
            raise ServiceEvidenceAdapterError(f"duplicate retrieved item ID: {item_id}")
        metadata = _as_mapping(result.get("metadata"), f"{item_id}.metadata")
        _assert_filters_match(metadata, predicates, item_id)
        if request.scope == "paper" and metadata.get("paper_id") != request.paper_id:
            raise ServiceEvidenceAdapterError(f"{item_id}: paper-scoped result leaks another paper")
        spans = _spans_for_result(result, generation, scorer)
        distance = result.get("distance")
        if distance is not None and (isinstance(distance, bool) or not isinstance(distance, Real) or not math.isfinite(distance)):
            raise ServiceEvidenceAdapterError(f"{item_id}: distance must be numeric or null")
        ranked_item_ids.append(item_id)
        item_source_spans[item_id] = spans
        ranked_items.append({
            "item_id": item_id, "rank": rank,
            "distance": float(distance) if distance is not None else None,
            "source_spans": [
                {"file_id": span.file_id, "file_hash": span.file_hash,
                 "pdf_page_index": span.pdf_page_index,
                 "char_start_in_normalized_page": span.char_start_in_normalized_page,
                 "char_end_in_normalized_page": span.char_end_in_normalized_page,
                 "page_text_hash": span.page_text_hash,
                 "quote": segment["quote"],
                 "quote_hash": segment["quote_hash"],
                 "quote_source": "canonical_pages.normalized_text"}
                for span, segment in zip(spans, result["evidence"]["segments"])
            ],
        })
    evaluable_groups = mapping.evaluable_groups
    if evaluable_groups:
        conditional_ranking = scorer.score_ranking(
            ranked_item_ids, evaluable_groups, item_source_spans=item_source_spans
        ).to_dict()
        conditional_metrics = conditional_ranking["metrics"]
        lower_bound = {
            "required_group_count": len(mapping.groups),
            "mapped_group_count": len(evaluable_groups),
            "unmapped_group_ids": list(mapping.unmapped_group_ids),
            "recall_at_5_lower_bound": conditional_metrics["groups_covered_at_5"] / len(mapping.groups),
            "recall_at_10_lower_bound": conditional_metrics["groups_covered_at_10"] / len(mapping.groups),
            "all_required_groups_success_at_5": 0.0 if mapping.unmapped_group_ids else conditional_metrics["all_required_groups_success_at_5"],
            "all_required_groups_success_at_10": 0.0 if mapping.unmapped_group_ids else conditional_metrics["all_required_groups_success_at_10"],
        }
    else:
        conditional_ranking = {"status": "not_evaluable", "reason": "B mapping has no verified groups"}
        lower_bound = {
            "required_group_count": len(mapping.groups), "mapped_group_count": 0,
            "unmapped_group_ids": list(mapping.unmapped_group_ids),
            "recall_at_5_lower_bound": 0.0 if mapping.groups else None,
            "recall_at_10_lower_bound": 0.0 if mapping.groups else None,
            "all_required_groups_success_at_5": 0.0 if mapping.groups else None,
            "all_required_groups_success_at_10": 0.0 if mapping.groups else None,
        }
    if client_total_seconds is None:
        client_timing: Mapping[str, Any] = {"status": "not_run", "reason": "client_total_seconds was not captured"}
    elif isinstance(client_total_seconds, bool) or not isinstance(client_total_seconds, Real) or not math.isfinite(client_total_seconds) or client_total_seconds < 0:
        raise ServiceEvidenceAdapterError("client_total_seconds must be a non-negative number")
    else:
        client_timing = {"status": "captured", "total_seconds": float(client_total_seconds)}
    if answer_context is None:
        context: Mapping[str, Any] = {"status": "not_run", "reason": "answer context was not captured"}
    else:
        selected_count = len(answer_context.selected_item_ids)
        if tuple(ranked_item_ids[:selected_count]) != answer_context.selected_item_ids:
            raise ServiceEvidenceAdapterError("answer context IDs must be an in-rank prefix")
        selected_results = results[:selected_count]
        expected_context = "\n\n".join(
            f"[rank={rank} id={item_id}]\n{_require_string(_as_mapping(item, 'result').get('content'), f'{item_id}.content')}"
            for rank, (item_id, item) in enumerate(zip(answer_context.selected_item_ids, selected_results), 1)
        )
        if answer_context.text != expected_context:
            raise ServiceEvidenceAdapterError("answer context text differs from ranked-content-v1 serialization")
        context = {
            "status": "captured", "selected_item_ids": list(answer_context.selected_item_ids),
            "text": answer_context.text, "text_sha256": _sha256_text(answer_context.text),
            "budget": {"unit": "unicode_codepoints", "limit": answer_context.budget_limit,
                       "measured": answer_context.measured_budget},
            "selection_rule": "ranked-content-v1 prefix top-k",
        }
    return ServiceScore({
        "schema_version": 1,
        "row_id": mapping.row_id, "paper_id": mapping.paper_id,
        "service_revision": service_revision,
        "trace_classification": trace_classification,
        "eligible_for_release_claim": False,
        "generation": {"generation_id": generation.generation_id,
                       "manifest_sha256": generation.manifest_sha256,
                       "pages_artifact_sha256": hashlib.sha256(generation.pages_bytes).hexdigest(),
                       "embedding": dict(generation.embedding)},
        "request": {"query": request.query, "effective_query": request.effective_query,
                    "scope": request.scope, "paper_id": request.paper_id,
                    "filters": request.expected_filters, "n": request.n},
        "retrieval": {"ranked_items": ranked_items, "conditional_ranking": conditional_ranking,
                      "full_group_lower_bound": lower_bound},
        "timings_seconds": {"server": server_timings, "client": client_timing},
        "answer_context": context,
    })
