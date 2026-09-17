"""Portable tests for the final repository paths; no private trace is read."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest

from benchmarks import researchqa_scoring as scorer
from benchmarks.service_evidence_adapter import (
    AnswerContext,
    CanonicalGeneration,
    ServiceEvidenceAdapterError,
    ServiceRequest,
    score_service_response,
)


def sha(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


class PortableFixture:
    def __init__(self) -> None:
        self.content = "portable evidence quote"
        self.file_id, self.generation_id, self.item_id = "file-portable", "generation-portable", "chunk-portable"
        self.file_hash, self.page_hash = sha("portable-pdf"), sha(self.content)
        self.embedding = {"provider": "fixture", "model": "fixture-model", "dimensions": 3}
        self.span = {"file_id": self.file_id, "pdf_page_index": 0, "char_start_in_normalized_page": 0, "char_end_in_normalized_page": len(self.content), "page_text_hash": self.page_hash}
        self.page = {**self.span, "normalized_text": self.content, "generation_id": self.generation_id, "file_hash": self.file_hash, "zotero_parent_key": "PARENT", "zotero_attachment_key": "ATTACH", "source_role": "main"}
        self.pages_bytes = (json.dumps(self.page, sort_keys=True) + "\n").encode("utf-8")
        self.manifest = {"generation_id": self.generation_id, "contract": {"embedding": self.embedding}, "artifacts": {"pages": "pages.jsonl"}, "artifact_hashes": {"pages.jsonl": sha(self.pages_bytes)}}
        self.manifest_sha = sha(json.dumps(self.manifest, sort_keys=True, separators=(",", ":")))

    def generation(self) -> CanonicalGeneration:
        with tempfile.TemporaryDirectory() as directory:
            page_path = Path(directory) / "pages.jsonl"
            page_path.write_bytes(self.pages_bytes)
            return CanonicalGeneration(self.manifest, self.manifest_sha, page_path.read_bytes())

    def tool_call(self, **extra):
        return {"query": "portable query", "n": 1, "zotero_parent_key": "PARENT", "source_role": "main", **extra}

    def response(self):
        return {"query": "portable query", "effective_query": "portable query", "filters": {"$and": [{"zotero_parent_key": "PARENT"}, {"source_role": "main"}]}, "index_mode": "canonical", "timings_seconds": {"query_embedding": 0.1, "retrieval": 0.1, "rerank": 0.0, "serialization": 0.1, "total": 0.3}, "results": [{"id": self.item_id, "content": self.content, "distance": 0.2, "metadata": {"generation_id": self.generation_id, "file_id": self.file_id, "file_hash": self.file_hash, "text_hash": sha(self.content), "zotero_parent_key": "PARENT", "zotero_attachment_key": "ATTACH", "source_role": "main", "paper_id": "paper-portable", "source_spans_json": json.dumps([self.span], sort_keys=True)}, "evidence": {"verified": True, "generation_id": self.generation_id, "chunk_id": self.item_id, "file_id": self.file_id, "file_hash": self.file_hash, "zotero_parent_key": "PARENT", "zotero_attachment_key": "ATTACH", "source_role": "main", "segments": [{**self.span, "page_number": 1, "quote": self.content, "quote_hash": sha(self.content)}]}}]}

    def mapping(self, *, mapped: bool = True):
        group, alt_id = {"group_id": "required", "alternatives": [self.content]}, scorer.evidence_alternative_id("row-portable", 0, 0)
        override = ({"mapped_item_ids": (), "verification_state": "exact", "gold_version": "fixture-v1", "gold_spans": [{**self.span, "file_hash": self.file_hash, "evidence_text": self.content, "evidence_text_hash": sha(self.content)}]} if mapped else {})
        return scorer.map_reference_groups(row_id="row-portable", paper_id="paper-portable", domain="fixture", question_type="fact", reference_groups=[group], mapper=lambda _: None, alternative_overrides={alt_id: override})


class ServiceEvidenceAdapterTests(unittest.TestCase):
    def score(self, fixture: PortableFixture, **kwargs):
        response = kwargs.pop("response", fixture.response())
        recorded_generation_id = kwargs.pop("recorded_generation_id", fixture.generation_id)
        recorded_embedding = kwargs.pop("recorded_embedding", fixture.embedding)
        return score_service_response(mapping=fixture.mapping(), request=ServiceRequest.from_tool_call(fixture.tool_call(), scope="paper", paper_id="paper-portable"), response=response, generation=fixture.generation(), scorer=scorer, service_revision="fixture-revision", trace_classification="portable_synthetic", recorded_generation_id=recorded_generation_id, recorded_embedding=recorded_embedding, **kwargs).to_dict()

    def test_happy_path_and_empty_context_are_explicit(self):
        score = self.score(PortableFixture(), answer_context=AnswerContext((), ""), client_total_seconds=0.4)
        self.assertEqual(score["retrieval"]["full_group_lower_bound"]["recall_at_10_lower_bound"], 1.0)
        self.assertEqual(score["answer_context"]["status"], "captured")
        self.assertEqual(score["answer_context"]["budget"]["unit"], "unicode_codepoints")
        span = score["retrieval"]["ranked_items"][0]["source_spans"][0]
        self.assertEqual(span["quote"], "portable evidence quote")
        self.assertEqual(span["quote_hash"], sha(span["quote"]))

    def test_rejects_tampering_unknown_filters_n_and_model_mismatch(self):
        fixture, response = PortableFixture(), PortableFixture().response()
        response["results"][0]["evidence"]["segments"][0].update(quote="changed", quote_hash=sha("changed"))
        with self.assertRaisesRegex(ServiceEvidenceAdapterError, "canonical page slice"):
            self.score(fixture, response=response)
        with self.assertRaisesRegex(ServiceEvidenceAdapterError, "unsupported search_papers"):
            ServiceRequest.from_tool_call(fixture.tool_call(include_context=True), scope="paper", paper_id="paper-portable")
        with self.assertRaisesRegex(ServiceEvidenceAdapterError, "unsupported search_papers"):
            ServiceRequest.from_tool_call(fixture.tool_call(unknown_filter="x"), scope="paper", paper_id="paper-portable")
        self.assertEqual(ServiceRequest.from_tool_call(fixture.tool_call(second_query="rewritten"), scope="paper", paper_id="paper-portable").effective_query, "rewritten")
        overflow = fixture.response(); overflow["results"].append(copy.deepcopy(overflow["results"][0]))
        with self.assertRaisesRegex(ServiceEvidenceAdapterError, "more results than requested"):
            self.score(fixture, response=overflow)
        with self.assertRaisesRegex(ServiceEvidenceAdapterError, "embedding differs"):
            self.score(fixture, recorded_embedding={"provider": "fixture", "model": "other", "dimensions": 3})
        with self.assertRaisesRegex(ServiceEvidenceAdapterError, "generation_id differs"):
            self.score(fixture, recorded_generation_id="other-generation")
        wrong_hit_filter = fixture.response(); wrong_hit_filter["results"][0]["metadata"]["source_role"] = "si"
        with self.assertRaisesRegex(ServiceEvidenceAdapterError, "fails requested filter"):
            self.score(fixture, response=wrong_hit_filter)

    def test_rejects_nonfinite_client_time_and_preserves_all_unmapped_lower_bound(self):
        fixture = PortableFixture()
        with self.assertRaisesRegex(ServiceEvidenceAdapterError, "client_total_seconds"):
            self.score(fixture, client_total_seconds=math.nan)
        score = score_service_response(mapping=fixture.mapping(mapped=False), request=ServiceRequest.from_tool_call(fixture.tool_call(), scope="paper", paper_id="paper-portable"), response=fixture.response(), generation=fixture.generation(), scorer=scorer, service_revision="fixture-revision", trace_classification="portable_synthetic", recorded_generation_id=fixture.generation_id, recorded_embedding=fixture.embedding).to_dict()
        lower = score["retrieval"]["full_group_lower_bound"]
        self.assertEqual(lower["recall_at_10_lower_bound"], 0.0)
        self.assertEqual(lower["all_required_groups_success_at_10"], 0.0)


if __name__ == "__main__":
    unittest.main()
