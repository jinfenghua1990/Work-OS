"""Read the first worksheet from an .xlsx without third-party or cloud services.

Only previews values for explicit human review; never executes formulas,
macros, links, or file contents. Unsupported formula cells are rejected instead
of silently trusting potentially stale cached financial calculations.
"""
from __future__ import annotations

import io
import posixpath
import re
import zipfile
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

XLSX_BYTES_LIMIT = 20 * 1024 * 1024
UNCOMPRESSED_LIMIT = 100 * 1024 * 1024
MAX_ROWS = 10000
MAX_COLUMNS = 160
MAX_STRING = 16384
MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_OFFICE = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
REL_PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"
DEFAULT_DATE_FORMATS = set(range(14,23)) | {45,46,47}


def _xml(archive: zipfile.ZipFile, name: str):
    if name not in archive.namelist():
        raise ValueError("workbook missing expected XML: " + name)
    try:
        return ET.fromstring(archive.read(name))
    except ET.ParseError as exc:
        raise ValueError("invalid Excel workbook XML") from exc


def _column_number(cell: str) -> int:
    match = re.match(r"^([A-Z]+)", cell.upper())
    if not match:
        raise ValueError("unsupported Excel cell reference")
    result = 0
    for letter in match.group(1):
        result = result * 26 + ord(letter) - ord("A") + 1
    if result < 1 or result > MAX_COLUMNS:
        raise ValueError(f"Excel contains more than {MAX_COLUMNS} columns")
    return result - 1


def _is_date_format(code: str) -> bool:
    code = re.sub(r'"[^"]*"', "", code.lower())
    code = re.sub(r"\[[^]]*\]", "", code)
    return bool(re.search(r"y{2,4}|d{1,4}|h{1,2}:", code))


def _date_style_ids(archive: zipfile.ZipFile) -> set[int]:
    if "xl/styles.xml" not in archive.namelist():
        return set()
    root = _xml(archive, "xl/styles.xml")
    formats = {
        int(element.attrib.get("numFmtId", "0")):element.attrib.get("formatCode", "")
        for element in root.findall(f"./{MAIN}numFmts/{MAIN}numFmt")
    }
    style_ids: set[int] = set()
    for index, style in enumerate(root.findall(f"./{MAIN}cellXfs/{MAIN}xf")):
        number_format = int(style.attrib.get("numFmtId", "0"))
        if number_format in DEFAULT_DATE_FORMATS or _is_date_format(formats.get(number_format, "")):
            style_ids.add(index)
    return style_ids


def _cell_text(cell, shared: list[str], date_styles: set[int], epoch: datetime) -> str:
    if cell.find(f"{MAIN}f") is not None:
        raise ValueError("Excel contains formulas; save a copy with calculated values before importing financial data")
    cell_type = cell.attrib.get("t", "")
    value_element = cell.find(f"{MAIN}v")
    value = value_element.text or "" if value_element is not None else ""
    if cell_type == "inlineStr":
        inline = cell.find(f"{MAIN}is")
        value = "".join((part.text or "") for part in inline.iter(f"{MAIN}t")) if inline is not None else ""
    elif cell_type == "s":
        try:
            value = shared[int(value)]
        except (ValueError, IndexError) as exc:
            raise ValueError("Excel shared string index is invalid") from exc
    elif cell_type == "e":
        raise ValueError("Excel contains an error cell; fix the source file before import")
    elif cell_type == "b":
        value = "TRUE" if value == "1" else "FALSE"
    elif cell_type == "d":
        value = value[:10]
    elif value and cell_type in ("", "n"):
        try:
            if int(cell.attrib.get("s", "0")) in date_styles:
                converted = epoch + timedelta(days=float(value))
                value = converted.date().isoformat()
        except (ValueError, OverflowError) as exc:
            raise ValueError("invalid Excel date value") from exc
    if len(value) > MAX_STRING:
        raise ValueError("Excel cell content exceeds supported limit")
    return value.strip()


def first_sheet_rows(blob: bytes) -> list[list[str]]:
    if not blob or len(blob) > XLSX_BYTES_LIMIT:
        raise ValueError("Excel file must be non-empty and no larger than 20 MiB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("invalid .xlsx file") from exc
    with archive:
        entries=archive.infolist()
        if len(entries)>600 or sum(i.file_size for i in entries)>UNCOMPRESSED_LIMIT:
            raise ValueError("Excel workbook exceeds safe uncompressed size")
        if any(i.flag_bits & 1 for i in entries):
            raise ValueError("password-encrypted Excel workbooks are not supported")
        workbook = _xml(archive,"xl/workbook.xml")
        relationships = _xml(archive,"xl/_rels/workbook.xml.rels")
        sheets = workbook.findall(f"./{MAIN}sheets/{MAIN}sheet")
        if not sheets:
            raise ValueError("Excel workbook contains no sheets")
        sheet_relation = sheets[0].attrib.get(f"{REL_OFFICE}id")
        targets = {x.attrib.get("Id"):x.attrib.get("Target")
                   for x in relationships.findall(f"{REL_PKG}Relationship")}
        target = targets.get(sheet_relation)
        if not target:
            raise ValueError("first Excel worksheet cannot be resolved")
        path = (target.lstrip("/") if target.startswith("/") else posixpath.join("xl",target))
        path = posixpath.normpath(path)
        if not path.startswith("xl/") or ".." in path.split("/"):
            raise ValueError("invalid Excel worksheet path")
        root = _xml(archive,path)
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = _xml(archive,"xl/sharedStrings.xml")
            for part in shared.findall(f"{MAIN}si"):
                strings.append("".join(t.text or "" for t in part.iter(f"{MAIN}t")))
                if len(strings)>500000:
                    raise ValueError("Excel shared string table is too large")
        styles = _date_style_ids(archive)
        props=workbook.find(f"{MAIN}workbookPr")
        use_1904=props is not None and props.attrib.get("date1904") in ("1","true","True")
        epoch=datetime(1904,1,1) if use_1904 else datetime(1899,12,30)
        result: list[list[str]] = []
        for row in root.findall(f"./{MAIN}sheetData/{MAIN}row"):
            if len(result)>=MAX_ROWS:
                raise ValueError(f"Excel import preview supports at most {MAX_ROWS} nonempty rows")
            values: list[str] = []
            for cell in row.findall(f"{MAIN}c"):
                index = _column_number(cell.attrib.get("r",""))
                while len(values)<=index:
                    values.append("")
                values[index] = _cell_text(cell,strings,styles,epoch)
            while values and not values[-1]:
                values.pop()
            if values and any(values):
                result.append(values)
        if len(result)<2:
            raise ValueError("Excel requires a header row and at least one data row")
        return result
