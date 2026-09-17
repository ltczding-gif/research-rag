"""Context assembly keeps exact source coordinates and the original ranked hit."""
from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def reader_factory(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "service"))
    module = importlib.import_module("generation_query")

    def make(texts, start, end):
        common = {"generation_id": "g1", "paper_id": "paper1", "file_id": "file1",
                  "file_hash": "pdf-hash", "extractor_fingerprint": "extractor1",
                  "zotero_parent_key": "PARENT", "zotero_attachment_key": "MAIN",
                  "source_role": "main"}
        pages = [{**common, "pdf_page_index": index, "normalized_text": text,
                  "page_text_hash": module.sha256(text)} for index, text in enumerate(texts)]
        spans, offset = [], 0
        for page in pages:
            left = max(start, offset)
            right = min(end, offset + len(page["normalized_text"]))
            if left < right:
                spans.append({"file_id": "file1", "pdf_page_index": page["pdf_page_index"],
                              "char_start_in_normalized_page": left - offset,
                              "char_end_in_normalized_page": right - offset,
                              "page_text_hash": page["page_text_hash"]})
            offset += len(page["normalized_text"]) + 1
        text = "\n".join(texts)[start:end]
        metadata = {**common, "source_spans_json": json.dumps(spans), "text_hash": module.sha256(text)}
        (tmp_path / "pages.jsonl").write_text("\n".join(json.dumps(p) for p in pages), encoding="utf-8")

        class Store:
            def generation_path(self, _manifest):
                return tmp_path

        reader = module.GenerationReader(Store(), {"generation_id": "g1", "artifacts": {"pages": "pages.jsonl"}})
        return reader, metadata, text, pages

    return make


def _plain(result):
    source = result["context_source"]
    marked = result["context"]
    start, end = source["match_start"], source["match_end"]
    # Remove only the inserted markers; literal source text stays untouched.
    return (marked[:start] + marked[start + len("[MATCH]"):end + len("[MATCH]")]
            + marked[end + len("[MATCH]") + len("[/MATCH]"):])


def test_cross_page_subject_condition_and_outcome_are_one_verified_context(reader_factory):
    pages = ["The current of Co/N-CNSNs at a fixed", "potential of 1.50 V vs RHE remained 79% after 30000 s."]
    full = "\n".join(pages)
    reader, metadata, hit, _ = reader_factory(pages, full.index("potential"), len(full))
    result = reader.context(metadata, hit, "ranked-hit")
    assert _plain(result) == full
    source = result["context_source"]
    assert source["verified"] and source["for_chunk_id"] == "ranked-hit"
    assert "chunk_id" not in source  # The expanded passage is not an indexed chunk.
    assert [s["page_number"] for s in source["segments"]] == [1, 2]
    assert [s["quote"] for s in source["segments"]] == pages
    assert hashlib.sha256(full.encode()).hexdigest() == source["text_hash"]


@pytest.mark.parametrize("unit", ["1.04 mA/cm Pt−2", "1.04 mA cm⁻²", "1.1 A mg−1", "0.9 V vs. RHE"])
def test_units_numbers_and_extracted_typography_are_preserved_exactly(reader_factory, unit):
    text = f"Specific activity was measured under stated conditions. The value was {unit}. No conversion applied."
    start = text.index(unit)
    reader, metadata, hit, _ = reader_factory([text], start, start + len(unit))
    result = reader.context(metadata, hit, "hit")
    assert _plain(result) == text
    assert f"[MATCH]{unit}[/MATCH]" in result["context"]
    assert result["context_source"]["segments"][0]["quote"] == text
    assert metadata["text_hash"] == hashlib.sha256(hit.encode()).hexdigest()


def test_overlapping_source_passages_are_not_duplicated(reader_factory):
    text = "The measurement at 0.9 V uses surface area normalization. Activity is 1.04 mA cm−2. The MOR test uses 0.67 V."
    reader, metadata, hit, _ = reader_factory([text], 30, 85)
    result = reader.context(metadata, hit, "hit")
    assert _plain(result) == text
    assert _plain(result).count("0.9 V") == 1
    assert _plain(result).count("0.67 V") == 1


def test_context_budget_keeps_full_hit_and_uses_sentence_edges(reader_factory):
    text = "A complete context sentence. " * 200
    start = 2003
    reader, metadata, hit, _ = reader_factory([text], start, start + 800)
    result = reader.context(metadata, hit, "hit")
    plain, source = _plain(result), result["context_source"]
    assert len(plain) <= 3200
    assert plain[source["match_start"]:source["match_end"]] == hit
    assert source["boundary_status"] == {"start": "sentence_heuristic", "end": "sentence_heuristic"}


def test_chinese_sentence_edges_do_not_require_spaces(reader_factory):
    text = "该测试在固定电位下进行。保留率为百分之七十九。" * 200
    reader, metadata, hit, _ = reader_factory([text], 2000, 2800)
    result = reader.context(metadata, hit, "hit")
    assert result["context_source"]["boundary_status"] == {"start": "sentence_heuristic", "end": "sentence_heuristic"}
    assert _plain(result).endswith("。")


def test_no_sentence_boundary_reports_budget_cut_without_inventing_completeness(reader_factory):
    text = "verylongtable " * 500
    reader, metadata, hit, _ = reader_factory([text], 3000, 3800)
    result = reader.context(metadata, hit, "hit")
    assert len(_plain(result)) == 3200
    assert result["context_source"]["boundary_status"] == {"start": "budget_cut", "end": "budget_cut"}


def test_large_original_hit_is_never_silently_truncated(reader_factory):
    text = "x" * 4000
    reader, metadata, hit, _ = reader_factory([text], 0, len(text))
    result = reader.context(metadata, hit, "hit")
    assert _plain(result) == hit
    assert result["context_source"]["oversized_match"] is True


def test_budget_cut_after_decimal_point_is_not_a_sentence_boundary(reader_factory):
    text = "x" * 3198 + "0.9 V" + "y" * 2000
    reader, metadata, hit, _ = reader_factory([text], 400, 800)
    result = reader.context(metadata, hit, "hit")
    assert result["context_source"]["boundary_status"]["end"] == "budget_cut"


@pytest.mark.parametrize("field,value", [("normalized_text", "tampered text"),
                                         ("zotero_attachment_key", "OTHER"),
                                         ("source_role", "si")])
def test_newly_included_page_is_validated_too(reader_factory, field, value):
    texts = ["The measurement was performed at 0.9 V.", "Specific activity is 1.04 mA cm−2."]
    reader, metadata, hit, pages = reader_factory(texts, 0, len(texts[0]))
    reader.evidence(metadata, hit, "hit")
    reader._pages[("file1", 1)][field] = value
    with pytest.raises(ValueError, match="(identity|hash) mismatch"):
        reader.context(metadata, hit, "hit")


def test_window_does_not_cross_main_and_si_files(reader_factory):
    reader, metadata, hit, pages = reader_factory(["Main measurement."], 0, 17)
    reader.evidence(metadata, hit, "hit")
    reader._pages[("si-file", 0)] = {**pages[0], "file_id": "si-file", "normalized_text": "SI condition 0.67 V"}
    result = reader.context(metadata, hit, "hit")
    assert "0.67 V" not in result["context"]
    assert all(s["file_id"] == "file1" for s in result["context_source"]["segments"])
