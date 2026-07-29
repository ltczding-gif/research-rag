from __future__ import annotations

import hashlib
import io
import json
import ssl
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from benchmarks import researchqa_sources as sources


REPO_ROOT = Path(__file__).resolve().parent.parent


def _source_record(
    path: Path,
    *,
    file_id: str,
    media_type: str,
    role: str = sources.ROLE_EXTERNAL_SI,
) -> sources.SourceRecord:
    size, digest = sources.hash_source_file(path)
    return sources.SourceRecord(
        schema_version=1,
        paper_id="W-test",
        file_id=file_id,
        source_role=role,
        media_type=media_type,
        original_filename=path.name,
        source_url=f"https://example.test/{path.name}",
        sha256=digest,
        bytes=size,
        parser_fingerprint=sources.parser_fingerprint_for(media_type),
        citation_coordinate_type={
            sources.MEDIA_PDF: "pdf_page",
            sources.MEDIA_DOCX: "docx_paragraph_table",
            sources.MEDIA_XLSX: "xlsx_cells",
            sources.MEDIA_CSV: "csv_rows_columns",
        }[media_type],
        acquisition_status="verified",
    )


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        (
            "https://assets.openpaper.ai.s3.us-east-1.amazonaws.com/"
            "op-evals/benchmark/W1.pdf",
            "https://s3.us-east-1.amazonaws.com/assets.openpaper.ai/"
            "op-evals/benchmark/W1.pdf",
        ),
        (
            "https://my-bucket.s3.amazonaws.com/key/file.pdf?versionId=1",
            "https://s3.amazonaws.com/my-bucket/key/file.pdf?versionId=1",
        ),
        (
            "https://s3.us-east-1.amazonaws.com/bucket/key.pdf",
            "https://s3.us-east-1.amazonaws.com/bucket/key.pdf",
        ),
        (
            "https://publisher.example/supplement/file.pdf",
            "https://publisher.example/supplement/file.pdf",
        ),
    ],
)
def test_normalize_researchqa_s3_url(original, expected):
    assert sources.normalize_researchqa_s3_url(original) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://bucket.s3.amazonaws.com/key.pdf",
        "s3://bucket/key.pdf",
        "https://user@example.test/key.pdf",
        "https://s3.us-east-1.amazonaws.com/bucket-only",
    ],
)
def test_url_normalization_fails_closed(url):
    with pytest.raises(sources.ResearchQASourceError):
        sources.normalize_researchqa_s3_url(url)


class _FakeResponse:
    def __init__(self, payload: bytes, final_url: str):
        self._payload = io.BytesIO(payload)
        self._final_url = final_url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size: int) -> bytes:
        return self._payload.read(size)

    def geturl(self) -> str:
        return self._final_url


def test_strict_tls_download_uses_verified_context_and_checks_hash(tmp_path):
    payload = b"pinned source bytes"
    observed = {}

    def fake_open(request, timeout, context):
        observed["url"] = request.full_url
        observed["timeout"] = timeout
        observed["check_hostname"] = context.check_hostname
        observed["verify_mode"] = context.verify_mode
        return _FakeResponse(payload, request.full_url)

    destination = tmp_path / "paper.pdf"
    receipt = sources.strict_tls_download(
        "https://assets.openpaper.ai.s3.us-east-1.amazonaws.com/"
        "op-evals/benchmark/W1.pdf",
        destination,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        expected_bytes=len(payload),
        timeout=9,
        open_url=fake_open,
    )

    assert observed == {
        "url": (
            "https://s3.us-east-1.amazonaws.com/assets.openpaper.ai/"
            "op-evals/benchmark/W1.pdf"
        ),
        "timeout": 9,
        "check_hostname": True,
        "verify_mode": ssl.CERT_REQUIRED,
    }
    assert destination.read_bytes() == payload
    assert receipt.sha256 == hashlib.sha256(payload).hexdigest()
    assert receipt.bytes == len(payload)


def test_strict_tls_download_rejects_downgrade_and_preserves_destination(tmp_path):
    destination = tmp_path / "paper.pdf"
    destination.write_bytes(b"previous")

    def fake_open(request, timeout, context):
        return _FakeResponse(b"replacement", "http://example.test/paper.pdf")

    with pytest.raises(sources.SourceDownloadError, match="non-HTTPS"):
        sources.strict_tls_download(
            "https://example.test/paper.pdf",
            destination,
            open_url=fake_open,
        )

    assert destination.read_bytes() == b"previous"
    assert not (tmp_path / "paper.pdf.part").exists()


