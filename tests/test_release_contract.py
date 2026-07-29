"""Release-surface contracts that must stay aligned with runtime defaults."""

from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_plugin_does_not_expose_unwired_user_config():
    plugin = json.loads(
        (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    assert "userConfig" not in plugin


def test_public_plugin_metadata_has_no_repository_url_placeholder():
    files = [
        REPO_ROOT / ".claude-plugin" / "plugin.json",
        REPO_ROOT / ".claude-plugin" / "marketplace.json",
    ]

    for path in files:
        text = path.read_text(encoding="utf-8")
        json.loads(text)
        assert "<" + "your-org>" not in text
        assert "github.com/" + "<org>" not in text


def test_marketplace_install_is_skills_only():
    marketplace = json.loads(
        (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(
            encoding="utf-8"
        )
    )
    plugin = marketplace["plugins"][0]

    assert plugin["strict"] is False
    assert len(plugin["skills"]) == 8
    assert "mcpServers" not in plugin
    for skill_path in plugin["skills"]:
        assert (REPO_ROOT / skill_path / "SKILL.md").is_file()


def test_local_codex_state_is_ignored():
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".codex/" in gitignore


def test_rq2_public_report_allowlist_is_explicit():
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    report_root = "benchmarks/reports/researchqa-rq2/"
    allowed = {
        "morning-report.md",
        "leaderboard.csv",
        "paper-domain-breakdown.csv",
        "paired-bootstrap.json",
        "pareto-frontier.json",
        "reconciliation.json",
        "run-manifest.json",
        "blocked-and-unmapped.jsonl",
    }

    assert f"!{report_root}" in gitignore
    assert f"{report_root}*" in gitignore
    assert f"!{report_root}**" not in gitignore
    assert {
        line.removeprefix(f"!{report_root}")
        for line in gitignore
        if line.startswith(f"!{report_root}") and line != f"!{report_root}"
    } == allowed


def test_readmes_link_to_each_other():
    english = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (REPO_ROOT / "README_zh-CN.md").read_text(encoding="utf-8")

    assert "[简体中文](README_zh-CN.md)" in english
    assert "[English](README.md)" in chinese


def test_published_skills_describe_current_embedding_defaults():
    skill_paths = [
        REPO_ROOT / "skills" / "rag-engineer" / "SKILL.md",
        REPO_ROOT / "skills" / "embedding-strategies" / "SKILL.md",
        REPO_ROOT / "skills" / "vector-database-engineer" / "SKILL.md",
    ]

    for path in skill_paths:
        text = path.read_text(encoding="utf-8")
        assert "Default local embedding model: `qwen3-embedding:4b`" not in text
        assert "fastembed" in text
