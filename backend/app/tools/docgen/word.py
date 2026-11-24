"""Word (.docx) generator - python-docx."""
from __future__ import annotations

import copy
import io
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Inches, Pt, RGBColor

from .common import (CALLOUT_COLORS, GenContext, format_cell, hex_rgb, is_numeric_cell, mix, parse_inline,
                     render_chart_png, resolve_table, strip_inline)
from .specs import Block, DocumentSpec, ListItem, TableSpec

ALIGN = {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER,
         "right": WD_ALIGN_PARAGRAPH.RIGHT, "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}
PAGE = {"A4": (Inches(8.27), Inches(11.69)), "Letter": (Inches(8.5), Inches(11))}


def rgb(h: str) -> RGBColor:
    return RGBColor(*hex_rgb(h))


# --------------------------------------------------------------------------- low-level XML helpers
def _shade(cell, fill: str) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tcPr.append(shd)


def _cell_borders(cell, **edges: tuple[str, int]) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    borders = OxmlElement("w:tcBorders")
    for edge, (color, size) in edges.items():
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single" if size else "nil")
        el.set(qn("w:sz"), str(size))
        el.set(qn("w:color"), color)
        borders.append(el)
    tcPr.append(borders)


def _table_borders(table, color: str = "D0D7DE") -> None:
    tblPr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")
        el.set(qn("w:color"), color)
        borders.append(el)
    tblPr.append(borders)


def _cell_margins(table, top=60, bottom=60, left=100, right=100) -> None:
    tblPr = table._tbl.tblPr
    mar = OxmlElement("w:tblCellMar")
    for k, v in (("top", top), ("left", left), ("bottom", bottom), ("right", right)):
        el = OxmlElement(f"w:{k}")
        el.set(qn("w:w"), str(v))
        el.set(qn("w:type"), "dxa")
        mar.append(el)
    tblPr.append(mar)


def _repeat_header(row) -> None:
    trPr = row._tr.get_or_add_trPr()
    el = OxmlElement("w:tblHeader")
    el.set(qn("w:val"), "true")
    trPr.append(el)


def _para_border(paragraph, side: str, color: str, size: int = 12, space: int = 4) -> None:
    pPr = paragraph._p.get_or_add_pPr()
    pBdr = pPr.find(qn("w:pBdr"))
    if pBdr is None:
        pBdr = OxmlElement("w:pBdr")
        pPr.append(pBdr)
    el = OxmlElement(f"w:{side}")
    el.set(qn("w:val"), "single")
    el.set(qn("w:sz"), str(size))
    el.set(qn("w:space"), str(space))
    el.set(qn("w:color"), color)
    pBdr.append(el)


def _para_shading(paragraph, fill: str) -> None:
    pPr = paragraph._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    pPr.append(shd)


def _field(paragraph, instr: str, placeholder: str = "") -> None:
    run = paragraph.add_run()
    for kind, text in (("begin", None), ("instr", instr), ("separate", None), ("text", placeholder), ("end", None)):
        if kind == "instr":
            el = OxmlElement("w:instrText")
            el.set(qn("xml:space"), "preserve")
            el.text = text
        elif kind == "text":
            el = OxmlElement("w:t")
            el.text = text or ""
        else:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), kind)
            if kind == "begin":
                el.set(qn("w:dirty"), "true")
        run._r.append(el)


def _hyperlink(paragraph, url: str, text: str, color: str) -> None:
    r_id = paragraph.part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
                                    is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    c = OxmlElement("w:color")
    c.set(qn("w:val"), color)
    u = OxmlElement("w:u")
    u.set(qn("w:val"), "single")
    rPr.extend([c, u])
    run.append(rPr)
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    run.append(t)
    link.append(run)
    paragraph._p.append(link)


def _add_runs(paragraph, text: str, theme, size: float | None = None, color: str | None = None,
              bold: bool = False, italic: bool = False) -> None:
    for r in parse_inline(text):
        if r.link:
            _hyperlink(paragraph, r.link, r.text, "2563EB")
            continue
        run = paragraph.add_run(r.text)
        run.bold = bold or r.bold or None
        run.italic = italic or r.italic or None
        if r.code:
            run.font.name = "Consolas"
            run.font.size = Pt((size or 10.5) - 1)
        elif size:
            run.font.size = Pt(size)
        if color:
            run.font.color.rgb = rgb(color)


