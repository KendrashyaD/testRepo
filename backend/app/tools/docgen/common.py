"""Helpers shared by all generators: data resolution, inline markdown, charts, colors."""
from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

from .specs import ChartSpec, TableSpec


# --------------------------------------------------------------------------- context
@dataclass
class GenContext:
    """Decouples generators from the agent runtime (makes them unit-testable)."""
    data_loader: Callable[[str], Any] = lambda key: (_ for _ in ()).throw(ValueError(f"no data_ref support: {key}"))
    image_resolver: Callable[[str], Optional[Path]] = lambda ref: None
    warnings: list[str] = field(default_factory=list)

    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)


# --------------------------------------------------------------------------- tabular data
def to_columns_rows(data: Any, select: list[str] | None = None) -> tuple[list[str], list[list[Any]]]:
    """Accepts list[dict] | list[list] (first row = header) | {columns|headers, rows} | {key: [values]}."""
    columns: list[str] = []
    rows: list[list[Any]] = []
    if isinstance(data, dict):
        if "rows" in data:
            columns = [str(c) for c in (data.get("columns") or data.get("headers") or [])]
            return to_columns_rows(
                [dict(zip(columns, r)) if columns and isinstance(r, list) else r for r in data["rows"]]
                if columns else data["rows"], select)
        if all(isinstance(v, list) for v in data.values()):  # column-oriented
            columns = [str(k) for k in data]
            n = max((len(v) for v in data.values()), default=0)
            rows = [[data[c][i] if i < len(data[c]) else None for c in data] for i in range(n)]
        else:
            columns, rows = ["Key", "Value"], [[k, v] for k, v in data.items()]
    elif isinstance(data, list) and data:
        if all(isinstance(r, dict) for r in data):
            seen: dict[str, None] = {}
            for r in data:
                for k in r:
                    seen.setdefault(str(k), None)
            columns = list(seen)
            rows = [[r.get(c) for c in columns] for r in data]
        elif isinstance(data[0], (list, tuple)):
            columns = [str(c) for c in data[0]]
            rows = [list(r) for r in data[1:]]
        else:
            columns, rows = ["Value"], [[v] for v in data]

    if select:
        idx = []
        lower = [c.lower() for c in columns]
        for name in select:
            if name in columns:
                idx.append(columns.index(name))
            elif name.lower() in lower:
                idx.append(lower.index(name.lower()))
        if idx:
            columns = [columns[i] for i in idx]
            rows = [[r[i] if i < len(r) else None for i in idx] for r in rows]
    width = len(columns)
    rows = [(list(r) + [None] * width)[:width] if width else list(r) for r in rows]
    return columns, [[_scalar(v) for v in r] for r in rows]


