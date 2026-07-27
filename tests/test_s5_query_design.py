import re
from collections import Counter
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DESIGN_PATH = REPO_ROOT / "benchmarks" / "design" / "S5_QUERY_DESIGN.md"


def _blocks(text: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"^### `([^`]+)`$", text, re.MULTILINE))
    return [
        (
            match.group(1),
            text[
                match.end() : matches[index + 1].start()
                if index + 1 < len(matches)
                else len(text)
            ],
        )
        for index, match in enumerate(matches)
    ]


def test_s5_query_design_has_balanced_unique_candidates():
    text = DESIGN_PATH.read_text(encoding="utf-8")
    blocks = _blocks(text)
    query_ids = [query_id for query_id, _ in blocks]

    assert len(query_ids) == len(set(query_ids)) == 25
    assert Counter(query_id.split(".")[1] for query_id in query_ids) == {
        "cat": 5,
        "bio": 5,
        "cs": 5,
        "env": 5,
        "soc": 5,
    }


def test_s5_query_design_covers_release_critical_slices_and_boundaries():
    text = DESIGN_PATH.read_text(encoding="utf-8")
    blocks = _blocks(text)
    answerability = Counter()
    slices = Counter()

    for _, block in blocks:
        answerability_match = re.search(
            r"^- \*\*Answerability:\*\* `([^`]+)`$", block, re.MULTILINE
        )
        slices_match = re.search(r"^- \*\*Slices:\*\* (.+)$", block, re.MULTILINE)
        assert answerability_match
        assert slices_match
        assert "- **Query:**" in block
        assert "- **Expected answer outline:**" in block
        assert "- **Proposed claims:**" in block
        assert "- **Required evidence groups:**" in block
        assert "- **Source targets for annotation:**" in block

        answerability[answerability_match.group(1)] += 1
        slices.update(re.findall(r"`([^`]+)`", slices_match.group(1)))

    assert answerability == {
        "answerable": 20,
        "conflicting": 2,
        "ambiguous": 1,
        "false-premise": 1,
        "no-answer": 1,
    }
    assert slices["cross-language"] == 25
    assert slices["negative"] == 5
    assert slices["exact-token"] >= 10
    assert slices["si"] >= 10
    assert slices["multi-hop"] >= 5
