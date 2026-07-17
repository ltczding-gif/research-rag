"""Tests for scanner/init_environment.py:register_codex_mcp.

The function writes the research-rag stdio MCP server into a Codex
config.toml. Every case targets a temp path — never the user's real
~/.codex/config.toml. Detection uses tomllib; writes are controlled text
edits that must preserve unrelated servers and comments.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import init_environment as ie


def _expected_launcher(repo_root: Path) -> str:
    return (repo_root / "scripts" / "run_mcp_server.py").resolve().as_posix()


def test_creates_config_when_absent(tmp_path):
    """No config.toml → written with absolute Python + launcher paths."""
    repo_root = tmp_path / "repo"
    config = tmp_path / "codex" / "config.toml"

    outcome = ie.register_codex_mcp(config, repo_root)

    assert outcome == "written"
    assert config.exists()
    parsed = tomllib.loads(config.read_text(encoding="utf-8"))
    server = parsed["mcp_servers"]["research-rag"]
    assert server["command"] == Path(sys.executable).resolve().as_posix()
    assert server["args"] == [_expected_launcher(repo_root)]


def test_idempotent_when_already_registered(tmp_path):
    """Second identical call → already-registered, byte-for-byte unchanged."""
    repo_root = tmp_path / "repo"
    config = tmp_path / "config.toml"

    assert ie.register_codex_mcp(config, repo_root) == "written"
    first = config.read_text(encoding="utf-8")

    assert ie.register_codex_mcp(config, repo_root) == "already-registered"
    assert config.read_text(encoding="utf-8") == first
    # No stray backups were created for a no-op.
    assert not list(config.parent.glob("config.toml.bak-*"))


def test_updates_and_backs_up_when_section_differs(tmp_path):
    """A stale research-rag section is replaced, and a timestamped backup of
    the original is written."""
    repo_root = tmp_path / "repo"
    config = tmp_path / "config.toml"
    config.write_text(
        "[mcp_servers.research-rag]\n"
        'command = "python"\n'
        'args = ["/old/stale/path/run_mcp_server.py"]\n',
        encoding="utf-8",
    )

    outcome = ie.register_codex_mcp(config, repo_root)

    assert outcome == "updated"
    parsed = tomllib.loads(config.read_text(encoding="utf-8"))
    assert parsed["mcp_servers"]["research-rag"]["args"] == [_expected_launcher(repo_root)]
    backups = list(config.parent.glob("config.toml.bak-*"))
    assert len(backups) == 1
    # The backup preserves the original stale content.
    assert "/old/stale/path/run_mcp_server.py" in backups[0].read_text(encoding="utf-8")


def test_preserves_unrelated_servers_and_comments(tmp_path):
    """Updating research-rag must not disturb another server or top-level keys."""
    repo_root = tmp_path / "repo"
    config = tmp_path / "config.toml"
    config.write_text(
        "# my codex config\n"
        'model = "o3"\n'
        "\n"
        "[mcp_servers.other]\n"
        'command = "node"\n'
        'args = ["server.js"]\n'
        "\n"
        "[mcp_servers.research-rag]\n"
        'command = "python"\n'
        'args = ["/old/path.py"]\n',
        encoding="utf-8",
    )

    outcome = ie.register_codex_mcp(config, repo_root)

    assert outcome == "updated"
    text = config.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    # Unrelated content intact.
    assert parsed["model"] == "o3"
    assert parsed["mcp_servers"]["other"] == {"command": "node", "args": ["server.js"]}
    # research-rag updated to the real launcher.
    assert parsed["mcp_servers"]["research-rag"]["args"] == [_expected_launcher(repo_root)]
    # Comment preserved.
    assert "# my codex config" in text


def test_appends_when_other_servers_present_but_research_rag_absent(tmp_path):
    """research-rag absent but another server present → append, keep the other."""
    repo_root = tmp_path / "repo"
    config = tmp_path / "config.toml"
    config.write_text(
        "[mcp_servers.other]\n"
        'command = "node"\n'
        'args = ["server.js"]\n',
        encoding="utf-8",
    )

    outcome = ie.register_codex_mcp(config, repo_root)

    assert outcome == "written"
    parsed = tomllib.loads(config.read_text(encoding="utf-8"))
    assert parsed["mcp_servers"]["other"] == {"command": "node", "args": ["server.js"]}
    assert parsed["mcp_servers"]["research-rag"]["args"] == [_expected_launcher(repo_root)]


def test_skips_malformed_toml_without_touching_file(tmp_path):
    """A config.toml that isn't valid TOML is left untouched (never clobbered)."""
    repo_root = tmp_path / "repo"
    config = tmp_path / "config.toml"
    original = "this is [not valid toml = = =\n"
    config.write_text(original, encoding="utf-8")

    outcome = ie.register_codex_mcp(config, repo_root)

    assert outcome == "skipped"
    assert config.read_text(encoding="utf-8") == original
