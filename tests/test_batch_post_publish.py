from __future__ import annotations

import zotero_batch_scanner as scanner


def test_missing_optional_review_queue_script_is_skipped(tmp_path, monkeypatch):
    missing = tmp_path / "scripts" / "export_review_queue.py"
    monkeypatch.setattr(scanner, "EXPORT_REVIEW_QUEUE_PATH", missing)

    results = scanner.run_batch_post_publish_actions(["review_queue"], tmp_path)

    assert results == [
        {
            "action": "review_queue",
            "status": "skipped",
            "reason": f"optional script not found: {missing}",
        }
    ]
