"""Verify the fixed synthetic main/SI/global service-evidence fixture only."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks import researchqa_scoring as scorer
from benchmarks.service_evidence_adapter import (
    ServiceRequest,
    CanonicalGeneration,
    score_service_response,
)


def _mapping(scorer, gold):
    groups = [{"group_id": group["role"], "alternatives": [group["reference_text"]]} for group in gold["groups"]]
    overrides = {}
    for group_index, group in enumerate(gold["groups"]):
        alternative_id = scorer.evidence_alternative_id(gold["row_id"], group_index, 0)
        overrides[alternative_id] = {
            "mapped_item_ids": (), "match_method": "fixture_exact",
            "verification_state": "exact", "gold_version": "synthetic-v1",
            "gold_spans": [group["gold_span"]],
        }
    return scorer.map_reference_groups(
        row_id=gold["row_id"], paper_id=gold["paper_id"], domain=gold["domain"],
        question_type=gold["question_type"], reference_groups=groups,
        mapper=lambda _: None, alternative_overrides=overrides,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args()
    manifest_payload = json.loads((args.trace_dir / "generation-manifest.json").read_text(encoding="utf-8"))
    generation = CanonicalGeneration(
        manifest=manifest_payload,
        manifest_sha256=args.manifest_sha256,
        pages_bytes=(args.trace_dir / "pages.jsonl").read_bytes(),
    )
    gold = json.loads((args.trace_dir / "synthetic-gold.json").read_text(encoding="utf-8"))
    mapping = _mapping(scorer, gold)
    payload = {"fixture_only": True, "real_research_quality_claim": False, "runs": {}}
    for name in ("main", "si", "global"):
        trace = json.loads((args.trace_dir / f"{name}-trace.json").read_text(encoding="utf-8"))
        if trace["scope"] not in {"paper-scoped", "global"}:
            raise RuntimeError(f"unsupported trace scope: {trace['scope']}")
        scope = "paper" if trace["scope"] == "paper-scoped" else "corpus"
        request = ServiceRequest.from_tool_call(
            trace["request"], scope=scope, paper_id=gold["paper_id"] if scope == "paper" else None,
        )
        payload["runs"][name] = score_service_response(
            mapping=mapping, request=request, response=trace["response"], generation=generation,
            scorer=scorer, service_revision=trace["service_revision"],
            trace_classification="synthetic_offline_service_trace",
            recorded_generation_id=trace["generation_id"], recorded_embedding=trace["embedding"],
            client_total_seconds=trace.get("client_total_seconds"),
        ).to_dict()
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
