import os
import sys
import json
import argparse
from datetime import datetime, timedelta, timezone


DEFAULT_PREFIX = "pdf-inputs/"


def resolve_project_id():
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if project_id:
        return project_id
    return ""


def resolve_bucket_name(cli_bucket, project_id):
    bucket = (cli_bucket or os.environ.get("GEMINI_VERTEX_GCS_BUCKET", "")).strip()
    if bucket:
        return bucket
    if project_id:
        return f"{project_id}-gemini-literature-temp"
    return ""


def load_storage_client(project_id):
    try:
        from google.cloud import storage
    except ImportError:
        print(
            "❌ Missing dependency: google-cloud-storage. Install it with:\n"
            "   pip install google-cloud-storage"
        )
        sys.exit(1)

    return storage.Client(project=project_id)


def list_archive_groups(bucket, prefix):
    groups = {}
    for blob in bucket.list_blobs(prefix=prefix):
        suffix = blob.name[len(prefix):]
        if not suffix:
            continue
        hash_key = suffix.split("/", 1)[0]
        if not hash_key:
            continue
        group = groups.setdefault(
            hash_key,
            {
                "hash": hash_key,
                "blob_names": [],
                "object_count": 0,
                "total_bytes": 0,
                "latest_updated": None,
                "manifest": None,
            },
        )
        group["blob_names"].append(blob.name)
        group["object_count"] += 1
        group["total_bytes"] += int(blob.size or 0)
        updated = blob.updated
        if group["latest_updated"] is None or (updated and updated > group["latest_updated"]):
            group["latest_updated"] = updated
        if blob.name.endswith("/manifest.json"):
            try:
                group["manifest"] = json.loads(blob.download_as_text(encoding="utf-8"))
            except Exception:
                group["manifest"] = {"manifest_error": True, "blob_name": blob.name}
    return groups


def should_keep_group(group, args, now_utc):
    if args.hash and group["hash"] != args.hash:
        return False

    if args.days is not None:
        latest = group["latest_updated"]
        if latest is None:
            return False
        threshold = now_utc - timedelta(days=args.days)
        if latest.astimezone(timezone.utc) > threshold:
            return False

    return True


def print_group(group):
    latest = (
        group["latest_updated"].astimezone(timezone.utc).isoformat()
        if group["latest_updated"] is not None
        else "unknown"
    )
    print(f"- hash: {group['hash']}")
    print(f"  objects: {group['object_count']}")
    print(f"  bytes: {group['total_bytes']}")
    print(f"  latest_updated_utc: {latest}")
    manifest = group.get("manifest") or {}
    if manifest:
        print(f"  status: {manifest.get('status', 'unknown')}")
        print(f"  model: {manifest.get('model', 'unknown')}")
        if manifest.get("generated_note_name"):
            print(f"  generated_note_name: {manifest['generated_note_name']}")


def delete_group(bucket, group):
    for blob_name in group["blob_names"]:
        bucket.blob(blob_name).delete()


def main():
    parser = argparse.ArgumentParser(
        description="Preview or delete archived Vertex AI PDF inputs stored in GCS."
    )
    parser.add_argument("--bucket", help="GCS bucket used for archived Vertex AI PDF inputs")
    parser.add_argument("--hash", help="Delete or preview a single combined_hash group")
    parser.add_argument(
        "--days",
        type=int,
        help="Only include groups whose latest object update is older than N days",
    )
    parser.add_argument("--limit", type=int, default=0, help="Limit the number of groups shown or deleted")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help=f"GCS prefix to inspect (default: {DEFAULT_PREFIX})")
    parser.add_argument("--delete", action="store_true", help="Actually delete matching groups")
    args = parser.parse_args()

    project_id = resolve_project_id()
    if not project_id:
        print("❌ Error: GOOGLE_CLOUD_PROJECT is not set.")
        sys.exit(1)

    bucket_name = resolve_bucket_name(args.bucket, project_id)
    if not bucket_name:
        print("❌ Error: No GCS bucket configured. Set GEMINI_VERTEX_GCS_BUCKET or use --bucket.")
        sys.exit(1)

    storage_client = load_storage_client(project_id)
    bucket = storage_client.lookup_bucket(bucket_name)
    if bucket is None:
        print(f"❌ Error: Bucket not found: gs://{bucket_name}")
        sys.exit(1)

    now_utc = datetime.now(timezone.utc)
    groups = list_archive_groups(bucket, args.prefix)
    selected = [g for g in groups.values() if should_keep_group(g, args, now_utc)]
    selected.sort(
        key=lambda g: g["latest_updated"] or datetime.min.replace(tzinfo=timezone.utc)
    )

    if args.limit > 0:
        selected = selected[: args.limit]

    if not selected:
        print("No matching archive groups found.")
        return

    print(f"Bucket: gs://{bucket_name}")
    print(f"Prefix: {args.prefix}")
    print(f"Matched groups: {len(selected)}")
    for group in selected:
        print_group(group)

    if not args.delete:
        print("\nDry run only. Re-run with --delete to remove the groups above.")
        return

    for group in selected:
        delete_group(bucket, group)
        print(f"Deleted: {group['hash']}")


if __name__ == "__main__":
    main()
