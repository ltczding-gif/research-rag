from pathlib import Path

import gemini_analyze_pdf as pipeline


REPO_ROOT = Path(__file__).resolve().parents[1]
CATALYSIS_GENERIC = (
    REPO_ROOT
    / "domain-packs"
    / "catalysis"
    / "templates"
    / "generic-research-note.txt"
)
BOOTSTRAP_GENERIC = (
    REPO_ROOT
    / "domain-packs"
    / "_template"
    / "templates"
    / "generic-research-note.txt"
)
CATALYSIS_ROUTING_HINTS = (
    REPO_ROOT
    / "domain-packs"
    / "catalysis"
    / "prompts"
    / "routing_disambiguation_hints.txt"
)
BOOTSTRAP_ROUTING_HINTS = (
    REPO_ROOT
    / "domain-packs"
    / "_template"
    / "prompts"
    / "routing_disambiguation_hints.txt"
)


def test_generic_system_prompt_excludes_domain_guidance(monkeypatch):
    monkeypatch.setattr(pipeline, "load_system_prompt", lambda _name: "BASE")
    monkeypatch.setattr(
        pipeline, "load_augmented_system_prompt", lambda _name: "BASE\nDOMAIN GUIDANCE"
    )

    assert pipeline.load_note_generator_system_prompt("generic-research-note") == "BASE"
    assert (
        pipeline.load_note_generator_system_prompt("electrocatalysis-experimental")
        == "BASE\nDOMAIN GUIDANCE"
    )


def test_generic_rules_exclude_domain_quality_rules(monkeypatch):
    monkeypatch.setattr(
        pipeline, "load_template_rules", lambda template_id: f"TEMPLATE:{template_id}"
    )
    monkeypatch.setattr(pipeline, "load_universal_rules", lambda: "UNIVERSAL")
    monkeypatch.setattr(pipeline, "load_domain_quality_rules", lambda: "DOMAIN")

    generic = pipeline.compose_note_generator_rules("generic-research-note")
    specialized = pipeline.compose_note_generator_rules(
        "electrocatalysis-experimental"
    )

    assert generic == "TEMPLATE:generic-research-note.txt\n\nUNIVERSAL"
    assert specialized == (
        "TEMPLATE:electrocatalysis-experimental.txt\n\nUNIVERSAL\n\nDOMAIN"
    )


def test_generic_template_is_field_neutral_and_kept_in_sync():
    catalysis = CATALYSIS_GENERIC.read_text(encoding="utf-8")
    bootstrap = BOOTSTRAP_GENERIC.read_text(encoding="utf-8")

    assert catalysis == bootstrap
    assert "默认只加载 `_universal_rules.txt`" in catalysis
    assert "generic 必须完整保存文档" in catalysis
    assert "恰好四个领域中立维度" in catalysis
    assert "当前 domain pack 的领域专属评分轴" in catalysis
    assert "当前 domain pack 的 `_domain_quality_rules.txt`" in catalysis
    assert "## 研究设计与证据地图" in catalysis
    assert "## 核心发现与证据链" in catalysis
    assert "Evidence ID" in catalysis
    assert "Claim ID" in catalysis
    assert "## 主导证据类型" not in catalysis
    assert "## 证据层级与混合处理" not in catalysis
    assert "## 核心内容" not in catalysis
    assert "## 最强证据或论证" not in catalysis
    assert "## 审稿人视角（Adaptive Red-Team Verdict）" in catalysis
    assert "只审查 3–7 个 load-bearing claims" in catalysis
    assert "最终输出不超过三个 surviving concerns" in catalysis
    assert "不得写成 `[SI p.S1]`" in catalysis
    assert "不得把 `N 个关联` 改写成 `N 个独立实体`" in catalysis
    assert "不列举或导入当前 active pack" in catalysis
    assert "按 claim 实际限定后的强度进行裁决" in catalysis
    assert "严重性只相对该行原样写出的 bounded claim" in catalysis
    assert "必须在逻辑上能够区分表中所写的替代解释" in catalysis
    assert "每个 major concern 只能指定一项最有判别力的补证设计" in catalysis
    assert "不得合并 `timeout/unknown`" in catalysis
    assert "不等于“按成功结果筛选”" in catalysis
    assert "具体输出结构在 reviewer-lens 方案确认后冻结" not in catalysis
    assert len(catalysis.splitlines()) <= 240


def test_note_generator_prompt_records_rule_scope():
    profile = {"recommended_template": "generic-research-note"}

    generic = pipeline.build_note_generator_user_prompt(
        profile, "generic-research-note", "RULES"
    )
    specialized = pipeline.build_note_generator_user_prompt(
        profile, "electrocatalysis-experimental", "RULES"
    )

    assert (
        "field-neutral: active domain-pack guidance and quality rules are excluded"
        in generic
    )
    assert (
        "active-domain: active domain-pack guidance and quality rules are included"
        in specialized
    )


def test_catalysis_pack_cannot_leak_into_actual_generic_prompt(monkeypatch):
    monkeypatch.setattr(
        pipeline, "DOMAIN_PACK_ROOT", str(REPO_ROOT / "domain-packs" / "catalysis")
    )
    monkeypatch.setattr(
        pipeline,
        "UNIVERSAL_RULES_PATH",
        str(REPO_ROOT / "prompts" / "_universal_rules.txt"),
    )

    system_prompt = pipeline.load_note_generator_system_prompt(
        "generic-research-note"
    )
    rules = pipeline.compose_note_generator_rules("generic-research-note")

    assert "Catalysis seed_terms calibration" not in system_prompt
    assert "## Catalysis Domain Quality Rules" not in rules
    assert "### Catalysis Trap Scan" not in rules
    assert "恰好四个领域中立维度" in rules


def test_profiler_routes_out_of_pack_methods_and_theory_to_generic():
    catalysis = CATALYSIS_ROUTING_HINTS.read_text(encoding="utf-8")
    bootstrap = BOOTSTRAP_ROUTING_HINTS.read_text(encoding="utf-8")

    assert "Active-pack boundary gate" in catalysis
    assert "software/ML" in catalysis
    assert "Formal reasoning or ML methodology" in catalysis
    assert "earth-system methods" in catalysis
    assert "Active-pack boundary gate (required)" in bootstrap
