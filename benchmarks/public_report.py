"""Allowlist-only conversion from internal retrieval hits to public records."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Iterable, Mapping


_PUBLIC_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class PublicReportError(ValueError):
    """Raised when an internal hit cannot be published safely."""


def _required_id(hit: Mapping[str, object], field: str) -> str:
    value = hit.get(field)
    if not isinstance(value, str) or not _PUBLIC_ID.fullmatch(value):
        raise PublicReportError(f"{field} is missing or is not a safe public id")
    return value


def _optional_id(hit: Mapping[str, object], field: str) -> str | None:
    value = hit.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not _PUBLIC_ID.fullmatch(value):
        raise PublicReportError(f"{field} is not a safe public id")
    return value


def _page_index(hit: Mapping[str, object]) -> int | None:
    value = hit.get("pdf_page_index")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PublicReportError("pdf_page_index must be a non-negative integer")
    return value


@dataclass(frozen=True)
class PublicHit:
    """The only retrieval-hit fields permitted in a public artifact."""

    paper_id: str
    file_id: str
    pdf_page_index: int | None
    evidence_id: str | None

    @classmethod
    def from_internal(cls, hit: Mapping[str, object]) -> "PublicHit":
        return cls(
            paper_id=_required_id(hit, "paper_id"),
            file_id=_required_id(hit, "file_id"),
            pdf_page_index=_page_index(hit),
            evidence_id=_optional_id(hit, "evidence_id"),
        )


def sanitize_hits(
    internal_hits: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Drop paths, source text, private queries, secrets, and model internals."""
    return [asdict(PublicHit.from_internal(hit)) for hit in internal_hits]