# --------------------------------------------------------------------------- numbering (restartable lists)
def _new_list_num(doc, style_name: str) -> int | None:
    """Create a fresh w:num pointing at the abstractNum of the given list style, so numbering restarts."""
    try:
        style = doc.styles[style_name]
        numPr = style.element.pPr.numPr
        num_id = int(numPr.numId.val)
        numbering = doc.part.numbering_part.numbering_definitions._numbering
        abstract_id = None
        for num in numbering.findall(qn("w:num")):
            if int(num.get(qn("w:numId"))) == num_id:
                abstract_id = num.find(qn("w:abstractNumId")).get(qn("w:val"))
        if abstract_id is None:
            return None
        new_id = max(int(n.get(qn("w:numId"))) for n in numbering.findall(qn("w:num"))) + 1
        num = OxmlElement("w:num")
        num.set(qn("w:numId"), str(new_id))
        absn = OxmlElement("w:abstractNumId")
        absn.set(qn("w:val"), abstract_id)
        num.append(absn)
        override = OxmlElement("w:lvlOverride")
        override.set(qn("w:ilvl"), "0")
        start = OxmlElement("w:startOverride")
        start.set(qn("w:val"), "1")
        override.append(start)
        num.append(override)
        numbering.append(num)
        return new_id
    except Exception:  # noqa: BLE001 - fall back to continuous numbering
        return None


def _set_num(paragraph, num_id: int, level: int) -> None:
    pPr = paragraph._p.get_or_add_pPr()
    numPr = OxmlElement("w:numPr")
    ilvl = OxmlElement("w:ilvl")
    ilvl.set(qn("w:val"), str(level))
    nid = OxmlElement("w:numId")
    nid.set(qn("w:val"), str(num_id))
    numPr.extend([ilvl, nid])
    pPr.insert(1 if pPr.find(qn("w:pStyle")) is not None else 0, numPr)


