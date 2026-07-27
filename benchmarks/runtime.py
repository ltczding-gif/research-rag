"""Fail-closed filesystem layout for isolated benchmark runs."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


_SAFE_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_REQUIRED_PRODUCTION_PATHS = frozenset(
    {
        "localrag_home",
        "notes",
        "chroma",
        "pdf_ledger",
        "notes_ledger",
        "textbook_ledger",
        "query_log",
        "zotero_db",
    }
)


class BenchmarkIsolationError(RuntimeError):
    """Raised before a benchmark could touch production-owned state."""


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


@dataclass(frozen=True)
class BenchmarkRunLayout:
    """All mutable paths and collection names owned by one benchmark run."""

    run_id: str
    root: Path
    home: Path
    notes: Path
    chroma: Path
    cache: Path
    artifacts: Path
    reports: Path
    pdf_ledger: Path
    notes_ledger: Path
    textbook_ledger: Path
    query_log: Path
    zotero_db: Path
    papers_collection: str
    notes_collection: str

    @property
    def directories(self) -> tuple[Path, ...]:
        return (
            self.root,
            self.home,
            self.notes,
            self.chroma,
            self.cache,
            self.artifacts,
            self.reports,
            self.pdf_ledger.parent,
            self.query_log,
        )

    def require_owned(self, path: str | Path) -> Path:
        """Return a canonical path only when it belongs to this run."""
        candidate = _resolved(path)
        if candidate != self.root and self.root not in candidate.parents:
            raise BenchmarkIsolationError(
                f"refusing path outside benchmark run_root: {candidate}"
            )
        return candidate

    def environment(self) -> dict[str, str]:
        """Environment overrides that keep product code inside this run."""
        return {
            "LOCALRAG_HOME": str(self.home),
            "LOCALRAG_NOTES_DIR": str(self.notes),
            "LOCALRAG_CHROMA_PATH": str(self.chroma),
            "LOCALRAG_PDF_LEDGER": str(self.pdf_ledger),
            "LOCALRAG_NOTES_LEDGER": str(self.notes_ledger),
            "LOCALRAG_TEXTBOOK_LEDGER": str(self.textbook_ledger),
            "LOCALRAG_QUERY_LOG_ROOT": str(self.query_log),
            "LOCALRAG_PAPERS_COLLECTION": self.papers_collection,
            "LOCALRAG_NOTES_COLLECTION": self.notes_collection,
            "ZOTERO_DB_PATH": str(self.zotero_db),
            "FASTEMBED_CACHE_PATH": str(self.cache / "fastembed"),
            "HF_HOME": str(self.cache / "huggingface"),
        }


@dataclass(frozen=True)
class IsolatedRunResult:
    """One completed child process and the run-owned paths it used."""

    layout: BenchmarkRunLayout
    process: subprocess.CompletedProcess[str]


def create_run_layout(
    run_root: str | Path,
    *,
    run_id: str,
    production_paths: Mapping[str, str | Path],
) -> BenchmarkRunLayout:
    """Validate isolation first, then create one run-owned directory tree."""
    if not _SAFE_RUN_ID.fullmatch(run_id):
        raise ValueError(
            "run_id must match ^[a-z0-9][a-z0-9._-]{0,127}$"
        )
    missing_paths = sorted(_REQUIRED_PRODUCTION_PATHS - production_paths.keys())
    if missing_paths:
        raise BenchmarkIsolationError(
            "production_paths is missing required entries: "
            + ", ".join(missing_paths)
        )

    root = _resolved(run_root)
    for label, raw_path in production_paths.items():
        production_path = _resolved(raw_path)
        if _paths_overlap(root, production_path):
            raise BenchmarkIsolationError(
                f"benchmark run_root overlaps production path "
                f"{label!r}: {production_path}"
            )

    layout = BenchmarkRunLayout(
        run_id=run_id,
        root=root,
        home=root / "home",
        notes=root / "notes",
        chroma=root / "chroma",
        cache=root / "cache",
        artifacts=root / "artifacts",
        reports=root / "reports",
        pdf_ledger=root / "ledgers" / "papers.txt",
        notes_ledger=root / "ledgers" / "notes.txt",
        textbook_ledger=root / "ledgers" / "textbooks.txt",
        query_log=root / "query-logs",
        zotero_db=root / "no-production-zotero.sqlite",
        papers_collection=f"benchmark_{run_id}_papers",
        notes_collection=f"benchmark_{run_id}_notes",
    )
    for directory in layout.directories:
        layout.require_owned(directory).mkdir(parents=True, exist_ok=True)
    return layout


def run_isolated(
    command: Sequence[str],
    *,
    run_root: str | Path,
    run_id: str,
    production_paths: Mapping[str, str | Path],
    base_environment: Mapping[str, str] | None = None,
) -> IsolatedRunResult:
    """Run a command without a shell under an isolated benchmark environment."""
    if not command:
        raise ValueError("command must contain at least one executable")

    layout = create_run_layout(
        run_root,
        run_id=run_id,
        production_paths=production_paths,
    )
    inherited = dict(os.environ)
    if base_environment is not None:
        inherited.update(base_environment)
    child_environment = {
        key: value
        for key, value in inherited.items()
        if not key.startswith("LOCALRAG_")
        and key not in {
            "ZOTERO_DB_PATH",
            "FASTEMBED_CACHE_PATH",
            "HF_HOME",
        }
    }
    child_environment.update(layout.environment())

    process = subprocess.run(
        list(command),
        cwd=layout.root,
        env=child_environment,
        text=True,
        capture_output=True,
        check=False,
        shell=False,
    )
    return IsolatedRunResult(layout=layout, process=process)
