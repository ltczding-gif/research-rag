"""Regression contracts for bounded, source-anchored answer packets."""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def workflow(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "service"))
    return importlib.import_module("answer_workflow"), importlib.import_module("generation_query")


@pytest.fixture
def reader_factory(tmp_path, workflow):
    _, generation_query = workflow

    def make(documents):
        """Build a real GenerationReader and exact hit records for one-page files."""
        generation_id = "generation-1"
        records, by_file = [], {}
        for index, document in enumerate(documents):
            text = document["text"]
            file_id = document.get("file_id", f"file-{index}")
            common = {
                "generation_id": generation_id,
                "paper_id": document.get("paper_id", "PAPER-1"),
                "file_id": file_id,
                "file_hash": document.get("file_hash", f"pdf-hash-{index}"),
                "extractor_fingerprint": "extractor-1",
                "zotero_parent_key": document.get("parent", "PARENT-1"),
                "zotero_attachment_key": document.get("attachment", f"ATTACHMENT-{index}"),
                "source_role": document.get("role", "main"),
            }
            page = {**common, "pdf_page_index": 0, "normalized_text": text,
                    "page_text_hash": generation_query.sha256(text)}
            records.append(page)
            by_file[file_id] = (common, text, page)

        (tmp_path / "pages.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records), encoding="utf-8"
        )

        class Store:
            def generation_path(self, _manifest):
                return tmp_path

        reader = generation_query.GenerationReader(
            Store(), {"generation_id": generation_id, "artifacts": {"pages": "pages.jsonl"}}
        )

        def hit(file_id, start=0, end=None, hit_id=None):
            common, text, page = by_file[file_id]
            end = len(text) if end is None else end
            content = text[start:end]
            span = {
                "file_id": file_id,
                "pdf_page_index": 0,
                "char_start_in_normalized_page": start,
                "char_end_in_normalized_page": end,
                "page_text_hash": page["page_text_hash"],
            }
            return {
                "id": hit_id or f"hit-{file_id}-{start}-{end}",
                "content": content,
                "metadata": {**common, "text_hash": generation_query.sha256(content),
                             "source_spans_json": json.dumps([span])},
            }

        return reader, hit

    return make


def _source_text(label):
    return (f"{label} electrocatalyst activity was measured at 0.90 V vs RHE under stated conditions. "
            "The retained current after 30,000 s was 79 percent. ") * 4


def _coordinates(items, file_id):
    points = set()
    for item in items:
        if item["metadata"]["file_id"] != file_id:
            continue
        for segment in item["source"]["segments"]:
            points.update((segment["pdf_page_index"], position)
                          for position in range(segment["char_start_in_normalized_page"],
                                                segment["char_end_in_normalized_page"]))
    return points


def test_prepare_packet_deduplicates_same_source_without_merging_attachments(
        workflow, reader_factory):
    answer_workflow, _ = workflow
    shared = _source_text("Shared")
    reader, hit = reader_factory([
        {"file_id": "main-file", "attachment": "MAIN", "role": "main", "text": shared},
        {"file_id": "si-file", "attachment": "SI", "role": "si", "text": shared},
    ])
    hits = [
        hit("main-file", 0, 160, "main-first"),
        hit("main-file", 80, 240, "main-overlap"),
        hit("si-file", 0, 160, "si-first"),
    ]
    original_hits = copy.deepcopy(hits)

    packet = answer_workflow.prepare_packet("activity at 0.90 V", hits, reader, budget=800)

    assert hits == original_hits
    assert {item["metadata"]["zotero_attachment_key"] for item in packet["evidence"]} == {"MAIN", "SI"}
    assert {item["metadata"]["file_id"] for item in packet["evidence"]} == {"main-file", "si-file"}
    assert {"main-first", "main-overlap", "si-first"} <= {
        item["source"]["for_chunk_id"] for item in packet["evidence"]
    }
    for file_id in ("main-file", "si-file"):
        source_points = _coordinates(packet["evidence"], file_id)
        original_points = _coordinates(
            [{"metadata": hit["metadata"], "source": reader.evidence(
                hit["metadata"], hit["content"], hit["id"]
            )} for hit in hits if hit["metadata"]["file_id"] == file_id],
            file_id,
        )
        assert original_points <= source_points
    seen = {}
    for item in packet["evidence"]:
        identity = tuple(item["metadata"][key] for key in (
            "generation_id", "paper_id", "file_id", "file_hash", "extractor_fingerprint",
            "zotero_parent_key", "zotero_attachment_key", "source_role",
        ))
        points = _coordinates([item], item["metadata"]["file_id"])
        assert not seen.setdefault(identity, set()).intersection(points)
        seen[identity].update(points)


def test_prepare_packet_preserves_all_nonduplicate_original_hits_that_fit_budget(
        workflow, reader_factory):
    answer_workflow, _ = workflow
    text = "Prefix condition text. " * 140
    reader, hit = reader_factory([{"file_id": "main-file", "text": text}])
    hits = [hit("main-file", 100, 250, "first"), hit("main-file", 1200, 1350, "second")]

    packet = answer_workflow.prepare_packet("condition text", hits, reader, budget=400)

    original_points = _coordinates(
        [{"metadata": item["metadata"], "source": reader.evidence(
            item["metadata"], item["content"], item["id"]
        )} for item in hits],
        "main-file",
    )
    assert packet["used_codepoints"] <= 400
    assert original_points <= _coordinates(packet["evidence"], "main-file")
    assert {"first", "second"} <= {item["source"]["for_chunk_id"] for item in packet["evidence"]}
    assert all(item["kind"] == "retrieved" for item in packet["evidence"][:2])


