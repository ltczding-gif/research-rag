"""
Zotero SQLite read helpers.

Single source of truth for the parent-item-key lookup used by both the
scanner (note generation) and the service (PDF chunk indexing). Zotero's
schema can change between versions; centralizing here means there's one
place to fix.

Two-strategy lookup (`get_parent_key`):

  1. **Storage-key extraction.** Zotero's default layout is
     ``<dataDir>/storage/<KEY>/<filename>`` where KEY is an 8-char
     alphanumeric attachment item id. When the PDF path matches this
     pattern, look up the parent item exactly via ``items.key``. No
     collision risk.

  2. **Filename LIKE fallback.** For linked-file attachments (where the
     path doesn't carry a Zotero key), fall back to a substring match
     on ``itemAttachments.path``. Approximate — same-named attachments
     under different parent items collide and the first match wins.

The fallback is the bug reported in earlier audits; the storage-key
strategy is the precise replacement. Both are kept because linked-file
mode users rely on the fallback.

Service-side note: ``service/build_pdf_db.py`` keeps its own near-copy
of this function (``get_parent_key_by_pdf_path``) because ``service/``
and ``scanner/`` are deployed independently. ``tests/test_zotero_client_parity.py``
asserts the two implementations produce identical results.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Optional, Union


_STORAGE_KEY_PATTERN = re.compile(r"[\\/]storage[\\/]([A-Za-z0-9]{8})[\\/]")


def get_parent_key(
    pdf_path: Union[str, Path],
    *,
    zotero_db: Union[str, Path],
) -> Optional[str]:
    """Look up the Zotero parent item key for a PDF path.

    Returns the parent item key, or None if no match was found, the DB
    couldn't be opened, or the path doesn't correspond to any registered
    attachment.

    Args:
        pdf_path: Filesystem path to the PDF (absolute or relative).
        zotero_db: Path to ``zotero.sqlite``. The DB is opened read-only
            from the caller's perspective (the function does not write).
    """
    try:
        conn = sqlite3.connect(str(zotero_db))
    except Exception:
        return None
    try:
        cursor = conn.cursor()

        # Strategy 1: exact attachment-key lookup from a storage path.
        m = _STORAGE_KEY_PATTERN.search(os.path.abspath(str(pdf_path)))
        if m:
            attach_key = m.group(1)
            cursor.execute(
                """
                SELECT i_parent.key
                FROM itemAttachments ia
                JOIN items i_attach ON ia.itemID = i_attach.itemID
                JOIN items i_parent ON ia.parentItemID = i_parent.itemID
                WHERE i_attach.key = ?
                LIMIT 1
                """,
                (attach_key,),
            )
            row = cursor.fetchone()
            if row:
                return row[0]

        # Strategy 2: filename LIKE fallback (approximate; for linked-file mode).
        filename = os.path.basename(str(pdf_path))
        cursor.execute(
            """
            SELECT i_parent.key
            FROM itemAttachments ia
            JOIN items i_attach ON ia.itemID = i_attach.itemID
            JOIN items i_parent ON ia.parentItemID = i_parent.itemID
            WHERE ia.path LIKE ?
            LIMIT 1
            """,
            (f"%{filename}%",),
        )
        row = cursor.fetchone()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_zotero_abstract_note(
    parent_key: Optional[str],
    *,
    zotero_db: Union[str, Path],
) -> str:
    """Fetch the ``abstractNote`` field for a Zotero parent item.

    Returns the abstract text stripped of leading/trailing whitespace,
    or an empty string if the parent has no abstract, the key is missing,
    or the DB couldn't be opened.

    Args:
        parent_key: Zotero item key (8-char alphanumeric). If None or
            empty, returns "".
        zotero_db: Path to ``zotero.sqlite``.
    """
    if not parent_key:
        return ""
    try:
        conn = sqlite3.connect(str(zotero_db))
    except Exception:
        return ""
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT v.value
            FROM items i
            JOIN itemData d ON i.itemID = d.itemID
            JOIN itemDataValues v ON d.valueID = v.valueID
            JOIN fields f ON d.fieldID = f.fieldID
            WHERE i.key = ? AND f.fieldName = 'abstractNote'
            LIMIT 1
            """,
            (parent_key,),
        )
        row = cursor.fetchone()
        if not row or not row[0]:
            return ""
        return str(row[0]).strip()
    except Exception:
        return ""
    finally:
        try:
            conn.close()
        except Exception:
            pass


__all__ = ["get_parent_key", "get_zotero_abstract_note"]
