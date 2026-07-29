"""ResearchQA source acquisition, manifests, and native-coordinate IR.

The module is deliberately independent of the overnight runner.  It provides
small, deterministic building blocks which can be used during source preflight
and by the note/chunking stages:

* normalize ResearchQA's dotted-bucket virtual-host S3 URLs to path style;
* download over verified HTTPS without exposing a certificate bypass;
* assign stable ``Main``/``SI-NN``/``AUX-NN`` file identities; and
* preserve physical PDF pages and native DOCX/XLSX/CSV coordinates.

DOCX and XLSX are parsed as OOXML packages with the standard library so the
offline benchmark tests do not require additional spreadsheet dependencies.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import ssl
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, ContextManager, Iterable, Mapping, Sequence
from xml.etree import ElementTree


SOURCE_SCHEMA_VERSION = 1
NATIVE_IR_SCHEMA_VERSION = 1

ROLE_BENCHMARK_PDF = "benchmark_pdf"
ROLE_BUNDLED_SUPPLEMENT = "bundled_supplement"
ROLE_EXTERNAL_SI = "external_si"
ROLE_AUXILIARY = "auxiliary_reporting_file"
SOURCE_ROLES = frozenset(
    {
        ROLE_BENCHMARK_PDF,
        ROLE_BUNDLED_SUPPLEMENT,
        ROLE_EXTERNAL_SI,
        ROLE_AUXILIARY,
    }
)

MEDIA_PDF = "application/pdf"
MEDIA_DOCX = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.document"
)
MEDIA_XLSX = (
    "application/vnd.openxmlformats-officedocument."
    "spreadsheetml.sheet"
)
MEDIA_CSV = "text/csv"
SUPPORTED_MEDIA_TYPES = frozenset(
    {MEDIA_PDF, MEDIA_DOCX, MEDIA_XLSX, MEDIA_CSV}
)

COORDINATE_PDF_PAGE = "pdf_page"
COORDINATE_DOCX_PARAGRAPH = "docx_paragraph"
COORDINATE_DOCX_TABLE = "docx_table"
COORDINATE_XLSX_CELLS = "xlsx_cells"
COORDINATE_CSV_ROWS_COLUMNS = "csv_rows_columns"

_MEDIA_BY_SUFFIX = {
    ".pdf": MEDIA_PDF,
    ".docx": MEDIA_DOCX,
    ".xlsx": MEDIA_XLSX,
    ".csv": MEDIA_CSV,
}
_COORDINATE_TYPE_BY_MEDIA = {
    MEDIA_PDF: COORDINATE_PDF_PAGE,
    MEDIA_DOCX: "docx_paragraph_table",
    MEDIA_XLSX: COORDINATE_XLSX_CELLS,
    MEDIA_CSV: COORDINATE_CSV_ROWS_COLUMNS,
}
_PARSER_CONFIG_BY_MEDIA: Mapping[str, Mapping[str, object]] = {
    MEDIA_PDF: {
        "extractor": "pdfplumber-page-text-flow",
        "use_text_flow": True,
        "x_tolerance": 1,
        "layout_structure": "unclassified",
        "normalization": "lf-newlines",
        "schema_version": 1,
    },
    MEDIA_DOCX: {
        "extractor": "stdlib-ooxml-docx",
        "paragraphs": "non-empty-body-paragraphs-1-based",
        "tables": "physical-table-row-column-ranges-1-based",
        "schema_version": NATIVE_IR_SCHEMA_VERSION,
    },
    MEDIA_XLSX: {
        "extractor": "stdlib-ooxml-xlsx",
        "sheets": "workbook-order-original-name",
        "units": "non-empty-physical-row-cell-ranges",
        "schema_version": NATIVE_IR_SCHEMA_VERSION,
    },
    MEDIA_CSV: {
        "extractor": "stdlib-csv",
        "encoding": "utf-8-sig",
        "rows": "physical-data-rows-1-based-after-header",
        "schema_version": NATIVE_IR_SCHEMA_VERSION,
    },
}

_S3_VIRTUAL_HOST = re.compile(
    r"^(?P<bucket>.+)\.s3[.-](?P<region>[a-z0-9-]+)\.amazonaws\.com$",
    re.IGNORECASE,
)
_S3_GLOBAL_VIRTUAL_HOST = re.compile(
    r"^(?P<bucket>.+)\.s3\.amazonaws\.com$",
    re.IGNORECASE,
)
_S3_PATH_HOST = re.compile(
    r"^s3[.-](?P<region>[a-z0-9-]+)\.amazonaws\.com$",
    re.IGNORECASE,
)
_CELL_REFERENCE = re.compile(r"^\$?([A-Za-z]+)\$?([1-9][0-9]*)$")

_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_SHEET_NAMESPACE = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_RELATIONSHIP_NAMESPACE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_PACKAGE_RELATIONSHIP_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)


class ResearchQASourceError(ValueError):
    """Raised when a source cannot be represented without losing provenance."""


class SourceDownloadError(ResearchQASourceError):
    """Raised when strict HTTPS acquisition or byte verification fails."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_text(value: str) -> str:
    return _hash_bytes(value.encode("utf-8"))


