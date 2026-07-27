from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from benchmarks.researchqa_notes import (
    GENERIC_TEMPLATE,
    NoteJob,
    assign_balanced_queues,
    build_scanner_command,
    freeze_audited_notes,
    invalid_native_citations,
    parse_native_citations,
    rotate_auditors,
    validate_citation_sources,
    write_native_source_packets,
)


def _job(paper_id: str, weight: int) -> NoteJob:
    return NoteJob(
        paper_id=paper_id,
        main_pdf=f"{paper_id}.pdf",
        si_pdfs=(),
        source_artifacts=(),
        run_dir=f"runs/{paper_id}",
        output_dir=f"notes/{paper_id}",
        page_count=weight,
        non_pdf_si_count=0,
    )


def test_scanner_command_is_canary_and_forces_generic(tmp_path):
    job = NoteJob(
        paper_id="p1",
        main_pdf="main.pdf",
        si_pdfs=("si.pdf",),
        source_artifacts=("si-native.json",),
        run_dir="run",
        output_dir="out",
        page_count=10,
        non_pdf_si_count=1,
    )
    command = build_scanner_command(
        job,
        python_executable="python311",
        scanner_path=tmp_path / "gemini_analyze_pdf.py",
    )

    assert command[:4] == [
        "python311",
        str((tmp_path / "gemini_analyze_pdf.py").resolve()),
        "main.pdf",
        "si.pdf",
    ]
    assert command[command.index("--note-template") + 1] == GENERIC_TEMPLATE
    assert command[command.index("--publish-target") + 1] == "canary"
    assert "--source-artifact" in command
    assert "--force" in command


def test_balanced_assignment_and_audit_rotation_are_deterministic():
    jobs = [_job("heavy", 20), _job("medium", 10), _job("small", 5)]
    queues = assign_balanced_queues(jobs, ["A", "B", "C"])
    assert [job.paper_id for job in queues["A"]] == ["heavy"]
    assert [job.paper_id for job in queues["B"]] == ["medium"]
    assert [job.paper_id for job in queues["C"]] == ["small"]
    assert rotate_auditors(["A", "B", "C"]) == {"A": "B", "B": "C", "C": "A"}


def test_parse_all_native_coordinate_shapes():
    text = " ".join(
        (
            "[Main p.5]",
            "[SI-01 p.3]",
            "[SI-02 para.14]",
            "[SI-02 table.2 rows.3-5 cols.A-D]",
            '[SI-03 sheet."Table S1" cells.A2:F18]',
            "[SI-04 rows.20-35 cols.model,score]",
            "[AUX-01 para.2]",
        )
    )
    parsed = parse_native_citations(text)
    assert [item.coordinate_type for item in parsed] == [
        "pdf_page",
        "pdf_page",
        "docx_paragraph",
        "docx_table",
        "xlsx_cells",
        "csv_rows",
        "docx_paragraph",
    ]
    assert invalid_native_citations(text) == []
    assert invalid_native_citations("[SI-02 page.3]") == ["[SI-02 page.3]"]


def test_validate_citation_source_type_and_bounds():
    citations = parse_native_citations(
        "[Main p.5] [SI-02 para.14] [SI-03 rows.2-3 cols.model,score]"
    )
    sources = [
        {
            "file_id": "Main",
            "citation_coordinate_type": "pdf_page",
            "coordinate_max": 4,
        },
        {
            "file_id": "SI-02",
            "citation_coordinate_type": "docx_paragraph_table",
            "coordinate_max": 20,
        },
        {
            "file_id": "SI-03",
            "citation_coordinate_type": "csv_rows_columns",
        },
    ]
    assert validate_citation_sources(citations, sources) == [
        "[Main p.5]: page exceeds source bounds"
    ]


def test_freeze_requires_complete_independently_audited_set(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    note = run / "04-rendered-note.md"
    note.write_text("Evidence [Main p.1]", encoding="utf-8")
    draft = run / "02-note-draft.json"
    draft.write_text(json.dumps({"frontmatter": {}}), encoding="utf-8")
    manifest = run / "manifest-note_generator.json"
    manifest.write_text(
        json.dumps(
            {
                "stage": "note_generator",
                "user_prompt": f"use {GENERIC_TEMPLATE}",
            }
        ),
        encoding="utf-8",
    )
    audit = run / "06-audit.json"
    audit.write_text(
        json.dumps(
            {
                "verdict": "PASS",
                "generator_id": "A",
                "auditor_id": "B",
                "p0": 0,
                "p1": 0,
                "note_sha256": hashlib.sha256(note.read_bytes()).hexdigest(),
                "draft_sha256": hashlib.sha256(draft.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    record = {
        "paper_id": "p1",
        "note_path": note,
        "draft_path": draft,
        "generation_manifest_path": manifest,
        "audit_path": audit,
        "source_records": [
            {
                "file_id": "Main",
                "citation_coordinate_type": "pdf_page",
                "coordinate_max": 1,
            }
        ],
        "valid_citations": ["[Main p.1]"],
    }

    output = freeze_audited_notes(
        [record], tmp_path / "frozen-notes.jsonl", expected_paper_ids={"p1"}
    )
    frozen = json.loads(output.read_text(encoding="utf-8"))
    assert frozen["paper_id"] == "p1"
    assert frozen["template"] == GENERIC_TEMPLATE

    with pytest.raises(ValueError, match="missing"):
        freeze_audited_notes(
            [record],
            tmp_path / "incomplete.jsonl",
            expected_paper_ids={"p1", "p2"},
        )


def test_write_native_source_packets_is_hash_bound_and_deterministic(tmp_path):
    source = {
        "paper_id": "p1",
        "file_id": "SI-02",
        "media_type": "text/csv",
        "sha256": "a" * 64,
        "source_role": "external_si",
    }
    unit = {
        "paper_id": "p1",
        "file_id": "SI-02",
        "source_sha256": "a" * 64,
        "ordinal": 1,
        "unit_id": "unit-1",
        "citation": "[SI-02 rows.1-1 cols.model]",
        "text": "model=a",
    }

    first = write_native_source_packets(
        paper_id="p1",
        source_records=[source],
        native_units=[unit],
        output_dir=tmp_path,
    )
    first_bytes = first[0].read_bytes()
    second = write_native_source_packets(
        paper_id="p1",
        source_records=[source],
        native_units=[unit],
        output_dir=tmp_path,
    )
    assert second[0].read_bytes() == first_bytes
    packet = json.loads(first[0].read_text(encoding="utf-8"))
    assert packet["units"][0]["citation"].startswith("[SI-02 rows.")
