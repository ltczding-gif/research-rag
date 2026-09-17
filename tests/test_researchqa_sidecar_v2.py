from __future__ import annotations

import copy
import hashlib
from types import SimpleNamespace

import pytest

from benchmarks.researchqa_chunking import chunk_pdf
from benchmarks.researchqa_review import rescore_payload
from benchmarks.researchqa_scoring import evidence_alternative_id
from benchmarks.researchqa_strategy import (
    StrategyCandidate,
    StrategyContractError,
    map_all_references,
    map_question_references,
    question_fingerprint,
    question_manifest_sha256,
    run_complete_candidate,
)
from benchmarks.scripts.rescore_review_rankings import (
    _load_adjudications_for_current_questions,
)
from service.pdf_ir import CanonicalDocument, DEFAULT_EXTRACTOR_FINGERPRINT, DocumentPage, hash_text


DATASET_ID = "fixture-rq"
DATASET_REVISION = "2026-09-17"


def _document(paper_id="W1", file_hash=None):
    text = "Observed source evidence supports the audited claim. " + "Supporting context. " * 40
    return CanonicalDocument(
        paper_id,
        "Main",
        file_hash or hash_text("canonical-file"),
        DEFAULT_EXTRACTOR_FINGERPRINT,
        (DocumentPage.create(paper_id=paper_id, file_id="Main", pdf_page_index=0, text=text),),
    )


def _question(paper_id="W1"):
    return {
        "row_id": "q1",
        "paper_id": paper_id,
        "domain": "d",
        "question_type": "lookup",
        "question": "What condition was audited?",
        "expected_references": [
            {"alternatives": ["The dose was 5 mg.", "The dose equalled 5 mg."]},
            {"alternatives": ["The condition was not active.", "The condition was inactive."]},
        ],
    }


def _sidecar(question, document):
    page = document.pages[0]
    evidence_text = page.normalized_text
    alternative_id = evidence_alternative_id(question["row_id"], 0, 0)
    return {
        "schema_version": 2,
        "protocol_version": "researchqa-adjudication-sidecar-v2",
        "dataset_id": DATASET_ID,
        "dataset_revision": DATASET_REVISION,
        "question_manifest_sha256": question_manifest_sha256([question]),
        "adjudications": {
            alternative_id: {
                "target": {
                    "paper_id": question["paper_id"],
                    "source": {"file_hash": document.file_hash, "extractor_fingerprint": document.extractor_fingerprint},
                    "question": {"row_id": question["row_id"], "fingerprint": question_fingerprint(question)},
                    "group_index": 0,
                    "alternative_index": 0,
                    "reference_sha256": hashlib.sha256(question["expected_references"][0]["alternatives"][0].encode("utf-8")).hexdigest(),
                },
                "verification_state": "adjudicated",
                "gold_version": "audit-v1",
                "provenance": {"label": "agent_adjudicated", "source": "audit.json", "source_revision": "audit-r1"},
                "spans": [{"file_id": page.file_id, "file_hash": document.file_hash, "pdf_page_index": 0,
                           "char_start_in_normalized_page": 0, "char_end_in_normalized_page": len(evidence_text),
                           "page_text_hash": page.page_text_hash, "evidence_text": evidence_text,
                           "evidence_text_hash": hash_text(evidence_text)}],
            }
        },
    }


def _map(question, document, sidecar, *, dataset_id=DATASET_ID, dataset_revision=DATASET_REVISION):
    chunks = chunk_pdf(document, "pdf-fixed-1200", is_main=True).chunks
    return map_all_references([question], chunks, documents={document.paper_id: document},
                              gold_adjudications=sidecar, dataset_id=dataset_id,
                              dataset_revision=dataset_revision, overall_minimum=0.0, per_paper_minimum=0.0)


