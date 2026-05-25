#!/usr/bin/env python3
"""Create a minimal DevTest template workbook with fallback headers.

This helper uses only the Python standard library so an agent can bootstrap a
blank .xlsx before installing openpyxl. For populated test-case generation,
the skill still prefers openpyxl.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import zipfile
from pathlib import Path


DEVTEST_TEMPLATE_HEADERS = [
    "folderPath",
    "Template ID",
    "Title",
    "Template State",
    "Template Owner",
    "Test Procedure",
    "Expected Result",
    "Test Description",
    "E. Setup Time",
    "E.Test Time",
    "Origin",
    "Template Priority",
    "Automation Status",
    "Automation Status Linux",
    "KPI",
    "Execution Owner",
    "SWQA User Mode",
    "Functional Test Type",
    "Release",
    "Tool/App. Location",
    "Tool/App. Required for Execution",
]


def _col_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _inline_cell(ref: str, value: str, *, style: int = 0) -> str:
    style_attr = f' s="{style}"' if style else ""
    escaped = html.escape(value, quote=False)
    return (
        f'<c r="{ref}" t="inlineStr"{style_attr}>'
        f"<is><t>{escaped}</t></is>"
        "</c>"
    )


def _sheet_xml() -> str:
    last_col = _col_name(len(DEVTEST_TEMPLATE_HEADERS))
    header_cells = [
        _inline_cell(f"{_col_name(idx)}1", header, style=1)
        for idx, header in enumerate(DEVTEST_TEMPLATE_HEADERS, start=1)
    ]
    widths = "\n".join(
        f'<col min="{idx}" max="{idx}" width="{max(14, min(42, len(header) + 4))}" customWidth="1"/>'
        for idx, header in enumerate(DEVTEST_TEMPLATE_HEADERS, start=1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:{last_col}1"/>
  <sheetViews>
    <sheetView workbookViewId="0">
      <pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>
      <selection pane="bottomLeft"/>
    </sheetView>
  </sheetViews>
  <sheetFormatPr defaultRowHeight="15"/>
  <cols>{widths}</cols>
  <sheetData>
    <row r="1" spans="1:{len(DEVTEST_TEMPLATE_HEADERS)}" ht="24" customHeight="1">
      {''.join(header_cells)}
    </row>
  </sheetData>
  <autoFilter ref="A1:{last_col}1"/>
  <pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>
</worksheet>
"""


def create_workbook(output: Path, *, sheet_name: str = "DevTest Templates") -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    sheet_name_xml = html.escape(sheet_name, quote=True)
    files = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>
""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
""",
        "xl/workbook.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="{sheet_name_xml}" sheetId="1" r:id="rId1"/></sheets>
</workbook>
""",
        "xl/_rels/workbook.xml.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
""",
        "xl/worksheets/sheet1.xml": _sheet_xml(),
        "xl/styles.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2">
    <font><sz val="11"/><color theme="1"/><name val="Calibri"/><family val="2"/></font>
    <font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/><family val="2"/></font>
  </fonts>
  <fills count="3">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF76B900"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="2">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
  <dxfs count="0"/>
  <tableStyles count="0" defaultTableStyle="TableStyleMedium2" defaultPivotStyle="PivotStyleLight16"/>
</styleSheet>
""",
        "docProps/core.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:creator>Agent Me</dc:creator>
  <cp:lastModifiedBy>Agent Me</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
</cp:coreProperties>
""",
        "docProps/app.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Agent Me</Application>
  <DocSecurity>0</DocSecurity>
  <ScaleCrop>false</ScaleCrop>
  <HeadingPairs><vt:vector size="2" baseType="variant"><vt:variant><vt:lpstr>Worksheets</vt:lpstr></vt:variant><vt:variant><vt:i4>1</vt:i4></vt:variant></vt:vector></HeadingPairs>
  <TitlesOfParts><vt:vector size="1" baseType="lpstr"><vt:lpstr>DevTest Templates</vt:lpstr></vt:vector></TitlesOfParts>
  <Company>NVIDIA VRDC SWQA</Company>
  <LinksUpToDate>false</LinksUpToDate>
  <SharedDoc>false</SharedDoc>
  <HyperlinksChanged>false</HyperlinksChanged>
  <AppVersion>16.0300</AppVersion>
</Properties>
""",
    }
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a blank DevTest template .xlsx")
    parser.add_argument("output", type=Path, help="Output .xlsx path")
    parser.add_argument("--sheet-name", default="DevTest Templates")
    args = parser.parse_args()
    create_workbook(args.output, sheet_name=args.sheet_name)
    print(args.output)


if __name__ == "__main__":
    main()
