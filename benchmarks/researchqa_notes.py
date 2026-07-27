"""ResearchQA note-generation contracts and freeze validation.

This module does not call a model. It prepares isolated scanner commands,
validates native-coordinate citations, assigns generation/audit queues, and
freezes only independently-audited sub-agent outputs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


GENERIC_TEMPLATE = "generic-research-note"
PASS_VERDICTS = frozenset({"pass", "passed"})
_MAIN_RE = re.compile(
    r"\[Main p\.(?P<page_start>[1-9]\d*)"
    r"(?:-(?P<page_end>[1-9]\d*))?\]"
)
_SOURCE_FILE_ID = r"(?:SI|AUX)-\d{2}"
_SI_PDF_RE = re.compile(
    rf"\[(?P<file>{_SOURCE_FILE_ID}) p\.(?P<page_start>[1-9]\d*)"
    r"(?:-(?P<page_end>[1-9]\d*))?\]"
)
_SI_PARAGRAPH_RE = re.compile(
    rf"\[(?P<file>{_SOURCE_FILE_ID}) para\.(?P<paragraph>[1-9]\d*)\]"
)
_SI_TABLE_RE = re.compile(
    rf"\[(?P<file>{_SOURCE_FILE_ID}) table\.(?P<table>[1-9]\d*) "
    r"rows\.(?P<row_start>[1-9]\d*)(?:-(?P<row_end>[1-9]\d*))? "
    r"cols\.(?P<columns>[A-Z]+(?:-[A-Z]+)?)\]"
)
_SI_SHEET_RE = re.compile(
    rf'\[(?P<file>{_SOURCE_FILE_ID}) sheet\."(?P<sheet>[^"]+)" '
    r"cells\.(?P<cells>[A-Z]+[1-9]\d*:[A-Z]+[1-9]\d*)\]"
)
_SI_CSV_RE = re.compile(
    rf"\[(?P<file>{_SOURCE_FILE_ID}) rows\.(?P<row_start>[1-9]\d*)"
    r"(?:-(?P<row_end>[1-9]\d*))? cols\.(?P<columns>[^\]\r\n]+)\]"
)
_ANY_NATIVE_CITATION_RE = re.compile(
    rf"\[(?:Main|{_SOURCE_FILE_ID}) [^\]\r\n]+\]"
)


@dataclass(frozen=True)
class NoteJob:
    paper_id: str
    main_pdf: str
    si_pdfs: tuple[str, ...]
    source_artifacts: tuple[str, ...]
    run_dir: str
    output_dir: str
    page_count: int
    non_pdf_si_count: int

    @property
    def weight(self) -> int:
        return self.page_count + len(self.si_pdfs) * 8 + self.non_pdf_si_count * 20

    @property
    def pdf_paths(self) -> tuple[str, ...]:
        return (self.main_pdf, *self.si_pdfs)


@dataclass(frozen=True)
class NativeCitation:
    raw: str
    file_id: str
    coordinate_type: str
    coordinate: Mapping[str, Any]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def build_scanner_command(
    job: NoteJob,
    *,
    python_executable: str,
    scanner_path: str | Path,
    resume_dir: str | Path | None = None,
) -> list[str]:
    """Build one scanner pass without touching the user's production ledger."""
    command = [
        python_executable,
        str(Path(scanner_path).resolve()),
        *job.pdf_paths,
        "--backend",
        "subagent",
        "--note-template",
        GENERIC_TEMPLATE,
        "--publish-target",
        "canary",
        "--post-publish",
        "none",
        "--out-dir",
        job.output_dir,
        "--force",
    ]
    for artifact in job.source_artifacts:
        command.extend(("--source-artifact", artifact))
    if resume_dir is not None:
        command.extend(("--resume", str(Path(resume_dir).resolve())))
    return command


def assign_balanced_queues(
    jobs: Sequence[NoteJob], worker_ids: Sequence[str]
) -> dict[str, list[NoteJob]]:
    """Deterministic longest-processing-time assignment."""
    if not worker_ids:
        raise ValueError("worker_ids must not be empty")
    queues = {worker_id: [] for worker_id in worker_ids}
    loads = {worker_id: 0 for worker_id in worker_ids}
    worker_order = {worker_id: index for index, worker_id in enumerate(worker_ids)}
    for job in sorted(jobs, key=lambda item: (-item.weight, item.paper_id)):
        worker = min(worker_ids, key=lambda item: (loads[item], worker_order[item]))
        queues[worker].append(job)
        loads[worker] += job.weight
    return queues


def rotate_auditors(worker_ids: Sequence[str]) -> dict[str, str]:
    if len(worker_ids) < 2:
        raise ValueError("at least two workers are required for independent audit")
    return {
        worker: worker_ids[(index + 1) % len(worker_ids)]
        for index, worker in enumerate(worker_ids)
    }


