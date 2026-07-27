from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from benchmarks.runtime import (
    BenchmarkIsolationError,
    create_run_layout,
    run_isolated,
)


def _production_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "localrag_home": tmp_path / "production",
        "chroma": tmp_path / "production" / "chroma",
        "pdf_ledger": tmp_path / "production" / "processed_groups.txt",
        "notes_ledger": tmp_path / "production" / "processed_notes.txt",
        "textbook_ledger": tmp_path / "production" / "textbook_ledger.txt",
        "notes": tmp_path / "vault",
        "query_log": tmp_path / "vault" / "_query_logs",
        "zotero_db": tmp_path / "zotero" / "zotero.sqlite",
    }


def test_create_run_layout_builds_only_isolated_paths(tmp_path):
    run_root = tmp_path / "benchmark-runs" / "run-001"

    layout = create_run_layout(
        run_root,
        run_id="run-001",
        production_paths=_production_paths(tmp_path),
    )

    assert layout.root == run_root.resolve()
    assert layout.chroma == layout.root / "chroma"
    assert layout.cache == layout.root / "cache"
    assert layout.pdf_ledger == layout.root / "ledgers" / "papers.txt"
    assert layout.notes_ledger == layout.root / "ledgers" / "notes.txt"
    assert layout.query_log == layout.root / "query-logs"
    assert layout.papers_collection == "benchmark_run-001_papers"
    assert layout.notes_collection == "benchmark_run-001_notes"
    assert all(path.is_dir() for path in layout.directories)

    env = layout.environment()
    assert Path(env["LOCALRAG_HOME"]) == layout.home
    assert Path(env["LOCALRAG_CHROMA_PATH"]) == layout.chroma
    assert Path(env["LOCALRAG_PDF_LEDGER"]) == layout.pdf_ledger
    assert Path(env["LOCALRAG_QUERY_LOG_ROOT"]) == layout.query_log
    assert env["LOCALRAG_PAPERS_COLLECTION"] == layout.papers_collection


@pytest.mark.parametrize("relationship", ["same", "inside", "contains"])
def test_run_root_rejects_overlap_with_production_paths(tmp_path, relationship):
    production_chroma = tmp_path / "production" / "chroma"
    if relationship == "same":
        run_root = production_chroma
    elif relationship == "inside":
        run_root = production_chroma / "benchmark"
    else:
        run_root = tmp_path / "production"

    with pytest.raises(BenchmarkIsolationError, match="production path"):
        production_paths = _production_paths(tmp_path)
        production_paths["chroma"] = production_chroma
        create_run_layout(
            run_root,
            run_id="unsafe-run",
            production_paths=production_paths,
        )

    assert not run_root.exists()


def test_run_layout_rejects_cleanup_target_outside_run_root(tmp_path):
    layout = create_run_layout(
        tmp_path / "runs" / "safe",
        run_id="safe",
        production_paths=_production_paths(tmp_path),
    )

    with pytest.raises(BenchmarkIsolationError, match="outside benchmark run_root"):
        layout.require_owned(tmp_path / "production" / "chroma")


@pytest.mark.parametrize("run_id", ["../escape", "spaces are unsafe", "UPPERCASE"])
def test_run_id_must_be_a_safe_public_identifier(tmp_path, run_id):
    with pytest.raises(ValueError, match="run_id"):
        create_run_layout(
            tmp_path / "run",
            run_id=run_id,
            production_paths=_production_paths(tmp_path),
        )


def test_run_isolated_replaces_inherited_product_environment(tmp_path):
    run_root = tmp_path / "runs" / "isolated"
    output_path = run_root / "artifacts" / "child-environment.json"
    child = (
        "import json, os, pathlib; "
        f"path = pathlib.Path({str(output_path)!r}); "
        "path.write_text(json.dumps({"
        "'cwd': str(pathlib.Path.cwd()), "
        "'home': os.environ.get('LOCALRAG_HOME'), "
        "'chroma': os.environ.get('LOCALRAG_CHROMA_PATH'), "
        "'private': os.environ.get('LOCALRAG_PRIVATE_SENTINEL')"
        "}), encoding='utf-8')"
    )
    inherited_env = {
        "PATH": "",
        "LOCALRAG_HOME": str(tmp_path / "production"),
        "LOCALRAG_CHROMA_PATH": str(tmp_path / "production" / "chroma"),
        "LOCALRAG_PRIVATE_SENTINEL": "must-not-leak",
    }
    result = run_isolated(
        [sys.executable, "-c", child],
        run_root=run_root,
        run_id="isolated",
        production_paths=_production_paths(tmp_path),
        base_environment=inherited_env,
    )

    assert result.process.returncode == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert Path(payload["cwd"]) == result.layout.root
    assert Path(payload["home"]) == result.layout.home
    assert Path(payload["chroma"]) == result.layout.chroma
    assert payload["private"] is None


def test_run_layout_requires_all_production_boundaries(tmp_path):
    run_root = tmp_path / "run"

    with pytest.raises(BenchmarkIsolationError, match="missing required entries"):
        create_run_layout(
            run_root,
            run_id="missing-boundaries",
            production_paths={"chroma": tmp_path / "production" / "chroma"},
        )

    assert not run_root.exists()


def test_run_isolated_requires_a_nonempty_command(tmp_path):
    with pytest.raises(ValueError, match="command"):
        run_isolated(
            [],
            run_root=tmp_path / "run",
            run_id="empty",
            production_paths=_production_paths(tmp_path),
        )