# --------------------------------------------------------------------------- builder
class WordBuilder:
    def __init__(self, spec: DocumentSpec, ctx: GenContext):
        self.spec, self.ctx, self.t = spec, ctx, spec.theme
        self.doc = Document()
        self._setup()

    @property
    def content_width(self) -> Emu:
        sec = self.doc.sections[-1]
        return Emu(sec.page_width - sec.left_margin - sec.right_margin)

    def _setup(self) -> None:
        t, doc = self.t, self.doc
        sec = doc.sections[0]
        w, h = PAGE[self.spec.page_size]
        if self.spec.orientation == "landscape":
            sec.orientation = WD_ORIENT.LANDSCAPE
            w, h = h, w
        sec.page_width, sec.page_height = w, h
        for side in ("left_margin", "right_margin"):
            setattr(sec, side, Inches(0.9))
        sec.top_margin = sec.bottom_margin = Inches(0.9)

        normal = doc.styles["Normal"]
        normal.font.name = t.font_body
        normal.element.rPr.rFonts.set(qn("w:eastAsia"), t.font_body)
        normal.font.size = Pt(10.5)
        normal.font.color.rgb = rgb(t.text)
        normal.paragraph_format.space_after = Pt(6)
        normal.paragraph_format.line_spacing = 1.15
        sizes = {1: 18, 2: 14, 3: 12, 4: 11}
        for lvl, size in sizes.items():
            st = doc.styles[f"Heading {lvl}"]
            st.font.name = t.font_heading
            rfonts = st.element.rPr.rFonts
            for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
                rfonts.attrib.pop(qn(attr), None)
            rfonts.set(qn("w:ascii"), t.font_heading)
            rfonts.set(qn("w:hAnsi"), t.font_heading)
            st.font.size = Pt(size)
            st.font.bold = True
            st.font.color.rgb = rgb(t.primary if lvl <= 2 else t.text)
            st.paragraph_format.space_before = Pt(14 if lvl == 1 else 10)
            st.paragraph_format.space_after = Pt(6)
            st.paragraph_format.keep_with_next = True
        for name in ("Title", "Subtitle"):
            st = doc.styles[name]
            st.font.name = t.font_heading
            rfonts = st.element.rPr.rFonts
            for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
                rfonts.attrib.pop(qn(attr), None)
        title_st = doc.styles["Title"]
        title_st.font.size = Pt(28)
        title_st.font.color.rgb = rgb(t.primary)
        pPr = title_st.element.get_or_add_pPr()
        for b in pPr.findall(qn("w:pBdr")):  # remove default underline border on Title
            pPr.remove(b)
        doc.styles["Subtitle"].font.color.rgb = rgb(t.secondary)
        doc.styles["Subtitle"].font.size = Pt(14)

        cp = doc.core_properties
        cp.title = self.spec.title or Path(self.spec.filename).stem
        cp.author = self.spec.author or "Web Agent"
        cp.subject = self.spec.subtitle

        self._header_footer(sec)

    def _header_footer(self, sec) -> None:
        t = self.t
        if self.spec.header_text:
            p = sec.header.paragraphs[0]
            p.text = ""
            _add_runs(p, self.spec.header_text, t, size=8.5, color="6B7280")
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        p = sec.footer.paragraphs[0]
        p.text = ""
        if self.spec.footer_text:
            _add_runs(p, self.spec.footer_text, t, size=8.5, color="6B7280")
        if self.spec.page_numbers:
            if self.spec.footer_text:
                p.add_run("    ")
            run = p.add_run("Page ")
            run.font.size = Pt(8.5)
            _field(p, "PAGE", "1")
            p.add_run(" of ").font.size = Pt(8.5)
            _field(p, "NUMPAGES", "1")
            for r in p.runs:
                r.font.size = Pt(8.5)
                r.font.color.rgb = rgb("6B7280")
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER if not self.spec.footer_text else WD_ALIGN_PARAGRAPH.LEFT

    # ----------------------------------------------------------------- content
    def build(self, out: Path) -> Path:
        s = self.spec
        if s.cover_page:
            self.doc.sections[0].different_first_page_header_footer = True
            self._cover()
        elif s.title:
            self.doc.add_paragraph(s.title, style="Title")
            if s.subtitle:
                self.doc.add_paragraph(s.subtitle, style="Subtitle")
        for block in s.blocks:
            try:
                getattr(self, f"_b_{block.type}")(block)
            except Exception as exc:  # noqa: BLE001 - never lose the whole document for one block
                self.ctx.warn(f"{block.type} block skipped: {exc}")
                p = self.doc.add_paragraph()
                _add_runs(p, f"[{block.type} could not be rendered: {exc}]", self.t, color="B91C1C", italic=True)
        self.doc.save(out)
        return out

    def _cover(self) -> None:
        s, doc = self.spec, self.doc
        for _ in range(6):
            doc.add_paragraph()
        p = doc.add_paragraph(s.title or Path(s.filename).stem, style="Title")
        _para_border(p, "left", self.t.accent, size=36, space=12)
        if s.subtitle:
            doc.add_paragraph(s.subtitle, style="Subtitle")
        meta = doc.add_paragraph()
        _add_runs(meta, " · ".join(x for x in (s.author, date.today().strftime("%d %B %Y")) if x), self.t,
                  size=10, color="6B7280")
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    def _b_heading(self, b: Block) -> None:
        p = self.doc.add_paragraph(style=f"Heading {b.level}")
        _add_runs(p, b.text, self.t)

    def _b_paragraph(self, b: Block) -> None:
        if b.align == "justify":  # justified lines before a manual break get stretched - use paragraphs
            for line in [x for x in (b.text or "").split("\n") if x.strip()]:
                p = self.doc.add_paragraph()
                p.alignment = ALIGN["justify"]
                _add_runs(p, line, self.t)
            return
        for chunk in (b.text or "").split("\n\n"):
            p = self.doc.add_paragraph()
            p.alignment = ALIGN[b.align]
            lines = chunk.split("\n")
            for i, line in enumerate(lines):
                _add_runs(p, line, self.t)
                if i < len(lines) - 1:
                    p.add_run().add_break()

    def _list(self, b: Block, ordered: bool) -> None:
        base = "List Number" if ordered else "List Bullet"
        num_id = _new_list_num(self.doc, base) if ordered else None
        for item in b.items:
            it = item if isinstance(item, ListItem) else ListItem(text=str(item))
            lvl = min(it.level, 2)
            style = base if lvl == 0 else f"{base} {lvl + 1}"
            p = self.doc.add_paragraph(style=style)
            if num_id and lvl == 0:
                _set_num(p, num_id, 0)
            p.paragraph_format.space_after = Pt(2)
            _add_runs(p, it.text, self.t)
        self.doc.paragraphs[-1].paragraph_format.space_after = Pt(8)

    def _b_bullets(self, b: Block) -> None:
        self._list(b, ordered=False)

    def _b_numbered(self, b: Block) -> None:
        self._list(b, ordered=True)

    def _table(self, columns: list[str], rows: list[list], spec: TableSpec | None, header: bool = True) -> None:
        t = self.t
        n = max(len(columns), 1)
        table = self.doc.add_table(rows=0, cols=n)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.autofit = False
        _table_borders(table)
        _cell_margins(table)
        weights = (spec.col_widths if spec and len(spec.col_widths) == n else None) or self._auto_weights(columns, rows)
        total = sum(weights)
        widths = [Emu(int(self.content_width * w / total)) for w in weights]
        numeric = [bool(rows) and all(is_numeric_cell(r[i]) or r[i] in (None, "") for r in rows) and
                   any(is_numeric_cell(r[i]) for r in rows) for i in range(n)]
        aligns = (spec.align if spec and len(spec.align) == n else
                  ["right" if numeric[i] else "left" for i in range(n)])
        small = n > 6 or len(rows) > 40
        fsize = 8.5 if small else 9.5

        def fill_row(values, is_header=False, shade=None, bold=False):
            row = table.add_row()
            for i, cell in enumerate(row.cells):
                cell.width = widths[i]
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                p = cell.paragraphs[0]
                p.paragraph_format.space_after = Pt(0)
                p.alignment = ALIGN["left" if is_header and not numeric[i] else aligns[i]]
                val = values[i] if i < len(values) else None
                _add_runs(p, format_cell(val), t, size=fsize, bold=is_header or bold,
                          color="FFFFFF" if is_header else None)
                if is_header:
                    _shade(cell, t.primary)
                elif shade:
                    _shade(cell, shade)
            return row

        if header and columns:
            _repeat_header(fill_row(columns, is_header=True))
        zebra = mix(t.primary, "FFFFFF", 0.93)
        for ri, r in enumerate(rows):
            fill_row(r, shade=zebra if (spec is None or spec.zebra) and ri % 2 == 1 else None)
        if spec and spec.total_row and rows:
            totals = ["Total"] + [sum(x for x in (r[i] for r in rows) if is_numeric_cell(x)) if numeric[i] else ""
                                  for i in range(1, n)]
            fill_row(totals, shade=mix(t.primary, "FFFFFF", 0.82), bold=True)
        for i, col in enumerate(table.columns):
            col.width = widths[i]
        if spec and spec.caption:
            cap = self.doc.add_paragraph()
            _add_runs(cap, spec.caption, t, size=8.5, color="6B7280", italic=True)
        else:
            self.doc.add_paragraph().paragraph_format.space_after = Pt(2)

    @staticmethod
    def _auto_weights(columns, rows) -> list[float]:
        weights = []
        for i, c in enumerate(columns):
            lens = [len(format_cell(r[i])) for r in rows[:200]] + [len(str(c))]
            avg = sum(sorted(lens)[-max(1, len(lens) // 2):]) / max(1, len(lens) // 2)
            weights.append(min(max(avg, 4), 40))
        return weights or [1]

    def _b_table(self, b: Block) -> None:
        if not b.table:
            raise ValueError("table block requires 'table'")
        columns, rows = resolve_table(b.table, self.ctx)
        self._table(columns, rows, b.table)

    def _b_key_value(self, b: Block) -> None:
        rows = [[(p[0] if p else ""), (p[1] if len(p) > 1 else "")] for p in b.pairs]
        spec = TableSpec(col_widths=[1, 2.2], zebra=False, align=["left", "left"])
        table_start = len(self.doc.tables)
        self._table(["", ""], rows, spec, header=False)
        tbl = self.doc.tables[table_start]
        for row in tbl.rows:
            _shade(row.cells[0], mix(self.t.primary, "FFFFFF", 0.9))
            for r in row.cells[0].paragraphs[0].runs:
                r.bold = True

    def _picture(self, data_or_path, width_in: float, caption: str) -> None:
        width = min(Inches(width_in), self.content_width)
        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        src = io.BytesIO(data_or_path) if isinstance(data_or_path, bytes) else str(data_or_path)
        p.add_run().add_picture(src, width=width)
        if caption:
            c = self.doc.add_paragraph()
            c.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _add_runs(c, caption, self.t, size=8.5, color="6B7280", italic=True)

    def _b_image(self, b: Block) -> None:
        path = self.ctx.image_resolver(b.image)
        if not path:
            raise ValueError(f"image '{b.image}' not found")
        self._picture(path, b.width_in, b.caption)

    def _b_chart(self, b: Block) -> None:
        if not b.chart:
            raise ValueError("chart block requires 'chart'")
        width = min(b.width_in, self.content_width / 914400)
        self._picture(render_chart_png(b.chart, self.ctx, self.t, width), width, b.caption)

    def _b_callout(self, b: Block) -> None:
        border, fill = CALLOUT_COLORS[b.style]
        table = self.doc.add_table(rows=1, cols=1)
        table.autofit = False
        _cell_margins(table, 120, 120, 180, 180)
        cell = table.rows[0].cells[0]
        cell.width = self.content_width
        _shade(cell, fill)
        _cell_borders(cell, left=(border, 24), top=("FFFFFF", 0), bottom=("FFFFFF", 0), right=("FFFFFF", 0))
        p = cell.paragraphs[0]
        p.paragraph_format.space_after = Pt(2 if b.title else 0)
        if b.title:
            _add_runs(p, b.title, self.t, bold=True, color=border)
            p = cell.add_paragraph()
            p.paragraph_format.space_after = Pt(0)
        _add_runs(p, b.text, self.t)
        self.doc.add_paragraph().paragraph_format.space_after = Pt(2)

    def _b_quote(self, b: Block) -> None:
        p = self.doc.add_paragraph()
        p.paragraph_format.left_indent = Inches(0.3)
        _para_border(p, "left", self.t.accent, size=18, space=10)
        _add_runs(p, b.text, self.t, italic=True, size=11, color="374151")
        if b.caption:
            c = self.doc.add_paragraph()
            c.paragraph_format.left_indent = Inches(0.3)
            _add_runs(c, f"— {b.caption}", self.t, size=9, color="6B7280")

    def _b_code(self, b: Block) -> None:
        for line in (b.text or "").split("\n"):
            p = self.doc.add_paragraph()
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.line_spacing = 1.0
            _para_shading(p, "F3F4F6")
            run = p.add_run(line or " ")
            run.font.name = "Consolas"
            run.font.size = Pt(9)
        self.doc.add_paragraph().paragraph_format.space_after = Pt(2)

    def _b_page_break(self, b: Block) -> None:
        self.doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    def _b_divider(self, b: Block) -> None:
        p = self.doc.add_paragraph()
        _para_border(p, "bottom", "D0D7DE", size=6, space=1)

    def _b_toc(self, b: Block) -> None:
        p = self.doc.add_paragraph(style="Heading 1")
        p.add_run(b.text or "Contents")
        p = self.doc.add_paragraph()
        _field(p, 'TOC \\o "1-3" \\h \\z \\u', "Right-click and choose Update Field to build the table of contents.")
        settings = self.doc.settings.element
        upd = OxmlElement("w:updateFields")
        upd.set(qn("w:val"), "true")
        settings.append(upd)


def build_docx(spec: DocumentSpec, out: Path, ctx: GenContext | None = None) -> Path:
    return WordBuilder(spec, ctx or GenContext()).build(out)
