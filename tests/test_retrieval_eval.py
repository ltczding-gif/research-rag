"""Retrieval evaluation: coordinate scoring, ledger privacy, pooling and resolution."""
from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from benchmarks import retrieval_eval as ev  # noqa: E402


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


PAGES = {
    # (parent, attachment, role): [page texts]
    ("PARENTA", "ATTA", "main"): [
        "Cobalt single atoms reach a half-wave potential of 0.91 V in alkaline media.",
        "Durability: the catalyst retains 95% activity after 10000 cycles.",
    ],
    ("PARENTA", "ATTS", "si"): [
        "Supplementary Table S3 lists the Co loading of 1.2 wt% measured by ICP-OES.",
    ],
    ("PARENTB", "ATTB", "main"): [
        "Nickel iron hydroxide shows an OER overpotential of 240 mV at 10 mA cm-2.",
    ],
}
VOCAB = ["cobalt", "durability", "loading", "nickel"]


def _embed(text):
    lowered = text.lower()
    vector = [float(lowered.count(word)) for word in VOCAB]
    return vector if any(vector) else [0.01] * len(VOCAB)


@pytest.fixture
def core(monkeypatch, tmp_path):
    pytest.importorskip("chromadb")
    import chromadb

    names = ("config", "embedding_client", "index_generation", "generation_query", "query_server")
    saved = {name: sys.modules.pop(name, None) for name in names}
    monkeypatch.syspath_prepend(str(REPO_ROOT / "service"))
    monkeypatch.setenv("LOCALRAG_SKIP_CHROMA_INIT", "1")
    module = importlib.import_module("query_server")
    from generation_query import GenerationReader

    store = module.GenerationStore(tmp_path, "papers")
    embedding = {"provider": "test", "model": "bag-of-words", "revision": "r1", "dimensions": len(VOCAB)}
    sources = []
    for index, (parent, attachment, role) in enumerate(PAGES):
        sources.append({"source_id": f"file-{index}", "content_hash": str(index), "status": "success",
                        "zotero_parent_key": parent, "zotero_attachment_key": attachment,
                        "source_role": role, "pdf_filename": f"{attachment}.pdf"})
    manifest = store.begin({"embedding": embedding}, sources)
    client = chromadb.PersistentClient(path=str(tmp_path))
    collection = client.create_collection(
        manifest["collection_name"],
        metadata={"hnsw:space": "cosine", "generation_id": manifest["generation_id"]})
    records = []
    for index, ((parent, attachment, role), texts) in enumerate(PAGES.items()):
        file_hash = _hash(f"pdf-{attachment}")
        common = {"generation_id": manifest["generation_id"], "paper_id": parent, "file_id": f"file-{index}",
                  "file_hash": file_hash, "extractor_fingerprint": "extractor-1",
                  "zotero_parent_key": parent, "zotero_attachment_key": attachment, "source_role": role}
        for page_index, text in enumerate(texts):
            records.append({**common, "pdf_page_index": page_index, "normalized_text": text,
                            "page_text_hash": _hash(text)})
            span = {"file_id": f"file-{index}", "pdf_page_index": page_index,
                    "char_start_in_normalized_page": 0, "char_end_in_normalized_page": len(text),
                    "page_text_hash": _hash(text)}
            meta = {**common, "text_hash": _hash(text), "source_spans_json": json.dumps([span]),
                    "source_type": "pdf", "pdf_filename": f"{attachment}.pdf",
                    "previous_chunk_id": "", "next_chunk_id": ""}
            collection.add(ids=[f"chunk-{attachment}-{page_index}"], documents=[text],
                           embeddings=[_embed(text)], metadatas=[meta])
    module.atomic_write_text(store.generation_path(manifest) / "pages.jsonl",
                             "\n".join(json.dumps(row) for row in records))
    store.publish(manifest, item_count=len(records), artifacts={"pages": "pages.jsonl"})
    manifest = store.load_active()
    monkeypatch.setattr(module, "pdf_col", collection)
    monkeypatch.setattr(module, "pdf_generation", GenerationReader(store, manifest))
    monkeypatch.setattr(module, "chroma_ready", True)
    monkeypatch.setattr(module, "embedding_contract", lambda **kw: dict(embedding))
    monkeypatch.setattr(module, "embed_index_text", _embed)
    try:
        yield module
    finally:
        for name in names:
            sys.modules.pop(name, None)
            if saved[name] is not None:
                sys.modules[name] = saved[name]


