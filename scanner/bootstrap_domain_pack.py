#!/usr/bin/env python3
"""Bootstrap a new domain pack from `domain-packs/_template/`.

Usage:
    python scanner/bootstrap_domain_pack.py --name <field-slug>

The CLI asks 5-6 questions, copies `_template/` to
`domain-packs/<field-slug>/`, and substitutes the user's answers into
`pack.yaml`, the schema's research_domain enum, the
`recommended_template` enum, and the `_domain_quality_rules.txt` axis-4
name. The user is then expected to hand-edit the prompts and templates;
this CLI doesn't try to LLM-generate field-specific prose.

Validation modes:
    --validate <pack-name>   Check pack invariants without bootstrapping.
                              Verifies every recommended_template enum
                              has a sibling templates/<id>.txt file.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "domain-packs" / "_template"
PACKS_DIR = REPO_ROOT / "domain-packs"


# --- Pack invariant validation ---------------------------------------------


def validate_pack(pack_root: Path) -> tuple[bool, list[str]]:
    """Check that pack contents are internally consistent.

    Returns (is_valid, error_messages).
    """
    errors: list[str] = []
    if not pack_root.exists():
        return False, [f"Pack directory missing: {pack_root}"]

    required_files = [
        "pack.yaml",
        "prompts/document_profiler.system.txt",
        "prompts/note_generator.system.txt",
        "prompts/seed_terms_guidance.txt",
        "prompts/routing_disambiguation_hints.txt",
        "schemas/document_profile.vertex.schema.json",
        "schemas/structured_note.vertex.schema.json",
        "templates/_domain_quality_rules.txt",
        "config/model_routing_policy.json",
    ]
    for rel in required_files:
        if not (pack_root / rel).exists():
            errors.append(f"missing required file: {rel}")

    # Cross-check: every recommended_template enum value must have a template file.
    schema_path = pack_root / "schemas" / "document_profile.vertex.schema.json"
    if schema_path.exists():
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            templates_enum = (
                schema.get("properties", {})
                .get("recommended_template", {})
                .get("enum", [])
            )
        except (json.JSONDecodeError, AttributeError) as e:
            errors.append(f"could not parse document_profile schema: {e}")
            templates_enum = []

        for template_id in templates_enum:
            tpath = pack_root / "templates" / f"{template_id}.txt"
            if not tpath.exists():
                errors.append(
                    f"recommended_template enum '{template_id}' has no "
                    f"corresponding templates/{template_id}.txt file"
                )

        # Reverse check: every templates/*.txt (besides underscore-prefix
        # internals) should appear in the enum so it's actually reachable.
        if pack_root / "templates" in pack_root.iterdir() if pack_root.exists() else []:
            pass
        for tpath in (pack_root / "templates").glob("*.txt"):
            if tpath.name.startswith("_"):
                continue
            template_id = tpath.stem
            if template_id not in templates_enum:
                errors.append(
                    f"templates/{tpath.name} exists but is not listed in "
                    f"document_profile schema's recommended_template enum"
                )

    return (not errors), errors


def cmd_validate(pack_name: str) -> int:
    pack_root = PACKS_DIR / pack_name
    ok, errors = validate_pack(pack_root)
    if ok:
        print(f"✓ pack '{pack_name}' passes invariant checks")
        return 0
    print(f"✗ pack '{pack_name}' has {len(errors)} issue(s):", file=sys.stderr)
    for err in errors:
        print(f"  - {err}", file=sys.stderr)
    return 1


# --- Interactive prompts ---------------------------------------------------


def _slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            answer = input(f"{prompt}{suffix}: ").strip()
        except EOFError:
            answer = ""
        if answer:
            return answer
        if default is not None:
            return default
        print("  (this field is required)")


def _ask_list(prompt: str, default: list[str] | None = None) -> list[str]:
    """Comma-separated input. Empty answer accepts default."""
    default_str = ", ".join(default) if default else ""
    suffix = f" [{default_str}]" if default_str else ""
    while True:
        try:
            answer = input(f"{prompt}{suffix}\n  > ").strip()
        except EOFError:
            answer = ""
        if not answer and default is not None:
            return list(default)
        if answer:
            return [item.strip() for item in answer.split(",") if item.strip()]
        print("  (provide at least one item, comma-separated)")


def gather_answers(slug: str) -> dict:
    print()
    print("=" * 64)
    print(f"  Bootstrap a new domain pack: {slug}")
    print("=" * 64)
    print()
    print("Six questions. Answers go into pack.yaml, the document-profile")
    print("schema, and the _domain_quality_rules.txt axis-4 axis name. You")
    print("can revise everything afterwards by hand-editing the files.")
    print()

    description = _ask(
        "1. One-sentence description of your field's literature scope"
    )

    language = _ask(
        "2. Note body language (BCP47 tag, e.g. zh-CN, en, ja)",
        default="zh-CN",
    )

    print()
    print("  Q3 calibration — what's the right granularity for research_domain?")
    print("    Too broad:  ['biology']                 — 1 entry, useless for routing")
    print("    Too narrow: ['T-cell-development', ...] — 50 entries, unmaintainable")
    print("    Right:      ['cell-biology', 'immunology', 'neuroscience',")
    print("                 'cancer-biology', 'developmental-biology']  — 5-10 buckets")
    print("  Pick named sub-disciplines a researcher in your field would recognize.")
    print()
    research_domains = _ask_list(
        "3. research_domain enum: 5-10 sub-areas of your field, comma-separated"
    )

    print()
    print("  Q4 calibration — how many domain-specific templates?")
    print("    Catalysis ships 3: electrocatalysis-experimental, thermocatalysis-experimental,")
    print("                       methods-or-materials-synthesis. Each has a body structure")
    print("                       distinct enough to warrant its own template.")
    print("    Biology might ship 3: wet-lab-experimental, omics-computational,")
    print("                          clinical-translational.")
    print("  Rule of thumb: a NEW template is only worth it when its body sections")
    print("  differ enough from your other templates that combining them would force")
    print("  the model to write awkward 'this section may not apply' filler.")
    print("  Start with 1 ('research-article'); add more once you've seen real output.")
    print()
    extra_templates = _ask_list(
        "4. Field-specific template ids you'll write (besides the 4 universal\n"
        "     ones: review-or-perspective, phd-dissertation, foundational-theory,\n"
        "     generic-research-note). Comma-separated kebab-case.",
        default=["research-article"],
    )

    primary_routing_field = _ask(
        "5. primary_routing_key field name in your domain (e.g. 'primary_reaction'\n"
        "     for catalysis, 'primary_paradigm' for biology, 'primary_task' for ML)",
        default="primary_topic",
    )

    axis_4_name = _ask(
        "6. Name for scoring axis-4 (the field-specific relevance dimension).\n"
        "     Catalysis uses '工业应用潜力 (industrial application potential)'.\n"
        "     Biology might use 'biological / clinical relevance'.",
        default="real-world relevance",
    )

    return {
        "name": slug,
        "description": description,
        "language": language,
        "research_domains": research_domains,
        "extra_templates": extra_templates,
        "primary_routing_field": primary_routing_field,
        "axis_4_name": axis_4_name,
    }


# --- File mutation helpers -------------------------------------------------


def _replace_in_file(path: Path, replacements: list[tuple[str, str]]) -> None:
    text = path.read_text(encoding="utf-8")
    for old, new in replacements:
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")


def _write_pack_yaml(pack_root: Path, answers: dict) -> None:
    universal_templates = [
        ("review-or-perspective", "Reviews, perspectives, roadmaps, commentaries (universal copy)"),
        ("phd-dissertation", "Multi-chapter dissertations and theses (universal copy)"),
        ("foundational-theory", "Theory papers, mathematical derivations (universal copy)"),
        ("generic-research-note", "Fallback template for documents that don't fit elsewhere (universal copy)"),
    ]

    template_lines = []
    for tid in answers["extra_templates"]:
        template_lines.append(f"  - id: {tid}")
        template_lines.append(f"    description: TODO — describe what this template covers")
        template_lines.append(f"    domain_specific: true")
    for tid, desc in universal_templates:
        template_lines.append(f"  - id: {tid}")
        template_lines.append(f"    description: {desc}")
        template_lines.append(f"    domain_specific: false")

    routing_examples = "\n".join(
        f"    - TODO-example-{i+1}" for i in range(3)
    )

    content = f"""# Domain pack manifest — bootstrapped {answers["name"]}