def _match_to_citation(match: re.Match[str], coordinate_type: str) -> NativeCitation:
    fields = {key: value for key, value in match.groupdict().items() if value}
    file_id = fields.pop("file", "Main")
    for key in (
        "page_start",
        "page_end",
        "paragraph",
        "table",
        "row_start",
        "row_end",
    ):
        if key in fields:
            fields[key] = int(fields[key])
    return NativeCitation(
        raw=match.group(0),
        file_id=file_id,
        coordinate_type=coordinate_type,
        coordinate=fields,
    )


def parse_native_citations(text: str) -> list[NativeCitation]:
    matches: list[tuple[int, NativeCitation]] = []
    patterns = (
        (_MAIN_RE, "pdf_page"),
        (_SI_PDF_RE, "pdf_page"),
        (_SI_PARAGRAPH_RE, "docx_paragraph"),
        (_SI_TABLE_RE, "docx_table"),
        (_SI_SHEET_RE, "xlsx_cells"),
        (_SI_CSV_RE, "csv_rows"),
    )
    occupied: list[tuple[int, int]] = []
    for pattern, coordinate_type in patterns:
        for match in pattern.finditer(text):
            span = match.span()
            if any(start <= span[0] and span[1] <= end for start, end in occupied):
                continue
            occupied.append(span)
            matches.append((span[0], _match_to_citation(match, coordinate_type)))
    return [citation for _, citation in sorted(matches, key=lambda item: item[0])]


def invalid_native_citations(text: str) -> list[str]:
    parsed_raw = {citation.raw for citation in parse_native_citations(text)}
    return [
        match.group(0)
        for match in _ANY_NATIVE_CITATION_RE.finditer(text)
        if match.group(0) not in parsed_raw
    ]


