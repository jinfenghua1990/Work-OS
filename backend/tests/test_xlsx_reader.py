"""Offline tests for XLSX import preview without third-party spreadsheet packages."""
import io
import sys
import unittest
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
import xlsx_reader


def workbook_bytes(formula:bool=False,invalid_cell:bool=False)->bytes:
    sheet_cell = ('<c r="C2"><f>100*2</f><v>200</v></c>' if formula
                  else '<c r="C2" s="1"><v>46346</v></c>')
    if invalid_cell:
        sheet_cell='<c r="C2" t="e"><v>#REF!</v></c>'
    sheet='''<?xml version="1.0" encoding="UTF-8"?>
    <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <sheetData>
        <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>
          <c r="C1" t="inlineStr"><is><t>日期</t></is></c></row>
        <row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>100.50</v></c>''' + sheet_cell + '''
        </row>
      </sheetData>
    </worksheet>'''
    # 1900-based Excel serial: 2026-11-20 equals 46346.
    files={
        "xl/workbook.xml":'''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
         xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
         <sheets><sheet name="Sheet 1" sheetId="1" r:id="rId1"/></sheets></workbook>''',
        "xl/_rels/workbook.xml.rels":'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
           Target="worksheets/sheet1.xml"/></Relationships>''',
        "xl/styles.xml":'''<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
           <cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>''',
        "xl/sharedStrings.xml":'''<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
          <si><t>订单</t></si><si><t>金额</t></si><si><t>订单一</t></si></sst>''',
        "xl/worksheets/sheet1.xml":sheet,
    }
    output=io.BytesIO()
    with zipfile.ZipFile(output,"w",compression=zipfile.ZIP_DEFLATED) as archive:
        for name,content in files.items():
            archive.writestr(name,content)
    return output.getvalue()


class OfflineXlsxReaderTests(unittest.TestCase):
    def test_extract_first_sheet_and_excel_date(self):
        rows=xlsx_reader.first_sheet_rows(workbook_bytes())
        self.assertEqual(rows[0],["订单","金额","日期"])
        self.assertEqual(rows[1][0:2],["订单一","100.50"])
        self.assertEqual(rows[1][2],"2026-11-20")

    def test_formula_not_silently_trusted(self):
        with self.assertRaisesRegex(ValueError,"formulas"):
            xlsx_reader.first_sheet_rows(workbook_bytes(formula=True))

    def test_excel_error_is_rejected(self):
        with self.assertRaisesRegex(ValueError,"error cell"):
            xlsx_reader.first_sheet_rows(workbook_bytes(invalid_cell=True))

    def test_invalid_archive_fails_cleanly(self):
        with self.assertRaisesRegex(ValueError,"invalid .xlsx"):
            xlsx_reader.first_sheet_rows(b"not a workbook")
        with self.assertRaisesRegex(ValueError,"non-empty"):
            xlsx_reader.first_sheet_rows(b"")

    def test_excel_serial_reference_for_example(self):
        self.assertEqual((date(2026,11,20)-date(1899,12,30)).days,46346)


if __name__ == "__main__":
    unittest.main()
