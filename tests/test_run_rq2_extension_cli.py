from __future__ import annotations

import json
from types import SimpleNamespace

from benchmarks.scripts import run_rq2_extension as cli


def test_extension_cli_accepts_r1(tmp_path) -> None:
    args = cli._parse_args(
        [
            "--run-root",
            str(tmp_path),
            "--extension",
            "R1",
        ]
    )

    assert args.extension == "R1"


def test_extension_cli_reports_completed_result(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    monkeypatch.setattr(cli, "load_config", lambda *_args: {"config": "ok"})

    def fake_runtime(config, run_root, *, extension_id):
        assert config == {"config": "ok"}
        assert run_root == tmp_path
        assert extension_id == "F2"
        return SimpleNamespace(
            extension_id="F2",
            record=SimpleNamespace(
                status="completed",
                candidate=SimpleNamespace(config_id="repair-f2-test"),
                primary=0.91,
                guardrails_passed=True,
                resumed=False,
                result_path="result.json",
            ),
            model_preflight_path="model-preflight.json",
            prequality_path="prequality.json",
            runtime_summary_path="runtime-summary.json",
        )

    monkeypatch.setattr(
        cli,
        "run_researchqa_extension_runtime",
        fake_runtime,
    )

    exit_code = cli.main(
        [
            "--run-root",
            str(tmp_path),
            "--extension",
            "F2",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["extension_id"] == "F2"
    assert payload["status"] == "completed"
    assert payload["config_id"] == "repair-f2-test"
    assert payload["guardrails_passed"] is True
