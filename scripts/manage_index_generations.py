"""Offline maintenance for one immutable index-generation namespace.

Stop the LocalRAG query service before running status, rollback, or prune.
The tool never performs automatic cleanup and never scans collections outside
the explicitly selected logical name.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIR = REPO_ROOT / "service"
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from index_generation import GenerationStore, PublicationCommittedError  # noqa: E402


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Inspect or maintain one LocalRAG index generation offline.",
        epilog="Stop the query service before using this command.",
    )
    parser.add_argument("--chroma-path", required=True)
    parser.add_argument("--logical-name", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Show active and latest-attempt state.")
    subparsers.add_parser(
        "rollback",
        help="Validate and atomically activate the bound previous generation.",
    )
    subparsers.add_parser(
        "prune",
        help="Delete only obsolete generations owned by this logical name.",
    )
    recovery = subparsers.add_parser("recover", help="Recover a damaged/missing active pointer offline.")
    recovery.add_argument("--generation-id", required=True)
    recovery.add_argument("--manifest-fingerprint", required=True,
                          help="Previously trusted fingerprint of the selected complete manifest.")
    return parser.parse_args(argv)


def main(argv=None):
    try:
        args = _parse_args(argv)
        chroma_path = Path(args.chroma_path).expanduser().resolve()
        if not chroma_path.is_dir():
            raise ValueError(f"Chroma path is unavailable: {chroma_path}")

        import chromadb

        client = chromadb.PersistentClient(path=str(chroma_path))
        store = GenerationStore(chroma_path, args.logical_name)
        if args.command == "status":
            result = store.status(client)
        elif args.command == "rollback":
            from embedding_client import embedding_contract

            with store.writer_lock():
                result = {
                    "active": store.rollback(client, embedding_contract()),
                    "operation": "rollback",
                }
        elif args.command == "recover":
            from embedding_client import embedding_contract

            with store.writer_lock():
                result = {"active": store.recover(
                    client, args.generation_id, embedding_contract(),
                    expected_manifest_fingerprint=args.manifest_fingerprint), "operation": "recover"}
        else:
            with store.writer_lock():
                result = store.prune(client) | {"operation": "prune"}
        try:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        except (OSError, KeyboardInterrupt):
            if args.command in {"rollback", "recover"}:
                return 3  # The operation already returned after committing.
            raise
        return 0
    except PublicationCommittedError as exc:
        print(f"Generation maintenance committed with warning: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("Generation maintenance interrupted; inspect status before retrying.")
        return 130
    except Exception as exc:
        print(f"Generation maintenance failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
