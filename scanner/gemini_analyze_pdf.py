# TODO (T10): YAML frontmatter validation & auto-fix
# - Issue: journal/title fields containing colons (e.g., "Applied Catalysis B: Environmental")
#   cause YAML parse errors if not quoted
# - Fix: Auto-wrap values containing special YAML chars (:, #, {, }, [, ], ,, &, *, ?, |, -, <, >, =, !, %, @, `) in quotes
# - Validation: After generation, parse YAML with yaml.safe_load(), log error if fails
# - Reference: 2025-03-13 fix applied to 3 notes (2005-ApplCatalBEnviron, 2007-JACS, 2023-JMemSci)

import os
import sys
import argparse
import hashlib
import json
import math
import re
import sqlite3
import shutil
import subprocess
import yaml
from datetime import datetime, timezone
from pathlib import Path

# `google.genai` is imported lazily inside the Vertex backend, so users who
# only need --backend anthropic / openai / gemini-api / subagent do not need
# to install google-genai.

# Local sibling modules. The mid-file aliasing ("as _foo") keeps a few
# backwards-compat shims (defined later) wrapping the canonical implementations.
from _hashing import (
    stable_combined_hash as get_combined_hash,
    legacy_combined_hash as get_legacy_combined_hash,
    combined_hash_variants as get_combined_hash_variants,
)
from zotero_client import (
    get_parent_key as _get_parent_key,
    get_zotero_abstract_note,
)
from note_render import (
    build_multifacet_frontmatter,
    resolve_multifacet_generated_name,
    render_multifacet_note as _render_multifacet_note,
    build_multifacet_validation_report,
)
from dedup_index import DedupIndex

from config import (
    ZOTERO_DB_PATH as _ZOTERO_DB_PATH_OBJ,
    CANONICAL_SKILL_ROOT as _CANONICAL_SKILL_ROOT_OBJ,
    DOMAIN_PACK_ROOT as _DOMAIN_PACK_ROOT_OBJ,
    DOMAIN_PACK_NAME,
    UNIVERSAL_RULES_PATH as _UNIVERSAL_RULES_PATH_OBJ,
    GEMINI_VERTEX_GCS_BUCKET_LOCATION as DEFAULT_BUCKET_LOCATION,
    PIPELINE_REPORT_ROOT as DEFAULT_PIPELINE_REPORT_ROOT,
    MODEL_ROUTING_POLICY_PATH as DEFAULT_MODEL_ROUTING_POLICY_PATH,
    PROCESSOR_BACKEND,
    PROCESSED_HISTORY_PATH,
    VAULT_ROOT as DEFAULT_VAULT_ROOT,
    LOCALRAG_MAIN_PYTHON as _APPROVED_MAIN_PYTHON_STR,
    LOCALRAG_RAG_PYTHON as _APPROVED_RAG_PYTHON_STR,
    BUILD_NOTES_DB_PATH,
    BUILD_PDF_DB_PATH,
    QUERY_SERVER_PATH,
    RUN_TAGGING_PIPELINE_PATH,
    EXPORT_REVIEW_QUEUE_PATH,
    PREFILL_CANDIDATE_TAGS_PATH,
    GCS_UPLOAD_TIMEOUT_SECONDS,
    VERTEX_PDF_MAX_SIZE_BYTES,
    VERTEX_PDF_MAX_PAGES,
)

# Keep these names as strings/Path objects matching the original API
ZOTERO_DB_PATH = str(_ZOTERO_DB_PATH_OBJ)
CANONICAL_SKILL_ROOT = str(_CANONICAL_SKILL_ROOT_OBJ)
DOMAIN_PACK_ROOT = str(_DOMAIN_PACK_ROOT_OBJ)
UNIVERSAL_RULES_PATH = str(_UNIVERSAL_RULES_PATH_OBJ)
APPROVED_MAIN_PYTHON = Path(_APPROVED_MAIN_PYTHON_STR)
APPROVED_RAG_PYTHON = Path(_APPROVED_RAG_PYTHON_STR)
LOCALRAG_ROOT = BUILD_NOTES_DB_PATH.parent  # for any code referencing the old name

DEFAULT_POST_PUBLISH_ACTIONS = [
    "prefill",
    "review_queue",
]
ALLOWED_POST_PUBLISH_ACTIONS = [
    "prefill",
    "kimi_fallback",
    "review_queue",
    "notes_db",
    "pdf_db",
    "restart_query",
]
POST_PUBLISH_ACTION_ALIASES = {
    "tagging": "kimi_fallback",
}
LIVE_VAULT_EXCLUDED_RELATIVE_PREFIXES = (
    "progress/gate_backups/",
    "progress/gate_reports/",
    "progress/version_snapshots/",
    "progress/schema_migration/",
    "progress/taxonomy_discovery/",
    "progress/pipeline_logs/",
    "progress/pipeline_reports/",
)
LIVE_VAULT_EXCLUDED_PATH_PARTS = {".claude", ".obsidian", "__pycache__", ".stfolder"}
FRONTMATTER_BLOCK_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)


class PDFPreflightError(RuntimeError):
    def __init__(self, error_code, message, pdf_path=None, details=None):
        super().__init__(message)
        self.error_code = error_code
        self.pdf_path = pdf_path
        self.details = details or {}


def load_system_prompt(name):
    """Load a system prompt from the active domain pack.

    Stage A and Stage B prompts (`document_profiler.system.txt`,
    `note_generator.system.txt`) are augmented with their per-pack
    guidance files via `load_augmented_system_prompt` — call that helper
    instead of this raw loader for those two stages.
    """
    path = Path(DOMAIN_PACK_ROOT) / "prompts" / name
    return path.read_text(encoding="utf-8")


def load_augmented_system_prompt(name):
    """Load a system prompt and append its domain-specific guidance.

    For ``document_profiler.system.txt`` we append the pack's
    ``routing_disambiguation_hints.txt``. For ``note_generator.system.txt``
    we append ``seed_terms_guidance.txt``. Both guidance files are
    optional — packs can ship empty stubs and the prompt is unchanged.
    """
    base = load_system_prompt(name)
    guidance_filename = {
        "document_profiler.system.txt": "routing_disambiguation_hints.txt",
        "note_generator.system.txt": "seed_terms_guidance.txt",
    }.get(name)
    if not guidance_filename:
        return base
    guidance_path = Path(DOMAIN_PACK_ROOT) / "prompts" / guidance_filename
    if not guidance_path.exists():
        return base
    guidance_text = guidance_path.read_text(encoding="utf-8").strip()
    if not guidance_text:
        return base
    section_heading = {
        "routing_disambiguation_hints.txt": "## Domain-specific routing disambiguation",
        "seed_terms_guidance.txt": "## Domain-specific seed_terms guidance",
    }[guidance_filename]
    return f"{base.rstrip()}\n\n{section_heading}\n\n{guidance_text}\n"


def load_vertex_schema(name):
    path = Path(DOMAIN_PACK_ROOT) / "schemas" / name
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_template_rules(template_id):
    path = Path(DOMAIN_PACK_ROOT) / "templates" / template_id
    return path.read_text(encoding="utf-8")


def load_universal_rules():
    """Repo-root universal rules (field-invariant: evidence, anti-hallucination,
    scoring discipline). Loaded once per pipeline run."""
    path = Path(UNIVERSAL_RULES_PATH)
    return path.read_text(encoding="utf-8")


def load_domain_quality_rules():
    """Pack-specific quality rules (filename slot semantics, trap scan checklist,
    domain-specific scoring axis name). Loaded once per pipeline run."""
    path = Path(DOMAIN_PACK_ROOT) / "templates" / "_domain_quality_rules.txt"
    return path.read_text(encoding="utf-8")


def load_shared_template_rules():
    """Backwards-compatible: returns universal + domain-quality concatenated.

    Prior to the domain-pack split (commit refactoring _shared_rules.txt into
    universal + per-pack), templates loaded a single combined "shared rules"
    file. This shim keeps the old function name working; new callers should
    invoke ``load_universal_rules`` and ``load_domain_quality_rules`` separately
    and decide their own composition.
    """
    universal = load_universal_rules().rstrip()
    domain = load_domain_quality_rules().rstrip()
    return f"{universal}\n\n{domain}\n"


def load_note_generator_system_prompt(note_template_id):
    """Load Stage B instructions without leaking pack guidance into generic notes."""
    if note_template_id == "generic-research-note":
        return load_system_prompt("note_generator.system.txt")
    return load_augmented_system_prompt("note_generator.system.txt")


def compose_note_generator_rules(note_template_id):
    """Compose template rules with the correct field-specificity boundary."""
    template = load_template_rules(f"{note_template_id}.txt").rstrip()
    universal = load_universal_rules().rstrip()
    parts = [template, universal]
    if note_template_id != "generic-research-note":
        parts.append(load_domain_quality_rules().rstrip())
    return "\n\n".join(part for part in parts if part)


