import os
import sys
import shutil
import sqlite3
import subprocess
import argparse
import hashlib
import json
import re
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

from config import (
    VAULT_ROOT as DEFAULT_VAULT_ROOT,
    LOCALRAG_MAIN_PYTHON as APPROVED_MAIN_PYTHON_STR,
    EXPORT_REVIEW_QUEUE_PATH,
    PIPELINE_REPORT_ROOT,
    PROCESSED_HISTORY_PATH,
    PROCESSOR_BACKEND,
    ZOTERO_DATA_DIR,
    ZOTERO_ATTACHMENT_BASE_DIR,
)


# Exit code emitted by gemini_analyze_pdf.py after writing a sub-agent
# manifest. The sub-agent is expected to fill the expected output file,
# then the orchestrator re-runs us; we treat 200 as "pending", not as
# success or failure, so batch totals stay honest.
SUBAGENT_PENDING_EXIT_CODE = 200


def _runs_dir() -> Path:
    """Where per-paper sub-agent run dirs live. Mirrors gemini_analyze_pdf.default_run_dir."""
    return PIPELINE_REPORT_ROOT / "runs"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# APPROVED_MAIN_PYTHON kept as Path for backwards compatibility with subprocess calls
APPROVED_MAIN_PYTHON = Path(APPROVED_MAIN_PYTHON_STR)
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
NON_RETRYABLE_ERROR_MARKERS = (
    "non_retryable_error[corrupt_pdf]",
    "non_retryable_error[oversize_pdf]",
    "non_retryable_error[missing_pdf]",
)


def is_non_retryable_error_text(text):
    normalized = (text or "").lower()
    return any(marker in normalized for marker in NON_RETRYABLE_ERROR_MARKERS)


from _hashing import (
    get_file_hash,
    normalize_pdf_group_paths,
    stable_combined_hash as get_stable_combined_hash,
    legacy_combined_hash as get_legacy_combined_hash,
    combined_hash_variants as get_combined_hash_variants,
)
from dedup_index import DedupIndex

# get_group_content_signature is identical to stable_combined_hash; kept as an
# alias for callers that historically used the "signature" framing.
get_group_content_signature = get_stable_combined_hash


def normalize_pdf_groups(groups):
    normalized_groups = []
    seen_signatures = set()

    for group in groups:
        normalized_group = normalize_pdf_group_paths(group)
        if not normalized_group:
            continue
        signature = get_group_content_signature(normalized_group)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        normalized_groups.append(normalized_group)

    return normalized_groups


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


def build_live_note_index(vault_root=DEFAULT_VAULT_ROOT):
    vault_root = Path(vault_root).resolve()
    index = {
        "generated_at": None,
        "vault_root": str(vault_root),
        "combined_hash": {},
        "zotero_parent_key": {},
    }
    for path in _iter_live_vault_note_paths(vault_root):
        payload = _read_note_frontmatter_mapping(path)
        if not payload:
            continue
        resolved = str(path.resolve())
        combined_hash = str(payload.get("combined_hash") or "").strip()
        zotero_parent_key = str(payload.get("zotero_parent_key") or "").strip()
        if combined_hash:
            index["combined_hash"].setdefault(combined_hash, [])
            if resolved not in index["combined_hash"][combined_hash]:
                index["combined_hash"][combined_hash].append(resolved)
        if zotero_parent_key:
            index["zotero_parent_key"].setdefault(zotero_parent_key, [])
            if resolved not in index["zotero_parent_key"][zotero_parent_key]:
                index["zotero_parent_key"][zotero_parent_key].append(resolved)
    return index


