"""Offline strict rescore of preserved rankings without rerunning models."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence

from benchmarks.researchqa_chunking import ResearchQAChunk, chunk_pdf
from benchmarks.overnight import canonical_json_bytes
from benchmarks.researchqa_scoring import (
    QuestionScore, STRICT_EVIDENCE_PROTOCOL_VERSION, macro_aggregate,
    score_ranking, strict_group_completion_ranks,
)
from benchmarks.researchqa_strategy import (
    item_source_spans_from_chunks, map_all_references, normalize_paper_id,
)


def load_verified_payload(path: Path, *, bindings: Mapping, questions_sha256: str) -> dict:
    """Bind historical route, inputs and bytes to an independently verified manifest."""
    if bindings.get("schema_version") != 1 or bindings.get("questions_sha256") != questions_sha256:
        raise ValueError("Historical question input hash does not match verified bindings")
    raw = path.read_bytes()
    envelope = json.loads(raw)
    payload = envelope["payload"]
    binding = bindings.get("candidates", {}).get(envelope.get("config_id"))
    if not isinstance(binding, Mapping):
        raise ValueError("Historical candidate has no independently verified binding")
    candidate = payload.get("candidate", {})
    if (candidate != binding.get("candidate")
            or candidate.get("config_id") != envelope.get("config_id")
            or candidate.get("stage_id") != envelope.get("stage_id")
            or envelope.get("input_fingerprint") != binding.get("input_fingerprint")
            or envelope.get("engine_revision") != binding.get("engine_revision")):
        raise ValueError("Historical candidate identity or inputs do not match verified bindings")
    if hashlib.sha256(raw).hexdigest() != binding.get("artifact_sha256"):
        raise ValueError("Historical artifact hash mismatch against verified bindings")
    encoded = canonical_json_bytes(payload)
    if (hashlib.sha256(encoded).hexdigest() != envelope.get("payload_sha256")
            or envelope.get("payload_sha256") != binding.get("payload_sha256")):
        raise ValueError(f"Historical payload hash mismatch: {path}")
    if envelope.get("status") != "completed":
        raise ValueError(f"Historical candidate is not completed: {path}")
    return payload


def budget_prefix(ranked_ids: Sequence[str], chunks: Mapping[str, ResearchQAChunk],
                  maximum: int = 8000) -> tuple[tuple[str, ...], int]:
    """Keep a whole-chunk prefix under a fixed Unicode-code-point ceiling."""
    if maximum <= 0:
        raise ValueError("context budget must be positive")
    result = []
    used = 0
    for item_id in ranked_ids:
        length = len(chunks[item_id].text)
        if used + length > maximum:
            break
        result.append(item_id)
        used += length
    return tuple(result), used


def rescore_payload(payload: Mapping, documents: Mapping, questions: Sequence[Mapping],
                    *, adjudications: Mapping | None = None,
                    context_budget: int = 8000) -> dict:
    """Validate complete row/ID provenance and report full plus conditional scores.

    Missing gold never disappears from the full denominator. No-reference
    questions retain null retrieval scores. This is an offline diagnostic,
    never a claim that a historical cache remains reusable by the live runner.
    """
    candidate = payload["candidate"]
    by_paper = {}
    for paper_id, document in documents.items():
        result = chunk_pdf(document, config_id=candidate["pdf_chunker"], is_main=True)
        if result.status != "completed":
            raise ValueError(f"Cannot reconstruct chunks for {paper_id}")
        by_paper[paper_id] = result.chunks + result.parents
    chunks = {chunk.chunk_id: chunk for values in by_paper.values() for chunk in values}
    mappings = map_all_references(
        questions, tuple(chunks.values()), documents=documents, gold_adjudications=adjudications,
    )
    source_spans = item_source_spans_from_chunks(tuple(chunks.values()))
    rows = payload["question_results"]
    row_ids = [row["row_id"] for row in rows]
    expected_ids = [str(question["row_id"]) for question in questions]
    if len(set(row_ids)) != len(row_ids) or set(row_ids) != set(expected_ids):
        raise ValueError("Historical ranked question set is incomplete or duplicated")
    question_by_id = {str(question["row_id"]): question for question in questions}
    mapping_by_id = {mapping.row_id: mapping for mapping in mappings.mappings}
    outputs = []
    aggregates = {"full": [], "conditional": [], "context_budget": []}
    for row in sorted(rows, key=lambda value: value["row_id"]):
        row_id = row["row_id"]
        question = question_by_id[row_id]
        paper_id = normalize_paper_id(question["paper_id"])
        if normalize_paper_id(row["paper_id"]) != paper_id:
            raise ValueError(f"Historical question paper changed: {row_id}")
        ranked_ids = tuple(row["ranked_item_ids"])
        if any(item_id not in chunks or chunks[item_id].paper_id != paper_id
               for item_id in ranked_ids):
            raise ValueError(f"Historical IDs do not match canonical paper chunks: {row_id}")
        mapping = mapping_by_id[row_id]
        conditional = score_ranking(ranked_ids, mapping.evaluable_groups,
                                    item_source_spans=source_spans)
        full_metrics = {key: value for key, value in conditional.metrics.items()
                        if not key.startswith("loose_hit_")}
        if mapping.groups:
            fraction = len(mapping.evaluable_groups) / len(mapping.groups)
            full_metrics = {key: value if value is not None else 0.0
                            for key, value in full_metrics.items()}
            for key in ("recall_at_5", "recall_at_10", "coverage_ndcg_at_10"):
                full_metrics[key] *= fraction
            if fraction < 1:
                full_metrics["all_required_groups_success_at_5"] = 0.0
                full_metrics["all_required_groups_success_at_10"] = 0.0
        limited, used = budget_prefix(ranked_ids, chunks, context_budget)
        covered = strict_group_completion_ranks(limited, mapping.evaluable_groups, source_spans)
        budget_metrics = {
            "group_recall": len(covered) / len(mapping.groups) if mapping.groups else None,
            "all_groups_success": float(len(covered) == len(mapping.groups)) if mapping.groups else None,
        }
        metrics_by_kind = {"full": full_metrics, "conditional": dict(conditional.metrics),
                           "context_budget": budget_metrics}
        for kind, metrics in metrics_by_kind.items():
            aggregates[kind].append(QuestionScore(
                row_id, paper_id, str(question["domain"]), str(question["question_type"]), metrics,
            ))
        outputs.append({
            "row_id": row_id, "paper_id": paper_id,
            "expected_groups": len(mapping.groups), "verified_groups": len(mapping.evaluable_groups),
            "ranked_item_ids": list(ranked_ids), "metrics": metrics_by_kind,
            "context_chars": used, "context_item_count": len(limited),
        })
    return {
        "schema_version": 1, "protocol_version": STRICT_EVIDENCE_PROTOCOL_VERSION,
        "candidate": dict(candidate), "classification": "offline-historical-rescore",
        "scope": "paper-scoped", "mapping": mappings.to_dict(),
        "denominator_policy": {"full": "verified-evidence lower bound over all expected groups; unresolved groups receive zero credit",
                               "conditional": "shared verified groups only; null when none verified"},
        "context_budget": {"maximum": context_budget, "unit": "Unicode code points",
                           "selection": "whole-chunk-prefix-stop-before-overflow"},
        "aggregates": {kind: macro_aggregate(scores).to_dict() for kind, scores in aggregates.items()},
        "question_results": outputs,
        "source_manifest": {paper_id: {"file_hash": document.file_hash,
            "extractor_fingerprint": document.extractor_fingerprint,
            "page_hashes": [page.page_text_hash for page in document.pages]}
            for paper_id, document in documents.items()},
        "evaluable_set": [[mapping.row_id, group.group_id] for mapping in mappings.mappings
                           for group in mapping.evaluable_groups],
        "eligible_for_release_claim": False,
        "limitations": ["No new retrieval or timing measurement", "Exact alignment does not establish scientific sufficiency", "Agent adjudication is not human annotation"],
    }