def load_model_routing_policy(path=None):
    policy_path = Path(path) if path else DEFAULT_MODEL_ROUTING_POLICY_PATH
    with policy_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def collect_pdf_preflight(pdf_paths):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError(
            "pypdf is required for PDF page-count preflight in multifacet-spec mode."
        ) from exc

    page_counts = []
    size_bytes = []
    pdf_inputs = []
    for pdf_path in pdf_paths:
        abs_path = os.path.abspath(pdf_path)
        try:
            file_size_bytes = os.path.getsize(pdf_path)
        except OSError as exc:
            raise PDFPreflightError(
                "missing_pdf",
                f"missing_pdf: {pdf_path}: {exc}",
                pdf_path=abs_path,
            ) from exc

        try:
            reader = PdfReader(pdf_path)
            page_count = len(reader.pages)
        except Exception as exc:
            raise PDFPreflightError(
                "corrupt_pdf",
                f"corrupt_pdf: {pdf_path}: {exc}",
                pdf_path=abs_path,
            ) from exc

        page_counts.append(page_count)
        size_bytes.append(file_size_bytes)
        pdf_inputs.append(
            {
                "path": abs_path,
                "page_count": page_count,
                "file_size_bytes": file_size_bytes,
                "requires_split": (
                    page_count > VERTEX_PDF_MAX_PAGES
                    or file_size_bytes > VERTEX_PDF_MAX_SIZE_BYTES
                ),
            }
        )

    return {
        "primary_pdf_pages": page_counts[0] if page_counts else 0,
        "total_pdf_pages": sum(page_counts),
        "pdf_page_counts": page_counts,
        "primary_pdf_size_bytes": size_bytes[0] if size_bytes else 0,
        "total_pdf_size_bytes": sum(size_bytes),
        "pdf_file_sizes_bytes": size_bytes,
        "pdf_inputs": pdf_inputs,
    }


def split_pdf_for_vertex(
    pdf_path,
    output_dir,
    source_index,
    max_pdf_size_bytes=VERTEX_PDF_MAX_SIZE_BYTES,
    max_pdf_pages=VERTEX_PDF_MAX_PAGES,
):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise RuntimeError(
            "pypdf is required for PDF chunking in multifacet-spec mode."
        ) from exc

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        reader = PdfReader(pdf_path)
        total_pages = len(reader.pages)
    except Exception as exc:
        raise PDFPreflightError(
            "corrupt_pdf",
            f"corrupt_pdf: {pdf_path}: {exc}",
            pdf_path=os.path.abspath(pdf_path),
        ) from exc

    if total_pages <= 0:
        raise PDFPreflightError(
            "corrupt_pdf",
            f"corrupt_pdf: {pdf_path}: zero readable pages",
            pdf_path=os.path.abspath(pdf_path),
        )

    source_size_bytes = os.path.getsize(pdf_path)
    initial_chunk_count = max(
        1,
        math.ceil(source_size_bytes / max_pdf_size_bytes),
        math.ceil(total_pages / max_pdf_pages),
    )
    initial_page_span = max(1, min(max_pdf_pages, math.ceil(total_pages / initial_chunk_count)))
    safe_stem = re.sub(r"[^0-9A-Za-z._-]+", "_", Path(pdf_path).stem).strip("._") or "pdf"
    # Keep generated chunk paths below Windows MAX_PATH on machines without long-path support.
    max_stem_len = max(24, 180 - len(str(output_dir)))
    safe_stem = safe_stem[:max_stem_len].rstrip("._-") or "pdf"

    prepared_paths = []
    prepared_manifest = []
    start_page = 0
    part_index = 0

    while start_page < total_pages:
        remaining_pages = total_pages - start_page
        candidate_span = min(initial_page_span, max_pdf_pages, remaining_pages)
        accepted_path = None
        accepted_span = None
        accepted_size_bytes = None

        while candidate_span >= 1:
            part_index += 1
            end_page = start_page + candidate_span
            chunk_path = output_dir / (
                f"{source_index:02d}_{safe_stem}_part_{part_index:02d}"
                f"_p{start_page + 1}-{end_page}.pdf"
            )

            writer = PdfWriter()
            for page_index in range(start_page, end_page):
                writer.add_page(reader.pages[page_index])
            with chunk_path.open("wb") as f:
                writer.write(f)

            chunk_size_bytes = os.path.getsize(chunk_path)
            if chunk_size_bytes <= max_pdf_size_bytes:
                accepted_path = chunk_path
                accepted_span = candidate_span
                accepted_size_bytes = chunk_size_bytes
                break

            chunk_path.unlink(missing_ok=True)
            part_index -= 1
            if candidate_span == 1:
                raise PDFPreflightError(
                    "oversize_pdf",
                    (
                        f"oversize_pdf: single-page chunk still exceeds {max_pdf_size_bytes} bytes "
                        f"for {pdf_path}"
                    ),
                    pdf_path=os.path.abspath(pdf_path),
                    details={
                        "file_size_bytes": source_size_bytes,
                        "page_count": total_pages,
                        "max_pdf_size_bytes": max_pdf_size_bytes,
                    },
                )
            candidate_span = max(1, candidate_span // 2)

        if accepted_path is None or accepted_span is None or accepted_size_bytes is None:
            raise PDFPreflightError(
                "oversize_pdf",
                f"oversize_pdf: failed to prepare compliant chunk for {pdf_path}",
                pdf_path=os.path.abspath(pdf_path),
            )

        prepared_paths.append(str(accepted_path))
        prepared_manifest.append(
            {
                "source_index": source_index,
                "source_pdf_path": os.path.abspath(pdf_path),
                "source_page_count": total_pages,
                "source_size_bytes": source_size_bytes,
                "prepared_pdf_path": str(accepted_path.resolve()),
                "prepared_page_count": accepted_span,
                "prepared_size_bytes": accepted_size_bytes,
                "part_index": len(prepared_manifest),
                "page_start": start_page + 1,
                "page_end": start_page + accepted_span,
                "transformation": "split",
            }
        )
        start_page += accepted_span

    return prepared_paths, prepared_manifest


def prepare_pdf_inputs_for_vertex(
    pdf_paths,
    work_dir,
    max_pdf_size_bytes=VERTEX_PDF_MAX_SIZE_BYTES,
    max_pdf_pages=VERTEX_PDF_MAX_PAGES,
    preflight=None,
    profiler_first_n_pages: int | None = 3,
):
    """Prepare full-set + profiler-set PDF inputs.

    Returns a dict with:
      prepared_pdf_paths   — full set used by Stage B (with chunking if needed).
      prepared_pdf_manifest — per-input metadata for the full set.
      chunking_enabled     — true if any source PDF was split.
      profiler_pdf_paths   — single-element list with the primary PDF
                              truncated to first-N pages, or the primary
                              source path itself when it's already <= N
                              pages. None when profiler_first_n_pages is
                              None (disables the optimization).
      profiler_manifest    — descriptor of the profiler slice for the
                              08-profiler-input.json audit artifact.

    `profiler_first_n_pages=None` reverts to the pre-optimization behavior
    (Stage A sees the full PDF set). Default 3 pages.
    """
    preflight = preflight or collect_pdf_preflight(pdf_paths)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    prepared_pdf_paths = []
    prepared_pdf_manifest = []
    chunking_enabled = False

    pdf_inputs = preflight.get("pdf_inputs") or []
    for source_index, item in enumerate(pdf_inputs):
        source_path = item["path"]
        page_count = int(item["page_count"])
        file_size_bytes = int(item["file_size_bytes"])
        needs_split = page_count > max_pdf_pages or file_size_bytes > max_pdf_size_bytes

        if not needs_split:
            prepared_pdf_paths.append(source_path)
            prepared_pdf_manifest.append(
                {
                    "source_index": source_index,
                    "source_pdf_path": source_path,
                    "source_page_count": page_count,
                    "source_size_bytes": file_size_bytes,
                    "prepared_pdf_path": source_path,
                    "prepared_page_count": page_count,
                    "prepared_size_bytes": file_size_bytes,
                    "part_index": 0,
                    "page_start": 1 if page_count else 0,
                    "page_end": page_count,
                    "transformation": "passthrough",
                }
            )
            continue

        chunking_enabled = True
        chunk_dir = work_dir / f"{source_index:02d}"
        chunk_paths, chunk_manifest = split_pdf_for_vertex(
            pdf_path=source_path,
            output_dir=chunk_dir,
            source_index=source_index,
            max_pdf_size_bytes=max_pdf_size_bytes,
            max_pdf_pages=max_pdf_pages,
        )
        prepared_pdf_paths.extend(chunk_paths)
        prepared_pdf_manifest.extend(chunk_manifest)

    profiler_pdf_paths = None
    profiler_manifest = None
    if profiler_first_n_pages and pdf_inputs:
        primary_input = pdf_inputs[0]
        primary_path = primary_input["path"]
        primary_pages = int(primary_input["page_count"])
        safe_name = "".join(
            c if c.isascii() and c not in r'\/:*?"<>|' else "_"
            for c in os.path.basename(primary_path)
        )
        profiler_dir = work_dir / "profiler"
        profiler_target = profiler_dir / f"00_{safe_name[:80]}_p1-{profiler_first_n_pages}.pdf"
        try:
            from pdf_slicer import slice_first_n_pages
            sliced = slice_first_n_pages(primary_path, profiler_target, profiler_first_n_pages)
            profiler_pdf_paths = [str(sliced)]
            transformation = (
                "passthrough"
                if Path(sliced).resolve() == Path(primary_path).resolve()
                else "sliced"
            )
            profiler_manifest = {
                "primary_pdf_path": os.path.abspath(primary_path),
                "primary_total_pages": primary_pages,
                "profiler_pdf_path": os.path.abspath(str(sliced)),
                "profiler_pages_sent": min(primary_pages, profiler_first_n_pages),
                "profiler_first_n_pages": profiler_first_n_pages,
                "transformation": transformation,
                "siblings_excluded": [
                    os.path.abspath(item["path"])
                    for item in pdf_inputs[1:]
                ],
            }
        except Exception as exc:
            # Fall back to full-PDF Stage A if slicing fails. The profiler
            # routing decision will still work via page-count escalators
            # (resolve_note_generator_model:402-406 fires before the
            # profile-flag checks). Capture the failure in the manifest
            # so an operator can see why no truncation happened.
            print(
                f"  [WARN] Stage A profiler slice failed for "
                f"{os.path.basename(primary_path)}: {exc}; Stage A will see full PDF.",
                file=sys.stderr,
            )
            profiler_pdf_paths = None
            profiler_manifest = {
                "primary_pdf_path": os.path.abspath(primary_path),
                "primary_total_pages": primary_pages,
                "transformation": "failed",
                "error": str(exc),
            }

    return {
        "prepared_pdf_paths": prepared_pdf_paths,
        "prepared_pdf_manifest": prepared_pdf_manifest,
        "chunking_enabled": chunking_enabled,
        "profiler_pdf_paths": profiler_pdf_paths,
        "profiler_manifest": profiler_manifest,
    }


def resolve_profiler_model(policy, preflight, cli_override=None, flash_model=None):
    if cli_override:
        return cli_override, "manual override (--model)"

    model_name = flash_model or policy.get("default_profiler_model", "gemini-2.5-flash")
    return model_name, "always flash for classification"


def resolve_note_generator_model(
    policy,
    preflight,
    document_profile=None,
    cli_override=None,
    flash_model=None,
    pro_model=None,
):
    if cli_override:
        return cli_override, "manual override (--model)"

    default_model = flash_model or policy.get("default_note_generator_model", "gemini-2.5-flash")
    pro_note_model = pro_model or policy.get("pro_note_generator_model", "gemini-2.5-pro")
    primary_threshold = int(policy.get("page_count_threshold_pro", 30))
    total_threshold = int(policy.get("total_page_count_threshold_pro", 10**9))

    if preflight.get("primary_pdf_pages", 0) >= primary_threshold:
        return pro_note_model, f"primary_pdf_pages >= {primary_threshold}"

    if preflight.get("total_pdf_pages", 0) >= total_threshold:
        return pro_note_model, f"total_pdf_pages >= {total_threshold}"

    if document_profile:
        document_type = document_profile.get("document_type")
        if document_type in set(policy.get("pro_document_types", [])):
            return pro_note_model, f"document_type={document_type}"

        if policy.get("review_like_upgrade", True) and document_profile.get("is_review_like"):
            return pro_note_model, "is_review_like=true"

        if policy.get("multichapter_upgrade", True) and document_profile.get("is_multichapter_thesis"):
            return pro_note_model, "is_multichapter_thesis=true"

    return default_model, "default flash route"


def build_model_plan(
    pdf_paths,
    preflight,
    policy,
    profiler_model,
    profiler_reason,
    note_generator_model,
    note_generator_reason,
    manual_override=None,
    flash_model_override=None,
    pro_model_override=None,
    prepared_pdf_paths=None,
    prepared_pdf_manifest=None,
):
    prepared_pdf_paths = prepared_pdf_paths or pdf_paths
    prepared_pdf_manifest = prepared_pdf_manifest or []
    chunking_used = any(
        item.get("transformation") != "passthrough"
        for item in prepared_pdf_manifest
    )
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pdf_paths": [os.path.abspath(path) for path in pdf_paths],
        "prepared_pdf_paths": [os.path.abspath(path) for path in prepared_pdf_paths],
        "primary_pdf_pages": preflight.get("primary_pdf_pages", 0),
        "total_pdf_pages": preflight.get("total_pdf_pages", 0),
        "pdf_page_counts": preflight.get("pdf_page_counts", []),
        "primary_pdf_size_bytes": preflight.get("primary_pdf_size_bytes", 0),
        "total_pdf_size_bytes": preflight.get("total_pdf_size_bytes", 0),
        "pdf_file_sizes_bytes": preflight.get("pdf_file_sizes_bytes", []),
        "profiler_model": profiler_model,
        "profiler_reason": profiler_reason,
        "note_generator_model": note_generator_model,
        "note_generator_reason": note_generator_reason,
        "manual_model_override": manual_override,
        "flash_model_override": flash_model_override,
        "pro_model_override": pro_model_override,
        "chunking_enabled": chunking_used,
        "chunking_note": policy.get("chunking_note", ""),
        "prepared_pdf_manifest": prepared_pdf_manifest,
        "policy_version": policy.get("policy_version", "unknown"),
    }