def write_live_note_index_file(index, directory=None):
    target_dir = Path(directory).resolve() if directory else Path(tempfile.gettempdir())
    target_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix="gemini-live-note-index-", suffix=".json", dir=str(target_dir))
    os.close(fd)
    path = Path(temp_path)
    payload = dict(index)
    payload["generated_at"] = payload.get("generated_at") or datetime.now().astimezone().isoformat()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def parse_batch_post_publish_actions(raw_value, publish_target):
    text = str(raw_value or "auto").strip().lower()
    if text in {"", "auto"}:
        return ["prefill", "review_queue"] if publish_target == "vault" else []
    if text == "none":
        return []

    normalized = []
    seen = set()
    aliases = {"tagging": "kimi_fallback"}
    for chunk in re.split(r"[,\s]+", text):
        token = chunk.strip().lower().replace("-", "_")
        if not token:
            continue
        if token == "none":
            return []
        token = aliases.get(token, token)
        if token not in seen:
            normalized.append(token)
            seen.add(token)
    return normalized


def serialize_post_publish_actions(actions):
    actions = [str(action).strip() for action in actions if str(action).strip()]
    if not actions:
        return "none"
    return ",".join(actions)


def split_batch_post_publish_actions(raw_value, publish_target, total_to_process):
    actions = parse_batch_post_publish_actions(raw_value, publish_target)
    if publish_target != "vault" or total_to_process <= 1:
        return serialize_post_publish_actions(actions), []

    per_item_actions = [action for action in actions if action != "review_queue"]
    batch_actions = ["review_queue"] if "review_queue" in actions else []
    return serialize_post_publish_actions(per_item_actions), batch_actions