def test_prepare_packet_enforces_budget_and_keeps_verifiable_quote_locations(
        workflow, reader_factory):
    answer_workflow, generation_query = workflow
    text = _source_text("Nickel") + "The specific activity was 1.04 mA cm−2."
    reader, hit = reader_factory([{"file_id": "main-file", "attachment": "MAIN", "text": text}])
    packet = answer_workflow.prepare_packet(
        "What activity was measured at 0.90 V?", [hit("main-file", 10, 330, "ranked")], reader,
        budget=256,
    )

    assert packet["used_codepoints"] == sum(len(item["content"]) for item in packet["evidence"])
    assert packet["used_codepoints"] <= packet["budget_codepoints"] == 256
    item = packet["evidence"][0]
    assert item["metadata"]["text_hash"] == generation_query.sha256(item["content"])
    proof = reader.evidence(item["metadata"], item["content"], item["source"]["for_chunk_id"])
    assert proof["segments"] == item["source"]["segments"]
    assert proof["segments"][0]["quote"] == item["content"]
    assert proof["segments"][0]["quote_hash"] == hashlib.sha256(item["content"].encode()).hexdigest()


def test_empty_packet_requires_an_explicit_insufficiency_explanation(workflow, reader_factory):
    answer_workflow, _ = workflow
    reader, _ = reader_factory([{"text": _source_text("Empty") }])
    packet = answer_workflow.prepare_packet("Which catalyst is best?", [], reader, budget=256)

    assert packet["status"] == "insufficient_evidence"
    assert packet["evidence"] == []
    blank = answer_workflow.check_answer(packet, {"claims": [], "missing_information": []}, reader)
    assert not blank["citation_checks_passed"]
    assert blank["errors"] == ["Provide supported claims or explain what evidence is missing"]
    explained = answer_workflow.check_answer(
        packet, {"claims": [], "missing_information": ["No retrieved evidence identifies a catalyst."]}, reader
    )
    assert explained["citation_checks_passed"]
    assert explained["semantic_support"] == "requires_review"
    with pytest.raises(ValueError, match="packet must be the prepare_answer result object"):
        answer_workflow.check_answer(None, {"claims": [], "missing_information": []}, reader)


def test_prepare_packet_rejects_budget_above_maximum(workflow, reader_factory):
    answer_workflow, _ = workflow
    reader, _ = reader_factory([{"text": _source_text("Limit")}])

    with pytest.raises(ValueError, match="integer between 256 and 8000"):
        answer_workflow.prepare_packet("What was measured?", [], reader, budget=8001)


def test_check_answer_requires_known_verbatim_quotes_but_never_certifies_semantics(
        workflow, reader_factory):
    answer_workflow, _ = workflow
    text = _source_text("Cobalt")
    reader, hit = reader_factory([{"file_id": "main-file", "text": text}])
    packet = answer_workflow.prepare_packet("What was retained?", [hit("main-file", 0, 170)], reader, budget=400)
    valid = {"claims": [{"text": "Cobalt eliminates all degradation forever.", "citations": [
        {"evidence_id": "E1"}
    ]}], "missing_information": []}

    accepted = answer_workflow.check_answer(packet, valid, reader)
    assert accepted["citation_checks_passed"]
    assert accepted["semantic_support"] == "requires_review"
    assert accepted["claim_bindings"][0]["quote"] == packet["evidence"][0]["content"]
    unknown = copy.deepcopy(valid)
    unknown["claims"][0]["citations"][0]["evidence_id"] = "E999"
    mismatched = copy.deepcopy(valid)
    mismatched["claims"][0]["citations"][0]["quote"] = "invented quotation"
    for invalid in (unknown, mismatched):
        result = answer_workflow.check_answer(packet, invalid, reader)
        assert not result["citation_checks_passed"]
        assert result["errors"] == ["Claim 1: unknown evidence or quote not present verbatim"]


def test_check_answer_rejects_tampered_packet_and_changed_canonical_source(workflow, reader_factory):
    answer_workflow, _ = workflow
    text = _source_text("Iron")
    reader, hit = reader_factory([{"file_id": "main-file", "text": text}])
    packet = answer_workflow.prepare_packet("What was retained?", [hit("main-file", 0, 170)], reader, budget=400)
    answer = {"claims": [], "missing_information": ["No comparison evidence was retrieved."]}

    tampered = copy.deepcopy(packet)
    tampered["evidence"][0]["content"] = "forged packet evidence"
    with pytest.raises(ValueError, match="Evidence packet changed"):
        answer_workflow.check_answer(tampered, answer, reader)

    reader._pages[("main-file", 0)]["normalized_text"] = "changed source"
    with pytest.raises(ValueError, match="Canonical page text hash mismatch"):
        answer_workflow.check_answer(packet, answer, reader)


def test_answer_messages_exposes_only_question_and_evidence_data(workflow, reader_factory):
    answer_workflow, _ = workflow
    reader, hit = reader_factory([{"file_id": "main-file", "text": _source_text("Gold")}])
    packet = answer_workflow.prepare_packet("What was retained?", [hit("main-file", 0, 170)], reader, budget=400)

    system, user = answer_workflow.answer_messages(packet)
    payload = json.loads(user["content"])
    assert system == {"role": "system", "content": answer_workflow.ANSWER_INSTRUCTIONS}
    assert user["role"] == "user" and payload["question"] == packet["question"]
    assert payload["evidence"][0]["id"] == "E1"
    assert payload["evidence"][0]["content"] == packet["evidence"][0]["content"]
    assert payload["evidence"][0]["pages"] == [1]