def test_sidecar_v2_unchanged_single_all_and_offline_agree():
    question, document = _question(), _document()
    sidecar = _sidecar(question, document)
    chunks = chunk_pdf(document, "pdf-fixed-1200", is_main=True).chunks
    assert map_question_references(question, chunks, document=document, gold_adjudications=sidecar,
                                   dataset_id=DATASET_ID, dataset_revision=DATASET_REVISION).groups[0].alternatives[0].mapped
    assert _map(question, document, sidecar).mappings[0].groups[0].alternatives[0].mapped
    result = rescore_payload({"candidate": {"pdf_chunker": "pdf-fixed-1200"}, "question_results": [{"row_id": "q1", "paper_id": "W1", "ranked_item_ids": [chunks[0].chunk_id]}]},
                             {"W1": document}, [question], adjudications=sidecar,
                             dataset_id=DATASET_ID, dataset_revision=DATASET_REVISION)
    assert result["question_manifest_sha256"] == question_manifest_sha256([question])
    assert result["adjudication_sidecar"]["schema_version"] == 2


def test_single_api_accepts_a_full_sidecar_only_with_its_frozen_manifest():
    first, second, document = _question(), _question(), _document()
    second["row_id"] = "q2"
    second["question"] = "What second condition was audited?"
    first_sidecar, second_sidecar = _sidecar(first, document), _sidecar(second, document)
    first_sidecar["adjudications"].update(second_sidecar["adjudications"])
    first_sidecar["question_manifest_sha256"] = question_manifest_sha256([first, second])
    chunks = chunk_pdf(document, "pdf-fixed-1200", is_main=True).chunks
    assert map_all_references([first, second], chunks, documents={"W1": document},
                              gold_adjudications=first_sidecar, dataset_id=DATASET_ID,
                              dataset_revision=DATASET_REVISION, overall_minimum=0.0,
                              per_paper_minimum=0.0).mappings[0].groups[0].alternatives[0].mapped
    assert map_question_references(first, chunks, document=document,
                                   gold_adjudications=first_sidecar,
                                   dataset_id=DATASET_ID, dataset_revision=DATASET_REVISION,
                                   dataset_questions=[first, second]).groups[0].alternatives[0].mapped
    with pytest.raises(StrategyContractError, match="question manifest"):
        map_question_references(first, chunks, document=document,
                                gold_adjudications=first_sidecar, dataset_id=DATASET_ID,
                                dataset_revision=DATASET_REVISION)
    changed = copy.deepcopy(first); changed["question"] = "Changed condition?"
    with pytest.raises(StrategyContractError, match="differs from dataset_questions"):
        map_question_references(changed, chunks, document=document,
                                gold_adjudications=first_sidecar, dataset_id=DATASET_ID,
                                dataset_revision=DATASET_REVISION,
                                dataset_questions=[first, second])
    with pytest.raises(StrategyContractError, match="not present"):
        map_question_references(first, chunks, document=document,
                                gold_adjudications=first_sidecar, dataset_id=DATASET_ID,
                                dataset_revision=DATASET_REVISION,
                                dataset_questions=[second])


@pytest.mark.parametrize("change", [
    "question", "reference_value", "reference_unit", "reference_negation",
    "swap_groups", "swap_alternatives", "paper",
])
def test_sidecar_v2_record_target_rejects_semantic_reuse(change):
    question, document = _question(), _document()
    sidecar = _sidecar(question, document)
    changed = copy.deepcopy(question)
    changed_document = document
    if change == "question":
        changed["question"] = "What opposite condition was audited?"
    elif change.startswith("reference_"):
        changed["expected_references"][0]["alternatives"][0] = {
            "reference_value": "The dose was 7 mg.",
            "reference_unit": "The dose was 5 g.",
            "reference_negation": "The dose was not 5 mg.",
        }[change]
        sidecar["adjudications"][evidence_alternative_id("q1", 0, 0)]["target"]["question"]["fingerprint"] = question_fingerprint(changed)
    elif change == "swap_groups":
        changed["expected_references"].reverse()
        sidecar["adjudications"][evidence_alternative_id("q1", 0, 0)]["target"]["question"]["fingerprint"] = question_fingerprint(changed)
    elif change == "swap_alternatives":
        changed["expected_references"][0]["alternatives"].reverse()
        sidecar["adjudications"][evidence_alternative_id("q1", 0, 0)]["target"]["question"]["fingerprint"] = question_fingerprint(changed)
    else:
        changed["paper_id"] = "W2"
        changed_document = _document("W2", document.file_hash)
        sidecar["adjudications"][evidence_alternative_id("q1", 0, 0)]["target"]["question"]["fingerprint"] = question_fingerprint(changed)
    sidecar["question_manifest_sha256"] = question_manifest_sha256([changed])
    with pytest.raises(StrategyContractError, match="target differs"):
        _map(changed, changed_document, sidecar)


