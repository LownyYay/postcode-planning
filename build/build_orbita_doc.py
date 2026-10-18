from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "deliverables" / "ОРБИТА_Полная_документация_проекта.md"
API = ROOT / "deliverables" / "ОРБИТА_HTTP_API.md"
GUIDE = ROOT / "deliverables" / "ОРБИТА_Инструкция_по_запуску_и_регистрации.md"
OUT = ROOT / "deliverables" / "ОРБИТА_Полная_документация_проекта.docx"

BLACK = "000000"
NAVY = "17365D"
BLUE = "DCE6F1"
PALE = "F5F8FC"
GRAY = "D9D9D9"
DARK_GRAY = "44546A"


def set_cell_shading(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_cell_border(cell, color: str = GRAY, size: str = "6") -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    borders = tc_pr.first_child_found_in("w:tcBorders")
    if borders is None:
        borders = OxmlElement("w:tcBorders")
        tc_pr.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        tag = f"w:{edge}"
        element = borders.find(qn(tag))
        if element is None:
            element = OxmlElement(tag)
            borders.append(element)
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), size)
        element.set(qn("w:color"), color)


def set_cell_margins(cell, top=110, start=130, bottom=110, end=130) -> None:
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for m, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{m}"))
        if node is None:
            node = OxmlElement(f"w:{m}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def set_repeat_table_header(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    tbl_header = OxmlElement("w:tblHeader")
    tbl_header.set(qn("w:val"), "true")
    tr_pr.append(tbl_header)


def set_repeat_heading(paragraph) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    keep_next = OxmlElement("w:keepNext")
    p_pr.append(keep_next)
    keep_lines = OxmlElement("w:keepLines")
    p_pr.append(keep_lines)


def set_font(run, name="Arial", size=None, bold=None, color=BLACK, italic=None) -> None:
    run.font.name = name
    run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), name)
    run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), name)
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), name)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic
    run.font.color.rgb = RGBColor.from_string(color)


def add_page_number(paragraph) -> None:
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = paragraph.add_run("Страница ")
    set_font(run, size=9, color="666666")
    fld_char1 = OxmlElement("w:fldChar")
    fld_char1.set(qn("w:fldCharType"), "begin")
    instr_text = OxmlElement("w:instrText")
    instr_text.set(qn("xml:space"), "preserve")
    instr_text.text = " PAGE "
    fld_char2 = OxmlElement("w:fldChar")
    fld_char2.set(qn("w:fldCharType"), "end")
    run._r.append(fld_char1)
    run._r.append(instr_text)
    run._r.append(fld_char2)


