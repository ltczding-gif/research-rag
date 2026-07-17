"""Tests for `scanner/doctor.py`.

Covers the env-loading logic, the result aggregation / exit-code mapping,
and a handful of individual checks whose behavior is purely a function of
the environment dict (path checks, processor backend validation).

Network-dependent checks (Ollama, internet) and process-dependent checks
(Zotero pgrep, ChromaDB import) are not mocked here — those are exercised
implicitly by the smoke run in CI / local invocation.
"""

from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path

import pytest


import doctor


def test_load_dotenv_into_map_parses_simple_kv(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FOO=bar\nBAZ=qux\n", encoding="utf-8")
    parsed = doctor._load_dotenv_into_map(env)
    assert parsed == {"FOO": "bar", "BAZ": "qux"}


def test_load_dotenv_skips_comments_and_blanks(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# this is a comment\n"
        "\n"
        "FOO=bar\n"
        "# BAZ=commented_out\n",
        encoding="utf-8",
    )
    parsed = doctor._load_dotenv_into_map(env)
    assert parsed == {"FOO": "bar"}


def test_load_dotenv_strips_quotes(tmp_path):
    env = tmp_path / ".env"
    env.write_text('FOO="bar"\nBAZ=\'qux\'\n', encoding="utf-8")
    parsed = doctor._load_dotenv_into_map(env)
    assert parsed == {"FOO": "bar", "BAZ": "qux"}


def test_load_dotenv_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    env = tmp_path / ".env"
    env.write_text("LOCALRAG_NOTES_DIR=$HOME/notes\n", encoding="utf-8")
    parsed = doctor._load_dotenv_into_map(env)
    assert parsed["LOCALRAG_NOTES_DIR"] == f"{tmp_path}/notes"


def test_load_dotenv_returns_empty_for_missing_file(tmp_path):
    parsed = doctor._load_dotenv_into_map(tmp_path / "does-not-exist.env")
    assert parsed == {}


# --- Individual check behaviors -------------------------------------------


def test_check_processor_backend_accepts_valid():
    r = doctor.check_processor_backend({"LOCALRAG_PROCESSOR_BACKEND": "anthropic"})
    assert r.status == "ok"


def test_check_processor_backend_rejects_invalid():
    r = doctor.check_processor_backend({"LOCALRAG_PROCESSOR_BACKEND": "deepseek-direct"})
    assert r.status == "error"
    assert "deepseek" in r.message


def test_check_processor_backend_defaults_to_subagent():
    """The doctor's fallback must match config.PROCESSOR_BACKEND — a drifted
    default misreports which backend an unconfigured install actually uses."""
    r = doctor.check_processor_backend({})
    assert r.status == "ok"
    assert "subagent" in r.message


def test_check_backend_credentials_for_subagent_skips():
    results = doctor.check_backend_credentials({"LOCALRAG_PROCESSOR_BACKEND": "subagent"})
    assert len(results) == 1
    assert results[0].status == "ok"
    assert "no credentials" in results[0].message


def test_check_backend_credentials_defaults_to_subagent():
    results = doctor.check_backend_credentials({})
    assert len(results) == 1
    assert results[0].status == "ok"
    assert "subagent" in results[0].name


def test_check_backend_credentials_anthropic_missing_key():
    results = doctor.check_backend_credentials({"LOCALRAG_PROCESSOR_BACKEND": "anthropic"})
    assert any(r.status == "error" and "ANTHROPIC_API_KEY" in r.name for r in results)


def test_check_backend_credentials_anthropic_present_key():
    env = {
        "LOCALRAG_PROCESSOR_BACKEND": "anthropic",
        "ANTHROPIC_API_KEY": "sk-ant-fakefakefakefakefake",
    }
    results = doctor.check_backend_credentials(env)
    statuses = {r.name: r.status for r in results}
    assert statuses["ANTHROPIC_API_KEY"] == "ok"


def test_check_backend_credentials_vertex_validates_sa_file(tmp_path):
    sa = tmp_path / "service-account.json"
    sa.write_text(
        '{"client_email": "test@example.iam.gserviceaccount.com", "type": "service_account"}',
        encoding="utf-8",
    )
    env = {
        "LOCALRAG_PROCESSOR_BACKEND": "vertex",
        "GOOGLE_APPLICATION_CREDENTIALS": str(sa),
        "GOOGLE_CLOUD_PROJECT": "my-project",
        "GEMINI_VERTEX_GCS_BUCKET": "my-bucket",
    }
    results = doctor.check_backend_credentials(env)
    statuses = {r.name: r.status for r in results}
    assert statuses["GOOGLE_APPLICATION_CREDENTIALS"] == "ok"
    assert statuses["GOOGLE_CLOUD_PROJECT"] == "ok"
    assert statuses["GEMINI_VERTEX_GCS_BUCKET"] == "ok"


def test_check_backend_credentials_vertex_flags_missing_sa_file(tmp_path):
    env = {
        "LOCALRAG_PROCESSOR_BACKEND": "vertex",
        "GOOGLE_APPLICATION_CREDENTIALS": str(tmp_path / "missing.json"),
        "GOOGLE_CLOUD_PROJECT": "my-project",
        "GEMINI_VERTEX_GCS_BUCKET": "my-bucket",
    }
    results = doctor.check_backend_credentials(env)
    sa_result = next(r for r in results if r.name == "GOOGLE_APPLICATION_CREDENTIALS")
    assert sa_result.status == "error"
    assert "not found" in sa_result.message


def test_check_notes_dir_writable(tmp_path):
    r = doctor.check_notes_dir({"LOCALRAG_NOTES_DIR": str(tmp_path)})
    assert r.status == "ok"


def test_check_notes_dir_missing_path():
    r = doctor.check_notes_dir({"LOCALRAG_NOTES_DIR": "/nonexistent/research-note"})
    assert r.status == "warn"
    assert "doesn't exist" in r.message


def test_check_notes_dir_unset():
    r = doctor.check_notes_dir({})
    assert r.status == "warn"


def test_check_chroma_collections_warns_when_store_missing(tmp_path):
    r = doctor.check_chroma_collections({"LOCALRAG_CHROMA_PATH": str(tmp_path / "missing")})
    assert r.status == "warn"
    assert "build_indexes.py" in (r.hint or "")


def test_check_mcp_tool_registration_accepts_expected_tools():
    expected = ["get_note", "index_status", "search_notes", "search_papers"]

    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout=json.dumps(expected) + "\n", stderr="")

    r = doctor.check_mcp_tool_registration(runner=runner)
    assert r.status == "ok"


