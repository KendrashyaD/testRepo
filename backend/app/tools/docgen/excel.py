"""Excel (.xlsx) generator - openpyxl. Totals/computed columns are live formulas, not hardcoded values."""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.chart import AreaChart, BarChart, DoughnutChart, LineChart, PieChart, Reference, ScatterChart, Series
from openpyxl.comments import Comment
from openpyxl.formatting.rule import CellIsRule, ColorScaleRule, DataBarRule, FormulaRule, Rule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.styles.differential import DifferentialStyle
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from .common import GenContext, mix, palette, to_columns_rows
from .specs import ColumnSpec, SheetSpec, WorkbookSpec

FORMATS = {
    "number": "#,##0.00;(#,##0.00);-",
    "integer": "#,##0;(#,##0);-",
    "currency": "#,##0.00;(#,##0.00);-",
    "percent": "0.0%",
    "date": "yyyy-mm-dd",
    "datetime": "yyyy-mm-dd hh:mm",
    "text": "@",
}
_NUMERIC_STR = re.compile(r"^\(?[-+]?[$€£₹]?\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s?%?\)?$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")
_BAD_SHEET = re.compile(r"[\[\]:*?/\\]")
AGG = {"sum": "SUBTOTAL(109,{r})", "average": "SUBTOTAL(101,{r})", "count": "SUBTOTAL(103,{r})",
       "min": "SUBTOTAL(105,{r})", "max": "SUBTOTAL(104,{r})"}


def _parse_value(v: Any, ctype: str) -> Any:
    """Convert scraped strings into real numbers/dates so Excel can compute with them."""
    if v is None or isinstance(v, (int, float, date, datetime)) and not isinstance(v, bool):
        return v
    if isinstance(v, bool):
        return v
    s = str(v).strip()
    if s == "":
        return None
    if ctype in ("text", "url"):
        return s
    if ctype in ("date", "datetime", "auto") and _ISO_DATE.match(s):
        try:
            return datetime.fromisoformat(s.replace(" ", "T")) if len(s) > 10 else date.fromisoformat(s)
        except ValueError:
            return s
    if ctype != "auto" or _NUMERIC_STR.match(s):
        digits = re.sub(r"[^\d.]", "", s)
        if ctype == "auto" and len(digits) > 1 and digits.startswith("0") and not digits.startswith("0."):
            return s  # IDs / zip codes with leading zeros stay text
        neg = s.startswith("(") or s.startswith("-")
        try:
            n = float(re.sub(r"[^\d.]", "", s))
        except ValueError:
            return s
        n = -n if neg else n
        if s.endswith("%"):
            n /= 100
        return int(n) if n.is_integer() and "." not in s and not s.endswith("%") else n
    return s


def _infer_type(values: list[Any], raw: list[Any]) -> str:
    vals = [v for v in values if v is not None]
    if not vals:
        return "text"
    if all(isinstance(v, (date, datetime)) for v in vals):
        return "datetime" if any(isinstance(v, datetime) for v in vals) else "date"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
        rs = [str(r) for r in raw if r not in (None, "")]
        if rs and all(r.strip().endswith("%") for r in rs):
            return "percent"
        if rs and any(re.search(r"[$€£₹]", r) for r in rs):
            return "currency"
        return "integer" if all(isinstance(v, int) for v in vals) else "number"
    if all(isinstance(v, str) and v.startswith(("http://", "https://")) for v in vals):
        return "url"
    return "text"


def _sheet_name(name: str, used: set[str]) -> str:
    base = _BAD_SHEET.sub("_", name or "Sheet")[:31] or "Sheet"
    out, n = base, 2
    while out.lower() in used:
        suffix = f" ({n})"
        out = base[: 31 - len(suffix)] + suffix
        n += 1
    used.add(out.lower())
    return out


