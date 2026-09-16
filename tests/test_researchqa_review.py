from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from benchmarks.overnight import canonical_json_bytes
from benchmarks.researchqa_review import budget_prefix, load_verified_payload, rescore_payload
from benchmarks.researchqa_chunking import chunk_pdf
from benchmarks.researchqa_strategy import map_all_references
from service.pdf_ir import CanonicalDocument, DocumentPage, DEFAULT_EXTRACTOR_FINGERPRINT, hash_text


def test_budget_counts_unicode_and_stops_without_skipping():
    chunks = {key: SimpleNamespace(text=text) for key, text in
              {"a": "中文abc", "b": "long", "c": "x"}.items()}
    assert budget_prefix(("a", "b", "c"), chunks, 6) == (("a",), 5)
    assert budget_prefix(("b",), chunks, 3) == ((), 0)


def test_historical_payload_requires_unchanged_hash(tmp_path):
    candidate = {"config_id": "c1", "stage_id": "retriever", "retriever": "dense"}
    payload = {"rankings": ["x"], "candidate": candidate}
    envelope = {"payload": payload, "payload_sha256": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(), "status": "completed",
                "config_id": "c1", "stage_id": "retriever", "input_fingerprint": "input1", "engine_revision": "v1"}
    path = tmp_path / "result.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    binding = {"artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "payload_sha256": envelope["payload_sha256"],
               "candidate": dict(candidate), "input_fingerprint": "input1", "engine_revision": "v1"}
    bindings = {"schema_version": 1, "questions_sha256": "questions1", "candidates": {"c1": binding}}
    assert load_verified_payload(path, bindings=bindings, questions_sha256="questions1") == payload
    with pytest.raises(ValueError, match="question input hash"):
        load_verified_payload(path, bindings=bindings, questions_sha256="changed-question-same-row-id")
    envelope["payload"]["candidate"]["retriever"] = "hybrid-rrf"
    envelope["payload_sha256"] = hashlib.sha256(canonical_json_bytes(envelope["payload"])).hexdigest()
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ValueError, match="candidate identity"):
        load_verified_payload(path, bindings=bindings, questions_sha256="questions1")
    envelope["payload"]["candidate"]["retriever"] = "dense"
    envelope["payload"]["rankings"] = ["changed"]
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_verified_payload(path, bindings=bindings, questions_sha256="questions1")


def test_rescore_preserves_unresolved_denominator_and_validates_ids():
    text = "The measured yield was 42 percent. " + "Supporting context. " * 40
    document = CanonicalDocument("W1", "Main", hash_text("file"), DEFAULT_EXTRACTOR_FINGERPRINT,
                                 (DocumentPage.create(paper_id="W1", file_id="Main", pdf_page_index=0, text=text),))
    chunk = chunk_pdf(document, "pdf-fixed-1200", is_main=True).chunks[0]
    questions = [{"row_id": "q1", "paper_id": "W1", "domain": "d", "question_type": "lookup", "question": "Yield?",
                  "expected_references": [{"alternatives": ["The measured yield was 42 percent."]},
                                          {"alternatives": ["An unrelated conclusion is established [Main p.1]"]}]}]
    payload = {"candidate": {"pdf_chunker": "pdf-fixed-1200"}, "question_results": [
        {"row_id": "q1", "paper_id": "W1", "ranked_item_ids": [chunk.chunk_id]}]}
    result = rescore_payload(payload, {"W1": document}, questions)
    row = result["question_results"][0]
    assert row["expected_groups"] == 2 and row["verified_groups"] == 1
    assert row["metrics"]["full"]["recall_at_10"] == 0.5
    assert row["metrics"]["conditional"]["recall_at_10"] == 1.0
    assert not result["eligible_for_release_claim"]
    questions[0]["expected_references"] = [{"alternatives": ["Unrelated unsupported conclusion"]}]
    unresolved = rescore_payload(payload, {"W1": document}, questions)["question_results"][0]
    assert unresolved["metrics"]["full"]["recall_at_10"] == 0.0
    assert unresolved["metrics"]["conditional"]["recall_at_10"] is None
    questions[0]["expected_references"] = []
    unreferenced = rescore_payload(payload, {"W1": document}, questions)["question_results"][0]
    assert unreferenced["metrics"]["full"]["recall_at_10"] is None
    assert unreferenced["metrics"]["conditional"]["recall_at_10"] is None
    payload["question_results"][0]["ranked_item_ids"] = ["missing"]
    with pytest.raises(ValueError, match="canonical paper chunks"):
        rescore_payload(payload, {"W1": document}, questions)


def test_decimal_conflict_does_not_reuse_an_exact_alternative_cache():
    text = "The measured yield was 1.2 percent. " + "Supporting context. " * 40
    document = CanonicalDocument("W1", "Main", hash_text("file"), DEFAULT_EXTRACTOR_FINGERPRINT,
                                 (DocumentPage.create(paper_id="W1", file_id="Main", pdf_page_index=0, text=text),))
    chunks = chunk_pdf(document, "pdf-fixed-1200", is_main=True).chunks
    question = {"row_id": "q1", "paper_id": "W1", "domain": "d", "question_type": "lookup",
                "expected_references": [{"alternatives": ["The measured yield was 1.2 percent.",
                                                           "The measured yield was 12 percent."]}]}
    mapping = map_all_references([question], chunks, documents={"W1": document})
    good, conflict = mapping.mappings[0].groups[0].alternatives
    assert good.verification_state == "exact"
    assert conflict.verification_state == "weak_hint"