def clean_inline(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    return text.replace("  ", " ").strip()


INLINE_RE = re.compile(r"(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)")


def add_inline(paragraph, text: str, base_size=10.8) -> None:
    text = clean_inline(text)
    pos = 0
    for match in INLINE_RE.finditer(text):
        if match.start() > pos:
            run = paragraph.add_run(text[pos : match.start()])
            set_font(run, size=base_size)
        token = match.group(0)
        if token.startswith("`"):
            run = paragraph.add_run(token[1:-1])
            set_font(run, name="Menlo", size=max(base_size - 0.7, 8.5), color="24364B")
        elif token.startswith("**"):
            run = paragraph.add_run(token[2:-2])
            set_font(run, size=base_size, bold=True)
        else:
            run = paragraph.add_run(token[1:-1])
            set_font(run, size=base_size, italic=True)
        pos = match.end()
    if pos < len(text):
        run = paragraph.add_run(text[pos:])
        set_font(run, size=base_size)


def configure_document(doc: Document) -> None:
    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(0.90)
    # Keep dense lists and tables clear of the footer in both Word and
    # LibreOffice-based rendering.
    section.bottom_margin = Inches(1.50)
    section.left_margin = Inches(0.78)
    section.right_margin = Inches(0.78)
    section.header_distance = Inches(0.28)
    section.footer_distance = Inches(0.28)

    styles = doc.styles
    normal = styles["Normal"]
    normal.font.name = "Arial"
    normal._element.rPr.rFonts.set(qn("w:ascii"), "Arial")
    normal._element.rPr.rFonts.set(qn("w:hAnsi"), "Arial")
    normal.font.size = Pt(10.8)
    normal.font.color.rgb = RGBColor(0, 0, 0)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.08

    specs = {
        "Title": (28, 700, 18, 1.0),
        "Subtitle": (16, 500, 10, 1.0),
        "Heading 1": (18, 700, 12, 1.05),
        "Heading 2": (14, 700, 8, 1.05),
        "Heading 3": (11.5, 700, 5, 1.02),
    }
    for style_name, (size, _weight, before, spacing) in specs.items():
        style = styles[style_name]
        style.font.name = "Arial"
        style._element.rPr.rFonts.set(qn("w:ascii"), "Arial")
        style._element.rPr.rFonts.set(qn("w:hAnsi"), "Arial")
        style.font.size = Pt(size)
        style.font.bold = style_name != "Subtitle"
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(5)
        style.paragraph_format.keep_with_next = True

    header = section.header.paragraphs[0]
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    hrun = header.add_run("ОРБИТА 3.0.0  |  Техническая документация")
    set_font(hrun, size=8.5, color="666666")
    add_page_number(section.footer.paragraphs[0])


def add_cover(doc: Document) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(92)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("ОРБИТА")
    set_font(r, size=31, bold=True)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.space_after = Pt(36)
    r = p.add_run("Полная документация проекта\nквартального планирования и звёздной карты")
    set_font(r, size=18, bold=True)

    meta = [
        ("Версия продукта", "3.0.0"),
        ("Версия документа", "1.0"),
        ("Дата", "22 сентября 2026 года"),
        ("Статус", "Автоматические проверки пройдены, ручная приёмка открыта"),
    ]
    table = doc.add_table(rows=len(meta), cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    for i, (label, value) in enumerate(meta):
        table.columns[0].width = Inches(1.65)
        table.columns[1].width = Inches(4.3)
        cells = table.rows[i].cells
        for c in cells:
            c.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            set_cell_margins(c, 130, 170, 130, 170)
            set_cell_border(c, color="C9D3DF")
        set_cell_shading(cells[0], BLUE)
        p0 = cells[0].paragraphs[0]
        p0.alignment = WD_ALIGN_PARAGRAPH.LEFT
        add_inline(p0, f"**{label}**", 10.5)
        p1 = cells[1].paragraphs[0]
        add_inline(p1, value, 10.5)

    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(46)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("Назначение документа")
    set_font(r, size=11, bold=True)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.left_indent = Inches(0.65)
    p.paragraph_format.right_indent = Inches(0.65)
    r = p.add_run(
        "Описание требований, архитектуры, алгоритмов, API, пользовательских процессов, "
        "эксплуатации, тестирования и ограничений итоговой системы"
    )
    set_font(r, size=10.5, color="444444")
    doc.add_page_break()


def add_contents(doc: Document, sources: list[tuple[str, Path]]) -> None:
    p = doc.add_paragraph("Содержание", style="Heading 1")
    set_repeat_heading(p)
    for prefix, path in sources:
        if prefix:
            if path == API:
                title = "Подробное описание HTTP API"
            else:
                title = "Подробная инструкция по запуску и регистрации"
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.18)
            p.paragraph_format.space_after = Pt(2.5)
            add_inline(p, f"{prefix} {title}", 10.2)
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            if line.startswith("## "):
                title = line[3:].strip()
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Inches(0.18 if not prefix else 0.35)
                p.paragraph_format.space_after = Pt(2.5)
                lead = f"{prefix} " if prefix else ""
                add_inline(p, lead + title, 10.2)
    doc.add_page_break()


def parse_table(lines: list[str], start: int, doc: Document) -> int:
    block = []
    i = start
    while i < len(lines) and lines[i].lstrip().startswith("|"):
        block.append(lines[i].strip())
        i += 1
    rows = []
    for line in block:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", c or "") for c in cells):
            continue
        rows.append(cells)
    if not rows:
        return i
    cols = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=cols)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True
    set_repeat_table_header(table.rows[0])
    for ri, values in enumerate(rows):
        for ci in range(cols):
            cell = table.cell(ri, ci)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            set_cell_margins(cell)
            set_cell_border(cell)
            text = values[ci] if ci < len(values) else ""
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.line_spacing = 1.0
            if ri == 0:
                set_cell_shading(cell, NAVY)
                run = p.add_run(clean_inline(text))
                set_font(run, size=9.1, bold=True, color="FFFFFF")
            else:
                if ri % 2 == 0:
                    set_cell_shading(cell, PALE)
                add_inline(p, text, 9.1)
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(1)
    return i