def _unique_headers(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for h in headers:
        h = str(h).strip() or "Column"
        if h.lower() in seen:
            seen[h.lower()] += 1
            h = f"{h} {seen[h.lower()]}"
        else:
            seen[h.lower()] = 1
        out.append(h)
    return out


class ExcelBuilder:
    def __init__(self, spec: WorkbookSpec, ctx: GenContext):
        self.spec, self.ctx, self.t = spec, ctx, spec.theme
        self.wb = Workbook()
        self.font = spec.font
        thin = Side(style="thin", color="D0D7DE")
        self.border = Border(left=thin, right=thin, top=thin, bottom=thin)
        self._tables = 0

    def build(self, out: Path) -> Path:
        used: set[str] = set()
        self.wb.remove(self.wb.active)
        for sheet in self.spec.sheets:
            ws = self.wb.create_sheet(_sheet_name(sheet.name, used))
            try:
                self._sheet(ws, sheet)
            except Exception as exc:  # noqa: BLE001
                self.ctx.warn(f"sheet '{sheet.name}': {exc}")
                ws["A1"] = f"Could not build sheet: {exc}"
        if not self.wb.sheetnames:
            self.wb.create_sheet("Sheet1")
        self.wb.properties.creator = "Web Agent"
        self.wb.save(out)
        return out

    # ------------------------------------------------------------------ sheet
    def _columns(self, sheet: SheetSpec) -> tuple[list[ColumnSpec], list[list[Any]]]:
        specs = [c if isinstance(c, ColumnSpec) else ColumnSpec(header=str(c)) for c in sheet.columns]
        if sheet.data_ref:
            data = self.ctx.data_loader(sheet.data_ref)
            select = [c.source or c.header for c in specs] if specs else None
            headers, rows = to_columns_rows(data, select)
            if specs and len(specs) == len(headers):
                pass
            else:
                by_name = {(c.source or c.header).lower(): c for c in specs}
                specs = [by_name.get(h.lower(), ColumnSpec(header=h)) for h in headers]
        else:
            rows = [list(r) for r in sheet.rows]
            if not specs and rows:
                specs, rows = [ColumnSpec(header=str(h)) for h in rows[0]], rows[1:]
            width = max([len(specs)] + [len(r) for r in rows]) if (specs or rows) else 0
            specs += [ColumnSpec(header=f"Column {i + 1}") for i in range(len(specs), width)]
            rows = [r + [None] * (width - len(r)) for r in rows]
        for h, c in zip(_unique_headers([c.header for c in specs]), specs):
            c.header = h
        return specs, rows

    def _sheet(self, ws, sheet: SheetSpec) -> None:
        t = self.t
        cols, raw_rows = self._columns(sheet)
        ncols = len(cols)
        r0 = 1
        if sheet.title:
            ws.cell(row=1, column=1, value=sheet.title).font = Font(name=self.font, size=14, bold=True, color=t.primary)
            ws.row_dimensions[1].height = 22
            r0 = 3

        # typed values
        rows = [[_parse_value(v, cols[i].type) for i, v in enumerate(r)] for r in raw_rows]
        types = []
        for i, c in enumerate(cols):
            types.append(c.type if c.type != "auto" else _infer_type([r[i] for r in rows], [r[i] for r in raw_rows]))

        # computed columns (row formulas)
        computed = list(sheet.computed_columns.items())
        all_headers = [c.header for c in cols] + [h for h, _ in computed]
        letters = {h: get_column_letter(i + 1) for i, h in enumerate(all_headers)}

        header_fill = PatternFill("solid", start_color=t.primary)
        hdr_font = Font(name=self.font, bold=True, color="FFFFFF")
        body_font = Font(name=self.font, size=10)
        for ci, h in enumerate(all_headers, start=1):
            cell = ws.cell(row=r0, column=ci, value=h)
            cell.font, cell.fill, cell.border = hdr_font, header_fill, self.border
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.row_dimensions[r0].height = 20

        first, last = r0 + 1, r0 + len(rows)
        for ri, row in enumerate(rows, start=first):
            for ci, v in enumerate(row, start=1):
                cell = ws.cell(row=ri, column=ci, value=v)
                cell.font, cell.border = body_font, self.border
                c = cols[ci - 1]
                fmt = c.number_format or FORMATS.get(types[ci - 1])
                if fmt and types[ci - 1] != "text":
                    cell.number_format = fmt
                if types[ci - 1] == "url" and isinstance(v, str) and v.startswith("http"):
                    cell.hyperlink = v
                    cell.font = Font(name=self.font, size=10, color="2563EB", underline="single")
                if isinstance(v, str) and len(v) > 60:
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
            for k, (h, formula) in enumerate(computed):
                ci = ncols + k + 1
                f = re.sub(r"\{([^}]+)\}", lambda m: f"{letters[m.group(1)]}{ri}" if m.group(1) in letters
                           else m.group(0), formula)
                if not f.startswith("="):
                    f = "=" + f
                cell = ws.cell(row=ri, column=ci, value=f)
                cell.font, cell.border = body_font, self.border
                cell.number_format = FORMATS["number"]

        total_cols = len(all_headers)
        last_col = get_column_letter(total_cols) if total_cols else "A"

        # Excel table or plain autofilter + zebra
        if rows and total_cols:
            ref = f"A{r0}:{last_col}{last}"
            if sheet.as_table:
                self._tables += 1
                tbl = Table(displayName=f"Table{self._tables}", ref=ref)
                tbl.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
                ws.add_table(tbl)
            else:
                ws.auto_filter.ref = ref
                zebra = PatternFill("solid", start_color=mix(t.primary, "FFFFFF", 0.92))
                for ri in range(first + 1, last + 1, 2):
                    for ci in range(1, total_cols + 1):
                        ws.cell(row=ri, column=ci).fill = zebra
        if sheet.freeze_header:
            ws.freeze_panes = ws.cell(row=r0 + 1, column=1)

        # totals row (live formulas)
        next_row = last + 1
        if sheet.totals and rows:
            tr = last + 1
            label = ws.cell(row=tr, column=1, value="Total")
            label.font = Font(name=self.font, bold=True)
            for h, agg in sheet.totals.items():
                if h not in letters:
                    self.ctx.warn(f"totals: unknown column '{h}'")
                    continue
                col = letters[h]
                cell = ws[f"{col}{tr}"]
                cell.value = "=" + AGG[agg].format(r=f"{col}{first}:{col}{last}")
                cell.font = Font(name=self.font, bold=True)
                idx = all_headers.index(h)
                cell.number_format = (cols[idx].number_format or FORMATS.get(types[idx], FORMATS["number"])) \
                    if idx < ncols else FORMATS["number"]
                if agg == "count":
                    cell.number_format = FORMATS["integer"]
            for ci in range(1, total_cols + 1):
                c = ws.cell(row=tr, column=ci)
                c.fill = PatternFill("solid", start_color=mix(t.primary, "FFFFFF", 0.8))
                c.border = Border(top=Side(style="double", color=t.primary))
            next_row = tr + 1

        if sheet.notes:
            ws.cell(row=next_row + 1, column=1, value=sheet.notes).font = Font(name=self.font, size=9, italic=True,
                                                                               color="6B7280")

        self._widths(ws, all_headers, rows, types, cols, r0)
        self._conditional(ws, sheet, letters, first, last)
        self._charts(ws, sheet, letters, all_headers, r0, last, total_cols)
        self._cells(ws, sheet)
        ws.sheet_view.showGridLines = not bool(rows)
        ws.print_options.gridLines = False
        ws.page_setup.orientation = "landscape" if total_cols > 6 else "portrait"
        ws.page_setup.fitToWidth = 1
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.print_title_rows = f"{r0}:{r0}"

    def _widths(self, ws, headers, rows, types, cols, r0) -> None:
        for ci, h in enumerate(headers, start=1):
            spec = cols[ci - 1] if ci - 1 < len(cols) else None
            if spec and spec.width:
                width = spec.width
            else:
                lens = [len(str(h)) + 2]
                for r in rows[:500]:
                    v = r[ci - 1] if ci - 1 < len(r) else None
                    if isinstance(v, float):
                        lens.append(len(f"{v:,.2f}") + 1)
                    elif isinstance(v, (date, datetime)):
                        lens.append(16 if isinstance(v, datetime) else 11)
                    elif v is not None:
                        lens.append(len(str(v)))
                width = min(max(max(lens) + 2, 9), 60)
            ws.column_dimensions[get_column_letter(ci)].width = width

    def _conditional(self, ws, sheet, letters, first, last) -> None:
        if last < first:
            return
        for cf in sheet.conditional_formats:
            if cf.column not in letters:
                self.ctx.warn(f"conditional format: unknown column '{cf.column}'")
                continue
            col = letters[cf.column]
            rng = f"{col}{first}:{col}{last}"
            fill = PatternFill("solid", start_color=cf.color, end_color=cf.color)
            if cf.rule == "color_scale":
                ws.conditional_formatting.add(rng, ColorScaleRule(start_type="min", start_color="F8696B",
                                                                  mid_type="percentile", mid_value=50,
                                                                  mid_color="FFEB84", end_type="max",
                                                                  end_color="63BE7B"))
            elif cf.rule == "data_bar":
                ws.conditional_formatting.add(rng, DataBarRule(start_type="min", end_type="max",
                                                               color=self.t.secondary, showValue=True))
            elif cf.rule in ("greater_than", "less_than", "equal"):
                op = {"greater_than": "greaterThan", "less_than": "lessThan", "equal": "equal"}[cf.rule]
                val = cf.value if isinstance(cf.value, (int, float)) else f'"{cf.value}"'
                ws.conditional_formatting.add(rng, CellIsRule(operator=op, formula=[str(val)], fill=fill))
            elif cf.rule == "contains":
                text = str(cf.value).replace('"', '""')
                ws.conditional_formatting.add(
                    rng, FormulaRule(formula=[f'NOT(ISERROR(SEARCH("{text}",{col}{first})))'], fill=fill))
            elif cf.rule == "top_n":
                rule = Rule(type="top10", rank=int(cf.value or 3), dxf=DifferentialStyle(fill=fill))
                ws.conditional_formatting.add(rng, rule)

    def _charts(self, ws, sheet, letters, headers, r0, last, total_cols) -> None:
        if last <= r0:
            return
        anchor_col = get_column_letter(total_cols + 2)
        for i, ch in enumerate(sheet.charts):
            cat = ch.category_column or headers[0]
            values = ch.value_columns or [h for h in headers[1:]]
            if cat not in letters or any(v not in letters for v in values):
                self.ctx.warn(f"chart '{ch.title}': unknown columns")
                continue
            kind = ch.chart_type
            cat_idx = headers.index(cat) + 1
            cats = Reference(ws, min_col=cat_idx, min_row=r0 + 1, max_row=last)
            colors = palette(self.t, len(values) + 1)
            if kind == "scatter":
                chart = ScatterChart()
                chart.style = 13
                for k, v in enumerate(values):
                    vi = headers.index(v) + 1
                    s = Series(Reference(ws, min_col=vi, min_row=r0, max_row=last), cats, title_from_data=True)
                    s.marker.symbol = "circle"
                    s.graphicalProperties.line.noFill = True
                    chart.series.append(s)
            else:
                chart = {"bar": BarChart, "column": BarChart, "line": LineChart, "pie": PieChart,
                         "doughnut": DoughnutChart, "area": AreaChart}[kind]()
                if kind in ("bar", "column"):
                    chart.type = "bar" if kind == "bar" else "col"
                    chart.gapWidth = 60
                for v in (values[:1] if kind in ("pie", "doughnut") else values):
                    vi = headers.index(v) + 1
                    chart.add_data(Reference(ws, min_col=vi, min_row=r0, max_row=last), titles_from_data=True)
                chart.set_categories(cats)
                if kind not in ("pie", "doughnut"):
                    for k, s in enumerate(chart.series):
                        s.graphicalProperties.solidFill = colors[k]
                        s.graphicalProperties.line.solidFill = colors[k]
            chart.title = ch.title or None
            if kind not in ("pie", "doughnut"):
                if ch.x_title:
                    chart.x_axis.title = ch.x_title
                if ch.y_title:
                    chart.y_axis.title = ch.y_title
                chart.x_axis.delete = False
                chart.y_axis.delete = False
                if len(values) == 1:
                    chart.legend = None
            chart.width, chart.height = 18, 9
            ws.add_chart(chart, ch.anchor or f"{anchor_col}{r0 + i * 20}")

    def _cells(self, ws, sheet) -> None:
        for c in sheet.cells:
            col = re.match(r"[A-Z]+", c.cell.upper()).group(0)
            dim = ws.column_dimensions[col]
            text = c.value if not (isinstance(c.value, str) and c.value.startswith("=")) else "0000000000"
            dim.width = max(dim.width or 8.43, min(len(str(text)) + 4, 60))
            cell = ws[c.cell]
            cell.value = c.value
            cell.font = Font(name=self.font, bold=c.bold, size=10)
            if c.fill:
                cell.fill = PatternFill("solid", start_color=c.fill)
            if c.number_format:
                cell.number_format = c.number_format
            if c.comment:
                cell.comment = Comment(c.comment, "Web Agent")


def build_xlsx(spec: WorkbookSpec, out: Path, ctx: GenContext | None = None) -> Path:
    return ExcelBuilder(spec, ctx or GenContext()).build(out)