def test_check_mcp_tool_registration_reports_import_failure():
    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, stdout="", stderr="missing mcp")

    r = doctor.check_mcp_tool_registration(runner=runner)
    assert r.status == "error"
    assert "missing mcp" in r.message


def test_selected_backend_runtime_subagent_needs_no_sdk():
    r = doctor.check_selected_backend_runtime({"LOCALRAG_PROCESSOR_BACKEND": "subagent"})
    assert r.status == "ok"


def test_check_zotero_db_missing(tmp_path):
    r = doctor.check_zotero_db({"ZOTERO_DB_PATH": str(tmp_path / "no-such-db.sqlite")})
    assert r.status == "error"
    assert "not found" in r.message


def test_check_zotero_db_valid(tmp_path):
    """A real (tiny) sqlite file with the expected schema validates."""
    import sqlite3
    db = tmp_path / "zotero.sqlite"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT);
        CREATE TABLE itemAttachments (itemID INTEGER PRIMARY KEY, parentItemID INTEGER, path TEXT, contentType TEXT);
        INSERT INTO items VALUES (1, 'AAAA1111');
        INSERT INTO itemAttachments VALUES (2, 1, 'storage:paper.pdf', 'application/pdf');
    """)
    conn.commit()
    conn.close()
    r = doctor.check_zotero_db({"ZOTERO_DB_PATH": str(db)})
    assert r.status == "ok"
    assert "1 items" in r.hint
    assert "1 PDF attachments" in r.hint


# --- Version-flexibility checks ------------------------------------------


def test_check_python_version_accepts_3_10(monkeypatch):
    """3.10 is the floor (codebase uses `int | None` syntax)."""
    monkeypatch.setattr(doctor.sys, "version_info", (3, 10, 0, "final", 0))
    r = doctor.check_python_version()
    assert r.status == "ok"


def test_check_python_version_accepts_3_12(monkeypatch):
    """Newer Python should also be ok (no upper bound)."""
    monkeypatch.setattr(doctor.sys, "version_info", (3, 12, 5, "final", 0))
    r = doctor.check_python_version()
    assert r.status == "ok"


def test_check_python_version_rejects_3_9(monkeypatch):
    monkeypatch.setattr(doctor.sys, "version_info", (3, 9, 18, "final", 0))
    r = doctor.check_python_version()
    assert r.status == "error"
    assert "3.10" in r.message or "3.10" in (r.hint or "")


def test_check_chromadb_version_accepts_any_1_x():
    """A minor-version drift from the reference 1.5.5 must not error.

    Skipped when chromadb isn't installed in the test environment — that
    case is covered by the doctor's own error path, not by this version
    check, and we don't want minimal local test environments to require the
    service stack. CI installs the full default requirements and runs this.
    """
    pytest.importorskip("chromadb")
    r = doctor.check_chromadb_version()
    assert r.status in ("ok", "info")


def test_check_chromadb_version_rejects_0_x(monkeypatch):
    """0.x is incompatible because the persistent index format changed at 1.0."""
    fake_chromadb = type(sys)("chromadb")
    fake_chromadb.__version__ = "0.4.24"
    monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)
    r = doctor.check_chromadb_version()
    assert r.status == "error"
    assert "0.4.24" in r.message or "1.0" in (r.hint or "")


def test_check_chromadb_version_info_for_unknown_major(monkeypatch):
    """A hypothetical 2.x release should be informational, not an error."""
    fake_chromadb = type(sys)("chromadb")
    fake_chromadb.__version__ = "2.0.0"
    monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)
    r = doctor.check_chromadb_version()
    assert r.status == "info"


def test_check_chromadb_version_ok_for_other_1_x(monkeypatch):
    """1.4.0 (different minor than the validated 1.5.5) should still be ok."""
    fake_chromadb = type(sys)("chromadb")
    fake_chromadb.__version__ = "1.4.0"
    monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)
    r = doctor.check_chromadb_version()
    assert r.status == "ok"
    assert "1.4.0" in r.message


def test_check_ollama_embed_model_accepts_alternative_when_configured(tmp_path, monkeypatch):
    """If the user configures OLLAMA_EMBED_MODEL=nomic-embed-text and that's
    pulled, the check passes — we don't insist on qwen3-embedding:4b."""
    fake_response = b'{"models": [{"name": "nomic-embed-text:latest"}]}'

    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return fake_response

    def fake_urlopen(*a, **kw):
        return _FakeResp()

    monkeypatch.setattr(doctor.urllib.request, "urlopen", fake_urlopen)
    r = doctor.check_ollama_embed_model({
        "OLLAMA_EMBED_MODEL": "nomic-embed-text",
        "OLLAMA_EMBED_URL": "http://localhost:11434/api/embeddings",
    })
    assert r.status == "ok"