name: {answers["name"]}
description: >
  {answers["description"]}
language: {answers["language"]}
pack_schema_version: 1
pack_version: "0.1.0"

# `id` must equal templates/<id>.txt and recommended_template enum value.
templates:
{chr(10).join(template_lines)}

primary_routing_key:
  field_name: {answers["primary_routing_field"]}
  examples:
{routing_examples}
"""
    (pack_root / "pack.yaml").write_text(content, encoding="utf-8")


def _patch_document_profile_schema(pack_root: Path, answers: dict) -> None:
    schema_path = pack_root / "schemas" / "document_profile.vertex.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema.pop("_TODO_BOOTSTRAP_NOTES", None)

    # Replace research_domain enum
    domain_enum = list(answers["research_domains"]) + ["multidomain", "other"]
    schema["properties"]["research_domain"]["enum"] = domain_enum

    # Replace recommended_template enum: extras + universals
    universal_templates = [
        "review-or-perspective",
        "phd-dissertation",
        "foundational-theory",
        "generic-research-note",
    ]
    template_enum = list(dict.fromkeys(answers["extra_templates"] + universal_templates))
    schema["properties"]["recommended_template"]["enum"] = template_enum

    schema_path.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _patch_quality_rules(pack_root: Path, answers: dict) -> None:
    """Insert the user's axis-4 name into _domain_quality_rules.txt.

    We replace the literal `TODO-axis-4-name` placeholder. Other TODOs
    remain for the user to fill in by hand — this CLI doesn't try to
    invent field-specific trap-scan items.
    """
    path = pack_root / "templates" / "_domain_quality_rules.txt"
    _replace_in_file(
        path,
        [("TODO-axis-4-name", answers["axis_4_name"])],
    )


def _create_extra_template_stubs(pack_root: Path, answers: dict) -> None:
    """For each extra_templates id beyond 'research-article', create a stub
    by copying research-article.txt and adding a header note. Skip
    'research-article' since it already exists.
    """
    base = (pack_root / "templates" / "research-article.txt").read_text(encoding="utf-8")
    for tid in answers["extra_templates"]:
        target = pack_root / "templates" / f"{tid}.txt"
        if target.exists():
            continue
        header = (
            f"# Stub template for {tid}\n"
            f"# Copied from research-article.txt during bootstrap.\n"
            f"# Customize the body structure for {tid}-specific papers.\n\n"
        )
        target.write_text(header + base, encoding="utf-8")


# --- Main flow -------------------------------------------------------------


def cmd_bootstrap(name: str) -> int:
    slug = _slugify(name)
    if not slug:
        print(f"✗ '{name}' produced an empty slug; pick a name with letters/digits.", file=sys.stderr)
        return 2

    pack_root = PACKS_DIR / slug
    if pack_root.exists():
        print(
            f"✗ {pack_root} already exists. Pick a different --name, or delete the directory if you really want to start over.",
            file=sys.stderr,
        )
        return 2

    if not TEMPLATE_DIR.exists():
        print(f"✗ template directory missing: {TEMPLATE_DIR}", file=sys.stderr)
        return 2

    answers = gather_answers(slug)

    print()
    print(f"Copying {TEMPLATE_DIR.relative_to(REPO_ROOT)} → {pack_root.relative_to(REPO_ROOT)}")
    shutil.copytree(TEMPLATE_DIR, pack_root)
    # Keep the README as a per-pack reference card. The "What's in this skeleton"
    # table is genuinely useful AFTER bootstrap as a "what each file does" cheat
    # sheet; the user can edit it if the "starter skeleton" framing bothers them.

    _write_pack_yaml(pack_root, answers)
    _patch_document_profile_schema(pack_root, answers)
    _patch_quality_rules(pack_root, answers)
    _create_extra_template_stubs(pack_root, answers)

    print()
    print("=" * 64)
    print(f"  Pack '{slug}' bootstrapped at: {pack_root}")
    print("=" * 64)
    print()
    print("Suggested authoring order:")
    print()
    print("  1. Edit schemas/document_profile.vertex.schema.json")
    print("     (the research_domain and recommended_template enums are")
    print("     filled in; review them, then refine if needed)")
    print()
    print("  2. Edit templates/_domain_quality_rules.txt")
    print("     - Replace the TODO trap-scan items with 5-10 quality checks")
    print("       specific to your field's recurring methodological failure")
    print("       modes")
    print("     - Replace the TODO filename slot semantics")
    print(f"     - Axis-4 name is already set to: {answers['axis_4_name']}")
    print()
    print(f"  3. Edit templates/{answers['extra_templates'][0]}.txt")
    print("     (your primary experimental-paper template body structure)")
    print()
    print("  4. Edit prompts/seed_terms_guidance.txt")
    print("     - Replace catalysis-style examples with 3-5 worked cases")
    print("       from YOUR field (good and bad seed_terms)")
    print()
    print("  5. Edit prompts/routing_disambiguation_hints.txt")
    print("     - Add 3-7 routing tie-breakers your documents trigger")
    print()
    print(f"  6. Validate: python scanner/bootstrap_domain_pack.py --validate {slug}")
    print()
    print(f"  7. Dry-run: pick 5 PDFs and run")
    print(f"       LOCALRAG_DOMAIN_PACK={slug} python scanner/zotero_batch_scanner.py --limit 5")
    print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap a new literature-note domain pack."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--name",
        help="Slug for the new pack (will become domain-packs/<slug>/)",
    )
    group.add_argument(
        "--validate",
        metavar="PACK",
        help="Validate an existing pack's invariants without bootstrapping",
    )
    args = parser.parse_args()

    if args.validate:
        return cmd_validate(args.validate)
    return cmd_bootstrap(args.name)


if __name__ == "__main__":
    sys.exit(main())
