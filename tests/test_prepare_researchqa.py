from __future__ import annotations

import hashlib
import json
from pathlib import Path

from benchmarks.scripts import prepare_researchqa


REPO_ROOT = Path(__file__).resolve().parent.parent


def _write_fixture(path: Path) -> list[dict]:
    rows = []
    for domain in ("domain-a", "domain-b"):
        for paper_number in range(4):
            paper_id = f"{domain}-paper-{paper_number}"
            for question_type in ("lookup", "multi_hop"):
                rows.append(
                    {
                        "paper_id": paper_id,
                        "paper_doi": f"10.0000/{paper_id}",
                        "paper_s3_url": f"https://example.test/{paper_id}.pdf",
                        "domain": domain,
                        "row_id": f"{paper_id}-{question_type}",
                        "question_type": question_type,
                        "question": f"{question_type} question for {paper_id}",
                        "expected_answer": f"answer for {paper_id}",
                        "expected_references": [],
                    }
                )
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        ),
        encoding="utf-8",
        newline="\n",
    )
    return rows


def _fixture_contract(source_path: Path, rows: list[dict]) -> dict:
    payload = source_path.read_bytes()
    return {
        "schema_version": 1,
        "benchmark_id": "researchqa",
        "status": "active",
        "source": {
            "filename": source_path.name,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "revision": "a" * 40,
        },
        "selection": {
            "seed": "fixture-seed",
            "unit": "paper",
            "ranking": "sha256-seed-domain-paper-id",
            "include_all_questions": True,
            "nested": True,
            "tiers": [
                {
                    "tier_id": "rq-2",
                    "papers_per_domain": 1,
                    "expected_papers": 2,
                    "expected_questions": 4,
                    "purpose": "fixture smoke",
                },
                {
                    "tier_id": "rq-5",
                    "papers_per_domain": 2,
                    "expected_papers": 4,
                    "expected_questions": 8,
                    "purpose": "fixture development",
                },
                {
                    "tier_id": "rq-10",
                    "papers_per_domain": 3,
                    "expected_papers": 6,
                    "expected_questions": 12,
                    "purpose": "fixture confirmation",
                },
                {
                    "tier_id": "rq-all",
                    "papers_per_domain": "all",
                    "expected_papers": 8,
                    "expected_questions": 16,
                    "purpose": "fixture full",
                },
            ],
        },
        "expected": {
            "papers": 8,
            "questions": len(rows),
            "domains": {
                "domain-a": {"papers": 4, "questions": 8},
                "domain-b": {"papers": 4, "questions": 8},
            },
            "question_types": {"lookup": 8, "multi_hop": 8},
        },
    }


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def test_committed_researchqa_contract_is_pinned_and_schema_valid():
    contract = prepare_researchqa.load_contract()

    assert contract["source"]["revision"] == (
        "33f3d7a83a1ae61511b4e3bfadab2f866eff2a03"
    )
    assert contract["source"]["redistribution"] == "external-only"
    assert contract["expected"]["papers"] == 494
    assert contract["expected"]["questions"] == 6211
    assert len(contract["expected"]["domains"]) == 10
    assert [
        tier["papers_per_domain"] for tier in contract["selection"]["tiers"]
    ] == [2, 5, 10, "all"]


def test_nested_tiers_are_deterministic_and_keep_all_selected_questions(tmp_path):
    source = tmp_path / "source.jsonl"
    rows = _write_fixture(source)
    contract = _fixture_contract(source, rows)
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_index = prepare_researchqa.build_tiers(contract, source, first)
    second_index = prepare_researchqa.build_tiers(contract, source, second)

    assert first_index == second_index
    assert (first / "index.json").read_bytes() == (second / "index.json").read_bytes()

    previous_papers: set[str] = set()
    expected_counts = {"rq-2": 2, "rq-5": 4, "rq-10": 6, "rq-all": 8}
    for tier_id, expected_papers in expected_counts.items():
        papers = _read_jsonl(first / "suites" / tier_id / "papers.jsonl")
        questions = _read_jsonl(first / "suites" / tier_id / "questions.jsonl")
        paper_ids = {paper["paper_id"] for paper in papers}

        assert len(paper_ids) == expected_papers
        assert previous_papers <= paper_ids
        assert len(questions) == expected_papers * 2
        assert {question["paper_id"] for question in questions} == paper_ids
        previous_papers = paper_ids


def test_source_hash_mismatch_fails_closed(tmp_path):
    source = tmp_path / "source.jsonl"
    rows = _write_fixture(source)
    contract = _fixture_contract(source, rows)
    contract["source"]["sha256"] = "0" * 64

    try:
        prepare_researchqa.build_tiers(contract, source, tmp_path / "output")
    except prepare_researchqa.ResearchQAContractError as exc:
        assert "SHA-256 mismatch" in str(exc)
    else:
        raise AssertionError("hash mismatch should fail closed")