def test_source_manifest_has_stable_roles_ids_hashes_and_sorting(tmp_path):
    main = tmp_path / "main.pdf"
    zeta = tmp_path / "zeta.csv"
    alpha_b = tmp_path / "Alpha.xlsx"
    alpha_a = tmp_path / "alpha.docx"
    auxiliary = tmp_path / "report.csv"
    for path, payload in (
        (main, b"%PDF-main"),
        (zeta, b"zeta"),
        (alpha_b, b"alpha-b"),
        (alpha_a, b"alpha-a"),
        (auxiliary, b"aux"),
    ):
        path.write_bytes(payload)

    artifacts = [
        sources.SourceArtifact(
            zeta,
            sources.ROLE_BUNDLED_SUPPLEMENT,
            "https://example.test/zeta.csv",
        ),
        sources.SourceArtifact(
            auxiliary,
            sources.ROLE_AUXILIARY,
            "https://example.test/report.csv",
        ),
        sources.SourceArtifact(
            alpha_b,
            sources.ROLE_EXTERNAL_SI,
            "https://example.test/Alpha.xlsx",
        ),
        sources.SourceArtifact(
            main,
            sources.ROLE_BENCHMARK_PDF,
            "https://assets.openpaper.ai.s3.us-east-1.amazonaws.com/key/main.pdf",
        ),
        sources.SourceArtifact(
            alpha_a,
            sources.ROLE_EXTERNAL_SI,
            "https://example.test/alpha.docx",
        ),
    ]

    first = sources.build_source_manifest("W-test", artifacts)
    second = sources.build_source_manifest("W-test", reversed(artifacts))

    assert first == second
    assert [record.file_id for record in first] == [
        "Main",
        "SI-01",
        "SI-02",
        "SI-03",
        "AUX-01",
    ]
    assert [record.original_filename for record in first[1:4]] == [
        "alpha.docx",
        "Alpha.xlsx",
        "zeta.csv",
    ]
    assert first[0].source_url.startswith(
        "https://s3.us-east-1.amazonaws.com/assets.openpaper.ai/"
    )
    assert first[0].sha256 == hashlib.sha256(b"%PDF-main").hexdigest()
    assert first[0].source_role == sources.ROLE_BENCHMARK_PDF
    assert first[-1].source_role == sources.ROLE_AUXILIARY


def _write_docx(path: Path) -> None:
    document_xml = """\
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>First paragraph</w:t></w:r></w:p>
    <w:p/>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>name</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>score</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>model-a</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>0.9</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("word/document.xml", document_xml)


def test_docx_ir_preserves_nonempty_paragraph_and_table_coordinates(tmp_path):
    path = tmp_path / "source.docx"
    _write_docx(path)
    source = _source_record(
        path,
        file_id="SI-02",
        media_type=sources.MEDIA_DOCX,
    )

    units = sources.extract_native_ir(source, path)

    assert [unit.coordinate.coordinate_type for unit in units] == [
        "docx_paragraph",
        "docx_table",
        "docx_paragraph",
    ]
    assert [unit.citation for unit in units] == [
        "[SI-02 para.1]",
        "[SI-02 table.1 rows.1-2 cols.A-B]",
        "[SI-02 para.2]",
    ]
    assert units[1].text == "A1=name\tB1=score\nA2=model-a\tB2=0.9"


def _write_xlsx(path: Path) -> None:
    workbook_xml = """\
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Table S1" sheetId="1" r:id="rId1"/></sheets>
</workbook>
"""
    relationships_xml = """\
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
   Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
   Target="worksheets/sheet1.xml"/>
</Relationships>
"""
    shared_strings_xml = """\
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <si><t>model</t></si><si><t>score</t></si>
</sst>
"""
    worksheet_xml = """\
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1">
      <c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>
    </row>
    <row r="2">
      <c r="A2" t="inlineStr"><is><t>model-a</t></is></c>
      <c r="B2"><v>0.9</v></c>
    </row>
  </sheetData>