def _scalar(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return ", ".join(f"{k}: {x}" for k, x in v.items()) if isinstance(v, dict) else ", ".join(map(str, v))
    return v


def resolve_table(t: TableSpec, ctx: GenContext) -> tuple[list[str], list[list[Any]]]:
    if t.data_ref:
        columns, rows = to_columns_rows(ctx.data_loader(t.data_ref), t.select_columns)
        if t.columns and len(t.columns) == len(columns):
            columns = list(t.columns)  # allow renaming headers
    else:
        columns, rows = list(t.columns), [list(r) for r in t.rows]
        if not columns and rows:
            columns, rows = [str(c) for c in rows[0]], rows[1:]
        width = max([len(columns)] + [len(r) for r in rows])
        columns += [""] * (width - len(columns))
        rows = [r + [None] * (width - len(r)) for r in rows]
    if t.max_rows and len(rows) > t.max_rows:
        ctx.warn(f"Table truncated to {t.max_rows} of {len(rows)} rows")
        rows = rows[: t.max_rows]
    return columns, rows


def resolve_chart(c: ChartSpec, ctx: GenContext) -> tuple[list[Any], list[tuple[str, list[float | None]]]]:
    if c.data_ref:
        columns, rows = to_columns_rows(ctx.data_loader(c.data_ref))
        cat = c.category_column or columns[0]
        if cat not in columns:
            raise ValueError(f"category_column '{cat}' not in data columns {columns}")
        values = c.value_columns or [col for col in columns if col != cat and _is_numeric_col(rows, columns.index(col))]
        ci = columns.index(cat)
        categories = [r[ci] for r in rows]
        series = []
        for v in values:
            if v not in columns:
                raise ValueError(f"value column '{v}' not in data columns {columns}")
            vi = columns.index(v)
            series.append((v, [to_number(r[vi]) for r in rows]))
        return categories, series
    return list(c.categories), [(s.name or f"Series {i + 1}", [to_number(v) for v in s.values])
                                for i, s in enumerate(c.series)]


def _is_numeric_col(rows: list[list[Any]], i: int) -> bool:
    vals = [r[i] for r in rows if r[i] not in (None, "")]
    return bool(vals) and all(to_number(v) is not None for v in vals)


_NUM_CLEAN = re.compile(r"[,\s$€£₹%]")


def to_number(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return None if isinstance(v, float) and math.isnan(v) else v
    s = str(v).strip()
    neg = s.startswith("(") and s.endswith(")")
    s = _NUM_CLEAN.sub("", s.strip("()"))
    try:
        n = float(s)
    except ValueError:
        return None
    return -n if neg else n


def format_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:,.2f}".rstrip("0").rstrip(".") if abs(v) < 1e15 else str(v)
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{v:,}" if abs(v) >= 10000 else str(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return str(v)


def is_numeric_cell(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# --------------------------------------------------------------------------- inline markdown
_INLINE = re.compile(
    r"(\*\*(?P<b>.+?)\*\*|__(?P<b2>.+?)__|(?<![\w*])\*(?P<i>[^*\n]+?)\*(?!\w)|(?<![\w_])_(?P<i2>[^_\n]+?)_(?!\w)"
    r"|`(?P<c>[^`]+)`|\[(?P<lt>[^\]]+)\]\((?P<lu>[^)\s]+)\))"
)


@dataclass
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    link: str = ""


def parse_inline(text: str) -> list[Run]:
    runs: list[Run] = []
    pos = 0
    for m in _INLINE.finditer(text or ""):
        if m.start() > pos:
            runs.append(Run(text[pos:m.start()]))
        if m.group("b") or m.group("b2"):
            for r in parse_inline(m.group("b") or m.group("b2")):
                r.bold = True
                runs.append(r)
        elif m.group("i") or m.group("i2"):
            runs.append(Run(m.group("i") or m.group("i2"), italic=True))
        elif m.group("c"):
            runs.append(Run(m.group("c"), code=True))
        else:
            runs.append(Run(m.group("lt"), link=m.group("lu")))
        pos = m.end()
    if pos < len(text or ""):
        runs.append(Run(text[pos:]))
    return runs


def strip_inline(text: str) -> str:
    return "".join(r.text for r in parse_inline(text))


# --------------------------------------------------------------------------- colors
def hex_rgb(h: str) -> tuple[int, int, int]:
    h = (h or "000000").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def mix(h: str, other: str = "FFFFFF", ratio: float = 0.5) -> str:
    a, b = hex_rgb(h), hex_rgb(other)
    return "".join(f"{round(x * (1 - ratio) + y * ratio):02X}" for x, y in zip(a, b))


def palette(theme: Any, n: int) -> list[str]:
    base = [theme.primary, theme.accent, theme.secondary, mix(theme.primary, "FFFFFF", 0.45),
            mix(theme.accent, "FFFFFF", 0.4), mix(theme.secondary, "000000", 0.3), "8D99AE", "6A994E"]
    return [base[i % len(base)] for i in range(max(n, 1))]


CALLOUT_COLORS = {
    "info": ("2563EB", "EFF6FF"),
    "success": ("15803D", "F0FDF4"),
    "warning": ("B45309", "FFFBEB"),
    "danger": ("B91C1C", "FEF2F2"),
}


# --------------------------------------------------------------------------- matplotlib charts (Word / PDF)
def render_chart_png(c: ChartSpec, ctx: GenContext, theme: Any, width_in: float = 6.0) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    categories, series = resolve_chart(c, ctx)
    labels = [format_cell(x) for x in categories]
    colors = ["#" + x for x in palette(theme, max(len(series), len(labels)))]
    height = width_in * (0.62 if c.chart_type not in ("pie", "doughnut") else 0.7)
    fig, ax = plt.subplots(figsize=(width_in, height), dpi=160)
    plt.rcParams.update({"font.size": 9})
    try:
        if not series:
            raise ValueError("chart has no series")
        if c.chart_type in ("pie", "doughnut"):
            vals = [v or 0 for v in series[0][1]]
            wedges, *_ = ax.pie(vals, labels=None, colors=colors[: len(vals)], startangle=90,
                                autopct="%1.0f%%", pctdistance=0.8 if c.chart_type == "doughnut" else 0.6,
                                wedgeprops={"width": 0.45} if c.chart_type == "doughnut" else None,
                                textprops={"color": "white", "fontsize": 8})
            ax.legend(wedges, labels, loc="center left", bbox_to_anchor=(1, 0.5), frameon=False)
            ax.axis("equal")
        else:
            x = list(range(len(labels)))
            n = len(series)
            bottoms = [0.0] * len(labels)
            for i, (name, vals) in enumerate(series):
                vals = [v if v is not None else 0 for v in vals][: len(labels)] + [0] * (len(labels) - len(vals))
                col = colors[i]
                if c.chart_type in ("column", "bar"):
                    if c.stacked:
                        pos, w = x, 0.6
                    else:
                        w = 0.8 / n
                        pos = [p - 0.4 + w * (i + 0.5) for p in x]
                    fn = ax.barh if c.chart_type == "bar" else ax.bar
                    kw = {"left": bottoms} if (c.chart_type == "bar" and c.stacked) else (
                        {"bottom": bottoms} if c.stacked else {})
                    bars = fn(pos, vals, w, label=name, color=col, **kw)
                    if c.show_values:
                        ax.bar_label(bars, fmt="%g", fontsize=7, padding=2)
                    if c.stacked:
                        bottoms = [b + v for b, v in zip(bottoms, vals)]
                elif c.chart_type == "line":
                    ax.plot(x, vals, marker="o", label=name, color=col, linewidth=2)
                elif c.chart_type == "area":
                    ax.fill_between(x, vals, alpha=0.35, color=col)
                    ax.plot(x, vals, label=name, color=col, linewidth=1.5)
                elif c.chart_type == "scatter":
                    xs = [to_number(v) for v in categories]
                    ax.scatter(xs if all(v is not None for v in xs) else x, vals, label=name, color=col)
            if c.chart_type == "bar":
                ax.set_yticks(x, labels)
                ax.invert_yaxis()
            elif c.chart_type != "scatter" or not all(to_number(v) is not None for v in categories):
                crowded = sum(len(s) + 2 for s in labels) * 0.075 > width_in or max(map(len, labels), default=0) > 12
                ax.set_xticks(x, labels, rotation=35 if crowded else 0, ha="right" if crowded else "center")
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
            ax.grid(axis="x" if c.chart_type == "bar" else "y", alpha=0.25)
            ax.set_axisbelow(True)
            if n > 1:
                ax.legend(frameon=False, fontsize=8, loc="best")
            if c.x_title:
                ax.set_xlabel(c.x_title)
            if c.y_title:
                ax.set_ylabel(c.y_title)
        if c.title:
            ax.set_title(c.title, fontsize=11, fontweight="bold", loc="left", color="#" + theme.text)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
        return buf.getvalue()
    finally:
        plt.close(fig)


def image_size(path_or_bytes: Path | bytes) -> tuple[int, int]:
    from PIL import Image

    src = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else path_or_bytes
    with Image.open(src) as im:
        return im.size
