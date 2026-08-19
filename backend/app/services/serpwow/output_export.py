# backend/app/services/serpwow/output_export.py
"""Dependency-free tabular export of an upload's output payload: XLSX and CSV.

Both formats come from ONE table builder (``build_upload_output_table``) so a column
added for one shows up in the other — the two exports cannot drift.
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from typing import Any
from xml.sax.saxutils import escape as xml_escape


def _sanitize_excel_text(value: Any) -> str:
    text = str(value or "")
    # Remove XML-disallowed control chars (keep tab/newline/carriage return).
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text)
    # Excel cell text limit.
    return text[:32767]


def _excel_col_name(idx: int) -> str:
    name = ""
    n = idx
    while n > 0:
        n, rem = divmod(n - 1, 26)
        name = chr(65 + rem) + name
    return name


def _xlsx_cell_xml(col_idx: int, row_idx: int, value: Any) -> str:
    ref = f"{_excel_col_name(col_idx)}{row_idx}"
    if value is None or value == "":
        return f'<c r="{ref}" t="inlineStr"><is><t></t></is></c>'
    if isinstance(value, bool):
        return f'<c r="{ref}" t="n"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}" t="n"><v>{value}</v></c>'
    text = _sanitize_excel_text(value)
    escaped = xml_escape(text)
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escaped}</t></is></c>'


def build_upload_output_table(
    output_data: dict[str, Any],
    *,
    ascii_json: bool = True,
) -> tuple[list[str], list[list[Any]]]:
    """(headers, rows) for one upload's output payload — the source for XLSX and CSV.

    ``ascii_json`` controls only the ``output_json`` column: the XLSX path keeps
    ``\\uXXXX`` escapes (its XML writer is ASCII-safe by construction), while the CSV
    path passes False so a Polish or Turkish address reads as itself in the file rather
    than as escape soup.
    """
    headers = [
        "upload_id",
        "file_status",
        "batch_status",
        "created_at",
        "updated_at",
        "total_rows",
        "processed_rows",
        "success_rows",
        "failed_rows",
        "row_index",
        "input_company_name",
        "input_country",
        "input_firm_id",
        "input_industry",
        "input_full_address",
        "input_official_website",
        "row_status",
        "row_error",
        "raw_response_s3_key",
        "output_official_website",
        "output_confidence_score",
        "output_confidence",
        "output_summary",
        "output_website_company_descirption_ai",
        "output_website_company_descirption_translated_ai",
        "output_address",
        "output_phone",
        "output_email",
        "output_industry",
        "output_products",
        "output_services",
        "output_massive_proxy_cost_usd",
        "output_serpwow_cost_usd",
        "output_gemini_cost_usd",
        "output_total_cost_usd",
        "output_processing_seconds",
        "output_json",
    ]

    upload_id = output_data.get("upload_id")
    file_status = output_data.get("status")
    batch_obj = output_data.get("gemini_batch") if isinstance(output_data.get("gemini_batch"), dict) else {}
    batch_status = batch_obj.get("status")
    created_at = output_data.get("created_at")
    updated_at = output_data.get("updated_at")
    total_rows = output_data.get("total_rows")
    processed_rows = output_data.get("processed_rows")
    success_rows = output_data.get("success_rows")
    failed_rows = output_data.get("failed_rows")

    data_rows: list[list[Any]] = []
    for item in output_data.get("results", []) or []:
        if not isinstance(item, dict):
            continue
        input_obj = item.get("input") if isinstance(item.get("input"), dict) else {}
        result_obj = item.get("output") if isinstance(item.get("output"), dict) else {}
        context_obj = result_obj.get("context") if isinstance(result_obj.get("context"), dict) else {}
        final_url_ai_obj = (
            context_obj.get("final_url_selection_ai")
            if isinstance(context_obj.get("final_url_selection_ai"), dict)
            else {}
        )
        final_url_ai_raw = (
            final_url_ai_obj.get("raw")
            if isinstance(final_url_ai_obj.get("raw"), dict)
            else {}
        )
        gemini_batch_ai_obj = (
            context_obj.get("gemini_batch_ai")
            if isinstance(context_obj.get("gemini_batch_ai"), dict)
            else {}
        )
        gemini_batch_ai_raw = (
            gemini_batch_ai_obj.get("raw")
            if isinstance(gemini_batch_ai_obj.get("raw"), dict)
            else {}
        )
        confidence_score = final_url_ai_raw.get("confidence_score")
        if confidence_score is None:
            confidence_score = gemini_batch_ai_raw.get("confidence_score")
        confidence = final_url_ai_raw.get("confidence")
        if confidence is None:
            confidence = gemini_batch_ai_raw.get("confidence")
        data_rows.append(
            [
                upload_id,
                file_status,
                batch_status,
                created_at,
                updated_at,
                total_rows,
                processed_rows,
                success_rows,
                failed_rows,
                item.get("row_index"),
                input_obj.get("company_name"),
                input_obj.get("country"),
                input_obj.get("firm_id"),
                input_obj.get("industry"),
                input_obj.get("full_address"),
                input_obj.get("official_website"),
                item.get("status"),
                item.get("error"),
                item.get("raw_response_s3_key"),
                result_obj.get("official_website"),
                confidence_score,
                confidence,
                result_obj.get("summary"),
                result_obj.get("website_company_descirption_ai"),
                result_obj.get("website_company_descirption_translated_ai"),
                result_obj.get("address"),
                result_obj.get("phone"),
                result_obj.get("email"),
                result_obj.get("industry"),
                ", ".join(result_obj.get("products") or []) if isinstance(result_obj.get("products"), list) else None,
                ", ".join(result_obj.get("services") or []) if isinstance(result_obj.get("services"), list) else None,
                result_obj.get("massive_proxy_cost_usd"),
                result_obj.get("serpwow_cost_usd"),
                result_obj.get("gemini_cost_usd"),
                result_obj.get("total_cost_usd"),
                (context_obj.get("timing") or {}).get("total_seconds"),
                json.dumps(result_obj, ensure_ascii=ascii_json),
            ]
        )

    return headers, data_rows


def build_upload_output_csv_bytes(output_data: dict[str, Any]) -> bytes:
    """UTF-8 CSV of the same table the XLSX export writes.

    Encoding, because this file is opened in Excel as often as it is parsed:
    - **utf-8-sig** (BOM). Excel assumes the OS legacy codepage for a BOM-less CSV, which
      is what turns ``Leśna`` into ``LeÅ›na``; the BOM is what makes it read UTF-8. Python's
      ``csv``/``pandas`` skip the BOM automatically, so nothing downstream regresses.
    - **CRLF** line endings (``excel`` dialect default) for the same reason.
    - Values keep their real characters and their FULL length. The XLSX writer truncates at
      Excel's 32767-per-cell limit; a CSV has no such limit and silently losing the tail of
      ``output_json`` would be worse than a long field.
    - Embedded newlines (products/services joins, multi-line addresses) are quoted by the
      csv module, so they stay inside one field instead of breaking the row.
    """
    headers, data_rows = build_upload_output_table(output_data, ascii_json=False)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, dialect="excel", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    for row in data_rows:
        writer.writerow(["" if value is None else _csv_text(value) for value in row])
    return buffer.getvalue().encode("utf-8-sig")


def _csv_text(value: Any) -> Any:
    """Numbers pass through unformatted; text loses only chars that corrupt a CSV.

    NUL and the other C0 controls (tab/CR/LF excepted) have no legal place in a CSV field
    and break some readers outright, so they are dropped exactly as the XLSX path drops
    them — but without that path's length cap.
    """
    if isinstance(value, (int, float, bool)):
        return value
    return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", str(value))


def build_upload_output_xlsx_bytes(output_data: dict[str, Any]) -> bytes:
    headers, data_rows = build_upload_output_table(output_data)
    rows = [headers] + data_rows
    sheet_rows_xml: list[str] = []
    for row_idx, row_values in enumerate(rows, start=1):
        cells_xml = "".join(_xlsx_cell_xml(col_idx, row_idx, value) for col_idx, value in enumerate(row_values, start=1))
        sheet_rows_xml.append(f'<row r="{row_idx}">{cells_xml}</row>')
    sheet_data_xml = "".join(sheet_rows_xml)
    max_col = _excel_col_name(len(headers))
    max_row = max(1, len(rows))
    dimension = f"A1:{max_col}{max_row}"

    worksheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<dimension ref="{dimension}"/>'
        "<sheetViews><sheetView workbookViewId=\"0\"/></sheetViews>"
        "<sheetFormatPr defaultRowHeight=\"15\"/>"
        f"<sheetData>{sheet_data_xml}</sheetData>"
        "</worksheet>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<sheets>"
        '<sheet name="results" sheetId="1" r:id="rId1"/>'
        "</sheets>"
        "</workbook>"
    )
    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        "</Relationships>"
    )
    root_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )
    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", root_rels_xml)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        zf.writestr("xl/worksheets/sheet1.xml", worksheet_xml)
        zf.writestr("xl/styles.xml", styles_xml)
    return buffer.getvalue()