def test_check_ollama_embed_model_warns_when_alternative_available(monkeypatch):
    """Configured model not pulled but a different embedding model is —
    warn (don't error) and suggest the user pick."""
    fake_response = b'{"models": [{"name": "nomic-embed-text:latest"}, {"name": "llama3:8b"}]}'

    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return fake_response

    def fake_urlopen(*a, **kw):
        return _FakeResp()

    monkeypatch.setattr(doctor.urllib.request, "urlopen", fake_urlopen)
    r = doctor.check_ollama_embed_model({
        "OLLAMA_EMBED_MODEL": "qwen3-embedding:4b",
        "OLLAMA_EMBED_URL": "http://localhost:11434/api/embeddings",
    })
    assert r.status == "warn"
    assert "nomic-embed-text" in r.message


# --- Embedding provider branching ------------------------------------------
# The two check_ollama_* tests above assert the ollama-branch behavior;
# these cover the dispatch itself. The default (fastembed) must never
# report Ollama problems on an unconfigured install.


def test_check_embedding_provider_defaults_to_fastembed_ok(monkeypatch):
    """Unset provider → fastembed branch; importable package → single ok."""
    monkeypatch.setattr(doctor, "_can_import", lambda m: m == "fastembed")
    results = doctor.check_embedding_provider({})
    assert len(results) == 1
    assert results[0].status == "ok"
    assert "fastembed" in results[0].message
    assert "Ollama" not in results[0].message


