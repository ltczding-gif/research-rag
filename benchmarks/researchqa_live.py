"""Fail-closed live rq-2 source, canary, and retrieval adapter.

This module bridges the audited ignored ResearchQA cache to the generic
overnight runner. ``prepare`` materializes hash-bound source manifests,
native-coordinate IR, non-PDF source packets, and scanner ``NoteJob`` records.
The parent process performs note generation and writes a hash-bound canary
marker. ``run`` consumes a complete frozen note set through the live runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from benchmarks import researchqa_runtime
from benchmarks.overnight import (
    ArtifactRecord,
    BlockedTaskError,
    DeterministicTaskError,
    RunState,
    RunStore,
    TaskContext,
    TaskOutput,
    TaskSpec,
    TransientTaskError,
    fingerprint_payload,
    sha256_path,
)
from benchmarks.researchqa_models import ModelTransportError
from benchmarks.researchqa_notes import (
    NoteJob,
    write_native_source_packets,
    write_note_jobs,
)
from benchmarks.researchqa_sources import (
    MEDIA_PDF,
    ROLE_AUXILIARY,
    ROLE_BENCHMARK_PDF,
    ROLE_EXTERNAL_SI,
    SourceArtifact,
    SourceRecord,
    build_source_manifest,
    extract_native_corpus,
)


SCHEMA_VERSION = 1
_PAPER_ID = re.compile(r"^W[1-9][0-9]*$")
_PREPARE_COMMAND = "prepare"
_CANARY_COMMAND = "canary"
_RUN_COMMAND = "run"
_CANARY_PAPER_ID = "W2792307011"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class ResearchQALiveError(ValueError):
    """Raised when the audited live cache violates the prepare contract."""


@dataclass(frozen=True)
class _CachePaths:
    cache_root: Path
    source_root: Path
    audit_path: Path
    main_download_manifest: Path
    supplementary_download_manifest: Path


@dataclass(frozen=True)
class _BoundArtifact:
    artifact: SourceArtifact
    path: Path
    audited: Mapping[str, Any]


def normalize_paper_id(value: object) -> str:
    """Normalize an OpenAlex URL or bare identifier to the canonical ``W...``."""

    rendered = str(value or "").strip().rstrip("/")
    paper_id = rendered.rsplit("/", 1)[-1]
    if not _PAPER_ID.fullmatch(paper_id):
        raise ResearchQALiveError(f"invalid ResearchQA paper_id: {value!r}")
    return paper_id


def _resolve_config_path(
    value: object,
    *,
    base: Path,
    default: Path,
) -> Path:
    if value is None:
        return default.resolve(strict=False)
    path = Path(str(value))
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


def _cache_paths(config: Mapping[str, Any]) -> _CachePaths:
    paths = config.get("paths")
    if not isinstance(paths, Mapping):
        raise ResearchQALiveError("config.paths must be a mapping")
    cache_value = paths.get("cache_root")
    if not cache_value:
        raise ResearchQALiveError("config.paths.cache_root is required")
    cache_root = Path(str(cache_value)).resolve(strict=False)
    source_value = paths.get("source_dir", "pdfs/rq-2")
    source_root = _resolve_config_path(
        source_value,
        base=cache_root,
        default=cache_root / "pdfs" / "rq-2",
    )
    return _CachePaths(
        cache_root=cache_root,
        source_root=source_root,
        audit_path=_resolve_config_path(
            paths.get("source_audit_file"),
            base=cache_root,
            default=source_root / "source-set-audit.json",
        ),
        main_download_manifest=_resolve_config_path(
            paths.get("main_download_manifest"),
            base=cache_root,
            default=source_root / "download-manifest.jsonl",
        ),
        supplementary_download_manifest=_resolve_config_path(
            paths.get("supplementary_download_manifest"),
            base=cache_root,
            default=source_root / "supplementary" / "download-manifest.jsonl",
        ),
    )


def _require_cache_owned(path: Path, cache_root: Path) -> Path:
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(cache_root)
    except ValueError as exc:
        raise ResearchQALiveError(
            f"audited source escapes cache_root: {resolved}"
        ) from exc
    if not resolved.is_file():
        raise ResearchQALiveError(f"audited source is missing: {resolved}")
    return resolved


def _resolve_audited_file(
    value: object,
    *,
    cache_root: Path,
    audit_dir: Path,
) -> Path:
    if not isinstance(value, str) or not value:
        raise ResearchQALiveError("audited source local_path is required")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = audit_dir / candidate
    return _require_cache_owned(candidate, cache_root)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ResearchQALiveError(f"required cache record is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ResearchQALiveError(
            f"{path}: invalid JSON at line {exc.lineno}: {exc.msg}"
        ) from exc
    if not isinstance(value, dict):
        raise ResearchQALiveError(f"{path}: expected a JSON object")
    return value


def _read_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    records = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError as exc:
        raise ResearchQALiveError(f"required cache record is missing: {path}") from exc
    for line_number, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ResearchQALiveError(
                f"{path}:{line_number}: invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(value, dict):
            raise ResearchQALiveError(
                f"{path}:{line_number}: expected a JSON object"
            )
        records.append(value)
    return tuple(records)


def _atomic_write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return path


def _canonical_jsonl(records: Iterable[Mapping[str, Any]]) -> bytes:
    return "".join(
        json.dumps(
            dict(record),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for record in records
    ).encode("utf-8")


def _atomic_write_jsonl(
    path: Path,
    records: Iterable[Mapping[str, Any]],
) -> Path:
    return _atomic_write(path, _canonical_jsonl(records))


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> Path:
    payload = (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    return _atomic_write(path, payload)


def _write_citation_source_index(
    *,
    paper_id: str,
    sources: Sequence[tuple[SourceRecord, Path]],
    output_path: Path,
) -> Path:
    """Write path-to-stable-file-ID metadata without copying source text."""

    payload = {
        "schema_version": SCHEMA_VERSION,
        "paper_id": paper_id,
        "purpose": "citation-source-index",
        "instructions": (
            "Map every PDF path and non-PDF packet to the exact file_id in "
            "this index before emitting a citation. Do not infer SI numbering "
            "from pdf_paths order or filenames."
        ),
        "sources": [
            {
                **record.to_dict(),
                "path": str(path.resolve()),
            }
            for record, path in sources
        ],
    }
    return _atomic_write_json(output_path, payload)


def _validate_audited_bytes(
    path: Path,
    record: Mapping[str, Any],
) -> None:
    size, digest = sha256_path(path)
    expected_size = record.get("bytes")
    expected_digest = record.get("sha256")
    if expected_size is None or expected_digest is None:
        raise ResearchQALiveError(
            f"{path.name}: audit must pin bytes and sha256"
        )
    if size != int(expected_size):
        raise ResearchQALiveError(
            f"{path.name}: audited bytes mismatch "
            f"(expected {expected_size}, found {size})"
        )
    if digest != str(expected_digest):
        raise ResearchQALiveError(
            f"{path.name}: audited SHA-256 mismatch"
        )


def _is_auxiliary(record: Mapping[str, Any], path: Path) -> bool:
    label = str(record.get("label") or "").strip().casefold()
    filename = path.name.casefold()
    return (
        label.startswith("description")
        or "reporting summary" in label
        or filename.startswith("description")
        or "reporting_summary" in filename
        or "reporting-summary" in filename
    )


def _download_indexes(
    paths: _CachePaths,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[tuple[str, str], dict[str, Any]],
]:
    main_by_paper: dict[str, dict[str, Any]] = {}
    for record in _read_jsonl(paths.main_download_manifest):
        paper_id = normalize_paper_id(record.get("paper_id"))
        if paper_id in main_by_paper:
            raise ResearchQALiveError(
                f"duplicate main download record for {paper_id}"
            )
        main_by_paper[paper_id] = record

    supplementary_by_hash: dict[tuple[str, str], dict[str, Any]] = {}
    for record in _read_jsonl(paths.supplementary_download_manifest):
        paper_id = normalize_paper_id(record.get("paper_id"))
        digest = str(record.get("sha256") or "")
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ResearchQALiveError(
                f"{paper_id}: supplementary download record lacks SHA-256"
            )
        key = paper_id, digest
        if key in supplementary_by_hash:
            raise ResearchQALiveError(
                f"duplicate supplementary download record for {paper_id}/{digest}"
            )
        supplementary_by_hash[key] = record
    return main_by_paper, supplementary_by_hash


def _source_url(record: Mapping[str, Any], *, label: str) -> str:
    for field in (
        "download_url",
        "source_url",
        "paper_s3_url",
        "final_url",
        "url",
    ):
        value = record.get(field)
        if isinstance(value, str) and value:
            return value
    raise ResearchQALiveError(f"{label}: no source URL in download manifest")


def _paper_bindings(
    *,
    paper_id: str,
    paper: Mapping[str, Any],
    paths: _CachePaths,
    main_download: Mapping[str, Any],
    supplementary_downloads: Mapping[tuple[str, str], Mapping[str, Any]],
) -> tuple[_BoundArtifact, ...]:
    main_audit = paper.get("main_pdf")
    if not isinstance(main_audit, Mapping):
        raise ResearchQALiveError(f"{paper_id}: main_pdf audit is missing")
    main_path = _resolve_audited_file(
        main_audit.get("local_path"),
        cache_root=paths.cache_root,
        audit_dir=paths.audit_path.parent,
    )
    _validate_audited_bytes(main_path, main_audit)
    if str(main_audit.get("parse_status") or "") != "ok":
        raise ResearchQALiveError(f"{paper_id}: main PDF did not pass parse audit")
    bindings = [
        _BoundArtifact(
            artifact=SourceArtifact(
                path=main_path,
                source_role=ROLE_BENCHMARK_PDF,
                source_url=_source_url(main_download, label=paper_id),
                original_filename=main_path.name,
                media_type=MEDIA_PDF,
                acquisition_status="verified",
            ),
            path=main_path,
            audited=main_audit,
        )
    ]

    supplementary = paper.get("supplementary_files", [])
    if not isinstance(supplementary, list):
        raise ResearchQALiveError(
            f"{paper_id}: supplementary_files must be a list"
        )
    for item in supplementary:
        if not isinstance(item, Mapping):
            raise ResearchQALiveError(
                f"{paper_id}: supplementary audit record must be an object"
            )
        source_path = _resolve_audited_file(
            item.get("local_path"),
            cache_root=paths.cache_root,
            audit_dir=paths.audit_path.parent,
        )
        _validate_audited_bytes(source_path, item)
        if str(item.get("validation") or "") != "ok":
            raise ResearchQALiveError(
                f"{paper_id}/{source_path.name}: supplementary audit failed"
            )
        digest = str(item["sha256"])
        try:
            download = supplementary_downloads[paper_id, digest]
        except KeyError as exc:
            raise ResearchQALiveError(
                f"{paper_id}/{source_path.name}: missing download provenance"
            ) from exc
        media_type = item.get("content_type") or download.get("content_type")
        bindings.append(
            _BoundArtifact(
                artifact=SourceArtifact(
                    path=source_path,
                    source_role=(
                        ROLE_AUXILIARY
                        if _is_auxiliary(item, source_path)
                        else ROLE_EXTERNAL_SI
                    ),
                    source_url=_source_url(
                        download,
                        label=f"{paper_id}/{source_path.name}",
                    ),
                    original_filename=source_path.name,
                    media_type=(
                        str(media_type) if media_type is not None else None
                    ),
                    acquisition_status="verified",
                ),
                path=source_path,
                audited=item,
            )
        )
    return tuple(bindings)


def _bindings_by_record(
    records: Sequence[SourceRecord],
    bindings: Sequence[_BoundArtifact],
) -> tuple[tuple[SourceRecord, Path], ...]:
    by_identity = {
        (
            binding.artifact.source_role,
            binding.path.name,
            sha256_path(binding.path)[1],
        ): binding.path
        for binding in bindings
    }
    resolved = []
    for record in records:
        key = record.source_role, record.original_filename, record.sha256
        try:
            path = by_identity[key]
        except KeyError as exc:
            raise ResearchQALiveError(
                f"{record.paper_id}/{record.file_id}: source binding was lost"
            ) from exc
        resolved.append((record, path))
    return tuple(resolved)


def _audited_page_count(
    records: Sequence[SourceRecord],
    bindings: Sequence[_BoundArtifact],
) -> int:
    audited_by_identity = {
        (
            binding.artifact.source_role,
            binding.path.name,
            sha256_path(binding.path)[1],
        ): binding.audited
        for binding in bindings
    }
    total = 0
    for record in records:
        if (
            record.media_type != MEDIA_PDF
            or record.source_role == ROLE_AUXILIARY
        ):
            continue
        audit = audited_by_identity[
            record.source_role,
            record.original_filename,
            record.sha256,
        ]
        page_count = audit.get("pages", audit.get("page_count"))
        if page_count is None or int(page_count) < 1:
            raise ResearchQALiveError(
                f"{record.paper_id}/{record.file_id}: PDF page count is not audited"
            )
        total += int(page_count)
    return total


def _expected_paper_count(config: Mapping[str, Any]) -> int:
    benchmark = config.get("benchmark")
    if not isinstance(benchmark, Mapping):
        raise ResearchQALiveError("config.benchmark must be a mapping")
    if benchmark.get("tier_id") != "rq-2":
        raise ResearchQALiveError("live adapter only supports benchmark tier rq-2")
    expected = int(benchmark.get("paper_count", 20))
    if expected < 1:
        raise ResearchQALiveError("benchmark.paper_count must be positive")
    return expected


def _paper_sort_key(paper_id: str) -> tuple[int, str]:
    return int(paper_id[1:]), paper_id


def _stable_pdf_combined_hash(records: Sequence[SourceRecord]) -> str:
    """Reproduce ``scanner._hashing.stable_combined_hash`` from pinned hashes."""

    pdf_hashes = sorted(
        record.sha256
        for record in records
        if record.media_type == MEDIA_PDF
        and record.source_role != ROLE_AUXILIARY
    )
    if not pdf_hashes:
        raise ResearchQALiveError("note job has no scientific PDF source")
    combined = hashlib.sha256()
    for digest in pdf_hashes:
        combined.update(digest.encode("utf-8"))
    return combined.hexdigest()


def prepare_rq2_corpus(
    config: Mapping[str, Any],
    run_root: str | Path,
) -> dict[str, Any]:
    """Prepare audited rq-2 sources and 20 note jobs under one run root."""

    if not isinstance(config, Mapping):
        raise ResearchQALiveError("config must be a mapping")
    paths = _cache_paths(config)
    audit = _read_json(paths.audit_path)
    papers = audit.get("papers")
    if not isinstance(papers, list):
        raise ResearchQALiveError("source-set audit must contain papers")
    expected_papers = _expected_paper_count(config)
    if len(papers) != expected_papers:
        raise ResearchQALiveError(
            f"expected {expected_papers} audited papers, found {len(papers)}"
        )
    main_downloads, supplementary_downloads = _download_indexes(paths)

    papers_by_id: dict[str, Mapping[str, Any]] = {}
    for paper in papers:
        if not isinstance(paper, Mapping):
            raise ResearchQALiveError("audit paper record must be an object")
        paper_id = normalize_paper_id(
            paper.get("paper_id") or paper.get("paper_url")
        )
        if paper_id in papers_by_id:
            raise ResearchQALiveError(f"duplicate audited paper_id {paper_id}")
        papers_by_id[paper_id] = paper
    if set(main_downloads) != set(papers_by_id):
        missing = sorted(set(papers_by_id) - set(main_downloads))
        unexpected = sorted(set(main_downloads) - set(papers_by_id))
        raise ResearchQALiveError(
            f"main download set differs from audit: "
            f"missing={missing}, unexpected={unexpected}"
        )

    root = Path(run_root).resolve(strict=False)
    source_root = root / "source"
    note_root = root / "note-runs"
    artifact_paths: list[Path] = []
    note_jobs: list[NoteJob] = []
    role_counts: Counter[str] = Counter()
    media_counts: Counter[str] = Counter()
    native_unit_count = 0
    source_index_count = 0
    source_packet_count = 0
    combined_hashes: set[str] = set()

    for paper_id in sorted(papers_by_id, key=_paper_sort_key):
        paper = papers_by_id[paper_id]
        bindings = _paper_bindings(
            paper_id=paper_id,
            paper=paper,
            paths=paths,
            main_download=main_downloads[paper_id],
            supplementary_downloads=supplementary_downloads,
        )
        records = build_source_manifest(
            paper_id,
            (binding.artifact for binding in bindings),
        )
        bound_records = _bindings_by_record(records, bindings)
        native_units = extract_native_corpus(bound_records)

        paper_source_root = source_root / paper_id
        manifest_path = _atomic_write_jsonl(
            paper_source_root / "source-manifest.jsonl",
            (record.to_dict() for record in records),
        )
        native_ir_path = _atomic_write_jsonl(
            paper_source_root / "native-ir.jsonl",
            (unit.to_dict() for unit in native_units),
        )
        source_index_path = _write_citation_source_index(
            paper_id=paper_id,
            sources=bound_records,
            output_path=paper_source_root / "source-index.json",
        )
        packet_paths = write_native_source_packets(
            paper_id=paper_id,
            source_records=[record.to_dict() for record in records],
            native_units=[unit.to_dict() for unit in native_units],
            output_dir=paper_source_root / "packets",
        )

        combined_hash = _stable_pdf_combined_hash(records)
        if combined_hash in combined_hashes:
            raise ResearchQALiveError(
                f"{paper_id}: stable PDF combined hash is shared by another paper"
            )
        combined_hashes.add(combined_hash)
        run_dir = note_root / "pipeline" / "runs" / combined_hash
        output_dir = note_root / paper_id / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        main_record, main_path = next(
            pair for pair in bound_records if pair[0].file_id == "Main"
        )
        scientific_si_pdfs = tuple(
            str(path.resolve())
            for record, path in bound_records
            if record.source_role == ROLE_EXTERNAL_SI
            and record.media_type == MEDIA_PDF
        )
        non_pdf_si_count = sum(
            record.source_role == ROLE_EXTERNAL_SI
            and record.media_type != MEDIA_PDF
            for record in records
        )
        note_jobs.append(
            NoteJob(
                paper_id=paper_id,
                main_pdf=str(main_path.resolve()),
                si_pdfs=scientific_si_pdfs,
                source_artifacts=(
                    str(source_index_path.resolve()),
                    *(
                        str(path.resolve())
                        for path in sorted(packet_paths)
                    ),
                ),
                run_dir=str(run_dir.resolve()),
                output_dir=str(output_dir.resolve()),
                page_count=_audited_page_count(records, bindings),
                non_pdf_si_count=non_pdf_si_count,
            )
        )
        artifact_paths.extend(
            (
                manifest_path,
                native_ir_path,
                source_index_path,
                *packet_paths,
            )
        )
        role_counts.update(record.source_role for record in records)
        media_counts.update(record.media_type for record in records)
        native_unit_count += len(native_units)
        source_index_count += 1
        source_packet_count += len(packet_paths)
        if main_record.source_role != ROLE_BENCHMARK_PDF:
            raise AssertionError("Main source role invariant was lost")

    note_jobs_path = write_note_jobs(
        note_jobs,
        note_root / "note-jobs.jsonl",
    )
    artifact_paths.append(note_jobs_path)
    summary_path = source_root / "prepare-summary.json"
    artifact_paths.append(summary_path)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "tier_id": "rq-2",
        "paper_count": len(papers_by_id),
        "source_count": sum(role_counts.values()),
        "native_unit_count": native_unit_count,
        "source_index_count": source_index_count,
        "source_packet_count": source_packet_count,
        "note_job_count": len(note_jobs),
        "source_role_counts": dict(sorted(role_counts.items())),
        "media_type_counts": dict(sorted(media_counts.items())),
        "note_jobs_path": str(note_jobs_path.resolve()),
        "artifact_paths": [
            str(path.resolve())
            for path in sorted(
                artifact_paths,
                key=lambda path: path.as_posix(),
            )
        ],
    }
    _atomic_write_json(summary_path, summary)
    return summary


def _prepare_input_fingerprint(config: Mapping[str, Any]) -> str:
    paths = _cache_paths(config)
    inputs = {}
    for label, path in (
        ("source_audit", paths.audit_path),
        ("main_download_manifest", paths.main_download_manifest),
        (
            "supplementary_download_manifest",
            paths.supplementary_download_manifest,
        ),
    ):
        size, digest = sha256_path(path)
        inputs[label] = {"bytes": size, "sha256": digest}
    return fingerprint_payload(
        {
            "adapter_schema_version": SCHEMA_VERSION,
            "config_id": str(config.get("config_id") or "rq2-overnight"),
            "inputs": inputs,
        }
    )


def _require_run_owned_file(
    path: str | Path,
    *,
    run_root: Path,
    label: str,
) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = run_root / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(run_root)
    except ValueError as exc:
        raise ResearchQALiveError(
            f"{label} escapes the run root: {resolved}"
        ) from exc
    if not resolved.is_file():
        raise ResearchQALiveError(f"{label} does not exist: {resolved}")
    return resolved


def _read_run_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ResearchQALiveError(f"{label} does not exist: {path}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchQALiveError(f"{label} is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ResearchQALiveError(f"{label} must be a JSON object: {path}")
    return value


def _relative_run_path(path: Path, run_root: Path) -> str:
    return path.resolve(strict=False).relative_to(run_root).as_posix()


def _path_snapshot(path: Path, run_root: Path) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "path": _relative_run_path(path, run_root),
        "exists": path.is_file(),
    }
    if path.is_file():
        size, digest = sha256_path(path)
        snapshot.update({"bytes": size, "sha256": digest})
    return snapshot


def _canary_marker_path(run_root: Path) -> Path:
    return run_root / "note-runs" / "canary-pass.json"


def _canary_input_fingerprint(run_root: Path) -> str:
    marker_path = _canary_marker_path(run_root)
    return fingerprint_payload(
        {
            "adapter_schema_version": SCHEMA_VERSION,
            "command": _CANARY_COMMAND,
            "marker": _path_snapshot(marker_path, run_root),
        }
    )


def _marker_artifact_value(
    marker: Mapping[str, Any],
    *,
    artifact: str,
    field: str,
) -> object | None:
    value = marker.get(f"{artifact}_{field}")
    if value is not None:
        return value
    nested = marker.get(artifact)
    if isinstance(nested, Mapping):
        return nested.get(field)
    artifacts = marker.get("artifacts")
    if isinstance(artifacts, Mapping):
        nested = artifacts.get(artifact)
        if isinstance(nested, Mapping):
            return nested.get(field)
    return None


def _default_canary_artifact_paths(run_root: Path) -> dict[str, Path]:
    jobs_path = run_root / "note-runs" / "note-jobs.jsonl"
    jobs = _read_jsonl(jobs_path)
    matches = [
        row
        for row in jobs
        if str(row.get("paper_id") or "") == _CANARY_PAPER_ID
    ]
    if len(matches) != 1:
        raise ResearchQALiveError(
            f"note jobs must contain exactly one canary {_CANARY_PAPER_ID}, "
            f"found {len(matches)}"
        )
    run_dir_value = matches[0].get("run_dir")
    if not isinstance(run_dir_value, str) or not run_dir_value:
        raise ResearchQALiveError("canary note job does not contain run_dir")
    run_dir = Path(run_dir_value)
    return {
        "note": run_dir / "04-rendered-note.md",
        "draft": run_dir / "02-note-draft.json",
        "audit": run_dir / "06-audit.json",
    }


def _zero_defect_count(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value != 0:
        raise ResearchQALiveError(f"{label} must be integer 0")
    return value


def _validate_canary_marker(run_root: Path) -> dict[str, Any]:
    marker_path = _canary_marker_path(run_root)
    if not marker_path.is_file():
        raise FileNotFoundError(marker_path)
    marker = _read_run_json(marker_path, label="canary pass marker")
    if marker.get("paper_id") != _CANARY_PAPER_ID:
        raise ResearchQALiveError(
            f"canary pass marker paper_id must be {_CANARY_PAPER_ID}"
        )
    if marker.get("verdict") != "PASS":
        raise ResearchQALiveError("canary pass marker verdict must be PASS")
    _zero_defect_count(marker.get("p0"), label="canary marker p0")
    _zero_defect_count(marker.get("p1"), label="canary marker p1")

    path_values = {
        artifact: _marker_artifact_value(
            marker,
            artifact=artifact,
            field="path",
        )
        for artifact in ("note", "draft", "audit")
    }
    if any(value is None for value in path_values.values()):
        defaults = _default_canary_artifact_paths(run_root)
        path_values = {
            artifact: value if value is not None else defaults[artifact]
            for artifact, value in path_values.items()
        }

    artifact_paths: dict[str, Path] = {}
    artifact_hashes: dict[str, str] = {}
    for artifact in ("note", "draft", "audit"):
        expected_sha = _marker_artifact_value(
            marker,
            artifact=artifact,
            field="sha256",
        )
        if not isinstance(expected_sha, str) or not _SHA256.fullmatch(
            expected_sha
        ):
            raise ResearchQALiveError(
                f"canary marker {artifact}_sha256 is invalid"
            )
        path = _require_run_owned_file(
            path_values[artifact],
            run_root=run_root,
            label=f"canary {artifact}",
        )
        _, actual_sha = sha256_path(path)
        if actual_sha != expected_sha:
            raise ResearchQALiveError(
                f"canary {artifact} SHA-256 mismatch"
            )
        artifact_paths[artifact] = path
        artifact_hashes[artifact] = actual_sha

    audit = _read_run_json(artifact_paths["audit"], label="canary audit")
    if audit.get("paper_id") != _CANARY_PAPER_ID:
        raise ResearchQALiveError(
            f"canary audit paper_id must be {_CANARY_PAPER_ID}"
        )
    if audit.get("verdict") != "PASS":
        raise ResearchQALiveError("canary audit verdict must be PASS")
    _zero_defect_count(audit.get("p0"), label="canary audit p0")
    _zero_defect_count(audit.get("p1"), label="canary audit p1")
    for artifact in ("note", "draft"):
        if audit.get(f"{artifact}_sha256") != artifact_hashes[artifact]:
            raise ResearchQALiveError(
                f"canary audit does not bind the {artifact} SHA-256"
            )

    return {
        "marker_path": marker_path,
        "artifact_paths": artifact_paths,
        "artifact_hashes": artifact_hashes,
        "paper_id": _CANARY_PAPER_ID,
        "verdict": "PASS",
        "p0": 0,
        "p1": 0,
    }


def _frozen_manifest_path(run_root: Path) -> Path:
    return run_root / "note-runs" / "frozen" / "frozen-notes.jsonl"


def _frozen_input_snapshot(run_root: Path) -> dict[str, Any]:
    manifest_path = _frozen_manifest_path(run_root)
    notes_root = manifest_path.parent / "notes"
    note_snapshots = []
    if notes_root.is_dir():
        note_snapshots = [
            _path_snapshot(path, run_root)
            for path in sorted(notes_root.glob("*.md"))
            if path.is_file()
        ]
    return {
        "manifest": _path_snapshot(manifest_path, run_root),
        "notes": note_snapshots,
    }


def _frozen_input_fingerprint(run_root: Path) -> str:
    return fingerprint_payload(
        {
            "adapter_schema_version": SCHEMA_VERSION,
            "command": _RUN_COMMAND,
            "frozen_notes": _frozen_input_snapshot(run_root),
        }
    )


def _validate_frozen_notes(run_root: Path) -> tuple[Path, tuple[Path, ...]]:
    manifest_path = _frozen_manifest_path(run_root)
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    rows = _read_jsonl(manifest_path)
    if len(rows) != researchqa_runtime.EXPECTED_PAPER_COUNT:
        raise ResearchQALiveError(
            "frozen note manifest must contain exactly "
            f"{researchqa_runtime.EXPECTED_PAPER_COUNT} rows, found {len(rows)}"
        )

    seen: set[str] = set()
    note_paths: list[Path] = []
    for index, row in enumerate(rows, 1):
        paper_id = normalize_paper_id(row.get("paper_id"))
        if row.get("paper_id") != paper_id:
            raise ResearchQALiveError(
                f"frozen note row {index} paper_id must be normalized"
            )
        if paper_id in seen:
            raise ResearchQALiveError(
                f"frozen note manifest contains duplicate paper_id {paper_id}"
            )
        seen.add(paper_id)
        expected_sha = row.get("note_sha256")
        if not isinstance(expected_sha, str) or not _SHA256.fullmatch(
            expected_sha
        ):
            raise ResearchQALiveError(
                f"frozen note {paper_id} has invalid note_sha256"
            )
        note_path = _require_run_owned_file(
            manifest_path.parent / "notes" / f"{paper_id}.md",
            run_root=run_root,
            label=f"frozen note {paper_id}",
        )
        _, actual_sha = sha256_path(note_path)
        if actual_sha != expected_sha:
            raise ResearchQALiveError(
                f"frozen note SHA-256 mismatch for {paper_id}"
            )
        note_paths.append(note_path)
    return manifest_path, tuple(note_paths)


def _artifact_media_type(path: str | Path) -> str:
    suffix = Path(path).suffix.casefold()
    if suffix == ".jsonl":
        return "application/x-ndjson"
    if suffix == ".json":
        return "application/json"
    if suffix == ".md":
        return "text/markdown"
    return "application/octet-stream"


def _caused_by(error: BaseException, error_type: type[BaseException]) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, error_type):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


class ResearchQALiveAdapter:
    """Overnight adapter for source preparation and the gated live runtime."""

    def __init__(self, config: Mapping[str, Any], run_root: str | Path):
        self.config = dict(config)
        self.run_root = Path(run_root).resolve(strict=False)

    def fingerprints(self, *, command: str) -> dict[str, str]:
        del command
        module_size, module_sha256 = sha256_path(Path(__file__))
        return {
            "researchqa_live_adapter": fingerprint_payload(
                {
                    "bytes": module_size,
                    "sha256": module_sha256,
                }
            ),
            "researchqa_live_inputs": _prepare_input_fingerprint(self.config),
        }

    def task_specs(
        self,
        command: str,
        state: RunState,
    ) -> Iterable[TaskSpec]:
        del state
        config_id = str(self.config.get("config_id") or "rq2-overnight")
        if command == _PREPARE_COMMAND:
            input_fingerprint = _prepare_input_fingerprint(self.config)
            stage_id = "sources"
        elif command == _CANARY_COMMAND:
            input_fingerprint = _canary_input_fingerprint(self.run_root)
            stage_id = "notes"
        elif command == _RUN_COMMAND:
            input_fingerprint = _frozen_input_fingerprint(self.run_root)
            stage_id = "runtime"
        else:
            raise ResearchQALiveError(f"unsupported live command: {command}")
        return (
            TaskSpec(
                stage_id=stage_id,
                paper_id="rq-2",
                config_id=f"{config_id}-{command}",
                input_fingerprint=input_fingerprint,
                estimated_atom_count=(
                    _expected_paper_count(self.config)
                    if command == _PREPARE_COMMAND
                    else 1
                ),
                metadata={"command": command},
            ),
        )

    def run_task(self, context: TaskContext) -> TaskOutput:
        command = str(context.spec.metadata.get("command") or "")
        if command == _CANARY_COMMAND:
            return self._run_canary_task(context)
        if command == _RUN_COMMAND:
            return self._run_runtime_task(context)
        if command != _PREPARE_COMMAND:
            raise DeterministicTaskError(
                f"unexpected ResearchQA live task command: {command!r}"
            )
        try:
            summary = prepare_rq2_corpus(self.config, self.run_root)
        except (ValueError, OSError) as exc:
            raise DeterministicTaskError(str(exc)) from exc
        artifacts = tuple(
            context.store.artifact_record(
                path,
                media_type=(
                    "application/x-ndjson"
                    if str(path).endswith(".jsonl")
                    else "application/json"
                ),
            )
            for path in summary["artifact_paths"]
        )
        return TaskOutput(
            artifacts=artifacts,
            metadata={
                key: value
                for key, value in summary.items()
                if key not in {"artifact_paths", "note_jobs_path"}
            },
        )

    def _run_canary_task(self, context: TaskContext) -> TaskOutput:
        current_fingerprint = _canary_input_fingerprint(self.run_root)
        if current_fingerprint != context.spec.input_fingerprint:
            raise DeterministicTaskError(
                "canary marker changed after task specification"
            )
        try:
            validation = _validate_canary_marker(self.run_root)
        except FileNotFoundError as exc:
            raise BlockedTaskError(
                f"canary pass marker is not available: {exc}"
            ) from exc
        except (ValueError, OSError) as exc:
            raise DeterministicTaskError(str(exc)) from exc

        paths = (
            validation["marker_path"],
            validation["artifact_paths"]["note"],
            validation["artifact_paths"]["draft"],
            validation["artifact_paths"]["audit"],
        )
        return TaskOutput(
            artifacts=tuple(
                context.store.artifact_record(
                    path,
                    media_type=_artifact_media_type(path),
                )
                for path in paths
            ),
            metadata={
                "paper_id": validation["paper_id"],
                "verdict": validation["verdict"],
                "p0": validation["p0"],
                "p1": validation["p1"],
                "note_sha256": validation["artifact_hashes"]["note"],
                "draft_sha256": validation["artifact_hashes"]["draft"],
                "audit_sha256": validation["artifact_hashes"]["audit"],
            },
        )

    def _run_runtime_task(self, context: TaskContext) -> TaskOutput:
        current_fingerprint = _frozen_input_fingerprint(self.run_root)
        if current_fingerprint != context.spec.input_fingerprint:
            raise DeterministicTaskError(
                "frozen notes changed after task specification"
            )
        try:
            _validate_frozen_notes(self.run_root)
        except FileNotFoundError as exc:
            raise BlockedTaskError(
                f"frozen note manifest is not available: {exc}"
            ) from exc
        except (ValueError, OSError) as exc:
            raise DeterministicTaskError(str(exc)) from exc

        try:
            result = researchqa_runtime.run_researchqa_runtime(
                self.config,
                self.run_root,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except researchqa_runtime.ResearchQARuntimeError as exc:
            if _caused_by(exc, ModelTransportError):
                raise TransientTaskError(str(exc)) from exc
            raise DeterministicTaskError(str(exc)) from exc
        except (ConnectionError, TimeoutError) as exc:
            raise BlockedTaskError(str(exc)) from exc
        except Exception as exc:
            raise DeterministicTaskError(
                f"unexpected ResearchQA runtime failure: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        try:
            summary_path = _require_run_owned_file(
                result.runtime_summary_path,
                run_root=self.run_root,
                label="runtime summary",
            )
            summary = _read_run_json(summary_path, label="runtime summary")
            if summary.get("status") != "completed":
                raise ResearchQALiveError(
                    "runtime summary status must be completed"
                )
            sweep_paths = tuple(result.sweep_result.artifact_paths)
            candidate_paths = (
                Path(result.model_preflight_path),
                summary_path,
                *(Path(path) for path in sweep_paths),
            )
            unique_paths: list[Path] = []
            seen: set[Path] = set()
            for path in candidate_paths:
                owned = _require_run_owned_file(
                    path,
                    run_root=self.run_root,
                    label="runtime artifact",
                )
                if owned not in seen:
                    seen.add(owned)
                    unique_paths.append(owned)
        except (AttributeError, TypeError, ValueError, OSError) as exc:
            raise DeterministicTaskError(str(exc)) from exc

        return TaskOutput(
            artifacts=tuple(
                context.store.artifact_record(
                    path,
                    media_type=_artifact_media_type(path),
                )
                for path in unique_paths
            ),
            metadata={
                "status": "completed",
                "provisional_winner": result.sweep_result.provisional_winner,
                "runtime_summary_path": _relative_run_path(
                    summary_path,
                    self.run_root,
                ),
            },
        )

    def write_report(
        self,
        state: RunState,
        store: RunStore,
    ) -> tuple[ArtifactRecord, ...]:
        del state
        summary_path = self.run_root / "runtime" / "runtime-summary.json"
        if not summary_path.is_file():
            return ()
        summary = _read_run_json(summary_path, label="runtime summary")
        if summary.get("status") != "completed":
            return ()
        paths = (
            self.run_root / "sweep" / "final" / "leaderboard.json",
            self.run_root / "sweep" / "final" / "pareto-frontier.json",
            self.run_root / "sweep" / "final" / "decision-summary.json",
            summary_path,
        )
        return tuple(
            store.artifact_record(
                path,
                media_type="application/json",
            )
            for path in paths
        )


def create_adapter(
    config: Mapping[str, Any],
    run_root: str | Path,
) -> ResearchQALiveAdapter:
    """Factory loaded by ``run_researchqa_overnight.py --adapter``."""

    return ResearchQALiveAdapter(config=config, run_root=run_root)


__all__ = [
    "ResearchQALiveAdapter",
    "ResearchQALiveError",
    "create_adapter",
    "normalize_paper_id",
    "prepare_rq2_corpus",
]