</worksheet>
"""
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("xl/workbook.xml", workbook_xml)
        package.writestr("xl/_rels/workbook.xml.rels", relationships_xml)
        package.writestr("xl/sharedStrings.xml", shared_strings_xml)
        package.writestr("xl/worksheets/sheet1.xml", worksheet_xml)


def test_xlsx_ir_uses_original_sheet_names_and_cell_ranges(tmp_path):
    path = tmp_path / "source.xlsx"
    _write_xlsx(path)
    source = _source_record(
        path,
        file_id="SI-03",
        media_type=sources.MEDIA_XLSX,
    )

    units = sources.extract_native_ir(source, path)

    assert [unit.coordinate.cell_range for unit in units] == ["A1:B1", "A2:B2"]
    assert [unit.citation for unit in units] == [
        '[SI-03 sheet."Table S1" cells.A1:B1]',
        '[SI-03 sheet."Table S1" cells.A2:B2]',
    ]
    assert units[1].text == "A2=model-a\tB2=0.9"


def test_csv_ir_uses_one_based_data_rows_and_header_columns(tmp_path):
    path = tmp_path / "source.csv"
    path.write_text(
        "\ufeffmodel,score,note\r\nmodel-a,0.9,\r\nmodel-b,,ok\r\n",
        encoding="utf-8",
        newline="",
    )
    source = _source_record(
        path,
        file_id="SI-04",
        media_type=sources.MEDIA_CSV,
    )

    units = sources.extract_native_ir(source, path)

    assert [unit.citation for unit in units] == [
        "[SI-04 rows.1-1 cols.model,score]",
        "[SI-04 rows.2-2 cols.model,note]",
    ]
    assert units[0].text == "model=model-a\tscore=0.9"


def test_csv_ir_gives_blank_or_duplicate_headers_stable_column_labels(tmp_path):
    path = tmp_path / "source.csv"
    path.write_text(
        ",score,score\r\nrow-a,1,2\r\n",
        encoding="utf-8",
        newline="",
    )
    source = _source_record(
        path,
        file_id="SI-04",
        media_type=sources.MEDIA_CSV,
    )

    units = sources.extract_native_ir(source, path)

    assert units[0].citation == (
        "[SI-04 rows.1-1 cols.column_A,score__B,score__C]"
    )
    assert units[0].text == "column_A=row-a\tscore__B=1\tscore__C=2"


def test_pdf_ir_uses_one_based_physical_pages_and_stable_unit_ids(tmp_path):
    path = tmp_path / "main.pdf"
    path.write_bytes(b"%PDF-fixture")
    source = _source_record(
        path,
        file_id="Main",
        media_type=sources.MEDIA_PDF,
        role=sources.ROLE_BENCHMARK_PDF,
    )

    def fake_pdf_extractor(
        artifact_path,
        *,
        paper_id,
        file_id,
        expected_file_hash,
    ):
        assert artifact_path == path
        assert paper_id == "W-test"
        assert file_id == "Main"
        assert expected_file_hash == source.sha256
        return SimpleNamespace(
            pages=(
                SimpleNamespace(normalized_text="Page one"),
                SimpleNamespace(normalized_text="Page two"),
            )
        )

    first = sources.extract_native_ir(
        source,
        path,
        pdf_extractor=fake_pdf_extractor,
    )
    second = sources.extract_native_ir(
        source,
        path,
        pdf_extractor=fake_pdf_extractor,
    )

    assert first == second
    assert [unit.citation for unit in first] == ["[Main p.1]", "[Main p.2]"]
    assert [unit.ordinal for unit in first] == [1, 2]
    assert all(unit.unit_id.startswith("native-") for unit in first)


def test_committed_source_and_coordinate_schemas_validate_records(tmp_path):
    main = tmp_path / "main.pdf"
    main.write_bytes(b"%PDF-main")
    record = sources.build_source_manifest(
        "W-test",
        [
            sources.SourceArtifact(
                main,
                sources.ROLE_BENCHMARK_PDF,
                "https://example.test/main.pdf",
            )
        ],
    )[0]
    source_schema = json.loads(
        (
            REPO_ROOT
            / "benchmarks"
            / "schemas"
            / "researchqa-source-record.schema.json"
        ).read_text(encoding="utf-8")
    )
    coordinate_schema = json.loads(
        (
            REPO_ROOT
            / "benchmarks"
            / "schemas"
            / "native-coordinate.schema.json"
        ).read_text(encoding="utf-8")
    )

    Draft202012Validator(
        source_schema,
        format_checker=FormatChecker(),
    ).validate(record.to_dict())
    Draft202012Validator(coordinate_schema).validate(
        sources.NativeCoordinate(
            coordinate_type=sources.COORDINATE_PDF_PAGE,
            page=1,
        ).to_dict()
    )