def test_sidecar_v2_envelope_unknown_and_source_fail_closed():
    question, document = _question(), _document()
    sidecar = _sidecar(question, document)
    for key, value, error in (("dataset_id", "other", "dataset identity"), ("dataset_revision", "other", "dataset identity"),
                              ("question_manifest_sha256", "0" * 64, "question manifest")):
        broken = copy.deepcopy(sidecar); broken[key] = value
        with pytest.raises(StrategyContractError, match=error): _map(question, document, broken)
    missing = copy.deepcopy(sidecar); del missing["adjudications"][evidence_alternative_id("q1", 0, 0)]["target"]
    with pytest.raises(StrategyContractError, match="target differs"): _map(question, document, missing)
    unknown = copy.deepcopy(sidecar); unknown["adjudications"]["ea-unknown"] = unknown["adjudications"].pop(evidence_alternative_id("q1", 0, 0))
    with pytest.raises(StrategyContractError, match="unknown alternative IDs"): _map(question, document, unknown)
    changed_document = _document("W1", "0" * 64)
    source = copy.deepcopy(sidecar); source["adjudications"][evidence_alternative_id("q1", 0, 0)]["target"]["source"]["file_hash"] = changed_document.file_hash
    with pytest.raises(StrategyContractError, match="file hash differs"): _map(question, changed_document, source)
    with pytest.raises(StrategyContractError, match="schema/protocol"):
        _map(question, document, {"schema_version": 1, "adjudications": {}})


def test_cli_sidecar_requires_dataset_context_but_exact_only_does_not():
    question = _question()
    errors = SimpleNamespace(error=lambda message: (_ for _ in ()).throw(ValueError(message)))
    assert _load_adjudications_for_current_questions(SimpleNamespace(adjudications=None, dataset_id="optional", dataset_revision=None), [question], errors) is None
    with pytest.raises(ValueError, match="requires --dataset-id"):
        _load_adjudications_for_current_questions(SimpleNamespace(adjudications="sidecar.json", dataset_id=None, dataset_revision=None), [question], errors)


class _Embedder:
    def embed_texts(self, texts):
        return [(1.0, 0.0) for _ in texts]


def test_live_cache_validates_before_reuse():
    question, document = _question(), _document()
    sidecar, cache = _sidecar(question, document), {}
    candidate = StrategyCandidate("pdf-chunker", "sidecar-cache", "pdf-fixed-1200", None, "dense", "pdf-only", "rerank-off", None)
    run_complete_candidate(candidate, {"W1": document}, [question], expected_paper_ids=("W1",), expected_question_ids=("q1",), embedder=_Embedder(), gold_adjudications=sidecar, dataset_id=DATASET_ID, dataset_revision=DATASET_REVISION, mapping_overall_minimum=0.0, mapping_per_paper_minimum=0.0, evidence_mapping_cache=cache)
    assert len(cache) == 1
    with pytest.raises(StrategyContractError, match="dataset identity"):
        run_complete_candidate(candidate, {"W1": document}, [question], expected_paper_ids=("W1",), expected_question_ids=("q1",), embedder=_Embedder(), gold_adjudications=sidecar, dataset_id=DATASET_ID, dataset_revision="other", mapping_overall_minimum=0.0, mapping_per_paper_minimum=0.0, evidence_mapping_cache=cache)
    with pytest.raises(StrategyContractError, match="target differs"):
        run_complete_candidate(candidate, {"W1": _document("W1", "f" * 64)}, [question], expected_paper_ids=("W1",), expected_question_ids=("q1",), embedder=_Embedder(), gold_adjudications=sidecar, dataset_id=DATASET_ID, dataset_revision=DATASET_REVISION, mapping_overall_minimum=0.0, mapping_per_paper_minimum=0.0, evidence_mapping_cache=cache)