def default_run_dir(combined_hash):
    return DEFAULT_PIPELINE_REPORT_ROOT / "runs" / combined_hash


def write_run_artifact(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Analyze PDFs using Gemini on Vertex AI")
    parser.add_argument("pdf_paths", nargs='+', help="Paths to the PDF files (Main article and SI)")
    parser.add_argument("--out-dir", help="Output directory path (optional)")
    parser.add_argument("-f", "--force", action="store_true", help="Force re-processing even if already processed")
    parser.add_argument(
        "--resume",
        help=(
            "Path to a prior run directory. Only meaningful with --backend subagent. "
            "Re-uses Stage A / Stage B JSON outputs already written by a sub-agent "
            "instead of writing a fresh manifest. See SKILL.md for the 3-invocation flow."
        ),
    )
    parser.add_argument(
        "--note-template",
        choices=["generic-research-note"],
        help=(
            "Force Stage B to use the field-neutral repository template instead "
            "of the profiler recommendation. Intended for controlled benchmarks."
        ),
    )
    parser.add_argument(
        "--source-artifact",
        action="append",
        default=[],
        help=(
            "Path to a native-coordinate non-PDF source packet. Repeat for "
            "multiple packets. Supported only by --backend subagent."
        ),
    )
    parser.add_argument("--gcs-bucket", help="GCS bucket used for temporary Vertex AI PDF uploads (vertex backend only).")
    parser.add_argument(
        "--backend",
        default=PROCESSOR_BACKEND,
        choices=["vertex", "gemini-api", "anthropic", "openai", "subagent"],
        help=(
            "Which processor backend to use. 'vertex' uses Vertex AI with GCS upload. "
            "'gemini-api' uses a direct Google AI Studio API key with inline PDFs. "
            "'anthropic' uses Anthropic Claude with base64 PDFs. "
            "'openai' uses the OpenAI Chat Completions protocol with locally-extracted PDF text "
            "(works with OpenAI, DeepSeek, Mistral, OpenRouter, vLLM, etc. via $OPENAI_BASE_URL). "
            "'subagent' writes a manifest for a Claude Code sub-agent to process. "
            "Override with $LOCALRAG_PROCESSOR_BACKEND."
        ),
    )
    parser.add_argument("--model", help="Manual override model for all stages. When omitted, the orchestrator auto-routes between flash and pro tiers.")
    parser.add_argument(
        "--model-router",
        default="auto",
        choices=["auto", "off"],
        help="Model routing mode for multifacet-spec. 'off' disables auto-routing and uses --model or Flash fallback.",
    )
    parser.add_argument("--routing-policy", help="Path to the model routing policy JSON.")
    parser.add_argument("--flash-model", help="Override the Flash model name used by auto-routing.")
    parser.add_argument("--pro-model", help="Override the Pro model name used by auto-routing.")
    parser.add_argument(
        "--publish-target",
        default="vault",
        choices=["canary", "vault"],
        help="Where multifacet-spec writes the final note. 'vault' publishes directly to $LOCALRAG_NOTES_DIR.",
    )
    parser.add_argument(
        "--post-publish",
        default="auto",
        help=(
            "Comma-separated post-publish actions for multifacet-spec: "
            "prefill, kimi-fallback, review-queue, notes-db, pdf-db, restart-query, none, or auto."
        ),
    )
    parser.add_argument(
        "--note-index-file",
        help="Optional JSON snapshot mapping {combined_hash,parent_key} to existing live note paths.",
    )
    return parser


def build_document_profiler_user_prompt():
    return "\n".join(
        [
            "Profile the attached PDF set for note routing.",
            "",
            "Decide:",
            "- the primary research domain",
            "- the document type",
            "- the finer article granularity",
            "- the best note template",
            "- confidence and routing evidence",
            "",
            "Attached files may include a main paper and SI, or a dissertation-style document.",
            "Base the decision on the attached documents only.",
        ]
    )


def _validate_against_schema(value, schema, field_name="root"):
    schema_type = schema.get("type")
    if value is None and schema.get("nullable"):
        return

    if schema_type == "OBJECT":
        if not isinstance(value, dict):
            raise ValueError(f"Field {field_name} must be a JSON object.")
        for required_field in schema.get("required", []):
            if required_field not in value:
                raise ValueError(f"Missing required field: {required_field}")
        for child_name, child_schema in schema.get("properties", {}).items():
            if child_name in value:
                _validate_against_schema(
                    value[child_name],
                    child_schema,
                    field_name=f"{field_name}.{child_name}",
                )
        return

    if schema_type == "ARRAY":
        if not isinstance(value, list):
            raise ValueError(f"Field {field_name} must be a list.")
        item_schema = schema.get("items")
        if item_schema:
            for idx, item in enumerate(value):
                _validate_against_schema(item, item_schema, field_name=f"{field_name}[{idx}]")
        return

    if schema_type == "BOOLEAN":
        if not isinstance(value, bool):
            raise ValueError(f"Field {field_name} must be a boolean.")
        return

    if schema_type == "INTEGER":
        if not isinstance(value, int):
            raise ValueError(f"Field {field_name} must be an integer.")
        return

    if schema_type == "STRING":
        if not isinstance(value, str):
            raise ValueError(f"Field {field_name} must be a string.")
        if "enum" in schema and value not in schema["enum"]:
            raise ValueError(f"Field {field_name} must be one of {schema['enum']}.")
        return

    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"Field {field_name} must be one of {schema['enum']}.")