def _fingerprint(value: Mapping[str, object]) -> str:
    return _hash_text(_canonical_json(value))


def hash_source_file(path: str | Path) -> tuple[int, str]:
    """Return byte count and streaming SHA-256 for one source artifact."""

    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()


def normalize_researchqa_s3_url(url: str) -> str:
    """Normalize an HTTPS S3 URL to certificate-safe path style.

    ResearchQA uses a virtual-host URL whose bucket contains dots, for example
    ``assets.openpaper.ai.s3.us-east-1.amazonaws.com``.  AWS's wildcard
    certificate does not cover that hostname.  The equivalent path-style URL
    keeps TLS verification enabled:
    ``s3.us-east-1.amazonaws.com/assets.openpaper.ai/...``.

    Non-S3 HTTPS URLs are returned unchanged.  Plain HTTP, credentials in the
    authority, fragments, and malformed S3 paths fail closed.
    """

    if not isinstance(url, str) or not url.strip() or url != url.strip():
        raise ResearchQASourceError("source URL must be a non-empty trimmed string")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise ResearchQASourceError("source URL must use HTTPS")
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ResearchQASourceError("source URL must have a host and no credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ResearchQASourceError("source URL has an invalid port") from exc
    if port not in (None, 443):
        raise ResearchQASourceError("source URL must use the default HTTPS port")
    if parsed.fragment:
        raise ResearchQASourceError("source URL must not contain a fragment")

    hostname = parsed.hostname.lower().rstrip(".")
    virtual_match = _S3_VIRTUAL_HOST.fullmatch(hostname)
    global_match = _S3_GLOBAL_VIRTUAL_HOST.fullmatch(hostname)
    path_match = _S3_PATH_HOST.fullmatch(hostname)

    if virtual_match:
        bucket = virtual_match.group("bucket")
        region = virtual_match.group("region")
        path_host = f"s3.{region}.amazonaws.com"
        path = _prepend_bucket(parsed.path, bucket)
    elif global_match:
        bucket = global_match.group("bucket")
        path_host = "s3.amazonaws.com"
        path = _prepend_bucket(parsed.path, bucket)
    elif path_match:
        path_host = f"s3.{path_match.group('region')}.amazonaws.com"
        path = _require_s3_path(parsed.path)
    elif hostname == "s3.amazonaws.com":
        path_host = hostname
        path = _require_s3_path(parsed.path)
    else:
        return urllib.parse.urlunsplit(
            ("https", parsed.netloc, parsed.path, parsed.query, "")
        )

    return urllib.parse.urlunsplit(
        ("https", path_host, path, parsed.query, "")
    )


def _prepend_bucket(path: str, bucket: str) -> str:
    if not bucket or "/" in bucket:
        raise ResearchQASourceError("S3 virtual-host URL has an invalid bucket")
    key = path.lstrip("/")
    if not key:
        raise ResearchQASourceError("S3 URL must identify an object")
    return f"/{bucket}/{key}"


def _require_s3_path(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        raise ResearchQASourceError(
            "path-style S3 URL must identify a bucket and object"
        )
    return "/" + "/".join(parts)


class _StrictHTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            newurl,
        )
        if redirected is None:
            return None
        if urllib.parse.urlsplit(redirected.full_url).scheme.lower() != "https":
            raise SourceDownloadError("HTTPS redirect attempted to downgrade to HTTP")
        return redirected


OpenURL = Callable[
    [urllib.request.Request, float, ssl.SSLContext],
    ContextManager[object],
]


def _open_strict_https(
    request: urllib.request.Request,
    timeout: float,
    context: ssl.SSLContext,
) -> ContextManager[object]:
    opener = urllib.request.build_opener(
        _StrictHTTPSRedirectHandler(),
        urllib.request.HTTPSHandler(context=context),
    )
    return opener.open(request, timeout=timeout)


@dataclass(frozen=True)
class DownloadReceipt:
    """Observed provenance from one completed strict-TLS download."""

    source_url: str
    download_url: str
    final_url: str
    destination: str
    bytes: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def strict_tls_download(
    source_url: str,
    destination: str | Path,
    *,
    expected_sha256: str | None = None,
    expected_bytes: int | None = None,
    timeout: float = 120.0,
    open_url: OpenURL = _open_strict_https,
) -> DownloadReceipt:
    """Atomically download one source through a verified HTTPS context.

    The SSL context is always created internally with the platform trust store,
    hostname checking, and ``CERT_REQUIRED``.  Callers cannot pass a relaxed
    context.  ``open_url`` exists only as a narrow test/transport seam and
    receives that strict context.
    """

    download_url = normalize_researchqa_s3_url(source_url)
    if expected_sha256 is not None and not re.fullmatch(
        r"[a-f0-9]{64}",
        expected_sha256,
    ):
        raise SourceDownloadError("expected_sha256 must be a lowercase SHA-256")
    if expected_bytes is not None and expected_bytes < 0:
        raise SourceDownloadError("expected_bytes must be non-negative")
    if timeout <= 0:
        raise SourceDownloadError("timeout must be greater than zero")

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    partial.unlink(missing_ok=True)
    context = ssl.create_default_context()
    if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
        raise SourceDownloadError("strict TLS context is not enforcing verification")

    request = urllib.request.Request(
        download_url,
        headers={"User-Agent": "research-rag-researchqa/1"},
    )
    digest = hashlib.sha256()
    total = 0
    final_url = download_url
    try:
        with open_url(request, timeout, context) as response:
            response_url = getattr(response, "geturl", lambda: download_url)()
            final_url = str(response_url)
            if urllib.parse.urlsplit(final_url).scheme.lower() != "https":
                raise SourceDownloadError(
                    "source response resolved to a non-HTTPS URL"
                )
            with partial.open("wb") as handle:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    if not isinstance(block, bytes):
                        raise SourceDownloadError(
                            "download transport returned non-byte content"
                        )
                    handle.write(block)
                    digest.update(block)
                    total += len(block)
        observed_sha256 = digest.hexdigest()
        if expected_bytes is not None and total != expected_bytes:
            raise SourceDownloadError(
                f"source byte-size mismatch: expected {expected_bytes}, found {total}"
            )
        if expected_sha256 is not None and observed_sha256 != expected_sha256:
            raise SourceDownloadError(
                "source SHA-256 mismatch: "
                f"expected {expected_sha256}, found {observed_sha256}"
            )
        partial.replace(target)
    except (OSError, urllib.error.URLError) as exc:
        raise SourceDownloadError(f"strict HTTPS download failed: {exc}") from exc
    finally:
        partial.unlink(missing_ok=True)

    return DownloadReceipt(
        source_url=source_url,
        download_url=download_url,
        final_url=final_url,
        destination=str(target.resolve()),
        bytes=total,
        sha256=digest.hexdigest(),
    )


@dataclass(frozen=True)
class SourceArtifact:
    """One local artifact proposed for a paper's source manifest."""

    path: str | Path
    source_role: str
    source_url: str
    original_filename: str | None = None
    media_type: str | None = None
    acquisition_status: str = "verified"


@dataclass(frozen=True)
class SourceRecord:
    """Hash-bound, citation-ready source manifest entry."""

    schema_version: int
    paper_id: str
    file_id: str
    source_role: str
    media_type: str
    original_filename: str
    source_url: str
    sha256: str
    bytes: int
    parser_fingerprint: str
    citation_coordinate_type: str
    acquisition_status: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def parser_fingerprint_for(media_type: str) -> str:
    """Return the versioned deterministic parser fingerprint for one medium."""

    try:
        config = _PARSER_CONFIG_BY_MEDIA[media_type]
    except KeyError as exc:
        raise ResearchQASourceError(
            f"unsupported source media type: {media_type}"
        ) from exc
    return _fingerprint(config)


def infer_media_type(path: str | Path, declared: str | None = None) -> str:
    """Resolve a supported media type from an explicit value or file suffix."""

    if declared is not None and declared != "application/octet-stream":
        if declared not in SUPPORTED_MEDIA_TYPES:
            raise ResearchQASourceError(
                f"unsupported source media type: {declared}"
            )
        return declared
    suffix = Path(path).suffix.lower()
    try:
        return _MEDIA_BY_SUFFIX[suffix]
    except KeyError as exc:
        raise ResearchQASourceError(
            f"cannot infer supported media type from {Path(path).name!r}"
        ) from exc


def _normalized_filename(filename: str) -> str:
    normalized = unicodedata.normalize("NFKC", filename).casefold()
    return " ".join(normalized.split())


@dataclass(frozen=True)
class _PreparedArtifact:
    artifact: SourceArtifact
    path: Path
    original_filename: str
    media_type: str
    size: int
    sha256: str

    @property
    def stable_key(self) -> tuple[str, str, str, str]:
        return (
            _normalized_filename(self.original_filename),
            self.sha256,
            self.artifact.source_url,
            self.artifact.source_role,
        )


def build_source_manifest(
    paper_id: str,
    artifacts: Iterable[SourceArtifact],
) -> tuple[SourceRecord, ...]:
    """Hash, sort, and assign stable file IDs for one paper source set."""

    if not isinstance(paper_id, str) or not paper_id.strip():
        raise ResearchQASourceError("paper_id must be a non-empty string")
    prepared: list[_PreparedArtifact] = []
    for artifact in artifacts:
        if artifact.source_role not in SOURCE_ROLES:
            raise ResearchQASourceError(
                f"unsupported source role: {artifact.source_role}"
            )
        normalize_researchqa_s3_url(artifact.source_url)
        path = Path(artifact.path)
        if not path.is_file():
            raise ResearchQASourceError(f"source artifact does not exist: {path}")
        original_filename = artifact.original_filename or path.name
        if (
            not original_filename
            or Path(original_filename).name != original_filename
        ):
            raise ResearchQASourceError(
                "original_filename must be a non-empty base filename"
            )
        size, digest = hash_source_file(path)
        prepared.append(
            _PreparedArtifact(
                artifact=artifact,
                path=path,
                original_filename=original_filename,
                media_type=infer_media_type(path, artifact.media_type),
                size=size,
                sha256=digest,
            )
        )

    mains = [
        item
        for item in prepared
        if item.artifact.source_role == ROLE_BENCHMARK_PDF
    ]
    if len(mains) != 1:
        raise ResearchQASourceError(
            "source manifest requires exactly one benchmark_pdf"
        )
    if mains[0].media_type != MEDIA_PDF:
        raise ResearchQASourceError("benchmark_pdf must use application/pdf")

    scientific_si = sorted(
        (
            item
            for item in prepared
            if item.artifact.source_role
            in {ROLE_BUNDLED_SUPPLEMENT, ROLE_EXTERNAL_SI}
        ),
        key=lambda item: item.stable_key,
    )
    auxiliary = sorted(
        (
            item
            for item in prepared
            if item.artifact.source_role == ROLE_AUXILIARY
        ),
        key=lambda item: item.stable_key,
    )
    duplicate_keys = [
        key
        for key in {
            item.stable_key for item in prepared
        }
        if sum(item.stable_key == key for item in prepared) > 1
    ]
    if duplicate_keys:
        raise ResearchQASourceError(
            "source manifest contains indistinguishable duplicate artifacts"
        )

    assigned_ids: dict[_PreparedArtifact, str] = {mains[0]: "Main"}
    assigned_ids.update(
        {
            item: f"SI-{index:02d}"
            for index, item in enumerate(scientific_si, 1)
        }
    )
    assigned_ids.update(
        {
            item: f"AUX-{index:02d}"
            for index, item in enumerate(auxiliary, 1)
        }
    )

    ordered = sorted(
        prepared,
        key=lambda item: _file_id_sort_key(assigned_ids[item]),
    )
    return tuple(
        SourceRecord(
            schema_version=SOURCE_SCHEMA_VERSION,
            paper_id=paper_id,
            file_id=assigned_ids[item],
            source_role=item.artifact.source_role,
            media_type=item.media_type,
            original_filename=item.original_filename,
            source_url=normalize_researchqa_s3_url(
                item.artifact.source_url
            ),
            sha256=item.sha256,
            bytes=item.size,
            parser_fingerprint=parser_fingerprint_for(item.media_type),
            citation_coordinate_type=_COORDINATE_TYPE_BY_MEDIA[
                item.media_type
            ],
            acquisition_status=item.artifact.acquisition_status,
        )
        for item in ordered
    )


def _file_id_sort_key(file_id: str) -> tuple[int, int, str]:
    if file_id == "Main":
        return 0, 0, file_id
    prefix, separator, number = file_id.partition("-")
    if separator and number.isdigit():
        return (1 if prefix == "SI" else 2), int(number), file_id
    return 3, 0, file_id


def source_record_sort_key(
    source: SourceRecord,
) -> tuple[str, int, int, str]:
    """Public deterministic order for records spanning multiple papers."""

    file_group, file_number, file_id = _file_id_sort_key(source.file_id)
    return source.paper_id, file_group, file_number, file_id


@dataclass(frozen=True)
class NativeCoordinate:
    """A typed coordinate in the original source medium."""

    coordinate_type: str
    page: int | None = None
    paragraph: int | None = None
    table: int | None = None
    row_start: int | None = None
    row_end: int | None = None
    col_start: str | None = None
    col_end: str | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        coordinate_fields = {
            "page": self.page,
            "paragraph": self.paragraph,
            "table": self.table,
            "row_start": self.row_start,
            "row_end": self.row_end,
            "col_start": self.col_start,
            "col_end": self.col_end,
            "sheet_name": self.sheet_name,
            "cell_range": self.cell_range,
            "columns": self.columns,
        }
        required_by_type = {
            COORDINATE_PDF_PAGE: {"page"},
            COORDINATE_DOCX_PARAGRAPH: {"paragraph"},
            COORDINATE_DOCX_TABLE: {
                "table",
                "row_start",
                "row_end",
                "col_start",
                "col_end",
            },
            COORDINATE_XLSX_CELLS: {"sheet_name", "cell_range"},
            COORDINATE_CSV_ROWS_COLUMNS: {
                "row_start",
                "row_end",
                "columns",
            },
        }
        try:
            required = required_by_type[self.coordinate_type]
        except KeyError as exc:
            raise ResearchQASourceError(
                f"unsupported native coordinate: {self.coordinate_type}"
            ) from exc
        populated = {
            name
            for name, value in coordinate_fields.items()
            if value is not None and value != ()
        }
        if populated != required:
            raise ResearchQASourceError(
                f"{self.coordinate_type} coordinate fields must be "
                f"{sorted(required)}"
            )
        for field_name in ("page", "paragraph", "table", "row_start", "row_end"):
            value = coordinate_fields[field_name]
            if value is not None and (not isinstance(value, int) or value < 1):
                raise ResearchQASourceError(
                    f"{field_name} must be a positive integer"
                )
        if (
            self.row_start is not None
            and self.row_end is not None
            and self.row_end < self.row_start
        ):
            raise ResearchQASourceError("coordinate row range is reversed")
        for field_name in ("col_start", "col_end"):
            value = coordinate_fields[field_name]
            if value is not None and not re.fullmatch(r"[A-Z]+", str(value)):
                raise ResearchQASourceError(
                    f"{field_name} must be an uppercase spreadsheet column"
                )
        if (
            self.col_start is not None
            and self.col_end is not None
            and _column_number(self.col_end) < _column_number(self.col_start)
        ):
            raise ResearchQASourceError("coordinate column range is reversed")
        if self.sheet_name is not None and not self.sheet_name:
            raise ResearchQASourceError("sheet_name must be non-empty")
        if self.cell_range is not None and not re.fullmatch(
            r"[A-Z]+[1-9][0-9]*(?::[A-Z]+[1-9][0-9]*)?",
            self.cell_range,
        ):
            raise ResearchQASourceError("cell_range is invalid")
        if self.columns and (
            any(not column for column in self.columns)
            or len(self.columns) != len(set(self.columns))
        ):
            raise ResearchQASourceError(
                "CSV coordinate columns must be non-empty and unique"
            )

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["columns"] = list(self.columns)
        return {
            key: field_value
            for key, field_value in value.items()
            if field_value is not None and field_value != []
        }

    def render(self, file_id: str) -> str:
        if self.coordinate_type == COORDINATE_PDF_PAGE:
            return f"[{file_id} p.{self.page}]"
        if self.coordinate_type == COORDINATE_DOCX_PARAGRAPH:
            return f"[{file_id} para.{self.paragraph}]"
        if self.coordinate_type == COORDINATE_DOCX_TABLE:
            return (
                f"[{file_id} table.{self.table} "
                f"rows.{self.row_start}-{self.row_end} "
                f"cols.{self.col_start}-{self.col_end}]"
            )
        if self.coordinate_type == COORDINATE_XLSX_CELLS:
            sheet = json.dumps(self.sheet_name, ensure_ascii=False)
            return f"[{file_id} sheet.{sheet} cells.{self.cell_range}]"
        if self.coordinate_type == COORDINATE_CSV_ROWS_COLUMNS:
            return (
                f"[{file_id} rows.{self.row_start}-{self.row_end} "
                f"cols.{','.join(self.columns)}]"
            )
        raise ResearchQASourceError(
            f"unsupported native coordinate: {self.coordinate_type}"
        )


@dataclass(frozen=True)
class NativeIRUnit:
    """One source-native, hash-bound retrieval/note input unit."""

    schema_version: int
    unit_id: str
    paper_id: str
    file_id: str
    source_role: str
    media_type: str
    source_sha256: str
    parser_fingerprint: str
    ordinal: int
    coordinate: NativeCoordinate
    citation: str
    text: str
    text_sha256: str

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["coordinate"] = self.coordinate.to_dict()
        return value


def _make_native_unit(
    source: SourceRecord,
    *,
    ordinal: int,
    coordinate: NativeCoordinate,
    text: str,
) -> NativeIRUnit:
    normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")
    text_sha256 = _hash_text(normalized_text)
    identity = {
        "schema_version": NATIVE_IR_SCHEMA_VERSION,
        "source_sha256": source.sha256,
        "parser_fingerprint": source.parser_fingerprint,
        "coordinate": coordinate.to_dict(),
        "text_sha256": text_sha256,
    }
    return NativeIRUnit(
        schema_version=NATIVE_IR_SCHEMA_VERSION,
        unit_id=f"native-{_fingerprint(identity)}",
        paper_id=source.paper_id,
        file_id=source.file_id,
        source_role=source.source_role,
        media_type=source.media_type,
        source_sha256=source.sha256,
        parser_fingerprint=source.parser_fingerprint,
        ordinal=ordinal,
        coordinate=coordinate,
        citation=coordinate.render(source.file_id),
        text=normalized_text,
        text_sha256=text_sha256,
    )


PDFExtractor = Callable[..., object]


def extract_native_ir(
    source: SourceRecord,
    path: str | Path,
    *,
    pdf_extractor: PDFExtractor | None = None,
) -> tuple[NativeIRUnit, ...]:
    """Extract a verified manifest source into native-coordinate units."""

    source_path = Path(path)
    size, digest = hash_source_file(source_path)
    if size != source.bytes or digest != source.sha256:
        raise ResearchQASourceError(
            f"source bytes do not match manifest for {source.file_id}"
        )
    if parser_fingerprint_for(source.media_type) != source.parser_fingerprint:
        raise ResearchQASourceError(
            f"parser fingerprint mismatch for {source.file_id}"
        )

    if source.media_type == MEDIA_PDF:
        return _extract_pdf_ir(source, source_path, pdf_extractor)
    if source.media_type == MEDIA_DOCX:
        return _extract_docx_ir(source, source_path)
    if source.media_type == MEDIA_XLSX:
        return _extract_xlsx_ir(source, source_path)
    if source.media_type == MEDIA_CSV:
        return _extract_csv_ir(source, source_path)
    raise ResearchQASourceError(
        f"unsupported source media type: {source.media_type}"
    )


def extract_native_corpus(
    sources: Iterable[tuple[SourceRecord, str | Path]],
    *,
    pdf_extractor: PDFExtractor | None = None,
) -> tuple[NativeIRUnit, ...]:
    """Extract and deterministically order native units across sources."""

    ordered_sources = sorted(
        sources,
        key=lambda item: source_record_sort_key(item[0]),
    )
    units = [
        unit
        for source, path in ordered_sources
        for unit in extract_native_ir(
            source,
            path,
            pdf_extractor=pdf_extractor,
        )
    ]
    return tuple(
        sorted(
            units,
            key=lambda unit: (
                unit.paper_id,
                _file_id_sort_key(unit.file_id),
                unit.ordinal,
                unit.unit_id,
            ),
        )
    )


def _extract_pdf_ir(
    source: SourceRecord,
    path: Path,
    pdf_extractor: PDFExtractor | None,
) -> tuple[NativeIRUnit, ...]:
    if pdf_extractor is None:
        from service.pdf_ir import extract_pdf_document

        pdf_extractor = extract_pdf_document
    document = pdf_extractor(
        path,
        paper_id=source.paper_id,
        file_id=source.file_id,
        expected_file_hash=source.sha256,
    )
    pages = getattr(document, "pages", None)
    if pages is None:
        raise ResearchQASourceError("PDF extractor did not return document pages")
    return tuple(
        _make_native_unit(
            source,
            ordinal=page_index,
            coordinate=NativeCoordinate(
                coordinate_type=COORDINATE_PDF_PAGE,
                page=page_index,
            ),
            text=str(getattr(page, "normalized_text")),
        )
        for page_index, page in enumerate(pages, 1)
    )


def _extract_docx_ir(
    source: SourceRecord,
    path: Path,
) -> tuple[NativeIRUnit, ...]:
    try:
        with zipfile.ZipFile(path) as package:
            document_xml = package.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ResearchQASourceError(f"invalid DOCX source: {path.name}") from exc

    root = ElementTree.fromstring(document_xml)
    body = root.find(f"{{{_WORD_NAMESPACE}}}body")
    if body is None:
        raise ResearchQASourceError("DOCX source has no document body")

    paragraph_number = 0
    table_number = 0
    units: list[NativeIRUnit] = []
    for child in body:
        if child.tag == f"{{{_WORD_NAMESPACE}}}p":
            text = _word_text(child).strip()
            if not text:
                continue
            paragraph_number += 1
            units.append(
                _make_native_unit(
                    source,
                    ordinal=len(units) + 1,
                    coordinate=NativeCoordinate(
                        coordinate_type=COORDINATE_DOCX_PARAGRAPH,
                        paragraph=paragraph_number,
                    ),
                    text=text,
                )
            )
        elif child.tag == f"{{{_WORD_NAMESPACE}}}tbl":
            table_number += 1
            table_unit = _docx_table_unit(
                source,
                child,
                table_number=table_number,
                ordinal=len(units) + 1,
            )
            if table_unit is not None:
                units.append(table_unit)
    return tuple(units)


def _word_text(element: ElementTree.Element) -> str:
    parts: list[str] = []
    for node in element.iter():
        if node.tag == f"{{{_WORD_NAMESPACE}}}t":
            parts.append(node.text or "")
        elif node.tag == f"{{{_WORD_NAMESPACE}}}tab":
            parts.append("\t")
        elif node.tag in {
            f"{{{_WORD_NAMESPACE}}}br",
            f"{{{_WORD_NAMESPACE}}}cr",
        }:
            parts.append("\n")
    return "".join(parts)


def _docx_table_unit(
    source: SourceRecord,
    table: ElementTree.Element,
    *,
    table_number: int,
    ordinal: int,
) -> NativeIRUnit | None:
    rows = table.findall(f"{{{_WORD_NAMESPACE}}}tr")
    occupied: list[tuple[int, int, str]] = []
    for row_number, row in enumerate(rows, 1):
        cells = row.findall(f"{{{_WORD_NAMESPACE}}}tc")
        for column_number, cell in enumerate(cells, 1):
            text = _word_text(cell).strip()
            if text:
                occupied.append((row_number, column_number, text))
    if not occupied:
        return None

    row_start = min(row for row, _, _ in occupied)
    row_end = max(row for row, _, _ in occupied)
    column_start = min(column for _, column, _ in occupied)
    column_end = max(column for _, column, _ in occupied)
    rendered_rows = []
    for row_number in range(row_start, row_end + 1):
        rendered_cells = [
            f"{_column_name(column)}{row_number}={text}"
            for row, column, text in occupied
            if row == row_number
        ]
        if rendered_cells:
            rendered_rows.append("\t".join(rendered_cells))
    return _make_native_unit(
        source,
        ordinal=ordinal,
        coordinate=NativeCoordinate(
            coordinate_type=COORDINATE_DOCX_TABLE,
            table=table_number,
            row_start=row_start,
            row_end=row_end,
            col_start=_column_name(column_start),
            col_end=_column_name(column_end),
        ),
        text="\n".join(rendered_rows),
    )


def _extract_xlsx_ir(
    source: SourceRecord,
    path: Path,
) -> tuple[NativeIRUnit, ...]:
    try:
        with zipfile.ZipFile(path) as package:
            workbook = ElementTree.fromstring(package.read("xl/workbook.xml"))
            relationships = ElementTree.fromstring(
                package.read("xl/_rels/workbook.xml.rels")
            )
            shared_strings = _xlsx_shared_strings(package)
            relationship_targets = {
                relationship.attrib["Id"]: relationship.attrib["Target"]
                for relationship in relationships.findall(
                    f"{{{_PACKAGE_RELATIONSHIP_NAMESPACE}}}Relationship"
                )
            }
            sheet_specs = []
            sheets = workbook.find(f"{{{_SHEET_NAMESPACE}}}sheets")
            if sheets is None:
                raise ResearchQASourceError("XLSX workbook has no sheets")
            for sheet in sheets:
                sheet_name = sheet.attrib.get("name")
                relationship_id = sheet.attrib.get(
                    f"{{{_RELATIONSHIP_NAMESPACE}}}id"
                )
                if not sheet_name or relationship_id not in relationship_targets:
                    raise ResearchQASourceError(
                        "XLSX workbook has an invalid sheet relationship"
                    )
                target = _xlsx_target_path(
                    relationship_targets[relationship_id]
                )
                sheet_specs.append((sheet_name, package.read(target)))
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ResearchQASourceError(f"invalid XLSX source: {path.name}") from exc

    units: list[NativeIRUnit] = []
    for sheet_name, worksheet_xml in sheet_specs:
        worksheet = ElementTree.fromstring(worksheet_xml)
        sheet_data = worksheet.find(f"{{{_SHEET_NAMESPACE}}}sheetData")
        if sheet_data is None:
            continue
        for inferred_row, row in enumerate(
            sheet_data.findall(f"{{{_SHEET_NAMESPACE}}}row"),
            1,
        ):
            row_number = int(row.attrib.get("r", inferred_row))
            cells = []
            for cell in row.findall(f"{{{_SHEET_NAMESPACE}}}c"):
                reference = cell.attrib.get("r")
                if not reference:
                    raise ResearchQASourceError(
                        "XLSX cell is missing its native reference"
                    )
                value = _xlsx_cell_value(cell, shared_strings)
                if value != "":
                    cells.append((reference, value))
            if not cells:
                continue
            cells.sort(key=lambda item: _cell_sort_key(item[0]))
            start_reference = cells[0][0].replace("$", "").upper()
            end_reference = cells[-1][0].replace("$", "").upper()
            units.append(
                _make_native_unit(
                    source,
                    ordinal=len(units) + 1,
                    coordinate=NativeCoordinate(
                        coordinate_type=COORDINATE_XLSX_CELLS,
                        sheet_name=sheet_name,
                        cell_range=(
                            start_reference
                            if start_reference == end_reference
                            else f"{start_reference}:{end_reference}"
                        ),
                    ),
                    text="\t".join(
                        f"{reference.replace('$', '').upper()}={value}"
                        for reference, value in cells
                    ),
                )
            )
    return tuple(units)


def _xlsx_target_path(target: str) -> str:
    pure_target = PurePosixPath(target.lstrip("/"))
    if any(part == ".." for part in pure_target.parts):
        raise ResearchQASourceError("XLSX sheet target escapes its package")
    if pure_target.parts and pure_target.parts[0] == "xl":
        return pure_target.as_posix()
    return (PurePosixPath("xl") / pure_target).as_posix()


def _xlsx_shared_strings(package: zipfile.ZipFile) -> tuple[str, ...]:
    try:
        raw = package.read("xl/sharedStrings.xml")
    except KeyError:
        return ()
    root = ElementTree.fromstring(raw)
    return tuple(
        "".join(
            text.text or ""
            for text in item.iter(f"{{{_SHEET_NAMESPACE}}}t")
        )
        for item in root.findall(f"{{{_SHEET_NAMESPACE}}}si")
    )


def _xlsx_cell_value(
    cell: ElementTree.Element,
    shared_strings: Sequence[str],
) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        inline = cell.find(f"{{{_SHEET_NAMESPACE}}}is")
        if inline is None:
            return ""
        return "".join(
            text.text or ""
            for text in inline.iter(f"{{{_SHEET_NAMESPACE}}}t")
        )
    value_node = cell.find(f"{{{_SHEET_NAMESPACE}}}v")
    raw_value = value_node.text if value_node is not None else None
    if cell_type == "s" and raw_value is not None:
        try:
            return shared_strings[int(raw_value)]
        except (IndexError, ValueError) as exc:
            raise ResearchQASourceError(
                "XLSX cell has an invalid shared-string index"
            ) from exc
    if cell_type == "b" and raw_value is not None:
        return "TRUE" if raw_value == "1" else "FALSE"
    if raw_value is not None:
        return raw_value
    formula = cell.find(f"{{{_SHEET_NAMESPACE}}}f")
    if formula is not None and formula.text:
        return f"={formula.text}"
    return ""


def _cell_sort_key(reference: str) -> tuple[int, int]:
    match = _CELL_REFERENCE.fullmatch(reference)
    if match is None:
        raise ResearchQASourceError(
            f"invalid XLSX cell reference: {reference!r}"
        )
    return int(match.group(2)), _column_number(match.group(1))


def _extract_csv_ir(
    source: SourceRecord,
    path: Path,
) -> tuple[NativeIRUnit, ...]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ResearchQASourceError(
            "CSV source must be valid UTF-8 or UTF-8 with BOM"
        ) from exc
    rows = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(rows)
    except StopIteration as exc:
        raise ResearchQASourceError("CSV source must contain a header") from exc
    if not header:
        raise ResearchQASourceError("CSV source must contain header columns")
    raw_columns = tuple(column.strip() for column in header)
    duplicate_names = {
        column
        for column in raw_columns
        if column and raw_columns.count(column) > 1
    }
    columns = tuple(
        (
            f"column_{_column_name(index)}"
            if not column
            else (
                f"{column}__{_column_name(index)}"
                if column in duplicate_names
                else column
            )
        )
        for index, column in enumerate(raw_columns, 1)
    )

    units = []
    for row_number, row in enumerate(rows, 1):
        if len(row) > len(columns):
            raise ResearchQASourceError(
                f"CSV data row {row_number} has more fields than its header"
            )
        padded = [*row, *([""] * (len(columns) - len(row)))]
        occupied = [
            (column, value)
            for column, value in zip(columns, padded)
            if value != ""
        ]
        if not occupied:
            continue
        occupied_columns = tuple(column for column, _ in occupied)
        units.append(
            _make_native_unit(
                source,
                ordinal=len(units) + 1,
                coordinate=NativeCoordinate(
                    coordinate_type=COORDINATE_CSV_ROWS_COLUMNS,
                    row_start=row_number,
                    row_end=row_number,
                    columns=occupied_columns,
                ),
                text="\t".join(
                    f"{column}={value}" for column, value in occupied
                ),
            )
        )
    return tuple(units)


def _column_name(number: int) -> str:
    if number < 1:
        raise ResearchQASourceError("column number must be positive")
    result = []
    current = number
    while current:
        current, remainder = divmod(current - 1, 26)
        result.append(chr(ord("A") + remainder))
    return "".join(reversed(result))


def _column_number(name: str) -> int:
    number = 0
    for character in name.upper():
        if character < "A" or character > "Z":
            raise ResearchQASourceError(f"invalid column name: {name!r}")
        number = number * 26 + ord(character) - ord("A") + 1
    return number


__all__ = [
    "COORDINATE_CSV_ROWS_COLUMNS",
    "COORDINATE_DOCX_PARAGRAPH",
    "COORDINATE_DOCX_TABLE",
    "COORDINATE_PDF_PAGE",
    "COORDINATE_XLSX_CELLS",
    "DownloadReceipt",
    "MEDIA_CSV",
    "MEDIA_DOCX",
    "MEDIA_PDF",
    "MEDIA_XLSX",
    "NativeCoordinate",
    "NativeIRUnit",
    "ROLE_AUXILIARY",
    "ROLE_BENCHMARK_PDF",
    "ROLE_BUNDLED_SUPPLEMENT",
    "ROLE_EXTERNAL_SI",
    "ResearchQASourceError",
    "SourceArtifact",
    "SourceDownloadError",
    "SourceRecord",
    "build_source_manifest",
    "extract_native_corpus",
    "extract_native_ir",
    "hash_source_file",
    "infer_media_type",
    "normalize_researchqa_s3_url",
    "parser_fingerprint_for",
    "source_record_sort_key",
    "strict_tls_download",
]