def parse_markdown(doc: Document, path: Path, skip_until_h2=False) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    if skip_until_h2:
        if path == MAIN:
            while i < len(lines) and not re.match(r"^## 1\s", lines[i]):
                i += 1
        else:
            while i < len(lines) and not lines[i].startswith("## "):
                i += 1
    in_code = False
    code_lines: list[str] = []
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            if not in_code:
                in_code = True
                code_lines = []
            else:
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Inches(0.28)
                p.paragraph_format.right_indent = Inches(0.18)
                p.paragraph_format.space_before = Pt(3)
                p.paragraph_format.space_after = Pt(7)
                p.paragraph_format.line_spacing = 1.0
                p_pr = p._p.get_or_add_pPr()
                shd = OxmlElement("w:shd")
                shd.set(qn("w:fill"), "F2F2F2")
                p_pr.append(shd)
                run = p.add_run("\n".join(code_lines))
                set_font(run, name="Menlo", size=8.6, color="1F2937")
                in_code = False
            i += 1
            continue
        if in_code:
            code_lines.append(line)
            i += 1
            continue

        if not stripped:
            i += 1
            continue

        image_match = re.fullmatch(r"!\[([^\]]*)\]\(([^\)]+)\)", stripped)
        if image_match:
            image_path = (path.parent / image_match.group(2)).resolve()
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_before = Pt(8)
            p.paragraph_format.space_after = Pt(3)
            p.add_run().add_picture(str(image_path), width=Inches(6.20))
            caption = doc.add_paragraph()
            caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
            caption.paragraph_format.space_after = Pt(9)
            r = caption.add_run(image_match.group(1))
            set_font(r, size=9, italic=True, color="555555")
            i += 1
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and lines[i + 1].lstrip().startswith("|"):
            i = parse_table(lines, i, doc)
            continue

        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            level = len(heading.group(1))
            text = clean_inline(heading.group(2))
            if level == 1:
                p = doc.add_paragraph(text, style="Heading 1")
            elif level == 2:
                p = doc.add_paragraph(text, style="Heading 1")
            elif level == 3:
                p = doc.add_paragraph(text, style="Heading 2")
            else:
                p = doc.add_paragraph(text, style="Heading 3")
            set_repeat_heading(p)
            i += 1
            continue

        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        numbered = re.match(r"^(\d+)\.\s+(.*)$", stripped)
        if bullet or numbered:
            p = doc.add_paragraph(style="List Bullet" if bullet else None)
            p.paragraph_format.left_indent = Inches(0.28)
            p.paragraph_format.first_line_indent = Inches(-0.17)
            p.paragraph_format.space_after = Pt(3.5)
            if bullet:
                add_inline(p, bullet.group(1), 10.5)
            else:
                run = p.add_run(f"{numbered.group(1)}. ")
                set_font(run, size=10.5)
                add_inline(p, numbered.group(2), 10.5)
            i += 1
            continue

        paragraph_lines = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if not nxt or nxt.startswith("#") or nxt.startswith("|") or nxt.startswith("```"):
                break
            if re.match(r"^[-*]\s+", nxt) or re.match(r"^\d+\.\s+", nxt) or nxt.startswith("!["):
                break
            paragraph_lines.append(nxt)
            i += 1
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        p.paragraph_format.space_after = Pt(6)
        p.paragraph_format.line_spacing = 1.08
        add_inline(p, " ".join(paragraph_lines), 10.8)


def add_appendix(doc: Document, title: str, path: Path) -> None:
    p = doc.add_paragraph(title, style="Heading 1")
    # A paragraph-level page break avoids an empty page when the preceding
    # content already finishes at the bottom of a page.
    p.paragraph_format.page_break_before = True
    set_repeat_heading(p)
    parse_markdown(doc, path, skip_until_h2=True)


def set_core_properties(doc: Document) -> None:
    props = doc.core_properties
    props.title = "ОРБИТА Полная документация проекта"
    props.subject = "Квартальное планирование, расчётное ядро, API и эксплуатация"
    props.keywords = "ОРБИТА, MILP, HiGHS, квартальное планирование, Bus Factor"
    props.comments = "Версия документа 1.0 от 22 сентября 2026 года"


def main() -> None:
    doc = Document()
    configure_document(doc)
    set_core_properties(doc)
    add_cover(doc)
    add_contents(
        doc,
        [
            ("", MAIN),
            ("Приложение А", API),
            ("Приложение Б", GUIDE),
        ],
    )
    parse_markdown(doc, MAIN, skip_until_h2=True)
    add_appendix(doc, "Приложение А Подробное описание HTTP API", API)
    add_appendix(doc, "Приложение Б Подробная инструкция по запуску и регистрации", GUIDE)

    # Prevent isolated table rows where Word allows it and keep headings with the next block.
    for table in doc.tables:
        for row in table.rows:
            tr_pr = row._tr.get_or_add_trPr()
            cant_split = OxmlElement("w:cantSplit")
            tr_pr.append(cant_split)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUT)
    print(OUT)


if __name__ == "__main__":
    main()