def _span(core, attachment, page_index, quote, evidence_id, group=None, relevance=3):
    pages = ev.load_pages(core.pdf_generation)
    page = next(p for p in pages.values()
                if p["zotero_attachment_key"] == attachment and p["pdf_page_index"] == page_index)
    start = page["normalized_text"].index(quote)
    return {"evidence_id": evidence_id, "group": group or evidence_id, "relevance": relevance,
            "file_hash": page["file_hash"], "pdf_page_index": page_index,
            "page_text_hash": page["page_text_hash"], "char_start": start, "char_end": start + len(quote)}


def _write_suite(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


# -- pure scoring --------------------------------------------------------------


def _interval(span, start, end):
    return (span.key, start, end)


def test_span_coverage_requires_the_union_to_contain_the_whole_span():
    span = ev.EvidenceSpan("e1", "g1", "a" * 64, 0, "b" * 64, 10, 50)
    assert ev.span_covered(span, [_interval(span, 0, 30), _interval(span, 30, 60)])
    assert not ev.span_covered(span, [_interval(span, 0, 30), _interval(span, 31, 60)])
    assert ev.span_touched(span, [_interval(span, 0, 11)])
    other_page = (("a" * 64, 1, "b" * 64), 0, 100)
    assert not ev.span_covered(span, [other_page])


def test_changed_page_hash_never_counts_as_coverage():
    span = ev.EvidenceSpan("e1", "g1", "a" * 64, 0, "b" * 64, 0, 5)
    assert not ev.span_covered(span, [(("a" * 64, 0, "c" * 64), 0, 100)])


def test_groups_are_alternatives_and_all_groups_complete_a_query():
    spans = [ev.EvidenceSpan("e1", "g1", "a" * 64, 0, "b" * 64, 0, 5),
             ev.EvidenceSpan("e2", "g1", "a" * 64, 1, "b" * 64, 0, 5),
             ev.EvidenceSpan("e3", "g2", "a" * 64, 2, "b" * 64, 0, 5),
             ev.EvidenceSpan("e4", "g3", "a" * 64, 3, "b" * 64, 0, 5, relevance=1)]
    query = ev.EvalQuery("q1", "question", evidence=spans)

    def hit(page):
        return {"evidence": {"verified": True, "file_hash": "a" * 64, "segments": [
            {"pdf_page_index": page, "page_text_hash": "b" * 64,
             "char_start_in_normalized_page": 0, "char_end_in_normalized_page": 10}]}}

    partial = ev.score_query(query, [hit(1)], [1])
    assert partial["evidence_spans"] == 3  # relevance-1 background is not scored
    assert partial["span_coverage@1"] == pytest.approx(1 / 3)
    assert partial["query_complete@1"] == 0.0
    complete = ev.score_query(query, [hit(1), hit(2)], [2])
    assert complete["query_complete@2"] == 1.0
    assert complete["first_evidence_rank"] == 1


def test_suite_validation_rejects_bad_records(tmp_path):
    bad = _write_suite(tmp_path / "bad.jsonl", [{"query_id": "q1", "text": "x", "evidence": [
        {"evidence_id": "e1", "file_hash": "nothex", "pdf_page_index": 0, "page_text_hash": "a" * 64,
         "char_start": 0, "char_end": 3}]}])
    with pytest.raises(ev.SuiteError, match="file_hash"):
        ev.load_suite(bad)
    dup = _write_suite(tmp_path / "dup.jsonl", [{"query_id": "q1", "text": "x"}, {"query_id": "q1", "text": "y"}])
    with pytest.raises(ev.SuiteError, match="duplicate query_id"):
        ev.load_suite(dup)
    with pytest.raises(ev.SuiteError, match="filters"):
        ev.parse_query({"query_id": "q1", "text": "x", "filters": {"where": "x"}})


def test_paired_delta_reports_direction_and_interval():
    suite = {"sha256": "s1", "evidence_fingerprint": "f1"}
    base = {"suite": suite, "per_query": {f"q{i}": {"span_coverage@10": 0.0} for i in range(10)}}
    better = {"suite": suite, "per_query": {f"q{i}": {"span_coverage@10": 1.0 if i < 6 else 0.0} for i in range(10)}}
    delta = ev.paired_delta(base, better)
    assert delta["mean_delta"] == pytest.approx(0.6)
    assert delta["wins"] == 6 and delta["losses"] == 0 and delta["ties"] == 4
    assert 0 < delta["ci95"][0] <= delta["mean_delta"] <= delta["ci95"][1]


# -- end to end against a canonical generation -----------------------------------------


def test_run_scores_a_real_generation_and_ledger_holds_no_private_text(core, tmp_path):
    suite_path = _write_suite(tmp_path / "private-set.jsonl", [
        {"query_id": "q-durability", "text": "SECRET-QUESTION durability of cobalt",
         "slices": ["single-paper"],
         "evidence": [_span(core, "ATTA", 1, "retains 95% activity after 10000 cycles", "e1")]},
        {"query_id": "q-loading", "text": "cobalt loading in the SI",
         "evidence": [_span(core, "ATTS", 0, "Co loading of 1.2 wt%", "e2"),
                      _span(core, "ATTA", 0, "half-wave potential of 0.91 V", "e3")]},
    ])
    suite = ev.load_suite(suite_path)
    record = ev.run_strategy(core, suite, "dense", ks=(1, 10), repetitions=2, packet_budget=8000)
    overall = record["metrics"]["overall"]
    assert overall["span_coverage@10"] == 1.0
    assert 0 < overall["span_coverage@1"] < 1.0
    assert overall["packet_span_coverage"] == 1.0
    assert record["stability"] == {"membership_identical": 2, "order_identical": 2, "queries": 2}
    assert record["index"]["papers_generation_id"] == core.pdf_generation.manifest["generation_id"]
    assert record["metrics"]["by_slice"]["single-paper"]["span_coverage@10"] == 1.0

    ledger = tmp_path / "ledger.jsonl"
    ev.append_ledger(record, ledger)
    text = ledger.read_text(encoding="utf-8")
    for private in ("SECRET-QUESTION", "PARENTA", "retains 95%", str(tmp_path)):
        assert private not in text
    assert ev.read_ledger(ledger)[0]["run_id"] == record["run_id"]


def test_unscorable_gold_fails_the_run_unless_explicitly_allowed(core, tmp_path):
    stale = _span(core, "ATTA", 1, "retains 95% activity", "e1")
    stale["page_text_hash"] = "f" * 64
    suite = ev.load_suite(_write_suite(tmp_path / "s.jsonl", [
        {"query_id": "q1", "text": "durability", "evidence": [
            stale, _span(core, "ATTS", 0, "Co loading of 1.2 wt%", "e2")]}]))
    with pytest.raises(ev.SuiteError, match="unscorable.*canonical page text changed"):
        ev.run_strategy(core, suite, "dense", ks=(10,), packet_budget=None)
    record = ev.run_strategy(core, suite, "dense", ks=(10,), packet_budget=None, allow_unscorable=True)
    assert record["suite"]["unscorable_spans"] == 1 and record["suite"]["scored_spans"] == 1
    assert record["unscorable"][0]["reason"] == "canonical page text changed"


def test_runs_that_scored_different_gold_are_never_paired(core, tmp_path, monkeypatch):
    # Same eval-set file; the second run happens after a re-extraction made one
    # hard-to-find span unscorable. Dropping it must not look like a gain.
    suite = ev.load_suite(_write_suite(tmp_path / "s.jsonl", [
        {"query_id": "q1", "text": "nickel", "evidence": [_span(core, "ATTA", 1, "retains 95% activity", "e1")]},
        {"query_id": "q2", "text": "nickel", "evidence": [_span(core, "ATTB", 0, "240 mV", "e2")]},
    ]))
    full = ev.run_strategy(core, suite, "dense", ks=(1, 10), packet_budget=None)
    original = ev.check_scorable
    monkeypatch.setattr(ev, "check_scorable", lambda s, p: [
        *original(s, p), {"query_id": "q1", "evidence_id": "e1", "reason": "canonical page text changed"}])
    reduced = ev.run_strategy(core, suite, "dense", ks=(1, 10), packet_budget=None, allow_unscorable=True)
    assert full["suite"]["sha256"] == reduced["suite"]["sha256"]
    assert full["suite"]["evidence_fingerprint"] != reduced["suite"]["evidence_fingerprint"]
    assert ev.paired_delta(full, reduced, "span_coverage@1")["not_comparable"] == "different scored evidence"
    table = ev.compare_table([full, reduced], ["span_coverage@1"], primary="span_coverage@1")
    assert "not comparable: different scored evidence" in table
    assert "excluded unscorable gold spans" in table


def test_empty_eval_sets_never_produce_a_successful_record(core, tmp_path):
    official = ev.load_official_suite(REPO_ROOT / "benchmarks", "s5")
    with pytest.raises(ev.SuiteError, match="no queries"):
        ev.run_strategy(core, official, "dense")
    no_gold = ev.load_suite(_write_suite(tmp_path / "s.jsonl", [{"query_id": "q1", "text": "cobalt"}]))
    with pytest.raises(ev.SuiteError, match="no scorable gold evidence"):
        ev.run_strategy(core, no_gold, "dense")


def test_packet_scoring_uses_the_same_filters_as_retrieval(core, tmp_path):
    # Gold lies in ATTA; the query is restricted to ATTB.pdf. Both scores must be 0.
    suite = ev.load_suite(_write_suite(tmp_path / "s.jsonl", [
        {"query_id": "q1", "text": "cobalt durability", "filters": {"pdf_filename": "ATTB.pdf"},
         "evidence": [_span(core, "ATTA", 1, "retains 95% activity", "e1")]}]))
    record = ev.run_strategy(core, suite, "dense", ks=(10,), packet_budget=8000)
    overall = record["metrics"]["overall"]
    assert overall["span_coverage@10"] == 0.0
    assert overall["packet_span_coverage"] == 0.0
    packet, status = core.prepare_answer_payload("cobalt durability", pdf_filename="ATTB.pdf")
    assert status == 200
    assert {item["metadata"]["zotero_attachment_key"] for item in packet["evidence"]} == {"ATTB"}


def test_resolve_is_lossless_for_candidates_it_does_not_convert(core, tmp_path):
    pooled = ev.pool_candidates(core, ev.load_suite(_write_suite(
        tmp_path / "s.jsonl", [{"query_id": "q1", "text": "cobalt nickel durability loading"}])), ["dense"], depth=4)
    candidates = pooled.queries[0].candidates
    assert len(candidates) == 4
    converted, bad_quote, irrelevant, unjudged = candidates
    converted.update(relevance=3, quote=converted["text"][:12])
    bad_quote.update(relevance=3, quote="text that is not on this page")
    irrelevant.update(relevance=0)
    path = tmp_path / "judged.jsonl"
    path.write_text(json.dumps(ev.dump_query(pooled.queries[0])) + "\n", encoding="utf-8")
    resolved, problems = ev.resolve_suite(ev.load_suite(path), ev.load_pages(core.pdf_generation))
    query = resolved.queries[0]
    assert len(query.evidence) == 1
    remaining = {c["candidate_id"] for c in query.candidates}
    assert remaining == {bad_quote["candidate_id"], irrelevant["candidate_id"], unjudged["candidate_id"]}
    assert len(problems) == 1 and "found 0 times" in problems[0]


def test_quote_only_evidence_must_be_resolved_before_running(core, tmp_path):
    suite = ev.load_suite(_write_suite(tmp_path / "s.jsonl", [
        {"query_id": "q1", "text": "durability", "evidence": [
            {"evidence_id": "e1", "quote": "retains 95% activity", "zotero_parent_key": "PARENTA"}]}]))
    with pytest.raises(ev.SuiteError, match="resolve"):
        ev.run_strategy(core, suite, "dense")
    resolved, problems = ev.resolve_suite(suite, ev.load_pages(core.pdf_generation))
    assert problems == []
    span = resolved.queries[0].evidence[0]
    page = ev.load_pages(core.pdf_generation)[(span.file_hash, span.pdf_page_index)]
    assert page["normalized_text"][span.char_start:span.char_end] == "retains 95% activity"
    assert span.zotero_parent_key == "PARENTA"


def test_quotes_match_across_whitespace_but_must_be_unique(core, tmp_path):
    pages = ev.load_pages(core.pdf_generation)
    suite = ev.load_suite(_write_suite(tmp_path / "s.jsonl", [
        {"query_id": "q1", "text": "x", "evidence": [
            {"evidence_id": "e1", "quote": "OER   overpotential\nof 240 mV"},
            {"evidence_id": "e2", "quote": "the"}]}]))
    resolved, problems = ev.resolve_suite(suite, pages)
    assert [s.evidence_id for s in resolved.queries[0].evidence] == ["e1"]
    assert len(problems) == 1 and "e2" in problems[0]


def test_pool_then_judge_then_resolve_round_trip(core, tmp_path):
    suite = ev.load_suite(_write_suite(tmp_path / "s.jsonl", [{"query_id": "q1", "text": "cobalt durability"}]))
    pooled = ev.pool_candidates(core, suite, ["dense"], depth=3)
    candidates = pooled.queries[0].candidates
    assert candidates and all(c["relevance"] is None for c in candidates)
    target = next(c for c in candidates if "10000 cycles" in c["text"])
    target.update(relevance=3, quote="after 10000 cycles")
    unquoted = next(c for c in candidates if c is not target)
    unquoted["relevance"] = 2
    path = tmp_path / "judged.jsonl"
    path.write_text(json.dumps(ev.dump_query(pooled.queries[0])) + "\n", encoding="utf-8")
    resolved, problems = ev.resolve_suite(ev.load_suite(path), ev.load_pages(core.pdf_generation))
    assert len(resolved.queries[0].evidence) == 1
    assert len(problems) == 1 and "no quote" in problems[0]
    record = ev.run_strategy(core, resolved, "dense", ks=(10,), packet_budget=None)
    assert record["metrics"]["overall"]["span_coverage@10"] == 1.0


def test_compare_table_marks_baseline_and_suite_version_drift():
    def record(run_id, value, sha="s1"):
        return {"run_id": run_id, "strategy": {"name": "dense"},
                "suite": {"sha256": sha, "evidence_fingerprint": "f1", "unscorable_spans": 0},
                "index": {"papers_generation_id": "abcdef1234", "embedding": {"provider": "p", "model": "m"}},
                "metrics": {"overall": {"span_coverage@10": value}},
                "per_query": {"q1": {"span_coverage@10": value}, "q2": {"span_coverage@10": value}}}

    table = ev.compare_table([record("a", 0.5), record("b", 1.0)], ["span_coverage@10"])
    assert "a (baseline)" in table and "+0.500" in table and "2/0/0" in table
    drift = ev.compare_table([record("a", 0.5), record("b", 1.0, sha="s2")], ["span_coverage@10"])
    assert "different versions of the eval set" in drift


def test_cli_compare_reads_ledger(tmp_path, capsys):
    from benchmarks.scripts import retrieval_eval as cli

    ledger = tmp_path / "ledger.jsonl"
    ev.append_ledger({"run_id": "r1", "strategy": {"name": "dense"},
                      "suite": {"suite_id": "w6", "sha256": "s"},
                      "index": {"papers_generation_id": "abcdef12", "embedding": {"provider": "p", "model": "m"}},
                      "metrics": {"overall": {"span_coverage@10": 0.548}}, "per_query": {}}, ledger)
    assert cli.main(["compare", "--suite-id", "w6", "--ledger", str(ledger)]) == 0
    assert "0.548" in capsys.readouterr().out


def test_cli_pool_resolve_run_compare_round_trip(core, tmp_path, monkeypatch, capsys):
    from benchmarks.scripts import retrieval_eval as cli

    monkeypatch.setattr(cli, "load_core", lambda: core)
    questions = _write_suite(tmp_path / "questions.jsonl", [{"query_id": "q1", "text": "nickel OER overpotential"}])
    pool = tmp_path / "pool.jsonl"
    assert cli.main(["pool", "--suite", str(questions), "--depth", "2", "--output", str(pool)]) == 0
    record = json.loads(pool.read_text(encoding="utf-8"))
    for candidate in record["candidates"]:
        relevant = "240 mV" in candidate["text"]
        candidate.update(relevance=3 if relevant else 0, quote="240 mV at 10 mA cm-2" if relevant else None)
    pool.write_text(json.dumps(record) + "\n", encoding="utf-8")
    eval_set = tmp_path / "eval.jsonl"
    assert cli.main(["resolve", "--suite", str(pool), "--output", str(eval_set)]) == 0
    ledger = tmp_path / "ledger.jsonl"
    for label in ("baseline", "again"):
        assert cli.main(["run", "--suite", str(eval_set), "--suite-id", "demo", "--repetitions", "1",
                         "--ledger", str(ledger), "--label", label]) == 0
    runs = ev.read_ledger(ledger)
    assert [r["label"] for r in runs] == ["baseline", "again"]
    assert runs[0]["metrics"]["overall"]["span_coverage@10"] == 1.0
    assert runs[0]["settings"]["packet_budget"] == 8000
    capsys.readouterr()
    assert cli.main(["compare", "--suite-id", "demo", "--ledger", str(ledger)]) == 0
    assert "+0.000" in capsys.readouterr().out
