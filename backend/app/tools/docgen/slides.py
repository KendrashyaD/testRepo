"""PowerPoint (.pptx) generator - python-pptx, 16:9, themed layouts drawn on blank slides."""
from __future__ import annotations

import math
from pathlib import Path

from pptx import Presentation
from pptx.chart.data import CategoryChartData, XyChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_LABEL_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

from .common import (GenContext, format_cell, hex_rgb, image_size, is_numeric_cell, mix, palette, parse_inline,
                     resolve_chart, resolve_table, strip_inline, to_number)
from .specs import Column, ListItem, PresentationSpec, SlideSpec, Stat, TableSpec

W, H = 13.333, 7.5
M = 0.6               # outer margin (in)
TITLE_TOP = 0.45
BODY_TOP = 1.55
MAX_TABLE_ROWS = 12

CHART_TYPES = {
    "column": XL_CHART_TYPE.COLUMN_CLUSTERED, "bar": XL_CHART_TYPE.BAR_CLUSTERED, "line": XL_CHART_TYPE.LINE_MARKERS,
    "pie": XL_CHART_TYPE.PIE, "doughnut": XL_CHART_TYPE.DOUGHNUT, "area": XL_CHART_TYPE.AREA,
    "scatter": XL_CHART_TYPE.XY_SCATTER,
}
STACKED = {"column": XL_CHART_TYPE.COLUMN_STACKED, "bar": XL_CHART_TYPE.BAR_STACKED,
           "area": XL_CHART_TYPE.AREA_STACKED}


def rgb(h: str) -> RGBColor:
    return RGBColor(*hex_rgb(h))


def fit_pt(text: str, w_in: float, h_in: float, max_pt: float, min_pt: float = 10, line: float = 1.25) -> float:
    """Rough font-size fitting (python-pptx cannot measure text)."""
    paragraphs = [p for p in (text or " ").split("\n")] or [" "]
    pt = max_pt
    while pt > min_pt:
        cpl = max(1, int(w_in * 72 / (pt * 0.52)))
        lines = sum(max(1, math.ceil(len(p) / cpl)) for p in paragraphs)
        if lines * pt * line / 72 <= h_in:
            return pt
        pt -= 1
    return min_pt


