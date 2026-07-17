#!/usr/bin/env python3
"""
批量把主 vault 里现有多 PDF note 的 combined_hash，以及 processed_history.txt，
迁移到新的“顺序无关”稳定规则。

默认 dry-run，只生成迁移计划和摘要；加 --write 才会真正写入。
写入模式下会自动备份被修改的 note、processed_history.txt，并输出 migration report。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

# Importing config hydrates os.environ from the repo .env — without this,
# .env-only settings (LOCALRAG_NOTES_DIR, GEMINI_PROCESSED_HISTORY) are
# silently ignored by the module-level defaults below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: F401, E402


_HOME = Path.home()
DEFAULT_NOTES_ROOT = Path(
    os.environ.get("LOCALRAG_NOTES_DIR", str(_HOME / "research-note"))
)
DEFAULT_HISTORY_PATH = Path(
    os.environ.get(
        "GEMINI_PROCESSED_HISTORY",
        str(Path(__file__).resolve().parent / "processed_history.txt"),
    )
)
DEFAULT_BACKUP_PARENT = DEFAULT_NOTES_ROOT / "progress" / "schema_migration"
LIVE_VAULT_EXCLUDED_RELATIVE_PREFIXES = (
    "progress/gate_backups/",
    "progress/gate_reports/",
    "progress/version_snapshots/",
    "progress/schema_migration/",
    "progress/taxonomy_discovery/",
    "progress/pipeline_logs/",
    "progress/pipeline_reports/",
)
LIVE_VAULT_EXCLUDED_PATH_PARTS = {".obsidian", "__pycache__", ".stfolder"}
FRONTMATTER_BLOCK_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)


def get_file_hash(filepath, chunk_size=8192):
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def get_stable_combined_hash(filepaths):
    hasher = hashlib.sha256()
    file_hashes = sorted(get_file_hash(filepath) for filepath in filepaths)
    for file_hash in file_hashes:
        hasher.update(file_hash.encode("utf-8"))
    return hasher.hexdigest()


def get_legacy_combined_hash(filepaths):
    hasher = hashlib.sha256()
    for filepath in filepaths:
        hasher.update(get_file_hash(filepath).encode("utf-8"))
    return hasher.hexdigest()


def iter_live_vault_note_paths(root: Path):
    root = root.resolve()
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


def extract_frontmatter(content: str):
    return FRONTMATTER_BLOCK_RE.match(content)


def extract_pdf_paths(frontmatter_text: str):
    paths = {}
    for match in re.finditer(r"^pdf_(\d+)_path:\s*(.+)$", frontmatter_text, re.MULTILINE):
        idx = int(match.group(1))
        value = match.group(2).strip().strip('"').strip("'")
        paths[idx] = value
    return [paths[index] for index in sorted(paths)]


def extract_combined_hash(frontmatter_text: str):
    match = re.search(r"^combined_hash:\s*(.+)$", frontmatter_text, re.MULTILINE)
    return match.group(1).strip().strip('"').strip("'") if match else None


def upsert_combined_hash(content: str, new_hash: str):
    if re.search(r"^combined_hash:", content, re.MULTILINE):
        return re.sub(
            r"^combined_hash:\s*.*$",
            f"combined_hash: {new_hash}",
            content,
            count=1,
            flags=re.MULTILINE,
        )

    if re.search(r"^zotero_parent_key:", content, re.MULTILINE):
        return re.sub(
            r"(^zotero_parent_key:[^\n]*$)",
            r"\1\n" + f"combined_hash: {new_hash}",
            content,
            count=1,
            flags=re.MULTILINE,
        )

    last_pdf_path = list(re.finditer(r"^(pdf_\d+_path:[^\n]*)$", content, re.MULTILINE))
    if last_pdf_path:
        match = last_pdf_path[-1]
        return content[: match.end()] + "\n" + f"combined_hash: {new_hash}" + content[match.end() :]

    return re.sub(r"\n---\r?\n", f"\ncombined_hash: {new_hash}\n---\n", content, count=1)


def analyze_note(note_path: Path):
    content = note_path.read_text(encoding="utf-8", errors="ignore")
    frontmatter_match = extract_frontmatter(content)
    if not frontmatter_match:
        return {
            "path": str(note_path),
            "status": "skipped_no_frontmatter",
        }

    frontmatter_text = frontmatter_match.group(1)
    pdf_paths = extract_pdf_paths(frontmatter_text)
    current_hash = extract_combined_hash(frontmatter_text)

    if len(pdf_paths) <= 1:
        return {
            "path": str(note_path),
            "status": "skipped_single_pdf",
            "pdf_count": len(pdf_paths),
        }

    missing_paths = [path for path in pdf_paths if not path or not Path(path).exists()]
    if missing_paths:
        return {
            "path": str(note_path),
            "status": "skipped_missing_pdf",
            "pdf_count": len(pdf_paths),
            "missing_paths": missing_paths,
        }

    legacy_hash = get_legacy_combined_hash(pdf_paths)
    stable_hash = get_stable_combined_hash(pdf_paths)
    aliases = list(OrderedDict.fromkeys(value for value in (current_hash, legacy_hash, stable_hash) if value))
    updated_content = content if current_hash == stable_hash else upsert_combined_hash(content, stable_hash)
    status = "changed" if current_hash != stable_hash else "already_stable"

    return {
        "path": str(note_path),
        "status": status,
        "pdf_count": len(pdf_paths),
        "current_hash": current_hash,
        "legacy_hash": legacy_hash,
        "stable_hash": stable_hash,
        "aliases": aliases,
        "updated_content": updated_content,
    }


def normalize_history_lines(raw_lines, note_records):
    alias_map = {}
    stable_hashes = []
    for record in note_records:
        if record["status"] not in {"changed", "already_stable"}:
            continue
        stable_hash = record["stable_hash"]
        stable_hashes.append(stable_hash)
        for alias in record["aliases"]:
            alias_map[alias] = stable_hash

    normalized = []
    seen = set()
    replaced = 0
    for line in raw_lines:
        candidate = alias_map.get(line, line)
        if candidate != line:
            replaced += 1
        if candidate not in seen:
            normalized.append(candidate)
            seen.add(candidate)

    appended = 0
    for stable_hash in stable_hashes:
        if stable_hash not in seen:
            normalized.append(stable_hash)
            seen.add(stable_hash)
            appended += 1

    return {
        "raw_lines": raw_lines,
        "normalized_lines": normalized,
        "replaced_count": replaced,
        "appended_count": appended,
        "changed": normalized != raw_lines,
    }


def summarize_note_records(note_records):
    summary = {
        "notes_scanned": len(note_records),
        "notes_changed": 0,
        "notes_already_stable": 0,
        "notes_skipped_single_pdf": 0,
        "notes_skipped_missing_pdf": 0,
        "notes_skipped_no_frontmatter": 0,
    }
    for record in note_records:
        status = record["status"]
        if status == "changed":
            summary["notes_changed"] += 1
        elif status == "already_stable":
            summary["notes_already_stable"] += 1
        elif status == "skipped_single_pdf":
            summary["notes_skipped_single_pdf"] += 1
        elif status == "skipped_missing_pdf":
            summary["notes_skipped_missing_pdf"] += 1
        elif status == "skipped_no_frontmatter":
            summary["notes_skipped_no_frontmatter"] += 1
    return summary


def build_migration_plan(notes_root=DEFAULT_NOTES_ROOT, history_path=DEFAULT_HISTORY_PATH):
    notes_root = Path(notes_root).resolve()
    history_path = Path(history_path).resolve()

    note_records = [analyze_note(path) for path in iter_live_vault_note_paths(notes_root)]

    if history_path.exists():
        raw_lines = [line.strip() for line in history_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        raw_lines = []

    history = normalize_history_lines(raw_lines, note_records)
    summary = summarize_note_records(note_records)
    summary["history_lines_before"] = len(raw_lines)
    summary["history_lines_after"] = len(history["normalized_lines"])
    summary["history_replaced"] = history["replaced_count"]
    summary["history_appended"] = history["appended_count"]

    return {
        "notes_root": str(notes_root),
        "history_path": str(history_path),
        "generated_at": datetime.now().astimezone().isoformat(),
        "note_records": note_records,
        "history": history,
        "summary": summary,
    }


def make_default_backup_dir(parent=DEFAULT_BACKUP_PARENT):
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-combined-hash-stable-migration-%H%M%S")
    return Path(parent) / stamp


def write_report(plan, backup_root: Path):
    sanitized_records = []
    for record in plan["note_records"]:
        sanitized = dict(record)
        sanitized.pop("updated_content", None)
        sanitized_records.append(sanitized)

    report = {
        "generated_at": plan["generated_at"],
        "notes_root": plan["notes_root"],
        "history_path": plan["history_path"],
        "summary": plan["summary"],
        "history": {
            "changed": plan["history"]["changed"],
            "replaced_count": plan["history"]["replaced_count"],
            "appended_count": plan["history"]["appended_count"],
            "history_lines_before": len(plan["history"]["raw_lines"]),
            "history_lines_after": len(plan["history"]["normalized_lines"]),
        },
        "note_records": sanitized_records,
    }
    report_path = backup_root / "migration-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


def apply_migration_plan(plan, backup_root):
    backup_root = Path(backup_root).resolve()
    backup_root.mkdir(parents=True, exist_ok=True)

    changed_records = [record for record in plan["note_records"] if record["status"] == "changed"]
    notes_backup_root = backup_root / "notes"
    notes_backup_root.mkdir(parents=True, exist_ok=True)

    for record in changed_records:
        note_path = Path(record["path"])
        relative = note_path.relative_to(Path(plan["notes_root"]))
        backup_path = notes_backup_root / relative
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(note_path, backup_path)
        note_path.write_text(record["updated_content"], encoding="utf-8")

    history_path = Path(plan["history_path"])
    if history_path.exists():
        shutil.copy2(history_path, backup_root / "processed_history_before.txt")
    else:
        (backup_root / "processed_history_before.txt").write_text("", encoding="utf-8")
    history_path.write_text(
        "".join(f"{line}\n" for line in plan["history"]["normalized_lines"]),
        encoding="utf-8",
    )

    report_path = write_report(plan, backup_root)
    applied_report = {
        "backup_root": str(backup_root),
        "report_path": str(report_path),
        "summary": plan["summary"],
    }
    return applied_report


def print_summary(plan):
    summary = plan["summary"]
    print("=" * 60)
    print("combined_hash stable migration")
    print("=" * 60)
    print(f"Notes root            : {plan['notes_root']}")
    print(f"History path          : {plan['history_path']}")
    print(f"Notes scanned         : {summary['notes_scanned']}")
    print(f"Notes changed         : {summary['notes_changed']}")
    print(f"Notes already stable  : {summary['notes_already_stable']}")
    print(f"Skipped single PDF    : {summary['notes_skipped_single_pdf']}")
    print(f"Skipped missing PDF   : {summary['notes_skipped_missing_pdf']}")
    print(f"Skipped no frontmatter: {summary['notes_skipped_no_frontmatter']}")
    print(f"History before        : {summary['history_lines_before']}")
    print(f"History after         : {summary['history_lines_after']}")
    print(f"History replaced      : {summary['history_replaced']}")
    print(f"History appended      : {summary['history_appended']}")


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Migrate multi-PDF combined_hash values to the stable rule.")
    parser.add_argument("--notes-root", default=str(DEFAULT_NOTES_ROOT), help="Vault root to scan.")
    parser.add_argument("--history-file", default=str(DEFAULT_HISTORY_PATH), help="processed_history.txt path.")
    parser.add_argument("--backup-dir", help="Explicit backup directory for write mode.")
    parser.add_argument("--write", action="store_true", help="Apply changes. Default is dry-run.")
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    plan = build_migration_plan(notes_root=args.notes_root, history_path=args.history_file)
    print_summary(plan)

    if not args.write:
        print("\nDry-run only. Re-run with --write to apply changes.")
        return

    backup_root = Path(args.backup_dir).resolve() if args.backup_dir else make_default_backup_dir()
    result = apply_migration_plan(plan, backup_root=backup_root)
    print(f"\nApplied migration. Backup: {result['backup_root']}")
    print(f"Report: {result['report_path']}")


if __name__ == "__main__":
    main()