def test_check_embedding_provider_fastembed_missing_package(monkeypatch):
    monkeypatch.setattr(doctor, "_can_import", lambda m: False)
    results = doctor.check_embedding_provider({})
    assert len(results) == 1
    assert results[0].status == "error"
    assert "fastembed" in results[0].message
    assert "pip install" in (results[0].hint or "")


def test_check_embedding_provider_ollama_branch_runs_existing_checks(monkeypatch):
    """provider=ollama delegates to the unchanged Ollama daemon+model checks."""
    monkeypatch.setattr(
        doctor, "check_ollama_running",
        lambda env: doctor.CheckResult("ok", "Ollama running", "stub"),
    )
    monkeypatch.setattr(
        doctor, "check_ollama_embed_model",
        lambda env: doctor.CheckResult("ok", "embedding model pulled", "stub"),
    )
    results = doctor.check_embedding_provider({"LOCALRAG_EMBED_PROVIDER": "ollama"})
    assert [r.name for r in results] == ["Ollama running", "embedding model pulled"]


def test_check_embedding_provider_openai_compat_requires_key():
    results = doctor.check_embedding_provider({"LOCALRAG_EMBED_PROVIDER": "openai-compat"})
    assert len(results) == 1
    assert results[0].status == "error"
    assert "OPENAI_EMBED_API_KEY" in results[0].message


def test_check_embedding_provider_openai_compat_with_key_ok():
    results = doctor.check_embedding_provider({
        "LOCALRAG_EMBED_PROVIDER": "openai-compat",
        "OPENAI_EMBED_API_KEY": "sk-fake",
    })
    assert len(results) == 1
    assert results[0].status == "ok"
    assert "openai-compat" in results[0].message


def test_check_embedding_provider_unknown_value_errors():
    results = doctor.check_embedding_provider({"LOCALRAG_EMBED_PROVIDER": "bogus"})
    assert len(results) == 1
    assert results[0].status == "error"
    assert "bogus" in results[0].message


# --- Aggregation -----------------------------------------------------------


def _make(status: str) -> doctor.CheckResult:
    return doctor.CheckResult(status=status, name="x", message="x")


def test_summarize_counts_each_status():
    groups = [
        ("g1", [_make("ok"), _make("warn"), _make("error")]),
        ("g2", [_make("ok"), _make("info")]),
    ]
    ok, warn, err, info = doctor.summarize(groups)
    assert (ok, warn, err, info) == (2, 1, 1, 1)


def test_summarize_empty_groups():
    ok, warn, err, info = doctor.summarize([("g", [])])
    assert (ok, warn, err, info) == (0, 0, 0, 0)


# --- Real catalysis pack should pass invariants ---------------------------


def test_check_domain_pack_for_real_catalysis_pack():
    repo_root = Path(__file__).resolve().parent.parent
    catalysis = repo_root / "domain-packs" / "catalysis"
    if not catalysis.exists():
        pytest.skip("catalysis pack not present in this checkout")
    results = doctor.check_domain_pack({"LOCALRAG_DOMAIN_PACK": "catalysis"})
    statuses = [r.status for r in results]
    assert "error" not in statuses, f"catalysis pack should pass: {results}"
