"""PDF generator - ReportLab Platypus (same block model as Word)."""
from __future__ import annotations

import io
import os
from datetime import date
from functools import lru_cache
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, LETTER, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Frame, HRFlowable, Image, KeepTogether,
                                ListFlowable, ListItem as RLListItem, PageBreak, PageTemplate, Paragraph,
                                Spacer, Table, TableStyle, XPreformatted)
from reportlab.platypus.tableofcontents import TableOfContents

from .common import (CALLOUT_COLORS, GenContext, format_cell, image_size, is_numeric_cell, mix, parse_inline,
                     render_chart_png, resolve_table, strip_inline)
from .specs import Block, DocumentSpec, ListItem, TableSpec

FONT_CANDIDATES = [
    ("Arial", ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf")),
    ("DejaVuSans", ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans-Oblique.ttf", "DejaVuSans-BoldOblique.ttf")),
    ("LiberationSans", ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf", "LiberationSans-Italic.ttf",
                        "LiberationSans-BoldItalic.ttf")),
]
FONT_DIRS = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"), "/usr/share/fonts",
             "/usr/local/share/fonts", "/Library/Fonts", "/System/Library/Fonts/Supplemental",
             os.path.expanduser("~/.fonts"), os.path.expanduser("~/.local/share/fonts")]


@lru_cache
def _font_family() -> tuple[str, str, str, str]:
    """Register a Unicode TTF family (₹, €, accents) - falls back to Helvetica."""
    index: dict[str, str] = {}
    for d in FONT_DIRS:
        if os.path.isdir(d):
            for root, _, files in os.walk(d):
                for f in files:
                    index.setdefault(f.lower(), os.path.join(root, f))
    for family, files in FONT_CANDIDATES:
        paths = [index.get(f.lower()) for f in files]
        if all(paths):
            names = (family, f"{family}-Bold", f"{family}-Italic", f"{family}-BoldItalic")
            try:
                for n, p in zip(names, paths):
                    pdfmetrics.registerFont(TTFont(n, p))
                pdfmetrics.registerFontFamily(family, normal=names[0], bold=names[1], italic=names[2],
                                              boldItalic=names[3])
                return names
            except Exception:  # noqa: BLE001
                continue
    return ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique")


def C(h: str) -> colors.Color:
    return colors.HexColor("#" + h.lstrip("#"))


def markup(text: str, link_color: str = "2563EB") -> str:
    """Inline markdown -> ReportLab paragraph markup (escaped)."""
    out = []
    for r in parse_inline(text or ""):
        s = escape(r.text)
        if r.code:
            s = f'<font face="Courier" backColor="#F3F4F6">{s}</font>'
        if r.bold:
            s = f"<b>{s}</b>"
        if r.italic:
            s = f"<i>{s}</i>"
        if r.link:
            s = f'<link href="{escape(r.link)}" color="#{link_color}"><u>{s}</u></link>'
        out.append(s)
    return "".join(out).replace("\n", "<br/>")


