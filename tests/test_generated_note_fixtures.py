import hashlib
import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS_ROOT = REPO_ROOT / "benchmarks"
FIXTURE_ROOT = BENCHMARKS_ROOT / "fixtures" / "generated_notes"
ABSOLUTE_PATH_RE = re.compile(r"(?im)^[A-Za-z0-9_]+_path:\s*['\"]?[A-Za-z]:[\\/]")


def _records() -> list[dict]:
    return [
        json.loads(line)
        for line in (FIXTURE_ROOT / "manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]


def test_s5_generated_note_fixtures_are_complete_and_path_safe():
    corpus_ids = {
        json.loads(line)["paper_id"]
        for line in (BENCHMARKS_ROOT / "corpus" / "manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    }
    records = _records()

    assert len(records) == 5
    assert {record["paper_id"] for record in records} == corpus_ids

    for record in records:
        path = BENCHMARKS_ROOT / record["artifact_path"]
        payload = path.read_bytes()
        note = payload.decode("utf-8")

        assert path.is_file()
        assert hashlib.sha256(payload).hexdigest() == record["note_sha256"]
        assert record["backend"] == "subagent"
        assert record["model"] == "gpt-5.6-sol"
        assert record["human_review_status"] == "pending"
        assert record["includes_main_pdf"] is True
        assert record["includes_si"] is True
        assert not ABSOLUTE_PATH_RE.search(note)
        assert "pdf_0_artifact_path: corpus/files/" in note
        assert "pdf_1_artifact_path: corpus/files/" in note
        assert re.search(r"\[(?:Main )?p\.\d+", note)
        assert re.search(r"\[SI p\.\d+", note)

        if record["paper_id"] == "liu-2024-single-atom-cobalt-orr":
            assert record["rule_scope"] == "active-domain"
            assert record["promotion_status"] == "eligible-for-human-review"
        else:
            assert record["rule_scope"] == "field-neutral"
            assert record["promotion_status"] == "eligible-for-human-review"
            assert "工业应用潜力" not in note
            assert not re.search(r"\[SI p\.S\d+", note)


def test_generated_note_manifest_contains_reproducibility_metadata():
    for record in _records():
        assert re.fullmatch(r"[0-9a-f]{64}", record["source_run_id"])
        assert re.fullmatch(r"[0-9a-f]{64}", record["prompt_sha256"])
        assert re.fullmatch(r"[0-9a-f]{64}", record["candidate_json_sha256"])
        assert re.fullmatch(r"[0-9a-f]{64}", record["note_sha256"])
        assert record["generated_at"].endswith("+00:00")
        assert record["domain_pack"]
        assert record["note_template"]
        assert isinstance(record["repair_applied"], bool)
        if record["repair_applied"]:
            assert record["repair_model"] == "gpt-5.6-sol"
            assert re.fullmatch(r"[0-9a-f]{64}", record["repair_prompt_sha256"])
            assert re.fullmatch(r"[0-9a-f]{64}", record["repair_brief_sha256"])
            assert re.fullmatch(
                r"[0-9a-f]{64}", record["pre_repair_candidate_json_sha256"]
            )