def _normalize_string_list(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    return []


def _canonicalize_hint_text(text):
    return "".join(ch for ch in str(text).casefold() if ch.isalnum())


def _seed_term_is_grounded(term, anchors):
    term_norm = _canonicalize_hint_text(term)
    if not term_norm:
        return False
    for anchor in anchors:
        anchor_norm = _canonicalize_hint_text(anchor)
        if not anchor_norm:
            continue
        if term_norm == anchor_norm:
            return True
        if len(term_norm) >= 6 and term_norm in anchor_norm:
            return True
        if len(anchor_norm) >= 6 and anchor_norm in term_norm:
            return True
    return False


def _sanitize_seed_terms(frontmatter):
    seed_terms = _normalize_string_list(frontmatter.get("seed_terms"))
    anchor_fields = [
        frontmatter.get("title_en"),
        frontmatter.get("title_zh"),
        *_normalize_string_list(frontmatter.get("keywords")),
        *_normalize_string_list(frontmatter.get("topic")),
    ]
    anchors = [str(item).strip() for item in anchor_fields if str(item).strip()]
    if not seed_terms:
        return frontmatter

    grounded = []
    seen = set()
    for term in seed_terms:
        if _seed_term_is_grounded(term, anchors) and term not in seen:
            grounded.append(term)
            seen.add(term)

    if not grounded:
        fallback = []
        for candidate in _normalize_string_list(frontmatter.get("keywords")) + _normalize_string_list(frontmatter.get("topic")):
            if candidate not in seen:
                fallback.append(candidate)
                seen.add(candidate)
            if len(fallback) >= 10:
                break
        grounded = fallback

    frontmatter["seed_terms"] = grounded
    return frontmatter


def validate_document_profile(payload):
    schema = load_vertex_schema("document_profile.vertex.schema.json")
    _validate_against_schema(payload, schema)
    return payload


def _requeue_invalid_subagent_output(backend, *, stage, system_prompt, user_prompt, schema, model_id):
    """Recover from a sub-agent output that is valid JSON but schema-invalid.

    Without this, `--resume` reloads the same bad payload forever: the run
    crashes with a ValueError on every pass while list_pending_subagent_runs
    reports zero pending work (the file parses as JSON, so it looks filled).
    For backends that support quarantining (the subagent backend in resume
    mode), move the bad file aside and re-emit the stage manifest — the
    re-emission raises SubagentManifestPending, which the caller lets
    propagate so the run exits 200 and the parent agent re-dispatches.
    For all other backends this is a no-op and the ValueError propagates.
    """
    quarantine = getattr(backend, "quarantine_invalid_output", None)
    if not callable(quarantine):
        return
    quarantined = quarantine(stage=stage)
    if quarantined is None:
        return
    print(
        f"[WARN] stage={stage}: sub-agent output failed schema validation; "
        f"quarantined to {quarantined} and re-emitting the manifest.",
        file=sys.stderr,
    )
    backend.call_model(
        stage=stage,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema=schema,
        model_id=model_id,
        temperature=0.0,
    )


def run_document_profiler(backend, model):
    """Stage A: classify the paper and pick a note template.

    `backend` must already have had `attach_pdfs()` called for the current paper.
    Backend-agnostic: works for Vertex, direct Gemini API, Anthropic, or
    sub-agent manifest backends.
    """
    schema = load_vertex_schema("document_profile.vertex.schema.json")
    system_prompt = load_augmented_system_prompt("document_profiler.system.txt")
    user_prompt = build_document_profiler_user_prompt()
    payload = backend.call_model(
        stage="profiler",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema=schema,
        model_id=model,
        temperature=0.0,
    )
    try:
        return validate_document_profile(payload)
    except ValueError:
        _requeue_invalid_subagent_output(
            backend,
            stage="profiler",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
            model_id=model,
        )
        raise


def build_note_generator_user_prompt(document_profile, note_template_id, template_rules_text):
    rule_scope = (
        "field-neutral: active domain-pack guidance and quality rules are excluded"
        if note_template_id == "generic-research-note"
        else "active-domain: active domain-pack guidance and quality rules are included"
    )
    return "\n".join(
        [
            "Generate a structured literature note for the attached PDF set.",
            "",
            "Use this routing profile:",
            json.dumps(document_profile, ensure_ascii=False, indent=2),
            "",
            "Use this note template:",
            note_template_id,
            "",
            "Use this rule scope:",
            rule_scope,
            "",
            "Use these template rules:",
            template_rules_text,
            "",
            "Return:",
            "- structured frontmatter fields",
            "- markdown body only",
            "- section diagnostics",
            "- adapter-only signals for downstream review",
            "",
            "Follow the response schema exactly.",
        ]
    )


def validate_note_draft(payload):
    schema = load_vertex_schema("structured_note.vertex.schema.json")
    _validate_against_schema(payload, schema)
    frontmatter = payload.get("frontmatter")
    if isinstance(frontmatter, dict):
        payload["frontmatter"] = _sanitize_seed_terms(dict(frontmatter))
    return payload


def run_note_generator(
    backend,
    model,
    document_profile,
    note_template_override=None,
):
    """Stage B: generate the structured note JSON.

    `backend` must already have had `attach_pdfs()` called for the current paper.
    """
    schema = load_vertex_schema("structured_note.vertex.schema.json")
    note_template_id = (
        note_template_override or document_profile["recommended_template"]
    )
    system_prompt = load_note_generator_system_prompt(note_template_id)
    combined_rules_text = compose_note_generator_rules(note_template_id)
    user_prompt = build_note_generator_user_prompt(
        document_profile=document_profile,
        note_template_id=note_template_id,
        template_rules_text=combined_rules_text,
    )
    payload = backend.call_model(
        stage="note_generator",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema=schema,
        model_id=model,
        temperature=0.0,
    )
    try:
        return validate_note_draft(payload)
    except ValueError:
        _requeue_invalid_subagent_output(
            backend,
            stage="note_generator",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
            model_id=model,
        )
        raise


def _read_note_frontmatter_mapping(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}

    match = FRONTMATTER_BLOCK_RE.match(text)
    if not match:
        return {}

    try:
        payload = yaml.safe_load(match.group(1)) or {}
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def normalize_note_index_payload(payload):
    normalized = {
        "combined_hash": {},
        "zotero_parent_key": {},
    }
    if not isinstance(payload, dict):
        return normalized

    for key in ("combined_hash", "zotero_parent_key"):
        bucket = payload.get(key) or {}
        if not isinstance(bucket, dict):
            continue
        for raw_value, raw_paths in bucket.items():
            value = str(raw_value).strip()
            if not value:
                continue
            if isinstance(raw_paths, str):
                paths = [raw_paths]
            elif isinstance(raw_paths, list):
                paths = raw_paths
            else:
                continue
            deduped = []
            seen = set()
            for item in paths:
                try:
                    resolved = str(Path(item).resolve())
                except OSError:
                    continue
                key_name = resolved.casefold()
                if key_name in seen:
                    continue
                seen.add(key_name)
                deduped.append(resolved)
            if deduped:
                normalized[key][value] = deduped
    return normalized


def load_note_index(path):
    if not path:
        return None
    index_path = Path(path)
    if not index_path.exists():
        return None
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return normalize_note_index_payload(payload)


def find_note_index_matches(note_index, combined_hash=None, legacy_combined_hash=None, zotero_parent_key=None):
    note_index = normalize_note_index_payload(note_index)
    accepted_hashes = [value for value in (combined_hash, legacy_combined_hash) if value]
    candidates = []
    for hash_value in accepted_hashes:
        candidates.extend(note_index.get("combined_hash", {}).get(hash_value, []))
    if zotero_parent_key:
        candidates.extend(note_index.get("zotero_parent_key", {}).get(zotero_parent_key, []))

    matches = []
    seen = set()
    for candidate in candidates:
        try:
            resolved = Path(candidate).resolve()
        except OSError:
            continue
        if not resolved.exists():
            continue
        key_name = str(resolved).casefold()
        if key_name in seen:
            continue
        seen.add(key_name)
        matches.append(resolved)
    return matches


def _iter_live_vault_note_paths(root):
    root = Path(root).resolve()
    for path in root.rglob("*_review_note.md"):
        try:
            relative = path.resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        if any(relative.startswith(prefix) for prefix in LIVE_VAULT_EXCLUDED_RELATIVE_PREFIXES):
            continue
        if any(part in LIVE_VAULT_EXCLUDED_PATH_PARTS or part.startswith(".tmp") for part in Path(relative).parts):
            continue
        yield path.resolve()


def _canonical_publish_match_key(path, generated_name):
    name = Path(path).name
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        mtime = 0
    return (
        0 if name == generated_name else 1,
        0 if name.isascii() else 1,
        -mtime,
        len(name),
        name.casefold(),
    )


def resolve_multifacet_publish_path(
    rendered_note,
    generated_name,
    output_root,
    combined_hash=None,
    legacy_combined_hash=None,
    zotero_parent_key=None,
    note_index=None,
):
    output_root = Path(output_root).resolve()
    generated_name = str(generated_name)
    if output_root != DEFAULT_VAULT_ROOT.resolve():
        return output_root / generated_name

    existing_matches = find_existing_multifacet_note_matches(
        combined_hash=combined_hash,
        legacy_combined_hash=legacy_combined_hash,
        zotero_parent_key=zotero_parent_key,
        output_root=output_root,
        note_index=note_index,
    )
    if not existing_matches:
        return output_root / generated_name

    return min(existing_matches, key=lambda path: _canonical_publish_match_key(path, generated_name))


def find_existing_multifacet_note_matches(
    combined_hash=None,
    legacy_combined_hash=None,
    zotero_parent_key=None,
    output_root=DEFAULT_VAULT_ROOT,
    note_index=None,
):
    indexed_matches = find_note_index_matches(
        note_index,
        combined_hash=combined_hash,
        legacy_combined_hash=legacy_combined_hash,
        zotero_parent_key=zotero_parent_key,
    )
    if indexed_matches:
        return indexed_matches
    if note_index is not None:
        return []

    output_root = Path(output_root).resolve()
    accepted_hashes = {value for value in (combined_hash, legacy_combined_hash) if value}
    existing_matches = []
    for path in _iter_live_vault_note_paths(output_root):
        payload = _read_note_frontmatter_mapping(path)
        if not payload:
            continue
        same_hash = bool(accepted_hashes) and payload.get("combined_hash") in accepted_hashes
        same_parent = bool(zotero_parent_key) and payload.get("zotero_parent_key") == zotero_parent_key
        if same_hash or same_parent:
            existing_matches.append(path)
    return existing_matches


def find_existing_multifacet_note_path(
    combined_hash=None,
    legacy_combined_hash=None,
    zotero_parent_key=None,
    output_root=DEFAULT_VAULT_ROOT,
    note_index=None,
):
    matches = find_existing_multifacet_note_matches(
        combined_hash=combined_hash,
        legacy_combined_hash=legacy_combined_hash,
        zotero_parent_key=zotero_parent_key,
        output_root=output_root,
        note_index=note_index,
    )
    if not matches:
        return None
    return min(matches, key=lambda path: _canonical_publish_match_key(path, ""))


def write_multifacet_output_note(
    rendered_note,
    generated_name,
    output_root,
    combined_hash=None,
    legacy_combined_hash=None,
    zotero_parent_key=None,
    note_index=None,
):
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    out_path = resolve_multifacet_publish_path(
        rendered_note=rendered_note,
        generated_name=generated_name,
        output_root=output_root,
        combined_hash=combined_hash,
        legacy_combined_hash=legacy_combined_hash,
        zotero_parent_key=zotero_parent_key,
        note_index=note_index,
    )
    out_path.write_text(rendered_note, encoding="utf-8")
    return out_path


def parse_post_publish_actions(raw_value, publish_target):
    text = str(raw_value or "auto").strip().lower()
    if text in {"", "auto"}:
        return list(DEFAULT_POST_PUBLISH_ACTIONS) if publish_target == "vault" else []
    if text == "none":
        return []

    normalized = []
    seen = set()
    allowed = set(ALLOWED_POST_PUBLISH_ACTIONS)
    for chunk in re.split(r"[,\s]+", text):
        token = chunk.strip().lower().replace("-", "_")
        if not token:
            continue
        if token == "none":
            return []
        token = POST_PUBLISH_ACTION_ALIASES.get(token, token)
        if token not in allowed:
            raise ValueError(
                f"Unknown post-publish action: {chunk}. Allowed actions: "
                + ", ".join(ALLOWED_POST_PUBLISH_ACTIONS)
            )
        if token not in seen:
            normalized.append(token)
            seen.add(token)
    return normalized


def _path_within_root(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def build_post_publish_plan(
    note_path,
    combined_hash,
    actions,
    workspace_root=DEFAULT_VAULT_ROOT,
    main_python=APPROVED_MAIN_PYTHON,
    rag_python=APPROVED_RAG_PYTHON,
):
    workspace_root = Path(workspace_root).resolve()
    note_path = Path(note_path).resolve()
    if not _path_within_root(note_path, workspace_root):
        raise ValueError(
            f"Post-publish workflow requires the note to live under the vault root: {note_path}"
        )

    plan = []
    session = f"gemini-postpublish-{str(combined_hash)[:12]}"
    actions = list(actions or [])

    if "prefill" in actions:
        plan.append(
            {
                "action": "prefill",
                "command": [
                    str(main_python),
                    str(PREFILL_CANDIDATE_TAGS_PATH),
                    "--root",
                    str(workspace_root),
                    "--files",
                    str(note_path),
                    "--mode",
                    "merge",
                ],
                "cwd": str(workspace_root),
            }
        )
    if "kimi_fallback" in actions:
        plan.append(
            {
                "action": "kimi_fallback",
                "command": [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(RUN_TAGGING_PIPELINE_PATH),
                    "-Workspace",
                    str(workspace_root),
                    "-Session",
                    session,
                    "-Files",
                    str(note_path),
                    "-BatchSize",
                    "1",
                    "-MaxBatches",
                    "1",
                ],
                "cwd": str(workspace_root),
            }
        )
    if "review_queue" in actions:
        plan.append(
            {
                "action": "review_queue",
                "command": [
                    str(main_python),
                    str(EXPORT_REVIEW_QUEUE_PATH),
                    "--root",
                    str(workspace_root),
                ],
                "cwd": str(workspace_root),
            }
        )
    if "notes_db" in actions:
        plan.append(
            {
                "action": "notes_db",
                "command": [str(rag_python), str(BUILD_NOTES_DB_PATH)],
                "cwd": str(LOCALRAG_ROOT),
            }
        )
    if "pdf_db" in actions:
        plan.append(
            {
                "action": "pdf_db",
                "command": [str(rag_python), str(BUILD_PDF_DB_PATH)],
                "cwd": str(LOCALRAG_ROOT),
            }
        )
    if "restart_query" in actions:
        restart_command = (
            "$existing = Get-CimInstance Win32_Process -Filter \"Name = 'python.exe'\" | "
            "Where-Object { $_.CommandLine -like '*query_server.py*' }; "
            "foreach ($proc in $existing) { "
            "Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue "
            "}; "
            "Start-Sleep -Seconds 2; "
            f"Start-Process '{rag_python}' -ArgumentList '{QUERY_SERVER_PATH}' -WindowStyle Hidden"
        )
        plan.append(
            {
                "action": "restart_query",
                "command": [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    restart_command,
                ],
                "cwd": str(LOCALRAG_ROOT),
            }
        )

    return plan


def _normalize_tag_list(value):
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def should_trigger_kimi_fallback(note_path):
    payload = _read_note_frontmatter_mapping(note_path)
    candidate_high = _normalize_tag_list(payload.get("candidate_tags_high"))
    candidate_medium = _normalize_tag_list(payload.get("candidate_tags_medium"))
    candidate_low = _normalize_tag_list(payload.get("candidate_tags_low"))
    total_candidates = len(candidate_high) + len(candidate_medium) + len(candidate_low)
    high_medium_total = len(candidate_high) + len(candidate_medium)

    if total_candidates == 0:
        return True, "candidate tags empty after deterministic prefill"
    if high_medium_total == 0 and total_candidates <= 1:
        return True, "candidate tags remain very sparse after deterministic prefill"

    note_template = str(payload.get("note_template") or "").strip()
    research_domain = str(payload.get("research_domain") or "").strip()
    scope_hint = str(payload.get("scope_hint") or "").strip()
    signal_quality = str(payload.get("signal_quality") or "").strip()

    if note_template == "generic-research-note":
        return True, "generic fallback template may need semantic candidate supplementation"
    if research_domain == "other":
        return True, "research_domain=other triggers edge-theme fallback"
    if scope_hint in {"other", "needs-body-evidence"}:
        return True, f"scope_hint={scope_hint} triggers fallback review"
    if signal_quality == "weak" and high_medium_total == 0:
        return True, "signal_quality=weak with no high/medium candidates"

    return False, "candidate tags already populated"


def run_post_publish_workflow(
    note_path,
    combined_hash,
    actions,
    runner=subprocess.run,
):
    results = []
    for step in build_post_publish_plan(
        note_path=note_path,
        combined_hash=combined_hash,
        actions=actions,
    ):
        if step["action"] == "kimi_fallback":
            should_run, reason = should_trigger_kimi_fallback(note_path)
            if not should_run:
                results.append(
                    {
                        "action": step["action"],
                        "command": step["command"],
                        "cwd": step.get("cwd"),
                        "status": "skipped",
                        "reason": reason,
                    }
                )
                continue
        # Post-publish actions call OPTIONAL scripts that live in the user's
        # vault, not this repo. A fresh open-source install doesn't have
        # them — skip gracefully instead of failing the run after the note
        # was already published.
        script_path = next(
            (str(part) for part in step["command"][1:] if str(part).endswith((".py", ".ps1"))),
            None,
        )
        if script_path and not Path(script_path).exists():
            results.append(
                {
                    "action": step["action"],
                    "command": step["command"],
                    "cwd": step.get("cwd"),
                    "status": "skipped",
                    "reason": (
                        f"optional vault script not found: {script_path} "
                        "(post-publish actions are opt-in; use --post-publish none to silence)"
                    ),
                }
            )
            continue
        try:
            completed = runner(
                step["command"],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=step.get("cwd"),
            )
        except FileNotFoundError as exc:
            results.append(
                {
                    "action": step["action"],
                    "command": step["command"],
                    "cwd": step.get("cwd"),
                    "status": "failed",
                    "reason": f"interpreter or script not found: {exc}",
                }
            )
            continue
        except subprocess.CalledProcessError as exc:
            results.append(
                {
                    "action": step["action"],
                    "command": step["command"],
                    "cwd": step.get("cwd"),
                    "status": "failed",
                    "returncode": exc.returncode,
                    "stdout": exc.stdout,
                    "stderr": exc.stderr,
                }
            )
            continue
        results.append(
            {
                "action": step["action"],
                "command": step["command"],
                "cwd": step.get("cwd"),
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "status": "completed",
            }
        )
    return results


def write_multifacet_canary_note(rendered_note, generated_name, canary_root):
    return write_multifacet_output_note(
        rendered_note=rendered_note,
        generated_name=generated_name,
        output_root=canary_root,
    )


def run_multifacet_spec_pipeline(
    backend,
    model,
    pdf_paths,
    combined_hash,
    canary_root,
    run_dir,
    legacy_combined_hash=None,
    zotero_parent_key=None,
    note_index=None,
    model_router="auto",
    routing_policy_path=None,
    flash_model=None,
    pro_model=None,
    publish_target="canary",
    post_publish_actions=None,
    preflight=None,
    prepared_pdf_paths=None,
    prepared_pdf_manifest=None,
    note_template_override=None,
):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    manual_override = model.strip() if isinstance(model, str) and model.strip() else None
    policy = load_model_routing_policy(routing_policy_path)
    preflight = preflight or collect_pdf_preflight(pdf_paths)

    if model_router == "off" and manual_override is None:
        manual_override = flash_model or policy.get("default_profiler_model", "gemini-2.5-flash")

    profiler_model, profiler_reason = resolve_profiler_model(
        policy=policy,
        preflight=preflight,
        cli_override=manual_override,
        flash_model=flash_model,
    )
    document_profile = run_document_profiler(
        backend=backend,
        model=profiler_model,
    )
    document_profile_path = run_dir / "01-document-profile.json"
    write_run_artifact(document_profile_path, document_profile)

    note_generator_model, note_generator_reason = resolve_note_generator_model(
        policy=policy,
        preflight=preflight,
        document_profile=document_profile,
        cli_override=manual_override,
        flash_model=flash_model,
        pro_model=pro_model,
    )
    model_plan = build_model_plan(
        pdf_paths=pdf_paths,
        preflight=preflight,
        policy=policy,
        profiler_model=profiler_model,
        profiler_reason=profiler_reason,
        note_generator_model=note_generator_model,
        note_generator_reason=note_generator_reason,
        manual_override=manual_override,
        flash_model_override=flash_model,
        pro_model_override=pro_model,
        prepared_pdf_paths=prepared_pdf_paths,
        prepared_pdf_manifest=prepared_pdf_manifest,
    )
    model_plan_path = run_dir / "00-model-plan.json"
    write_run_artifact(model_plan_path, model_plan)

    note_draft = run_note_generator(
        backend=backend,
        model=note_generator_model,
        document_profile=document_profile,
        note_template_override=note_template_override,
    )
    note_draft_path = run_dir / "02-note-draft.json"
    write_run_artifact(note_draft_path, note_draft)

    generated_name = resolve_multifacet_generated_name(
        note_draft=note_draft,
        pdf_paths=pdf_paths,
    )
    rendered_note = render_multifacet_note(
        note_draft=note_draft,
        pdf_paths=pdf_paths,
        combined_hash=combined_hash,
        zotero_parent_key=zotero_parent_key,
        legacy_combined_hash=legacy_combined_hash,
    )
    rendered_note_path = run_dir / "04-rendered-note.md"
    write_run_artifact(rendered_note_path, rendered_note)

    validation_report = build_multifacet_validation_report(rendered_note)
    validation_report_path = run_dir / "05-validation-report.json"
    write_run_artifact(validation_report_path, validation_report)

    note_path = write_multifacet_output_note(
        rendered_note=rendered_note,
        generated_name=generated_name,
        output_root=canary_root,
        combined_hash=combined_hash,
        legacy_combined_hash=legacy_combined_hash,
        zotero_parent_key=zotero_parent_key,
        note_index=note_index,
    )
    post_publish_results = []
    if publish_target == "vault" and post_publish_actions:
        post_publish_results = run_post_publish_workflow(
            note_path=note_path,
            combined_hash=combined_hash,
            actions=post_publish_actions,
        )

    result = {
        "document_profile": document_profile,
        "note_draft": note_draft,
        "rendered_note": rendered_note,
        "validation_report": validation_report,
        "note_path": str(note_path),
        "publish_target": publish_target,
        "post_publish_results": post_publish_results,
        "artifacts": {
            "model_plan": str(model_plan_path),
            "document_profile": str(document_profile_path),
            "note_draft": str(note_draft_path),
            "rendered_note": str(rendered_note_path),
            "validation_report": str(validation_report_path),
        },
    }
    if publish_target == "canary":
        result["canary_note_path"] = str(note_path)
    if publish_target == "vault":
        result["vault_note_path"] = str(note_path)
    return result

def get_parent_key(pdf_path, zotero_db=ZOTERO_DB_PATH):
    """Backwards-compatible shim. Canonical implementation is in
    `scanner/zotero_client.py`."""
    return _get_parent_key(pdf_path, zotero_db=zotero_db)


def render_multifacet_note(
    note_draft,
    pdf_paths,
    combined_hash,
    zotero_parent_key=None,
    legacy_combined_hash=None,
):
    """Backwards-compatible shim. Fetches the Zotero abstract from the
    user's library here so the canonical render function in note_render
    stays IO-free.

    Callers wanting to supply their own abstract (e.g. a test, or a
    workflow that already has the abstract in memory) should call
    `note_render.render_multifacet_note` directly with `zotero_abstract=...`
    and skip this shim.
    """
    abstract = (
        get_zotero_abstract_note(zotero_parent_key, zotero_db=ZOTERO_DB_PATH)
        if zotero_parent_key else ""
    )
    return _render_multifacet_note(
        note_draft=note_draft,
        pdf_paths=pdf_paths,
        combined_hash=combined_hash,
        zotero_parent_key=zotero_parent_key,
        zotero_abstract=abstract,
        legacy_combined_hash=legacy_combined_hash,
    )


def get_file_hash(filepath, chunk_size=8192):
    hasher = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def resolve_project_id():
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if project_id:
        return project_id

    credentials_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if credentials_path and os.path.exists(credentials_path):
        try:
            with open(credentials_path, "r", encoding="utf-8") as f:
                return json.load(f).get("project_id", "").strip()
        except Exception:
            return ""
    return ""


def resolve_bucket_name(cli_bucket, project_id):
    bucket = (cli_bucket or os.environ.get("GEMINI_VERTEX_GCS_BUCKET", "")).strip()
    if bucket:
        return bucket
    if project_id:
        return f"{project_id}-gemini-literature-temp"
    return ""


def make_backend_from_args(args, *, run_dir=None):
    """Thin shim around `backends.make_backend_from_env`.

    Most backends just need their env vars (centralized in the backends
    package). The subagent backend additionally takes a `run_dir` provided
    by the orchestrator and (optionally) a `resume_dir` from `--resume`.
    """
    from backends import make_backend_from_env

    overrides = {}
    backend_name = (args.backend or "").lower().replace("_", "-")
    resume_dir = getattr(args, "resume", None)
    if backend_name == "subagent":
        if run_dir is not None:
            overrides["run_dir_provider"] = lambda: run_dir
        if resume_dir:
            overrides["resume_dir"] = resume_dir
        resume_cli_args = []
        if getattr(args, "force", False):
            resume_cli_args.append("--force")
        for flag, attribute in (
            ("--model", "model"),
            ("--model-router", "model_router"),
            ("--routing-policy", "routing_policy"),
            ("--flash-model", "flash_model"),
            ("--pro-model", "pro_model"),
            ("--out-dir", "out_dir"),
            ("--publish-target", "publish_target"),
            ("--post-publish", "post_publish"),
            ("--note-index-file", "note_index_file"),
            ("--note-template", "note_template"),
        ):
            value = getattr(args, attribute, None)
            if value is not None:
                resume_cli_args.extend((flag, str(value)))
        for source_artifact in getattr(args, "source_artifact", ()) or ():
            resume_cli_args.extend(("--source-artifact", str(source_artifact)))
        overrides["resume_cli_args"] = resume_cli_args
    elif resume_dir:
        # User passed --resume but selected a non-subagent backend. The flag
        # has no meaning there; warn loudly so they don't think it worked.
        print(
            f"[WARN] --resume is only meaningful with --backend subagent; "
            f"ignoring with --backend {backend_name}.",
            file=sys.stderr,
        )
    if getattr(args, "gcs_bucket", None):
        overrides["bucket_name"] = args.gcs_bucket
    return make_backend_from_env(args.backend, **overrides)


def write_archive_manifest(bucket, combined_hash, manifest):
    blob = bucket.blob(f"pdf-inputs/{combined_hash}/manifest.json")
    blob.upload_from_string(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        content_type="application/json",
    )


def main():
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    parser = build_arg_parser()
    args = parser.parse_args()
    if args.source_artifact and args.backend != "subagent":
        parser.error("--source-artifact requires --backend subagent")
    source_artifact_records = [
        {
            "path": str(Path(path).expanduser().resolve()),
            "sha256": get_file_hash(Path(path).expanduser().resolve()),
        }
        for path in args.source_artifact
    ]
    note_index = load_note_index(args.note_index_file)

    hash_variants = get_combined_hash_variants(args.pdf_paths)
    combined_hash = hash_variants["combined_hash"]
    legacy_combined_hash = hash_variants["legacy_combined_hash"]
    # Single source of truth for the dedup ledger — the same path the batch
    # prefilter, verify_and_clean, and the migration script read
    # ($GEMINI_PROCESSED_HISTORY, default scanner/processed_history.txt).
    ledger_path = str(PROCESSED_HISTORY_PATH)
    # In --resume mode (sub-agent flow), reuse the directory the user passed.
    # Otherwise allocate a fresh per-paper run dir keyed by combined_hash.
    if getattr(args, "resume", None):
        run_dir = Path(args.resume)
        if not run_dir.exists():
            print(f"❌ --resume path does not exist: {run_dir}", file=sys.stderr)
            sys.exit(1)
        # Identity guard: refuse to resume a run_dir that belongs to a
        # different PDF set. Without this, paper B's staged outputs get
        # rendered under paper A's frontmatter/hash (hybrid note) and paper
        # A is falsely marked processed.
        recorded_hash = None
        recorded_bootstrap = {}
        bootstrap_path = run_dir / "00-pipeline-bootstrap.json"
        if bootstrap_path.exists():
            try:
                recorded_bootstrap = (
                    json.loads(bootstrap_path.read_text(encoding="utf-8")) or {}
                )
                recorded_hash = recorded_bootstrap.get("combined_hash")
            except (OSError, json.JSONDecodeError):
                recorded_hash = None
                recorded_bootstrap = {}
        if not recorded_hash and re.fullmatch(r"[0-9a-f]{64}", run_dir.name):
            # Default run dirs are named after the combined_hash.
            recorded_hash = run_dir.name
        if recorded_hash and recorded_hash not in (combined_hash, legacy_combined_hash):
            print(
                "❌ --resume run_dir belongs to a different PDF set:\n"
                f"   run_dir hash : {recorded_hash}\n"
                f"   argv PDFs    : {combined_hash}\n"
                "   Refusing to mix papers. Pass the run_dir that matches these "
                "PDFs, or drop --resume to start fresh.",
                file=sys.stderr,
            )
            sys.exit(1)
        recorded_source_artifacts = recorded_bootstrap.get("source_artifacts")
        if (
            recorded_source_artifacts is not None
            and recorded_source_artifacts != source_artifact_records
        ):
            print(
                "❌ --resume source artifacts differ from the original run. "
                "Refusing to mix native-coordinate source packets.",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        run_dir = default_run_dir(combined_hash)

    parent_key = get_parent_key(args.pdf_paths[0])

    # Unified dedup: ledger ∪ vault scan, queried by (stable_hash,
    # legacy_hash, parent_key). Vault recovery now fires in solo mode
    # too; before this it only triggered when the batch scanner pre-built
    # a --note-index-file. Canary mode (publish_target != "vault") still
    # skips the vault scan because canary notes live elsewhere.
    publish_target = getattr(args, "publish_target", "canary")
    dedup_vault_root = DEFAULT_VAULT_ROOT if publish_target == "vault" else Path("__nonexistent_vault__")
    dedup_index = DedupIndex.build(
        history_path=ledger_path,
        vault_root=dedup_vault_root,
        cached_note_index=note_index if publish_target == "vault" else None,
    )
    dedup_hit = dedup_index.lookup(
        combined_hash=combined_hash,
        legacy_combined_hash=legacy_combined_hash,
        zotero_parent_key=parent_key,
    )
    if dedup_hit and not args.force:
        matched_hash, existing_note_path = dedup_hit
        # Always canonicalize the ledger to the stable hash on a hit;
        # legacy-only entries get upgraded so future scans hit ledger fast-path.
        dedup_index.append(combined_hash)
        match_label = "stable" if matched_hash == combined_hash else "legacy"
        if existing_note_path is not None:
            print(
                "⏭️ Skipping: An existing vault note already covers this PDF set "
                f"(Hash: {matched_hash[:8]}, match={match_label}). Existing note: {existing_note_path}"
            )
        else:
            print(
                "⏭️ Skipping: These PDFs have already been processed "
                f"(Hash: {matched_hash[:8]}, match={match_label}). Use --force to re-process."
            )
        return

    write_run_artifact(
        run_dir / "00-pipeline-bootstrap.json",
        {
            "combined_hash": combined_hash,
            "pdf_paths": [os.path.abspath(p) for p in args.pdf_paths],
            "source_artifacts": source_artifact_records,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "domain_pack": DOMAIN_PACK_NAME,
            "assets_available": {
                "document_profiler_prompt": (Path(DOMAIN_PACK_ROOT) / "prompts" / "document_profiler.system.txt").exists(),
                "document_profile_schema": (Path(DOMAIN_PACK_ROOT) / "schemas" / "document_profile.vertex.schema.json").exists(),
                "note_generator_prompt": (Path(DOMAIN_PACK_ROOT) / "prompts" / "note_generator.system.txt").exists(),
                "structured_note_schema": (Path(DOMAIN_PACK_ROOT) / "schemas" / "structured_note.vertex.schema.json").exists(),
                "universal_rules": Path(UNIVERSAL_RULES_PATH).exists(),
                "domain_quality_rules": (Path(DOMAIN_PACK_ROOT) / "templates" / "_domain_quality_rules.txt").exists(),
            },
        },
    )

    try:
        preflight = collect_pdf_preflight(args.pdf_paths)
        prepared_inputs = prepare_pdf_inputs_for_vertex(
            pdf_paths=args.pdf_paths,
            work_dir=run_dir / "prepared_pdfs",
            preflight=preflight,
        )
    except PDFPreflightError as exc:
        print(
            f"NON_RETRYABLE_ERROR[{exc.error_code}]: {exc}",
            file=sys.stderr,
        )
        sys.exit(2)

    prepared_pdf_paths = prepared_inputs["prepared_pdf_paths"]
    prepared_pdf_manifest = prepared_inputs["prepared_pdf_manifest"]
    profiler_pdf_paths = prepared_inputs.get("profiler_pdf_paths")
    profiler_manifest = prepared_inputs.get("profiler_manifest")

    # Audit: record what got sent to Stage A. Lets us catch silent
    # routing regressions where a long review article was classified on
    # only its first 3 pages and missed the is_review_like flag.
    if profiler_manifest is not None:
        write_run_artifact(run_dir / "08-profiler-input.json", profiler_manifest)

    backend = make_backend_from_args(args, run_dir=run_dir)
    print(f"🔌 Backend: {backend.name}")

    from backends import SubagentManifestPending
    from backends.vertex import VertexBackend

    is_vertex = isinstance(backend, VertexBackend)
    archive_manifest = None

    try:
        backend.attach_pdfs(
            prepared_pdf_paths,
            combined_hash=combined_hash,
            profiler_pdf_paths=profiler_pdf_paths,
        )
        if args.source_artifact:
            backend.attach_source_artifacts(args.source_artifact)

        if is_vertex:
            print(f"🪣 Vertex bucket: gs://{backend.bucket_name}")
            archive_manifest = {
                "combined_hash": combined_hash,
                "status": "uploaded",
                "project_id": backend.project_id,
                "bucket": backend.bucket_name,
                "archive_prefix": f"pdf-inputs/{combined_hash}/",
                "model": args.model or "auto-route",
                "model_router": args.model_router,
                "flash_model": args.flash_model,
                "pro_model": args.pro_model,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
                "pdfs": backend.archived_files,
                "source_pdf_paths": [os.path.abspath(path) for path in args.pdf_paths],
                "prepared_pdf_manifest": prepared_pdf_manifest,
                "chunking_enabled": prepared_inputs["chunking_enabled"],
            }
            if parent_key:
                archive_manifest["zotero_parent_key"] = parent_key
            write_archive_manifest(backend.gcs_bucket, combined_hash, archive_manifest)

        display_model = args.model or f"auto-route ({args.flash_model or 'gemini-2.5-flash'} -> {args.pro_model or 'gemini-2.5-pro'})"
        publish_target = args.publish_target
        post_publish_actions = parse_post_publish_actions(
            args.post_publish,
            publish_target=publish_target,
        )
        # --out-dir never DIVERTS a vault publication out of the vault — it
        # is an *additional* copy destination (see the copy after the
        # pipeline call). Only canary runs treat it as the primary root;
        # previously `--out-dir X --publish-target vault` wrote the note to X
        # only, then ran vault post-publish actions against a note the vault
        # never received.
        if publish_target == "vault":
            output_root = str(DEFAULT_VAULT_ROOT)
        else:
            output_root = args.out_dir or str(DEFAULT_PIPELINE_REPORT_ROOT / "canary_notes")
        print(
            f"Generating {publish_target} note with {display_model} via backend={backend.name}..."
        )
        multifacet_result = run_multifacet_spec_pipeline(
            backend=backend,
            model=args.model,
            pdf_paths=args.pdf_paths,
            combined_hash=combined_hash,
            legacy_combined_hash=legacy_combined_hash,
            canary_root=output_root,
            run_dir=run_dir,
            zotero_parent_key=parent_key,
            note_index=note_index,
            model_router=args.model_router,
            routing_policy_path=args.routing_policy,
            flash_model=args.flash_model,
            pro_model=args.pro_model,
            publish_target=publish_target,
            post_publish_actions=post_publish_actions,
            preflight=preflight,
            prepared_pdf_paths=prepared_pdf_paths,
            prepared_pdf_manifest=prepared_pdf_manifest,
            note_template_override=args.note_template,
        )
        if is_vertex:
            archive_manifest["status"] = "completed"
            archive_manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
            archive_manifest["publish_target"] = publish_target
            archive_manifest["post_publish_actions"] = post_publish_actions
            archive_manifest["generated_note_name"] = os.path.basename(multifacet_result["note_path"])
            archive_manifest["note_paths"] = [os.path.abspath(multifacet_result["note_path"])]
            archive_manifest["run_dir"] = str(run_dir)
            archive_manifest["model_plan"] = multifacet_result["artifacts"]["model_plan"]
            archive_manifest["validation_report"] = multifacet_result["artifacts"]["validation_report"]
            archive_manifest["post_publish_results"] = multifacet_result["post_publish_results"]
            write_archive_manifest(backend.gcs_bucket, combined_hash, archive_manifest)
        if args.out_dir and publish_target == "vault":
            extra_dir = Path(args.out_dir)
            extra_dir.mkdir(parents=True, exist_ok=True)
            note_file = Path(multifacet_result["note_path"])
            shutil.copy2(note_file, extra_dir / note_file.name)
        # Canary runs must not touch the production ledger: a canary hash in
        # the ledger would permanently block the later real vault publication
        # of the same paper.
        if publish_target == "vault":
            dedup_index.append(combined_hash)
        print(f"✅ {publish_target} note saved to {multifacet_result['note_path']}")
        return

    except SubagentManifestPending as exc:
        manifest_name = Path(exc.manifest_path).name
        # Determine which stage we just emitted from the manifest filename
        if "profiler" in manifest_name:
            stage_label = "Stage A (Document Profiler)"
            next_hint = (
                "After the sub-agent writes 01-document-profile.json, re-run with:\n"
                f"     python scanner/gemini_analyze_pdf.py {' '.join(args.pdf_paths)} \\\n"
                f"         --backend subagent --resume \"{exc.run_dir}\"\n"
                "   to produce Stage B's manifest."
            )
        elif "note_generator" in manifest_name:
            stage_label = "Stage B (Note Generator)"
            next_hint = (
                "After the sub-agent writes 02-note-draft.json, re-run the same\n"
                f"   command (with --resume \"{exc.run_dir}\") to render the final\n"
                "   Markdown note and update the ledger."
            )
        else:
            stage_label = manifest_name
            next_hint = (
                "Run again with --resume <run_dir> after the sub-agent fills the "
                "expected output."
            )
        print(f"📝 Sub-agent manifest written ({stage_label}): {exc.manifest_path}")
        print(f"   Run dir: {exc.run_dir}")
        print(f"   {next_hint}")
        # Exit code 200 = "manifest pending sub-agent". This is distinct from
        # 0 (note generated) so batch wrappers can tell the two cases apart
        # and resume on the next pass instead of declaring success.
        sys.exit(200)
    except Exception:
        if archive_manifest is not None and is_vertex:
            archive_manifest["status"] = "generation_failed"
            archive_manifest["last_error_at"] = datetime.now(timezone.utc).isoformat()
            try:
                write_archive_manifest(backend.gcs_bucket, combined_hash, archive_manifest)
            except Exception:
                pass
        raise


if __name__ == "__main__":
    main()