class _NumberedCanvas(rl_canvas.Canvas):
    """Two-pass canvas so the footer can say 'Page X of Y'."""

    def __init__(self, *args, decorate=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved: list[dict] = []
        self._decorate = decorate

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            if self._decorate:
                self._decorate(self, total)
            super().showPage()
        super().save()


class _Doc(BaseDocTemplate):
    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and getattr(flowable, "_toc_level", None) is not None:
            key = f"h{id(flowable)}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(flowable.getPlainText(), key, level=flowable._toc_level, closed=False)
            self.notify("TOCEntry", (flowable._toc_level, flowable.getPlainText(), self.page, key))


class PdfBuilder:
    def __init__(self, spec: DocumentSpec, ctx: GenContext):
        self.spec, self.ctx, self.t = spec, ctx, spec.theme
        self.font, self.bold, self.italic, _ = _font_family()
        size = A4 if spec.page_size == "A4" else LETTER
        self.pagesize = landscape(size) if spec.orientation == "landscape" else size
        self.margin = 0.8 * inch
        self.width = self.pagesize[0] - 2 * self.margin
        self._styles()
        self.story: list = []
        self.has_toc = False

    def _styles(self) -> None:
        t, f, b = self.t, self.font, self.bold
        base = getSampleStyleSheet()
        self.st = {
            "body": ParagraphStyle("body", parent=base["Normal"], fontName=f, fontSize=10, leading=14.5,
                                   textColor=C(t.text), spaceAfter=6),
            "title": ParagraphStyle("title", fontName=b, fontSize=26, leading=31, textColor=C(t.primary),
                                    spaceAfter=6),
            "subtitle": ParagraphStyle("subtitle", fontName=f, fontSize=13, leading=17, textColor=C(t.secondary),
                                       spaceAfter=14),
            "small": ParagraphStyle("small", fontName=f, fontSize=8.5, leading=11, textColor=C("6B7280")),
            "caption": ParagraphStyle("caption", fontName=self.italic, fontSize=8.5, leading=11,
                                      textColor=C("6B7280"), alignment=TA_CENTER, spaceBefore=3, spaceAfter=10),
            "cell": ParagraphStyle("cell", fontName=f, fontSize=8.8, leading=11.2, textColor=C(t.text)),
            "cell_r": ParagraphStyle("cell_r", fontName=f, fontSize=8.8, leading=11.2, alignment=TA_RIGHT),
            "cell_h": ParagraphStyle("cell_h", fontName=b, fontSize=8.8, leading=11.2, textColor=colors.white),
            "quote": ParagraphStyle("quote", fontName=self.italic, fontSize=11, leading=16, textColor=C("374151")),
            "code": ParagraphStyle("code", fontName="Courier", fontSize=8.5, leading=11, textColor=C("111827")),
        }
        for lvl, (size, color) in {1: (17, t.primary), 2: (13.5, t.primary), 3: (11.5, t.text),
                                   4: (10.5, t.text)}.items():
            self.st[f"h{lvl}"] = ParagraphStyle(f"h{lvl}", fontName=b, fontSize=size, leading=size * 1.3,
                                                textColor=C(color), spaceBefore=14 if lvl == 1 else 10,
                                                spaceAfter=6, keepWithNext=1)

    # ---------------------------------------------------------------- page decoration
    def _decorate(self, canv, total: int) -> None:
        page = canv.getPageNumber()
        if self.spec.cover_page and page == 1:
            return
        w, h = self.pagesize
        canv.saveState()
        canv.setFont(self.font, 8)
        canv.setFillColor(C("6B7280"))
        if self.spec.header_text:
            canv.drawRightString(w - self.margin, h - 0.5 * inch, strip_inline(self.spec.header_text))
            canv.setStrokeColor(C("E5E7EB"))
            canv.setLineWidth(0.5)
            canv.line(self.margin, h - 0.56 * inch, w - self.margin, h - 0.56 * inch)
        if self.spec.footer_text:
            canv.drawString(self.margin, 0.45 * inch, strip_inline(self.spec.footer_text))
        if self.spec.page_numbers:
            canv.drawRightString(w - self.margin, 0.45 * inch, f"Page {page} of {total}")
        canv.restoreState()

    # ---------------------------------------------------------------- blocks
    def build(self, out: Path) -> Path:
        s = self.spec
        if s.cover_page:
            self._cover()
        elif s.title:
            self.story.append(Paragraph(markup(s.title), self.st["title"]))
            if s.subtitle:
                self.story.append(Paragraph(markup(s.subtitle), self.st["subtitle"]))
        for block in s.blocks:
            try:
                getattr(self, f"_b_{block.type}")(block)
            except Exception as exc:  # noqa: BLE001
                self.ctx.warn(f"{block.type} block skipped: {exc}")
                self.story.append(Paragraph(f'<font color="#B91C1C"><i>[{escape(block.type)} could not be '
                                            f'rendered: {escape(str(exc))}]</i></font>', self.st["body"]))

        doc = _Doc(str(out), pagesize=self.pagesize, leftMargin=self.margin, rightMargin=self.margin,
                   topMargin=self.margin, bottomMargin=self.margin, title=s.title or Path(s.filename).stem,
                   author=s.author or "Web Agent", subject=s.subtitle)
        frame = Frame(self.margin, self.margin, self.width, self.pagesize[1] - 2 * self.margin, id="body")
        doc.addPageTemplates([PageTemplate(id="page", frames=[frame])])
        maker = lambda *a, **k: _NumberedCanvas(*a, decorate=self._decorate, **k)  # noqa: E731
        if self.has_toc:
            doc.multiBuild(self.story, canvasmaker=maker)
        else:
            doc.build(self.story, canvasmaker=maker)
        return out

    def _cover(self) -> None:
        s, t = self.spec, self.t
        self.story.append(Spacer(1, 2.2 * inch))
        band = Table([[Paragraph(markup(s.title or Path(s.filename).stem), self.st["title"])]],
                     colWidths=[self.width])
        band.setStyle(TableStyle([("LINEBEFORE", (0, 0), (0, 0), 5, C(t.accent)),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 16)]))
        self.story.append(band)
        if s.subtitle:
            sub = ParagraphStyle("cs", parent=self.st["subtitle"], leftIndent=21)
            self.story.append(Paragraph(markup(s.subtitle), sub))
        meta = " · ".join(x for x in (s.author, date.today().strftime("%d %B %Y")) if x)
        self.story.append(Paragraph(escape(meta), ParagraphStyle("cm", parent=self.st["small"], leftIndent=21)))
        self.story.append(PageBreak())

    def _b_heading(self, b: Block) -> None:
        p = Paragraph(markup(b.text), self.st[f"h{b.level}"])
        p._toc_level = b.level - 1 if b.level <= 3 else None
        if b.level <= 2:  # avoid headings stranded at the bottom of a page
            self.story.append(CondPageBreak((2.4 if b.level == 1 else 1.6) * inch))
        self.story.append(p)

    def _b_paragraph(self, b: Block) -> None:
        style = ParagraphStyle("p", parent=self.st["body"], alignment={"left": TA_LEFT, "center": TA_CENTER,
                                                                      "right": TA_RIGHT,
                                                                      "justify": TA_JUSTIFY}[b.align])
        for chunk in (b.text or "").split("\n\n"):
            self.story.append(Paragraph(markup(chunk), style))

    def _list(self, b: Block, ordered: bool) -> None:
        items = [i if isinstance(i, ListItem) else ListItem(text=str(i)) for i in b.items]

        def build(level: int, start: int) -> tuple[ListFlowable, int]:
            entries: list[list] = []          # each entry = flowables of one list item
            i = start
            while i < len(items):
                it = items[i]
                if it.level < level:
                    break
                if it.level > level:
                    nested, i = build(it.level, i)
                    if entries:
                        entries[-1].append(nested)   # nest inside the previous item (no extra bullet)
                    else:
                        entries.append([nested])
                    continue
                entries.append([Paragraph(markup(it.text), ParagraphStyle("li", parent=self.st["body"], spaceAfter=2))])
                i += 1
            kind = dict(bulletType="1", bulletFormat="%s.") if ordered else dict(
                bulletType="bullet", start="•" if level == 0 or self.font == "Helvetica" else ("◦" if level == 1 else "▪"))
            flow = [RLListItem(e if len(e) > 1 else e[0]) for e in entries]
            return ListFlowable(flow, leftIndent=16, bulletFontName=self.font, bulletFontSize=9,
                                bulletColor=C(self.t.primary), **kind), i

        flow, _ = build(0, 0)
        self.story.extend([flow, Spacer(1, 4)])

    def _b_bullets(self, b: Block) -> None:
        self._list(b, False)

    def _b_numbered(self, b: Block) -> None:
        self._list(b, True)

    def _table(self, columns, rows, spec: TableSpec | None, header: bool = True, kv: bool = False) -> None:
        t = self.t
        n = max(len(columns), 1)
        numeric = [bool(rows) and all(is_numeric_cell(r[i]) or r[i] in (None, "") for r in rows) and
                   any(is_numeric_cell(r[i]) for r in rows) for i in range(n)]
        weights = (spec.col_widths if spec and len(spec.col_widths) == n else None) or self._auto_weights(columns, rows)
        widths = [self.width * w / sum(weights) for w in weights]
        aligns = spec.align if spec and len(spec.align) == n else ["right" if x else "left" for x in numeric]
        cell_style = lambda i: self.st["cell_r"] if aligns[i] == "right" else (  # noqa: E731
            ParagraphStyle("cc", parent=self.st["cell"], alignment=TA_CENTER) if aligns[i] == "center"
            else self.st["cell"])

        data = []
        if header and columns:
            data.append([Paragraph(markup(str(c)), self.st["cell_h"]) for c in columns])
        for r in rows:
            data.append([Paragraph(markup(format_cell(v)), cell_style(i)) for i, v in enumerate(r[:n])])
        if spec and spec.total_row and rows:
            tot = ["Total"] + [format_cell(sum(x for x in (r[i] for r in rows) if is_numeric_cell(x)))
                               if numeric[i] else "" for i in range(1, n)]
            data.append([Paragraph(f"<b>{escape(v)}</b>", cell_style(i)) for i, v in enumerate(tot)])
        if not data:
            return
        tbl = Table(data, colWidths=widths, repeatRows=1 if header and columns else 0, hAlign="CENTER")
        style = [
            ("GRID", (0, 0), (-1, -1), 0.4, C("D0D7DE")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]
        start = 0
        if header and columns:
            style.append(("BACKGROUND", (0, 0), (-1, 0), C(t.primary)))
            start = 1
        if kv:
            style.append(("BACKGROUND", (0, 0), (0, -1), C(mix(t.primary, "FFFFFF", 0.9))))
        elif spec is None or spec.zebra:
            for ri in range(start + 1, len(data), 2):
                style.append(("BACKGROUND", (0, ri), (-1, ri), C(mix(t.primary, "FFFFFF", 0.93))))
        if spec and spec.total_row and rows:
            style += [("BACKGROUND", (0, -1), (-1, -1), C(mix(t.primary, "FFFFFF", 0.82))),
                      ("LINEABOVE", (0, -1), (-1, -1), 1.2, C(t.primary))]
        tbl.setStyle(TableStyle(style))
        self.story.append(tbl)
        if spec and spec.caption:
            self.story.append(Paragraph(markup(spec.caption), self.st["caption"]))
        else:
            self.story.append(Spacer(1, 8))

    @staticmethod
    def _auto_weights(columns, rows):
        out = []
        for i, c in enumerate(columns):
            lens = sorted(len(format_cell(r[i])) for r in rows[:200]) + [len(str(c))]
            top = lens[-max(1, len(lens) // 2):]
            out.append(min(max(sum(top) / len(top), 4), 40))
        return out or [1]

    def _b_table(self, b: Block) -> None:
        if not b.table:
            raise ValueError("table block requires 'table'")
        columns, rows = resolve_table(b.table, self.ctx)
        self._table(columns, rows, b.table)

    def _b_key_value(self, b: Block) -> None:
        rows = [[f"**{p[0]}**" if p else "", p[1] if len(p) > 1 else ""] for p in b.pairs]
        self._table(["", ""], rows, TableSpec(col_widths=[1, 2.2], zebra=False), header=False, kv=True)

    def _image(self, src, width_in: float, caption: str) -> None:
        w_px, h_px = image_size(src)
        width = min(width_in * inch, self.width)
        height = width * h_px / w_px
        max_h = self.pagesize[1] - 2 * self.margin - 1 * inch
        if height > max_h:
            width, height = width * max_h / height, max_h
        img = Image(io.BytesIO(src) if isinstance(src, bytes) else str(src), width=width, height=height)
        parts = [img] + ([Paragraph(markup(caption), self.st["caption"])] if caption else [Spacer(1, 8)])
        self.story.append(KeepTogether(parts))

    def _b_image(self, b: Block) -> None:
        path = self.ctx.image_resolver(b.image)
        if not path:
            raise ValueError(f"image '{b.image}' not found")
        self._image(path, b.width_in, b.caption)

    def _b_chart(self, b: Block) -> None:
        if not b.chart:
            raise ValueError("chart block requires 'chart'")
        width = min(b.width_in, self.width / inch)
        self._image(render_chart_png(b.chart, self.ctx, self.t, width), width, b.caption)

    def _b_callout(self, b: Block) -> None:
        border, fill = CALLOUT_COLORS[b.style]
        content = []
        if b.title:
            content.append(Paragraph(f'<font color="#{border}"><b>{markup(b.title)}</b></font>', self.st["body"]))
        content.append(Paragraph(markup(b.text), ParagraphStyle("co", parent=self.st["body"], spaceAfter=0)))
        tbl = Table([[content]], colWidths=[self.width])
        tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), C(fill)),
                                 ("LINEBEFORE", (0, 0), (0, -1), 3.5, C(border)),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                                 ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
        self.story.extend([tbl, Spacer(1, 10)])

    def _b_quote(self, b: Block) -> None:
        content = [Paragraph(markup(b.text), self.st["quote"])]
        if b.caption:
            content.append(Paragraph(f"— {escape(b.caption)}", self.st["small"]))
        tbl = Table([[content]], colWidths=[self.width - 20])
        tbl.setStyle(TableStyle([("LINEBEFORE", (0, 0), (0, -1), 3, C(self.t.accent)),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 14)]))
        self.story.extend([tbl, Spacer(1, 10)])

    def _b_code(self, b: Block) -> None:
        pre = XPreformatted(escape(b.text or ""), self.st["code"])
        tbl = Table([[pre]], colWidths=[self.width])
        tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), C("F3F4F6")),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 8),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
        self.story.extend([tbl, Spacer(1, 10)])

    def _b_page_break(self, b: Block) -> None:
        self.story.append(PageBreak())

    def _b_divider(self, b: Block) -> None:
        self.story.append(HRFlowable(width="100%", thickness=0.6, color=C("D0D7DE"), spaceBefore=6, spaceAfter=10))

    def _b_toc(self, b: Block) -> None:
        self.has_toc = True
        toc = TableOfContents()
        toc.levelStyles = [
            ParagraphStyle("t0", fontName=self.bold, fontSize=10.5, leading=16, leftIndent=0),
            ParagraphStyle("t1", fontName=self.font, fontSize=10, leading=14, leftIndent=14),
            ParagraphStyle("t2", fontName=self.font, fontSize=9.5, leading=13, leftIndent=28),
        ]
        self.story.extend([Paragraph(markup(b.text or "Contents"), self.st["h1"]), toc, PageBreak()])


def build_pdf(spec: DocumentSpec, out: Path, ctx: GenContext | None = None) -> Path:
    return PdfBuilder(spec, ctx or GenContext()).build(out)