def validate_citation_sources(
    citations: Iterable[NativeCitation],
    source_records: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Return fail-closed citation/source mismatches."""
    records = {
        str(record.get("file_id") or ""): record
        for record in source_records
        if record.get("file_id")
    }
    errors: list[str] = []
    for citation in citations:
        record = records.get(citation.file_id)
        if record is None:
            errors.append(f"{citation.raw}: unknown source file_id")
            continue
        expected = str(record.get("citation_coordinate_type") or "")
        compatible_types = {
            "docx_paragraph_table": {"docx_paragraph", "docx_table"},
            "csv_rows_columns": {"csv_rows"},
        }.get(expected, {expected})
        if expected and citation.coordinate_type not in compatible_types:
            errors.append(
                f"{citation.raw}: expected coordinate type {expected}, "
                f"got {citation.coordinate_type}"
            )
            continue
        maximum = record.get("coordinate_max")
        if citation.coordinate_type == "pdf_page" and maximum is not None:
            page_start = int(citation.coordinate["page_start"])
            page_end = int(citation.coordinate.get("page_end", page_start))
            if page_end < page_start:
                errors.append(f"{citation.raw}: page range is reversed")
            elif page_end > int(maximum):
                errors.append(f"{citation.raw}: page exceeds source bounds")
        if citation.coordinate_type == "docx_paragraph" and maximum is not None:
            if int(citation.coordinate["paragraph"]) > int(maximum):
                errors.append(f"{citation.raw}: paragraph exceeds source bounds")
    return errors


def _atomic_citation_coordinates(citation: NativeCitation) -> tuple[str, ...]:
    if citation.coordinate_type != "pdf_page":
        return (citation.raw,)
    page_start = int(citation.coordinate["page_start"])
    page_end = int(citation.coordinate.get("page_end", page_start))
    return tuple(
        f"[{citation.file_id} p.{page}]" for page in range(page_start, page_end + 1)
    )


def validate_audited_note(
    *,
    note_path: str | Path,
    draft_path: str | Path,
    generation_manifest_path: str | Path,
    audit_path: str | Path,
    source_records: Sequence[Mapping[str, Any]],
    valid_citations: Iterable[str] | None = None,
) -> dict[str, Any]:
    paths = {
        "note": Path(note_path),
        "draft": Path(draft_path),
        "manifest": Path(generation_manifest_path),
        "audit": Path(audit_path),
    }
    for label, path in paths.items():
        if not path.is_file():
            raise FileNotFoundError(f"{label}: {path}")

    note = paths["note"].read_text(encoding="utf-8")
    draft = json.loads(paths["draft"].read_text(encoding="utf-8"))
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    audit = json.loads(paths["audit"].read_text(encoding="utf-8"))

    if manifest.get("stage") != "note_generator":
        raise ValueError("generation manifest must be note_generator stage")
    if GENERIC_TEMPLATE not in manifest.get("user_prompt", ""):
        raise ValueError("note generation did not force generic-research-note")
    if str(audit.get("verdict") or "").strip().lower() not in PASS_VERDICTS:
        raise ValueError("independent audit did not pass")
    if audit.get("generator_id") == audit.get("auditor_id"):
        raise ValueError("generator cannot audit its own note")
    if audit.get("p0", 0) or audit.get("p1", 0):
        raise ValueError("audit contains unresolved P0/P1 defects")

    note_sha256 = _sha256_file(paths["note"])
    draft_sha256 = _sha256_file(paths["draft"])
    if audit.get("note_sha256") != note_sha256:
        raise ValueError("audit does not bind the rendered note SHA-256")
    if audit.get("draft_sha256") != draft_sha256:
        raise ValueError("audit does not bind the draft SHA-256")

    invalid = invalid_native_citations(note)
    if invalid:
        raise ValueError(f"invalid native citations: {invalid}")
    citations = parse_native_citations(note)
    if not citations:
        raise ValueError("audited note contains no native source citations")
    source_errors = validate_citation_sources(citations, source_records)
    if source_errors:
        raise ValueError("; ".join(source_errors))
    if valid_citations is not None:
        allowed = set(valid_citations)
        unknown = sorted(
            {
                citation.raw
                for citation in citations
                if any(
                    atomic not in allowed
                    for atomic in _atomic_citation_coordinates(citation)
                )
            }
        )
        if unknown:
            raise ValueError(f"citations do not resolve to native IR: {unknown}")

    return {
        "note_sha256": note_sha256,
        "draft_sha256": draft_sha256,
        "manifest_sha256": _sha256_file(paths["manifest"]),
        "audit_sha256": _sha256_file(paths["audit"]),
        "citation_count": len(citations),
        "draft_keys": sorted(draft),
    }


def freeze_audited_notes(
    records: Sequence[Mapping[str, Any]],
    output_path: str | Path,
    *,
    expected_paper_ids: Iterable[str],
) -> Path:
    """Validate and atomically freeze a complete, unique 20-paper note set."""
    expected = set(expected_paper_ids)
    by_paper: dict[str, Mapping[str, Any]] = {}
    frozen: list[dict[str, Any]] = []
    for record in records:
        paper_id = str(record["paper_id"])
        if paper_id in by_paper:
            raise ValueError(f"duplicate note record for {paper_id}")
        by_paper[paper_id] = record
        validation = validate_audited_note(
            note_path=record["note_path"],
            draft_path=record["draft_path"],
            generation_manifest_path=record["generation_manifest_path"],
            audit_path=record["audit_path"],
            source_records=record["source_records"],
            valid_citations=record.get("valid_citations"),
        )
        frozen.append(
            {
                "schema_version": 1,
                "paper_id": paper_id,
                "template": GENERIC_TEMPLATE,
                **validation,
            }
        )

    missing = sorted(expected - set(by_paper))
    unexpected = sorted(set(by_paper) - expected)
    if missing or unexpected:
        raise ValueError(f"incomplete note set: missing={missing}, unexpected={unexpected}")

    output = Path(output_path)
    text = "".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
        for item in sorted(frozen, key=lambda item: item["paper_id"])
    )
    _atomic_write(output, text)
    return output


def write_note_jobs(jobs: Sequence[NoteJob], output_path: str | Path) -> Path:
    output = Path(output_path)
    text = "".join(
        json.dumps(asdict(job), ensure_ascii=False, sort_keys=True) + "\n"
        for job in sorted(jobs, key=lambda item: item.paper_id)
    )
    _atomic_write(output, text)
    return output


def write_native_source_packets(
    *,
    paper_id: str,
    source_records: Sequence[Mapping[str, Any]],
    native_units: Sequence[Mapping[str, Any]],
    output_dir: str | Path,
) -> tuple[Path, ...]:
    """Write one deterministic, sub-agent-readable packet per non-PDF source."""
    records = {
        str(record["file_id"]): dict(record)
        for record in source_records
        if record.get("media_type") != "application/pdf"
    }
    units_by_file: dict[str, list[dict[str, Any]]] = {
        file_id: [] for file_id in records
    }
    for unit in native_units:
        if unit.get("paper_id") != paper_id:
            raise ValueError("native unit belongs to a different paper")
        file_id = str(unit.get("file_id") or "")
        if file_id not in units_by_file:
            continue
        if unit.get("source_sha256") != records[file_id].get("sha256"):
            raise ValueError(f"{file_id}: native unit source hash mismatch")
        units_by_file[file_id].append(dict(unit))

    root = Path(output_dir)
    outputs: list[Path] = []
    for file_id in sorted(records):
        units = sorted(
            units_by_file[file_id],
            key=lambda unit: (int(unit.get("ordinal", 0)), str(unit.get("unit_id", ""))),
        )
        if not units:
            raise ValueError(f"{file_id}: non-PDF source produced no native units")
        packet = {
            "schema_version": 1,
            "paper_id": paper_id,
            "file_id": file_id,
            "source": records[file_id],
            "instructions": (
                "Treat each unit.citation as the only valid coordinate for its "
                "unit.text. Preserve Main/SI/AUX source roles and do not invent pages."
            ),
            "units": units,
        }
        output = root / f"{file_id}-native-source.json"
        _atomic_write(
            output,
            json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        outputs.append(output)
    return tuple(outputs)
