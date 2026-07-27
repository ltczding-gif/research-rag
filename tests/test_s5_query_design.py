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
    difficulty = Counter()
    slices = Counter()

    for _, block in blocks:
        answerability_match = re.search(
            r"^- \*\*Answerability:\*\* `([^`]+)`$", block, re.MULTILINE
        )
        difficulty_match = re.search(
            r"^- \*\*Difficulty:\*\* `(hard|very-hard)` "
            r"\(([0-9]|10)/10\)$",
            block,
            re.MULTILINE,
        )
        factor_match = re.search(
            r"^- \*\*Difficulty factors:\*\* "
            r"`E=([0-2]), C=([0-2]), R=([0-2]), A=([0-2]), D=([0-2])`$",
            block,
            re.MULTILINE,
        )
        slices_match = re.search(r"^- \*\*Slices:\*\* (.+)$", block, re.MULTILINE)
        assert answerability_match
        assert difficulty_match
        assert factor_match
        assert slices_match
        assert "- **Query:**" in block
        assert "- **Expected answer outline:**" in block
        assert "- **Proposed claims:**" in block
        assert "- **Required evidence groups:**" in block
        assert "- **Source targets for annotation:**" in block

        answerability[answerability_match.group(1)] += 1
        tier = difficulty_match.group(1)
        score = int(difficulty_match.group(2))
        factors = [int(value) for value in factor_match.groups()]
        assert score == sum(factors)
        assert score >= 6
        assert tier == ("hard" if score <= 7 else "very-hard")
        if tier == "hard":
            assert factors[0] >= 1
            assert factors[2] >= 1
            assert factors[4] >= 1
        else:
            assert factors[0] == 2
            assert factors[2] == 2
            assert 2 in (factors[1], factors[3], factors[4])
        difficulty[tier] += 1
        block_slices = re.findall(r"`([^`]+)`", slices_match.group(1))
        assert block_slices.count(f"difficulty-{tier}") == 1
        assert (
            len(
                {
                    value
                    for value in block_slices
                    if value in {"difficulty-hard", "difficulty-very-hard"}
                }
            )
            == 1
        )
        slices.update(block_slices)

        evidence_section = re.search(
            r"^- \*\*Required evidence groups:\*\* (.+?)"
            r"(?=^- \*\*Source targets for annotation:\*\*)",
            block,
            re.MULTILINE | re.DOTALL,
        )
        assert evidence_section
        evidence_groups = set(
            re.findall(r"`(eg\.[a-z0-9._-]+)`", evidence_section.group(1))
        )
        assert len(evidence_groups) >= 2

    assert answerability == {
        "answerable": 20,
        "conflicting": 2,
        "ambiguous": 1,
        "false-premise": 1,
        "no-answer": 1,
    }
    assert difficulty == {"hard": 10, "very-hard": 15}
    assert slices["cross-language"] == 25
    assert slices["negative"] == 5
    assert slices["exact-token"] >= 10
    assert slices["si"] >= 10
    assert slices["multi-hop"] >= 5


def test_benchmark_design_directory_has_an_index():
    index = DESIGN_PATH.with_name("README.md").read_text(encoding="utf-8")

    assert "[`S5_QUERY_DESIGN.md`](S5_QUERY_DESIGN.md)" in index
    assert "not a scratch space" in index


def test_negative_questions_do_not_leak_the_disputed_answer_in_the_prompt():
    text = DESIGN_PATH.read_text(encoding="utf-8")
    blocks = dict(_blocks(text))

    leaked_tokens = {
        "s5.cat.05": {"95.2", "92.2"},
        "s5.cs.05": {"Timeout", "超时"},
        "s5.env.05": {"不可回答", "无法回答", "不可识别"},
        "s5.soc.05": {"45%", "50%", "7:03", "7:08"},
    }
    for query_id, forbidden in leaked_tokens.items():
        query = re.search(
            r"^- \*\*Query:\*\* (.+)$", blocks[query_id], re.MULTILINE
        ).group(1)
        assert not forbidden.intersection(query)


def test_matrix_questions_keep_independent_completeness_groups():
    text = DESIGN_PATH.read_text(encoding="utf-8")
    blocks = dict(_blocks(text))
    minimum_groups = {
        "s5.cat.03": 4,
        "s5.cat.04": 3,
        "s5.bio.03": 4,
    }

    for query_id, minimum in minimum_groups.items():
        evidence_section = re.search(
            r"^- \*\*Required evidence groups:\*\* (.+?)"
            r"(?=^- \*\*Source targets for annotation:\*\*)",
            blocks[query_id],
            re.MULTILINE | re.DOTALL,
        )
        evidence_groups = set(
            re.findall(r"`(eg\.[a-z0-9._-]+)`", evidence_section.group(1))
        )
        assert len(evidence_groups) >= minimum

    bio_flow_claims = set(
        re.findall(
            r"`(cl\.s5\.bio\.05\.[a-z0-9._-]+)`",
            re.search(
                r"^- \*\*Proposed claims:\*\* (.+?)"
                r"(?=^- \*\*Required evidence groups:\*\*)",
                blocks["s5.bio.05"],
                re.MULTILINE | re.DOTALL,
            ).group(1),
        )
    )
    assert bio_flow_claims == {
        "cl.s5.bio.05.observational-flow",
        "cl.s5.bio.05.genetic-flow",
        "cl.s5.bio.05.unresolved-denominators",
    }