class SlideBuilder:
    def __init__(self, spec: PresentationSpec, ctx: GenContext):
        self.spec, self.ctx, self.t = spec, ctx, spec.theme
        self.prs = Presentation()
        self.prs.slide_width, self.prs.slide_height = Inches(W), Inches(H)
        self.blank = self.prs.slide_layouts[6]
        self.n = 0
        self.section_no = 0

    # ---------------------------------------------------------------- primitives
    def _slide(self, dark: bool = False):
        s = self.prs.slides.add_slide(self.blank)
        self.n += 1
        bg = s.background.fill
        bg.solid()
        bg.fore_color.rgb = rgb(self.t.primary if dark else "FFFFFF")
        if not dark:  # motif: slim primary band on the left edge of every content slide
            self._rect(s, 0, 0, 0.16, H, self.t.primary)
        return s

    def _rect(self, s, x, y, w, h, fill, shape=MSO_SHAPE.RECTANGLE, line: str | None = None):
        shp = s.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
        shp.fill.solid()
        shp.fill.fore_color.rgb = rgb(fill)
        if line:
            shp.line.color.rgb = rgb(line)
            shp.line.width = Pt(1)
        else:
            shp.line.fill.background()
        shp.shadow.inherit = False
        if shape == MSO_SHAPE.ROUNDED_RECTANGLE:
            shp.adjustments[0] = 0.08
        return shp

    def _text(self, s, x, y, w, h, text, size=16, color=None, bold=False, font=None, align=PP_ALIGN.LEFT,
              anchor=MSO_ANCHOR.TOP, italic=False, spacing=1.1):
        tb = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = tb.text_frame
        tf.word_wrap = True
        tf.vertical_anchor = anchor
        for attr in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
            setattr(tf, attr, 0)
        for i, line in enumerate((text or "").split("\n")):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.alignment = align
            p.line_spacing = spacing
            self._runs(p, line, size, color or self.t.text, bold, font or self.t.font_body, italic)
        return tb

    def _runs(self, p, text, size, color, bold=False, font=None, italic=False):
        for r in parse_inline(text):
            run = p.add_run()
            run.text = r.text
            f = run.font
            f.size, f.bold, f.italic = Pt(size), bold or r.bold, italic or r.italic
            f.name = "Consolas" if r.code else (font or self.t.font_body)
            f.color.rgb = rgb("2563EB" if r.link else color)
            if r.link:
                run.hyperlink.address = r.link

    def _title(self, s, title, subtitle=""):
        size = fit_pt(title, W - 2 * M, 0.75, 34, 22)
        self._text(s, M, TITLE_TOP, W - 2 * M, 0.8, title, size=size, bold=True, font=self.t.font_heading,
                   color=self.t.primary, anchor=MSO_ANCHOR.MIDDLE)
        if subtitle:
            self._text(s, M, TITLE_TOP + 0.78, W - 2 * M, 0.35, subtitle, size=14, color="6B7280")

    def _footer(self, s, spec: SlideSpec, dark=False):
        color = mix(self.t.primary, "FFFFFF", 0.55) if dark else "9CA3AF"
        if spec.source:
            self._text(s, M, H - 0.42, W - 2 * M - 1, 0.3, spec.source, size=9, color=color, italic=True)
        if self.spec.slide_numbers and self.n > 1:
            self._text(s, W - M - 0.8, H - 0.42, 0.8, 0.3, str(self.n), size=10, color=color, align=PP_ALIGN.RIGHT)
        if spec.notes:
            s.notes_slide.notes_text_frame.text = spec.notes

    def _bullets(self, s, x, y, w, h, items, size=None, color=None):
        items = [i if isinstance(i, ListItem) else ListItem(text=str(i)) for i in items]
        text = "\n".join(strip_inline(i.text) for i in items)
        size = size or fit_pt(text, w - 0.4, h, 20, 11, line=1.5)
        tb = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = tb.text_frame
        tf.word_wrap = True
        for attr in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
            setattr(tf, attr, 0)
        for k, it in enumerate(items):
            p = tf.paragraphs[0] if k == 0 else tf.add_paragraph()
            p.level = min(it.level, 4)
            p.space_after = Pt(size * 0.55)
            p.line_spacing = 1.1
            bullet = "■ " if it.level == 0 else "– "
            r = p.add_run()
            r.text = "    " * it.level + bullet
            r.font.size = Pt(size * (0.55 if it.level == 0 else 0.9))
            r.font.color.rgb = rgb(self.t.accent)
            r.font.name = self.t.font_body
            self._runs(p, it.text, size - 2 * it.level, color or self.t.text)
        return tb

    def _badge(self, s, x, y, d, number, color):
        b = self._rect(s, x, y, d, d, color, MSO_SHAPE.OVAL)
        b.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER
        b.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        self._runs(b.text_frame.paragraphs[0], str(number), d * 26, "FFFFFF", bold=True, font=self.t.font_heading)
        return b

    @staticmethod
    def _split_point(text):
        """'Heading: detail' -> (heading, detail); otherwise ('', text)."""
        plain = strip_inline(text)
        head, sep, rest = plain.partition(":")
        if sep and rest.strip() and len(head) <= 60:
            return head.strip(), rest.strip()
        return "", text

    def _picture(self, s, path, x, y, w, h):
        pw, ph = image_size(path)
        scale = min(w / pw, h / ph)
        iw, ih = pw * scale, ph * scale
        pic = s.shapes.add_picture(str(path), Inches(x + (w - iw) / 2), Inches(y + (h - ih) / 2), Inches(iw),
                                   Inches(ih))
        pic.line.color.rgb = rgb("E5E7EB")
        pic.line.width = Pt(0.75)
        return pic

    # ---------------------------------------------------------------- layouts
    def build(self, out: Path) -> Path:
        for spec in self.spec.slides:
            before = len(self.prs.slides)
            try:
                getattr(self, f"_l_{spec.layout}")(spec)
            except Exception as exc:  # noqa: BLE001
                self._drop_slides_after(before)
                self.ctx.warn(f"slide '{spec.title}' ({spec.layout}) fell back to bullets: {exc}")
                s = self._slide()
                self._title(s, spec.title or spec.layout)
                self._bullets(s, M, BODY_TOP, W - 2 * M, 4.8,
                              spec.bullets or [spec.text or f"Content could not be rendered: {exc}"])
                self._footer(s, spec)
        self.prs.core_properties.title = self.spec.title or Path(self.spec.filename).stem
        self.prs.core_properties.author = "Web Agent"
        self.prs.save(out)
        return out

    def _drop_slides_after(self, count: int) -> None:
        sld_ids = self.prs.slides._sldIdLst
        for sld in list(sld_ids)[count:]:
            self.prs.part.drop_rel(sld.rId)
            sld_ids.remove(sld)
        self.n = count

    def _l_title(self, sp: SlideSpec):
        s, t = self._slide(dark=True), self.t
        self._rect(s, 9.4, -1.2, 5.6, 5.6, mix(t.primary, "FFFFFF", 0.12), MSO_SHAPE.OVAL)
        self._rect(s, 10.9, 3.9, 3.4, 3.4, t.accent, MSO_SHAPE.OVAL)
        self._rect(s, M, 2.35, 0.12, 2.0, t.accent)
        size = fit_pt(sp.title, 8.4, 1.7, 44, 28)
        self._text(s, M + 0.4, 2.2, 8.4, 1.7, sp.title, size=size, bold=True, font=t.font_heading, color="FFFFFF",
                   anchor=MSO_ANCHOR.BOTTOM)
        if sp.subtitle:
            self._text(s, M + 0.4, 4.0, 8.4, 0.9, sp.subtitle, size=18, color=mix(t.primary, "FFFFFF", 0.75))
        if sp.text:
            self._text(s, M + 0.4, H - 1.2, 8, 0.4, sp.text, size=12, color=mix(t.primary, "FFFFFF", 0.6))
        self._footer(s, sp, dark=True)

    def _l_closing(self, sp: SlideSpec):
        s, t = self._slide(dark=True), self.t
        self._rect(s, -1.5, 4.4, 4.6, 4.6, mix(t.primary, "FFFFFF", 0.12), MSO_SHAPE.OVAL)
        self._text(s, M, 2.3, W - 2 * M, 1.3, sp.title or "Thank you", size=fit_pt(sp.title, W - 2 * M, 1.3, 44, 28),
                   bold=True, font=t.font_heading, color="FFFFFF", align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        sub = sp.subtitle or sp.text
        if sub:
            self._text(s, M + 1.5, 3.75, W - 2 * M - 3, 1.2, sub, size=18, color=mix(t.primary, "FFFFFF", 0.75),
                       align=PP_ALIGN.CENTER)
        if sp.bullets:
            self._text(s, M + 1.5, 4.9, W - 2 * M - 3, 1.5, "  ·  ".join(strip_inline(str(getattr(b, "text", b)))
                                                                      for b in sp.bullets),
                       size=13, color=t.accent, align=PP_ALIGN.CENTER)
        self._footer(s, sp, dark=True)

    def _l_section(self, sp: SlideSpec):
        s, t = self._slide(dark=True), self.t
        self.section_no += 1
        self._rect(s, 0, 0, 4.3, H, mix(t.primary, "000000", 0.25))
        self._text(s, M, 2.4, 3.2, 2.0, f"{self.section_no:02d}",
                   size=88, bold=True, font=t.font_heading, color=t.accent, anchor=MSO_ANCHOR.MIDDLE)
        self._text(s, 4.9, 2.5, W - 4.9 - M, 1.4, sp.title, size=fit_pt(sp.title, W - 5.5, 1.4, 40, 24), bold=True,
                   font=t.font_heading, color="FFFFFF", anchor=MSO_ANCHOR.BOTTOM)
        if sp.subtitle or sp.text:
            self._text(s, 4.9, 4.0, W - 4.9 - M, 1.2, sp.subtitle or sp.text, size=16,
                       color=mix(t.primary, "FFFFFF", 0.75))
        self._footer(s, sp, dark=True)

    def _l_bullets(self, sp: SlideSpec):
        s, t = self._slide(), self.t
        self._title(s, sp.title, sp.subtitle)
        items = [i if isinstance(i, ListItem) else ListItem(text=str(i)) for i in sp.bullets]
        img = self.ctx.image_resolver(sp.image) if sp.image else None
        top = BODY_TOP + (0.35 if sp.subtitle else 0)
        avail_h = H - top - 0.75
        if img:
            self._bullets(s, M, top, 6.3, avail_h, items)
            self._picture(s, img, 7.3, top, W - 7.3 - M, avail_h)
        elif 2 <= len(items) <= 4 and all(i.level == 0 for i in items):
            # card grid: badge + heading + detail, fills the slide
            n, gap = len(items), 0.35
            w = (W - 2 * M - gap * (n - 1)) / n
            h = avail_h - 0.1
            for k, it in enumerate(items):
                x = M + k * (w + gap)
                color = t.primary if k % 2 == 0 else t.accent
                self._rect(s, x, top, w, h, mix(color, "FFFFFF", 0.92), MSO_SHAPE.ROUNDED_RECTANGLE)
                self._badge(s, x + 0.35, top + 0.4, 0.75, k + 1, color)
                head, rest = self._split_point(it.text)
                if head:
                    self._text(s, x + 0.35, top + 1.4, w - 0.7, 0.9, head, size=fit_pt(head, w - 0.7, 0.9, 22, 14, 1.2),
                               bold=True, color=t.primary, font=t.font_heading, anchor=MSO_ANCHOR.BOTTOM)
                body = rest if head else it.text
                self._text(s, x + 0.35, top + 2.45, w - 0.7, h - 2.75, body,
                           size=fit_pt(body, w - 0.7, h - 2.75, 18, 11, 1.35), color="374151", spacing=1.2)
        elif len(items) in (1, 5) and all(i.level == 0 for i in items):
            row_h = min(1.6, avail_h / len(items))
            for k, it in enumerate(items):
                y = top + k * row_h
                self._badge(s, M, y + 0.1, 0.6, k + 1, t.primary if k % 2 == 0 else t.accent)
                head, rest = self._split_point(it.text)
                if head:
                    self._text(s, M + 0.95, y + 0.02, W - 2 * M - 1, 0.45, head, size=20, bold=True, color=t.primary)
                    self._text(s, M + 0.95, y + 0.5, W - 2 * M - 1, row_h - 0.55, rest,
                               size=fit_pt(rest, W - 2 * M - 1, row_h - 0.55, 17, 11), color="4B5563")
                else:
                    self._text(s, M + 0.95, y, W - 2 * M - 1, row_h - 0.1, it.text,
                               size=fit_pt(it.text, W - 2 * M - 1, row_h - 0.1, 20, 11), anchor=MSO_ANCHOR.MIDDLE)
        else:
            self._rect(s, M, top - 0.05, W - 2 * M, avail_h + 0.1, mix(t.primary, "FFFFFF", 0.95),
                       MSO_SHAPE.ROUNDED_RECTANGLE)
            self._bullets(s, M + 0.35, top + 0.2, W - 2 * M - 0.7, avail_h - 0.3, items)
        self._footer(s, sp)

    def _column_card(self, s, x, y, w, h, col, color):
        self._rect(s, x, y, w, h, mix(color, "FFFFFF", 0.92), MSO_SHAPE.ROUNDED_RECTANGLE)
        self._rect(s, x, y, w, 0.6, color, MSO_SHAPE.ROUNDED_RECTANGLE)
        self._rect(s, x, y + 0.35, w, 0.25, color)
        self._text(s, x + 0.3, y, w - 0.6, 0.6, col.heading, size=17, bold=True, color="FFFFFF",
                   font=self.t.font_heading, anchor=MSO_ANCHOR.MIDDLE)
        if col.bullets:
            self._bullets(s, x + 0.3, y + 0.85, w - 0.6, h - 1.05, col.bullets)
        elif col.text:
            self._text(s, x + 0.3, y + 0.85, w - 0.6, h - 1.05, col.text, size=fit_pt(col.text, w - 0.6, h - 1.05, 16))

    def _l_two_column(self, sp: SlideSpec):
        s, t = self._slide(), self.t
        self._title(s, sp.title, sp.subtitle)
        top = BODY_TOP + (0.4 if sp.subtitle else 0.05)
        w = (W - 2 * M - 0.4) / 2
        h = H - top - 0.75
        self._column_card(s, M, top, w, h, sp.left or Column(), t.primary)
        self._column_card(s, M + w + 0.4, top, w, h, sp.right or Column(), t.accent)
        self._footer(s, sp)

    _l_comparison = _l_two_column

    def _l_table(self, sp: SlideSpec):
        if not sp.table:
            raise ValueError("table layout requires 'table'")
        columns, rows = resolve_table(sp.table, self.ctx)
        n_chunks = max(1, math.ceil(len(rows) / MAX_TABLE_ROWS))
        per = max(1, math.ceil(len(rows) / n_chunks))
        chunks = [rows[i:i + per] for i in range(0, len(rows), per)] or [[]]
        if len(chunks) > 4:
            self.ctx.warn(f"Slide table limited to {4 * MAX_TABLE_ROWS} of {len(rows)} rows")
            chunks = chunks[:4]
        for ci, chunk in enumerate(chunks):
            s = self._slide()
            self._title(s, sp.title + (" (cont.)" if ci else ""), sp.subtitle if not ci else "")
            self._table(s, columns, chunk, sp.table, BODY_TOP + (0.4 if sp.subtitle and not ci else 0.05))
            self._footer(s, sp)

    def _table(self, s, columns, rows, spec: TableSpec, top: float):
        t = self.t
        n = max(1, len(columns))
        avail_h = H - top - 0.8
        row_h = min(0.48, avail_h / (len(rows) + 1))
        shape = s.shapes.add_table(len(rows) + 1, n, Inches(M), Inches(top), Inches(W - 2 * M),
                                   Inches(row_h * (len(rows) + 1)))
        tbl = shape.table
        weights = spec.col_widths if len(spec.col_widths) == n else [
            min(max(max([len(str(columns[i]))] + [len(format_cell(r[i])) for r in rows]), 4), 40) for i in range(n)]
        for i, wgt in enumerate(weights):
            tbl.columns[i].width = Emu(int(Inches(W - 2 * M) * wgt / sum(weights)))
        numeric = [bool(rows) and all(is_numeric_cell(r[i]) or r[i] in (None, "") for r in rows) for i in range(n)]
        size = 14 if n <= 5 and len(rows) <= 10 else 12 if n <= 7 else 10
        for ri in range(len(rows) + 1):
            tbl.rows[ri].height = Inches(row_h)
            for ci in range(n):
                cell = tbl.cell(ri, ci)
                val = columns[ci] if ri == 0 else format_cell(rows[ri - 1][ci])
                cell.fill.solid()
                cell.fill.fore_color.rgb = rgb(t.primary if ri == 0 else (
                    mix(t.primary, "FFFFFF", 0.93) if ri % 2 == 0 else "FFFFFF"))
                cell.margin_left = cell.margin_right = Inches(0.08)
                cell.margin_top = cell.margin_bottom = Inches(0.03)
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                p = cell.text_frame.paragraphs[0]
                p.alignment = PP_ALIGN.RIGHT if numeric[ci] and ri else PP_ALIGN.LEFT
                self._runs(p, str(val), size, "FFFFFF" if ri == 0 else t.text, bold=ri == 0)

    def _chart(self, s, c, x, y, w, h):
        categories, series = resolve_chart(c, self.ctx)
        kind = c.chart_type
        if kind == "scatter":
            data = XyChartData()
            for name, vals in series:
                ser = data.add_series(name)
                for xv, yv in zip(categories, vals):
                    if to_number(xv) is not None and yv is not None:
                        ser.add_data_point(to_number(xv), yv)
        else:
            data = CategoryChartData()
            data.categories = [format_cell(v) for v in categories]
            for name, vals in (series[:1] if kind in ("pie", "doughnut") else series):
                data.add_series(name, vals)
        ctype = STACKED.get(kind, CHART_TYPES[kind]) if c.stacked else CHART_TYPES[kind]
        chart = s.shapes.add_chart(ctype, Inches(x), Inches(y), Inches(w), Inches(h), data).chart
        chart.font.size = Pt(11)
        chart.font.name = self.t.font_body
        colors = palette(self.t, max(len(series), len(categories)))
        chart.has_title = bool(c.title)
        if c.title:
            chart.chart_title.text_frame.text = c.title
            self._style_title(chart)
        if kind in ("pie", "doughnut"):
            for k, point in enumerate(chart.plots[0].series[0].points):
                point.format.fill.solid()
                point.format.fill.fore_color.rgb = rgb(colors[k])
            chart.has_legend = True
            chart.legend.position = XL_LEGEND_POSITION.RIGHT
            chart.legend.include_in_layout = False
            plot = chart.plots[0]
            plot.has_data_labels = True
            plot.data_labels.number_format = "0%"
            plot.data_labels.show_percentage = True
            plot.data_labels.show_value = False
            plot.data_labels.font.color.rgb = rgb("FFFFFF")
        else:
            for k, ser in enumerate(chart.plots[0].series):
                fmt = ser.format
                if kind in ("line", "scatter"):
                    fmt.line.color.rgb = rgb(colors[k])
                    fmt.line.width = Pt(2.5)
                else:
                    fmt.fill.solid()
                    fmt.fill.fore_color.rgb = rgb(colors[k])
            if kind in ("column", "bar"):
                chart.plots[0].gap_width = 60
            chart.has_legend = len(series) > 1
            if chart.has_legend:
                chart.legend.position = XL_LEGEND_POSITION.BOTTOM
                chart.legend.include_in_layout = False
            va = chart.value_axis
            va.has_major_gridlines = True
            va.major_gridlines.format.line.color.rgb = rgb("E5E7EB")
            va.format.line.fill.background()
            chart.category_axis.format.line.color.rgb = rgb("D1D5DB")
            if c.y_title:
                va.has_title = True
                va.axis_title.text_frame.text = c.y_title
            if c.x_title:
                chart.category_axis.has_title = True
                chart.category_axis.axis_title.text_frame.text = c.x_title
            if c.show_values:
                plot = chart.plots[0]
                plot.has_data_labels = True
                plot.data_labels.font.size = Pt(10)
                if kind in ("column", "bar") and not c.stacked:
                    plot.data_labels.position = XL_LABEL_POSITION.OUTSIDE_END
        return chart

    def _style_title(self, chart):
        for p in chart.chart_title.text_frame.paragraphs:
            for r in p.runs:
                r.font.size = Pt(14)
                r.font.bold = True
                r.font.color.rgb = rgb(self.t.text)

    def _l_chart(self, sp: SlideSpec):
        if not sp.chart:
            raise ValueError("chart layout requires 'chart'")
        s, t = self._slide(), self.t
        self._title(s, sp.title, sp.subtitle)
        top = BODY_TOP + (0.4 if sp.subtitle else 0.05)
        h = H - top - 0.75
        if sp.bullets or sp.text:
            self._chart(s, sp.chart, M, top, 8.1, h)
            self._rect(s, 9.0, top, W - 9.0 - M, h, mix(t.primary, "FFFFFF", 0.94), MSO_SHAPE.ROUNDED_RECTANGLE)
            self._text(s, 9.3, top + 0.25, W - 9.6 - M, 0.4, "Key takeaways", size=15, bold=True, color=t.primary)
            if sp.bullets:
                self._bullets(s, 9.3, top + 0.8, W - 9.6 - M, h - 1.0, sp.bullets, size=None)
            else:
                self._text(s, 9.3, top + 0.8, W - 9.6 - M, h - 1.0, sp.text,
                           size=fit_pt(sp.text, W - 9.6 - M, h - 1.0, 15, 10))
        else:
            self._chart(s, sp.chart, M, top, W - 2 * M, h)
        self._footer(s, sp)

    def _l_image(self, sp: SlideSpec):
        s, t = self._slide(), self.t
        self._title(s, sp.title, sp.subtitle)
        img = self.ctx.image_resolver(sp.image)
        if not img:
            raise ValueError(f"image '{sp.image}' not found")
        top = BODY_TOP + (0.4 if sp.subtitle else 0.05)
        h = H - top - 0.8
        if sp.bullets or sp.text:
            self._picture(s, img, M, top, 7.8, h)
            if sp.bullets:
                self._bullets(s, 8.8, top + 0.1, W - 8.8 - M, h, sp.bullets)
            else:
                self._text(s, 8.8, top + 0.1, W - 8.8 - M, h, sp.text, size=fit_pt(sp.text, W - 9.4, h, 16))
        else:
            self._picture(s, img, M, top, W - 2 * M, h)
        self._footer(s, sp)

    def _l_stats(self, sp: SlideSpec):
        s, t = self._slide(), self.t
        self._title(s, sp.title, sp.subtitle)
        stats = sp.stats[:4] or [Stat(value="-", label="")]
        n = len(stats)
        gap = 0.35
        w = (W - 2 * M - gap * (n - 1)) / n
        top = BODY_TOP + (0.6 if sp.subtitle else 0.4)
        h = 3.3
        for k, st in enumerate(stats):
            x = M + k * (w + gap)
            color = t.primary if k % 2 == 0 else t.accent
            self._rect(s, x, top, w, h, mix(color, "FFFFFF", 0.9), MSO_SHAPE.ROUNDED_RECTANGLE)
            self._rect(s, x, top, w, 0.12, color)
            vsize = max(24, min(54, (w - 0.5) * 72 / (max(len(st.value), 1) * 0.78)))
            self._text(s, x + 0.25, top + 0.4, w - 0.5, 1.3, st.value, size=vsize,
                       bold=True, color=color, font=t.font_heading, anchor=MSO_ANCHOR.MIDDLE)
            self._text(s, x + 0.25, top + 1.8, w - 0.5, 0.45, st.label, size=16, bold=True, color=t.text)
            if st.detail:
                self._text(s, x + 0.25, top + 2.3, w - 0.5, 0.9, st.detail,
                           size=fit_pt(st.detail, w - 0.5, 0.9, 13, 10), color="4B5563")
        if sp.text:
            self._text(s, M, top + h + 0.35, W - 2 * M, 0.8, sp.text, size=15, color="4B5563")
        self._footer(s, sp)

    def _l_timeline(self, sp: SlideSpec):
        s, t = self._slide(), self.t
        self._title(s, sp.title, sp.subtitle)
        steps = sp.steps[:7]
        n = max(1, len(steps))
        y_line = 3.4
        self._rect(s, M + 0.3, y_line - 0.03, W - 2 * M - 0.6, 0.06, mix(t.primary, "FFFFFF", 0.6))
        w = (W - 2 * M) / n
        for k, st in enumerate(steps):
            cx = M + w * k + w / 2
            color = t.accent if k == n - 1 else t.primary
            dot = self._rect(s, cx - 0.32, y_line - 0.32, 0.64, 0.64, color, MSO_SHAPE.OVAL)
            dot.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER
            self._runs(dot.text_frame.paragraphs[0], str(k + 1), 15, "FFFFFF", bold=True)
            self._text(s, cx - w / 2 + 0.1, y_line + 0.55, w - 0.2, 0.5, st.title, size=fit_pt(st.title, w - 0.2, 0.5, 16, 11),
                       bold=True, color=t.primary, align=PP_ALIGN.CENTER)
            if st.text:
                self._text(s, cx - w / 2 + 0.1, y_line + 1.1, w - 0.2, 2.0, st.text,
                           size=fit_pt(st.text, w - 0.2, 2.0, 13, 9), color="4B5563", align=PP_ALIGN.CENTER)
        if sp.text:
            self._text(s, M, BODY_TOP + 0.3, W - 2 * M, 1.0, sp.text, size=15, color="4B5563")
        self._footer(s, sp)

    def _l_quote(self, sp: SlideSpec):
        s, t = self._slide(), self.t
        self._rect(s, M + 0.3, 1.4, 1.2, 1.2, mix(t.accent, "FFFFFF", 0.75), MSO_SHAPE.OVAL)
        self._text(s, M + 0.45, 1.25, 1.0, 1.4, "\u201C", size=96, bold=True, color=t.accent, font="Georgia")
        quote = sp.text or sp.title
        self._text(s, M + 1.9, 1.7, W - 2 * M - 2.4, 3.2, quote, size=fit_pt(quote, W - 2 * M - 2.4, 3.2, 30, 16),
                   italic=True, color=t.primary, font=t.font_heading, anchor=MSO_ANCHOR.MIDDLE)
        if sp.quote_author:
            self._text(s, M + 1.9, 5.1, W - 2 * M - 2.4, 0.5, f"— {sp.quote_author}", size=16, color="6B7280")
        self._footer(s, sp)


def build_pptx(spec: PresentationSpec, out: Path, ctx: GenContext | None = None) -> Path:
    return SlideBuilder(spec, ctx or GenContext()).build(out)