def load_processed_hashes(history_path):
    history_path = Path(history_path)
    if not history_path.exists():
        return set()
    return {
        line.strip()
        for line in history_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def prefilter_pdf_groups(pdf_groups, processed_hashes=None, note_index=None, force=False, *, dedup_index=None):
    """Filter out PDF groups already covered by ledger or vault.

    The legacy parameters (`processed_hashes`, `note_index`) are still
    accepted so existing tests and external callers keep working. New
    callers should pass `dedup_index=` directly — the legacy params then
    feed an in-memory `DedupIndex` constructed without reading the
    ledger or scanning the vault.

    Returned `skipped` items carry `reason` ∈ {processed_history,
    live_note_index} for telemetry; the distinction is preserved by
    asking the dedup index for both ledger and vault state.
    """
    if force:
        return [normalize_pdf_group_paths(group) for group in pdf_groups], []

    if dedup_index is None:
        # Legacy path: build a lightweight DedupIndex from the supplied
        # parameters. No vault scan, no ledger I/O.
        dedup_index = DedupIndex.build(
            history_path=PROCESSED_HISTORY_PATH,
            vault_root=Path("__nonexistent_vault__"),
            cached_note_index=note_index,
        )
        # Inject any explicitly-supplied processed_hashes (overrides the
        # ledger read, for tests that want a controlled set).
        if processed_hashes is not None:
            dedup_index._ledger_hashes = set(processed_hashes)  # noqa: SLF001

    filtered = []
    skipped = []
    for group in pdf_groups:
        normalized_group = normalize_pdf_group_paths(group)
        variants = get_combined_hash_variants(normalized_group)
        hit = dedup_index.lookup(
            combined_hash=variants["combined_hash"],
            legacy_combined_hash=variants["legacy_combined_hash"],
        )
        if hit is None:
            filtered.append(normalized_group)
            continue

        _matched_hash, matched_path = hit
        skipped.append(
            {
                "group": normalized_group,
                "combined_hash": variants["combined_hash"],
                "legacy_combined_hash": variants["legacy_combined_hash"],
                "reason": "live_note_index" if matched_path is not None else "processed_history",
            }
        )

    return filtered, skipped


def run_batch_post_publish_actions(batch_actions, workspace_root=DEFAULT_VAULT_ROOT):
    if "review_queue" not in batch_actions:
        return []
    if not EXPORT_REVIEW_QUEUE_PATH.is_file():
        return [
            {
                "action": "review_queue",
                "status": "skipped",
                "reason": f"optional script not found: {EXPORT_REVIEW_QUEUE_PATH}",
            }
        ]
    completed = subprocess.run(
        [
            str(APPROVED_MAIN_PYTHON),
            str(EXPORT_REVIEW_QUEUE_PATH),
            "--root",
            str(Path(workspace_root).resolve()),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(Path(workspace_root).resolve()),
    )
    return [
        {
            "action": "review_queue",
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "status": "completed",
        }
    ]

def _zotero_is_running():
    """Best-effort check for a running Zotero process.

    Returns True only when a process is *confirmed* alive. Detection failure
    (e.g. tasklist/pgrep absent or restricted) returns False, on the
    principle that we don't want to falsely block users — the SQLite copy
    fallback below still handles the lock-error case if Zotero is open.
    """
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq zotero.exe"],
                capture_output=True, text=True, timeout=5,
            )
            return "zotero.exe" in result.stdout.lower()
        result = subprocess.run(
            ["pgrep", "-x", "zotero"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def get_zotero_pdf_groups(zotero_data_dir, manual_base_dir=None, since=None):
    """
    Returns a list of lists of absolute PDF paths.
    Each list represents one parent item (e.g. ['/path/to/main.pdf', '/path/to/si.pdf'])
    """
    db_path = os.path.join(zotero_data_dir, 'zotero.sqlite')
    if not os.path.exists(db_path):
        print(f"Error: Zotero database not found at {db_path}")
        return []

    # Refuse to run while Zotero is alive: prefs.js parsing below races
    # against Zotero's writer, and the SQLite copy may catch a transient
    # state. We'd rather error early than produce subtly-wrong attachment
    # paths or skip newly added papers.
    if _zotero_is_running():
        print(
            "❌ Zotero appears to be running. Close it before scanning — its "
            "prefs.js and zotero.sqlite are racy when the desktop app is alive.",
            file=sys.stderr,
        )
        return []

    # Try to find base attachment path from prefs.js across OSes.
    # Zotero's profile dir varies: %APPDATA%\Zotero\Zotero\Profiles on Windows,
    # ~/Library/Application Support/Zotero/Profiles on macOS,
    # ~/.zotero/zotero/Profiles on Linux (with snap variants).
    base_attachment_path = manual_base_dir
    profile_root_candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get('APPDATA')
        if appdata:
            profile_root_candidates.append(os.path.join(appdata, 'Zotero', 'Zotero', 'Profiles'))
    elif sys.platform == "darwin":
        profile_root_candidates.append(
            os.path.expanduser('~/Library/Application Support/Zotero/Profiles')
        )
    else:
        # Linux + others
        profile_root_candidates.append(os.path.expanduser('~/.zotero/zotero/Profiles'))
        profile_root_candidates.append(
            os.path.expanduser('~/snap/zotero-snap/common/.zotero/zotero/Profiles')
        )

    for profiles_dir in profile_root_candidates:
        if not os.path.exists(profiles_dir):
            continue
        for d in os.listdir(profiles_dir):
            prefs_path = os.path.join(profiles_dir, d, 'prefs.js')
            if not os.path.exists(prefs_path):
                continue
            try:
                with open(prefs_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        if 'extensions.zotero.baseAttachmentPath' in line:
                            m = re.search(
                                r'user_pref\("extensions.zotero.baseAttachmentPath",\s*"(.*?)"\);',
                                line,
                            )
                            if m:
                                # Use json.loads to un-escape both Windows
                                # backslash sequences AND any other escape
                                # the JSON serializer Zotero uses produced.
                                try:
                                    base_attachment_path = json.loads(f'"{m.group(1)}"')
                                except (json.JSONDecodeError, ValueError):
                                    # Fall back to ad-hoc unescape on parse failure
                                    base_attachment_path = m.group(1).replace('\\\\', '\\')
                                break
            except Exception:
                pass
            if base_attachment_path:
                break
        if base_attachment_path:
            break

    # Copy database to avoid locking issues if Zotero is open. Use tempfile
    # so we don't pollute the repo dir or fail on read-only installs.
    fd, temp_db_path = tempfile.mkstemp(prefix="zotero_scan_", suffix=".sqlite")
    os.close(fd)
    try:
        shutil.copy2(db_path, temp_db_path)
    except Exception as e:
        print(f"Error copying Zotero database: {e}")
        try:
            os.unlink(temp_db_path)
        except OSError:
            pass
        return []

    groups = {}
    try:
        conn = sqlite3.connect(temp_db_path)
        cursor = conn.cursor()
        
        # Query all PDF attachments
        since_clause = ""
        since_params = []
        if since:
            since_clause = "AND (parent.dateAdded >= ? OR attach_item.dateAdded >= ?)"
            since_params = [since, since]

        query = f'''
        SELECT 
            parent.itemID as parentID,
            attach_item.key as attachKey,
            attach.path as path
        FROM itemAttachments attach
        JOIN items attach_item ON attach.itemID = attach_item.itemID
        JOIN items parent ON attach.parentItemID = parent.itemID
        WHERE (attach.path LIKE '%.pdf' OR attach.contentType = 'application/pdf')
        {since_clause}
        ORDER BY parent.itemID ASC, attach_item.key ASC, attach.path ASC
        '''
        cursor.execute(query, since_params)
        rows = cursor.fetchall()
        
        for parent_id, attach_key, path in rows:
            if not path:
                continue
                
            # If path starts with 'storage:', it's stored in Zotero's storage repo
            if path.startswith('storage:'):
                filename = path.replace('storage:', '')
                abs_path = os.path.join(zotero_data_dir, 'storage', attach_key, filename)
            elif path.startswith('attachments:'):
                if base_attachment_path:
                    filename = path.replace('attachments:', '')
                    abs_path = os.path.join(base_attachment_path, filename)
                else:
                    continue
            else:
                # Absolute or relative linked file
                # In many cases, it's just absolute
                abs_path = path
                
            if os.path.exists(abs_path):
                if parent_id not in groups:
                    groups[parent_id] = []
                groups[parent_id].append(abs_path)
            
    finally:
        conn.close()
        if os.path.exists(temp_db_path):
            os.remove(temp_db_path)
            
    return normalize_pdf_groups(list(groups.values()))

def build_arg_parser():
    parser = argparse.ArgumentParser(description="Scan Zotero and run gemini_analyze_pdf.py on PDF attachments with Vertex AI.")
    parser.add_argument("--zotero-dir", default=str(ZOTERO_DATA_DIR), help="Path to Zotero data directory (containing zotero.sqlite). Override via $ZOTERO_DATA_DIR.")
    parser.add_argument("--base-dir", default=ZOTERO_ATTACHMENT_BASE_DIR or None, help="Base path for linked attachments (Zotero linked-file mode). Override via $ZOTERO_ATTACHMENT_BASE_DIR.")
    parser.add_argument("--limit", type=int, default=0, help="Max number of parent items to process (0 for unlimited)")
    parser.add_argument("--force", "-f", action="store_true", help="Force reprocessing even if hash is recorded")
    parser.add_argument("--out-dir", help="Additional directory to save all generated notes")
    parser.add_argument("--gcs-bucket", help="GCS bucket used for temporary Vertex AI PDF uploads (vertex backend only).")
    parser.add_argument(
        "--backend",
        default=PROCESSOR_BACKEND,
        choices=["vertex", "gemini-api", "anthropic", "openai", "subagent"],
        help=(
            "Processor backend forwarded to gemini_analyze_pdf.py. "
            "subagent (default) | vertex | gemini-api | anthropic | openai. "
            "The openai backend works with any OpenAI-compatible provider "
            "(DeepSeek, Mistral, OpenRouter, vLLM, etc.) via $OPENAI_BASE_URL. "
            "Override via $LOCALRAG_PROCESSOR_BACKEND."
        ),
    )
    parser.add_argument("--since", help="只处理此日期之后添加到 Zotero 的条目，格式：YYYY-MM-DD，如 2026-03-01")
    parser.add_argument(
        "--model-router",
        default="auto",
        choices=["auto", "off"],
        help="Model routing mode forwarded to gemini_analyze_pdf.py.",
    )
    parser.add_argument("--routing-policy", help="Path to a model routing policy JSON.")
    parser.add_argument("--model", help="Manual model override forwarded to gemini_analyze_pdf.py.")
    parser.add_argument("--flash-model", help="Optional Flash model override forwarded to gemini_analyze_pdf.py.")
    parser.add_argument("--pro-model", help="Optional Pro model override forwarded to gemini_analyze_pdf.py.")
    parser.add_argument(
        "--publish-target",
        default="vault",
        choices=["canary", "vault"],
        help="Where multifacet-spec writes notes before any downstream processing.",
    )
    parser.add_argument(
        "--post-publish",
        default="auto",
        help="Comma-separated multifacet post-publish actions forwarded to gemini_analyze_pdf.py.",
    )
    parser.add_argument(
        "--note-index-file",
        help="Optional JSON snapshot mapping {combined_hash,parent_key} to existing live note paths.",
    )
    return parser


def _subagent_run_dir_for_group(group):
    """Return the deterministic run_dir gemini_analyze_pdf would use for this group.

    Mirrors `default_run_dir(combined_hash)` in gemini_analyze_pdf.py without
    importing it (the import path is fragile under different cwd setups).
    """
    combined_hash = get_stable_combined_hash(list(group))
    return _runs_dir() / combined_hash


def build_analyze_command(group, args, analyze_script):
    cmd = [sys.executable, analyze_script] + list(group)
    if args.force:
        cmd.append("--force")
    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        cmd.extend(["--out-dir", args.out_dir])
    if args.gcs_bucket:
        cmd.extend(["--gcs-bucket", args.gcs_bucket])
    if args.backend:
        cmd.extend(["--backend", args.backend])
    if args.model_router:
        cmd.extend(["--model-router", args.model_router])
    if args.routing_policy:
        cmd.extend(["--routing-policy", args.routing_policy])
    if args.model:
        cmd.extend(["--model", args.model])
    if args.flash_model:
        cmd.extend(["--flash-model", args.flash_model])
    if args.pro_model:
        cmd.extend(["--pro-model", args.pro_model])
    if args.publish_target:
        cmd.extend(["--publish-target", args.publish_target])
    if args.post_publish:
        cmd.extend(["--post-publish", args.post_publish])
    if getattr(args, "note_index_file", None):
        cmd.extend(["--note-index-file", args.note_index_file])
    # Sub-agent backend: if a run dir already exists for this group, the
    # sub-agent has already been dispatched against an earlier manifest.
    # Auto-resume so the next pass advances to Stage B / final render
    # without the user having to manage paths by hand.
    # --force means "reprocess from scratch": clear any stale run dir so the
    # auto-resume doesn't silently reuse old stage outputs.
    if args.backend == "subagent":
        run_dir = _subagent_run_dir_for_group(group)
        if getattr(args, "force", False):
            if run_dir.exists():
                shutil.rmtree(run_dir, ignore_errors=True)
        elif run_dir.exists():
            cmd.extend(["--resume", str(run_dir)])
    return cmd


def main():
    parser = build_arg_parser()
    args = parser.parse_args()
    
    zotero_dir = args.zotero_dir
    print(f"Scanning Zotero database at: {zotero_dir}")
    print(f"Using base attachment directory: {args.base_dir}")
    
    pdf_groups = get_zotero_pdf_groups(zotero_dir, manual_base_dir=args.base_dir, since=args.since)
    print(f"Found {len(pdf_groups)} parent items with PDF attachments.")
    
    analyze_script = os.path.join(SCRIPT_DIR, 'gemini_analyze_pdf.py')
    
    if args.limit > 0:
        pdf_groups = pdf_groups[:args.limit]

    total_to_process = len(pdf_groups)
    per_item_post_publish, batch_post_publish_actions = split_batch_post_publish_actions(
        raw_value=args.post_publish,
        publish_target=args.publish_target,
        total_to_process=total_to_process,
    )
    args.post_publish = per_item_post_publish

    cleanup_paths = []
    note_index = None
    if not getattr(args, "note_index_file", None) and args.publish_target == "vault" and total_to_process > 0:
        note_index = build_live_note_index(DEFAULT_VAULT_ROOT)
        note_index_path = write_live_note_index_file(note_index)
        args.note_index_file = str(note_index_path)
        cleanup_paths.append(note_index_path)
    elif getattr(args, "note_index_file", None):
        try:
            payload = json.loads(Path(args.note_index_file).read_text(encoding="utf-8"))
            note_index = payload if isinstance(payload, dict) else None
        except Exception:
            note_index = None

    dedup_index = DedupIndex.build(
        history_path=PROCESSED_HISTORY_PATH,
        vault_root=DEFAULT_VAULT_ROOT if args.publish_target == "vault" else Path("__nonexistent_vault__"),
        cached_note_index=note_index,
    )
    pdf_groups, skipped_groups = prefilter_pdf_groups(
        pdf_groups,
        dedup_index=dedup_index,
        force=args.force,
    )
    if skipped_groups:
        reason_counts = {}
        for item in skipped_groups:
            reason_counts[item["reason"]] = reason_counts.get(item["reason"], 0) + 1
        summary = ", ".join(f"{reason}={count}" for reason, count in sorted(reason_counts.items()))
        print(f"Prefilter skipped {len(skipped_groups)} groups before subprocess launch ({summary}).")
    total_to_process = len(pdf_groups)
    
    pending_subagent_groups = []  # populated when --backend subagent emits a manifest

    def process_group(group, index, total, args, analyze_script):
        """Returns one of:
           True   — note generated successfully
           False  — failed (or non-retryable bad input)
           "pending" — sub-agent manifest was written; needs another pass
        """
        import time
        import random
        # Stagger start times slightly to avoid hitting an API exactly at
        # the same millisecond. Sub-agent mode makes no remote API calls,
        # so the stagger is pure latency — skip it.
        if args.backend != "subagent":
            time.sleep(random.uniform(0.5, 2.0))

        print(f"\nProcessing Group {index}/{total}:")
        for pdf in group:
            print(f"  - {os.path.basename(pdf)}")

        env = os.environ.copy()
        env['PYTHONUTF8'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'

        cmd = build_analyze_command(group, args, analyze_script)

        max_retries = 3
        for attempt in range(max_retries):
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                encoding='utf-8',
                env=env,
            )
            if completed.returncode == 0:
                print(f"  Result Group {index}: Success")
                return True
            if completed.returncode == SUBAGENT_PENDING_EXIT_CODE:
                # Sub-agent manifest was written; the next batch pass will
                # auto-resume this group. Surface the printed hint so the
                # parent agent can act on it without scraping logs.
                print(f"  Result Group {index}: Sub-agent manifest pending")
                if completed.stdout:
                    for line in completed.stdout.splitlines():
                        if line.strip():
                            print(f"    {line}")
                return "pending"
            err_text = (str(completed.stderr) + str(completed.stdout)).lower()
            if is_non_retryable_error_text(err_text):
                print(f"  Result Group {index}: Non-retryable input failure")
                print(completed.stderr)
                print(completed.stdout)
                return False
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 30 if any(
                    x in err_text for x in ['resource_exhausted', 'quota exceeded', '429']
                ) else (attempt + 1) * 10
                print(f"  [Group {index}] Error hit (exit {completed.returncode}). Retrying in {wait_time}s...")
                time.sleep(wait_time)
            else:
                print(f"  Result Group {index} Attempt {attempt+1}: Failed (exit {completed.returncode})")
                print(completed.stderr)
                print(completed.stdout)
                return False
    successful_groups = 0
    pending_groups = 0
    failed_groups = 0
    try:
        if total_to_process <= 5:
            print("Sequential mode (<= 5 items).")
            for i, group in enumerate(pdf_groups):
                outcome = process_group(group, i + 1, total_to_process, args, analyze_script)
                if outcome is True:
                    successful_groups += 1
                elif outcome == "pending":
                    pending_groups += 1
                    pending_subagent_groups.append(group)
                else:
                    failed_groups += 1
        else:
            print(f"Concurrent mode (> 5 items). Using ThreadPoolExecutor with 3 workers max.")
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                futures = {}
                for i, group in enumerate(pdf_groups):
                    fut = executor.submit(process_group, group, i + 1, total_to_process, args, analyze_script)
                    futures[fut] = group
                for future in concurrent.futures.as_completed(futures):
                    outcome = future.result()
                    if outcome is True:
                        successful_groups += 1
                    elif outcome == "pending":
                        pending_groups += 1
                        pending_subagent_groups.append(futures[future])
                    else:
                        failed_groups += 1
            print("Batch processing completed.")

        if successful_groups > 0 and batch_post_publish_actions:
            print(f"Running batch-end post-publish actions: {', '.join(batch_post_publish_actions)}")
            run_batch_post_publish_actions(batch_post_publish_actions, workspace_root=DEFAULT_VAULT_ROOT)

        if pending_groups:
            _print_pending_subagent_summary(pending_subagent_groups, args)
    finally:
        for path in cleanup_paths:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass

    # Exit code reflects what the parent agent should do next:
    #   0   → all done (or nothing to do)
    #   200 → some notes still need the sub-agent to fill manifests
    #   1   → at least one paper failed for non-pending reasons
    if failed_groups:
        sys.exit(1)
    if pending_groups:
        sys.exit(SUBAGENT_PENDING_EXIT_CODE)


def _print_pending_subagent_summary(pending_groups, args):
    """Tell the parent agent exactly what to do next, in a host-agnostic shape.

    ASCII-only output: this function runs through scanner stdout that may
    be captured by a parent agent's shell tool, sometimes on Windows
    `cmd.exe` with a legacy code page (CP936 / CP1252). Emoji and bullet
    glyphs render as `?` or `��` there, which confuses LLMs reading the
    transcript. Stick to ASCII.
    """
    print("")
    print("=" * 72)
    print(f"[PENDING] {len(pending_groups)} paper(s) waiting on sub-agent.")
    print("=" * 72)
    print("Each pending paper has a manifest at:")
    print("  <run_dir>/manifest-<stage>.json")
    print("")
    print("What the parent agent must do next:")
    print("  1. For each manifest below, dispatch a sub-agent with this task:")
    print("       Read the manifest at <manifest_path>. Read every PDF in")
    print("       pdf_paths. Apply system_prompt + user_prompt. Produce a")
    print("       JSON object that strictly conforms to response_schema.")
    print("       Write the result to expected_output_path. Do not re-invoke")
    print("       the scanner; do not touch any other files.")
    print("  2. After every sub-agent has written its expected_output_path,")
    print("     re-run THIS SAME batch command. The scanner will auto-resume.")
    print("  3. Repeat until pending count reaches 0 (each paper takes 3 passes).")
    print("")
    print("Pending manifests:")
    # Reuse discover_pending so this summary stays in sync with the helper.
    # Earlier ad-hoc code here used `sorted(run_dir.glob("manifest-*.json"))[-1]`
    # which falls into the same trap as helper v1: alphabetical order
    # ("profiler" > "note_generator") points at the wrong stage when both
    # manifests exist. The helper now reads manifest contents and prefers
    # the unfilled stage; we delegate to it.
    try:
        from list_pending_subagent_runs import discover_pending  # type: ignore
    except ImportError:
        discover_pending = None
    pending_run_dirs = {str(_subagent_run_dir_for_group(g)) for g in pending_groups}
    if discover_pending is not None:
        all_pending = discover_pending(_runs_dir())
        for entry in all_pending:
            if entry["run_dir"] not in pending_run_dirs:
                continue
            print(f"  - manifest:        {entry['manifest_path']}")
            print(f"    expected_output: {entry['expected_output_path']}")
    else:
        # Fallback (path issue prevented import). Best-effort: just list run dirs.
        for run_dir_str in sorted(pending_run_dirs):
            print(f"  - run_dir: {run_dir_str}")
    print("")
    print("Tip: `python scanner/list_pending_subagent_runs.py --json` returns the")
    print("     same data in a parsable form for non-Claude-Code hosts.")
    print("=" * 72)

if __name__ == '__main__':
    main()
