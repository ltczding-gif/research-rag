from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path

import yaml

from benchmarks.scripts import validate_benchmark


REPO_ROOT = Path(__file__).resolve().parent.parent
BENCHMARK_ROOT = REPO_ROOT / "benchmarks"


def _ignore_local_benchmark_state(directory: str, names: list[str]) -> set[str]:
    current = Path(directory)
    if current == BENCHMARK_ROOT:
        return {"artifacts"} & set(names)
    if current == BENCHMARK_ROOT / "corpus":
        return {"files"} & set(names)
    return set()


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        for record in records
    )
    path.write_text(payload, encoding="utf-8")


def _write_suite(root: Path, suite_id: str, papers: list[str], queries: list[str]) -> None:
    path = root / "suites" / f"{suite_id}.yaml"
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    value["paper_ids"] = papers
    value["query_ids"] = queries
    path.write_text(
        yaml.safe_dump(value, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _minimal_valid_root(tmp_path: Path) -> Path:
    root = tmp_path / "benchmarks"
    shutil.copytree(BENCHMARK_ROOT, root, ignore=_ignore_local_benchmark_state)
    sha = "a" * 64
    quote = "The catalyst reached 95 percent selectivity."
    quote_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()

    manifest = {
        "schema_version": 1,
        "paper_id": "paper-1",
        "domain": "catalysis-materials",
        "document_type": "research-article",
        "main_pdf": {
            "file_id": "paper-1-main",
            "artifact_path": "files/paper-1.pdf",
            "source_url": "https://example.org/paper-1.pdf",
            "sha256": sha,
            "license": "CC-BY-4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "redistribution": "allowed",
            "verified_at": "2026-07-27",
            "attribution": "Example Author",
        },
        "si": [
            {
                "file_id": "paper-1-si-1",
                "artifact_path": "files/paper-1-si-1.pdf",
                "source_url": "https://example.org/paper-1-si-1.pdf",
                "sha256": "c" * 64,
                "license": "CC-BY-4.0",
                "license_url": "https://creativecommons.org/licenses/by/4.0/",
                "redistribution": "allowed",
                "verified_at": "2026-07-27",
                "attribution": "Example Author",
            }
        ],
        "doi": "10.0000/example",
        "language": "en",
        "structure_tags": ["single-column"],
    }
    evidence = {
        "schema_version": 1,
        "evidence_id": "ev-1",
        "paper_id": "paper-1",
        "file_id": "paper-1-main",
        "pdf_page_index": 0,
        "printed_page_label": "1",
        "canonical_page_hash": "b" * 64,
        "verbatim_quote": quote,
        "quote_hash": quote_hash,
        "locator": {"char_start": 10, "char_end": 55},
        "evidence_group_id": "group-1",
        "role": "required",
    }
    claim = {
        "schema_version": 1,
        "claim_id": "claim-1",
        "paper_ids": ["paper-1"],
        "text": "The catalyst reached 95 percent selectivity.",
        "evidence_group_ids": ["group-1"],
    }
    query = {
        "schema_version": 1,
        "query_id": "query-1",
        "partition": "d20",
        "domain": "catalysis-materials",
        "text": "What selectivity did the catalyst reach?",
        "language": "en",
        "corpus_language": "en",
        "slice_ids": ["exact-token"],
        "answerability": "answerable",
        "expected_claim_ids": ["claim-1"],
        "required_evidence_group_ids": ["group-1"],
    }
    answer = {
        "schema_version": 1,
        "query_id": "query-1",
        "reference_answer": "It reached 95 percent selectivity.",
        "expected_claim_ids": ["claim-1"],
        "acceptable_abstention": False,
        "abstention_reason": None,
    }
    document_qrel = {
        "schema_version": 1,
        "query_id": "query-1",
        "paper_id": "paper-1",
        "relevance": 3,
        "assessor_id": "assessor-1",
        "adjudication_status": "reviewed",
    }
    evidence_qrel = {
        "schema_version": 1,
        "query_id": "query-1",
        "evidence_id": "ev-1",
        "relevance": 3,
        "assessor_id": "assessor-1",
        "adjudication_status": "reviewed",
    }

    _write_jsonl(root / "corpus" / "manifest.jsonl", [manifest])
    _write_jsonl(root / "gold" / "answers.jsonl", [answer])
    _write_jsonl(root / "gold" / "claims.jsonl", [claim])
    _write_jsonl(root / "gold" / "evidence_units.jsonl", [evidence])
    _write_jsonl(root / "queries" / "queries.jsonl", [query])
    _write_jsonl(root / "queries" / "document_qrels.jsonl", [document_qrel])
    _write_jsonl(root / "queries" / "evidence_qrels.jsonl", [evidence_qrel])
    _write_jsonl(root / "queries" / "judgment_pools.jsonl", [])

    _write_suite(root, "s5", ["paper-1"], ["query-1"])
    _write_suite(root, "d20", ["paper-1"], ["query-1"])
    _write_suite(root, "v20", [], [])
    _write_suite(root, "h60", [], [])
    _write_suite(root, "s100", ["paper-1"], ["query-1"])
    return root


def test_committed_wave0b_corpus_lock_is_valid():
    result = validate_benchmark.validate_benchmark(
        BENCHMARK_ROOT,
        allow_empty=True,
    )

    assert result.ok, result.errors
    assert result.counts["suites"] == 5
    assert result.counts["configs"] == 1
    assert result.counts["manifest"] == 5


def test_committed_s5_has_one_cc_by_main_plus_si_paper_per_domain():
    records = [
        json.loads(line)
        for line in (BENCHMARK_ROOT / "corpus" / "manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    expected_domains = {
        "catalysis-materials",
        "biomedicine",
        "cs-ml",
        "environment-energy-geoscience",
        "social-science-economics",
    }

    assert {record["domain"] for record in records} == expected_domains
    assert len(records) == len(expected_domains)
    for record in records:
        assert record["main_pdf"]["license"] == "CC-BY-4.0"
        assert record["main_pdf"]["redistribution"] == "allowed"
        assert record["si"]
        assert all(item["license"] == "CC-BY-4.0" for item in record["si"])
        assert all(item["redistribution"] == "allowed" for item in record["si"])


def test_minimal_cross_referenced_contract_is_valid(tmp_path):
    root = _minimal_valid_root(tmp_path)

    result = validate_benchmark.validate_benchmark(root)

    assert result.ok, result.errors


def test_evidence_contract_rejects_candidate_chunk_id(tmp_path):
    root = _minimal_valid_root(tmp_path)
    path = root / "gold" / "evidence_units.jsonl"
    evidence = json.loads(path.read_text(encoding="utf-8"))
    evidence["chunk_id"] = "candidate-dependent-id"
    _write_jsonl(path, [evidence])

    result = validate_benchmark.validate_benchmark(root)

    assert not result.ok
    assert any("chunk_id" in error and "unexpected" in error for error in result.errors)


def test_manifest_rejects_absolute_or_parent_artifact_paths(tmp_path):
    root = _minimal_valid_root(tmp_path)
    path = root / "corpus" / "manifest.jsonl"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    unsafe_values = [
        r"C:\Users\maintainer\private.pdf",
        "../private.pdf",
        "/home/maintainer/private.pdf",
    ]

    for unsafe in unsafe_values:
        candidate = copy.deepcopy(manifest)
        candidate["main_pdf"]["artifact_path"] = unsafe
        _write_jsonl(path, [candidate])
        result = validate_benchmark.validate_benchmark(root)
        assert any("safe relative path" in error for error in result.errors)


def test_s5_must_remain_a_subset_of_d20(tmp_path):
    root = _minimal_valid_root(tmp_path)
    _write_suite(root, "d20", [], [])
    _write_suite(root, "s100", [], [])

    result = validate_benchmark.validate_benchmark(root)

    assert not result.ok
    assert "suites: S5 paper_ids must be a subset of D20" in result.errors
    assert "suites: S5 query_ids must be a subset of D20" in result.errors


def test_every_s5_paper_requires_at_least_one_si_file(tmp_path):
    root = _minimal_valid_root(tmp_path)
    path = root / "corpus" / "manifest.jsonl"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["si"] = []
    _write_jsonl(path, [manifest])

    result = validate_benchmark.validate_benchmark(root)

    assert not result.ok
    assert (
        "suite:s5: paper 'paper-1' must include at least one SI file"
        in result.errors
    )


def test_si_requirement_is_scoped_to_s5(tmp_path):
    root = _minimal_valid_root(tmp_path)
    path = root / "corpus" / "manifest.jsonl"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["si"] = []
    _write_jsonl(path, [manifest])
    _write_suite(root, "s5", [], [])

    result = validate_benchmark.validate_benchmark(root)

    assert result.ok, result.errors


def test_unknown_cross_file_reference_is_rejected(tmp_path):
    root = _minimal_valid_root(tmp_path)
    path = root / "queries" / "queries.jsonl"
    query = json.loads(path.read_text(encoding="utf-8"))
    query["expected_claim_ids"] = ["missing-claim"]
    _write_jsonl(path, [query])

    result = validate_benchmark.validate_benchmark(root)

    assert not result.ok
    assert any("unknown expected_claim_id 'missing-claim'" in error for error in result.errors)


def test_release_ready_enforces_partition_quotas(tmp_path):
    root = _minimal_valid_root(tmp_path)

    result = validate_benchmark.validate_benchmark(root, release_ready=True)

    assert not result.ok
    assert "release:s5: expected 5 papers, found 1" in result.errors
    assert "release:h60: expected at least 30 negative queries, found 0" in result.errors
    assert not any(
        "pending fingerprints are not allowed" in error for error in result.errors
    )


def test_cli_accepts_committed_empty_skeleton(capsys):
    status = validate_benchmark.main(
        ["--root", str(BENCHMARK_ROOT), "--allow-empty"]
    )

    captured = capsys.readouterr()
    assert status == 0
    assert "[OK] benchmark contract valid" in captured.out
