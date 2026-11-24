"""Pydantic specs shared by the document generators and the LangChain tool schemas.

Flat models (one `type` field + optional fields) are used on purpose: they produce compact
JSON schemas that LLMs fill in reliably, unlike deep discriminated unions.
"""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

Cell = Union[str, int, float, bool, None]


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


# --------------------------------------------------------------------------- shared
class Series(_Base):
    name: str = ""
    values: list[Optional[float]] = Field(default_factory=list)


class ChartSpec(_Base):
    chart_type: Literal["bar", "column", "line", "pie", "doughnut", "area", "scatter"] = "column"
    title: str = ""
    categories: list[Cell] = Field(default_factory=list, description="X axis labels")
    series: list[Series] = Field(default_factory=list)
    data_ref: Optional[str] = Field(None, description="Scratchpad key with tabular data; use with category_column/value_columns")
    category_column: Optional[str] = None
    value_columns: list[str] = Field(default_factory=list)
    x_title: str = ""
    y_title: str = ""
    show_values: bool = False
    stacked: bool = False


class TableSpec(_Base):
    columns: list[str] = Field(default_factory=list, description="Header labels")
    rows: list[list[Cell]] = Field(default_factory=list)
    data_ref: Optional[str] = Field(None, description="Scratchpad key holding the table data (list of objects or {columns, rows})")
    select_columns: list[str] = Field(default_factory=list, description="Subset/order of columns when using data_ref")
    max_rows: Optional[int] = None
    caption: str = ""
    col_widths: list[float] = Field(default_factory=list, description="Relative column widths")
    align: list[Literal["left", "center", "right"]] = Field(default_factory=list)
    zebra: bool = True
    total_row: bool = False


class Theme(_Base):
    primary: str = Field("1F3A5F", description="Hex color without #")
    secondary: str = "4A6FA5"
    accent: str = "E07A5F"
    light: str = "F2F5F9"
    text: str = "1F2933"
    font_heading: str = "Calibri"
    font_body: str = "Calibri"


# --------------------------------------------------------------------------- Word / PDF
BlockType = Literal[
    "heading", "paragraph", "bullets", "numbered", "table", "image", "chart", "callout",
    "quote", "code", "key_value", "page_break", "toc", "divider",
]


class ListItem(_Base):
    text: str
    level: int = Field(0, ge=0, le=3)


class Block(_Base):
    type: BlockType
    text: str = Field("", description="Markdown inline supported: **bold**, *italic*, `code`, [link](url)")
    level: int = Field(1, ge=1, le=4, description="Heading level")
    items: list[Union[str, ListItem]] = Field(default_factory=list, description="bullets/numbered items")
    table: Optional[TableSpec] = None
    chart: Optional[ChartSpec] = None
    pairs: list[list[Cell]] = Field(default_factory=list, description="key_value: [[key, value], ...]")
    image: str = Field("", description="'screenshot:latest', 'screenshot:-2', or a file name from the workspace")
    width_in: float = Field(6.0, description="image/chart width in inches")
    caption: str = ""
    style: Literal["info", "success", "warning", "danger"] = "info"
    title: str = Field("", description="callout title")
    align: Literal["left", "center", "right", "justify"] = "left"


class DocumentSpec(_Base):
    filename: str = Field(..., description="Output file name (extension optional)")
    title: str = ""
    subtitle: str = ""
    author: str = ""
    page_size: Literal["A4", "Letter"] = "A4"
    orientation: Literal["portrait", "landscape"] = "portrait"
    header_text: str = ""
    footer_text: str = ""
    page_numbers: bool = True
    cover_page: bool = False
    theme: Theme = Field(default_factory=Theme)
    blocks: list[Block] = Field(default_factory=list)


# --------------------------------------------------------------------------- Excel
class ColumnSpec(_Base):
    header: str
    type: Literal["auto", "text", "number", "integer", "currency", "percent", "date", "datetime", "url"] = "auto"
    width: Optional[float] = None
    number_format: Optional[str] = Field(None, description="Excel format code overriding type, e.g. '#,##0.00'")
    source: Optional[str] = Field(None, description="Key in data_ref objects if different from header")


class ConditionalFormat(_Base):
    column: str
    rule: Literal["color_scale", "data_bar", "greater_than", "less_than", "equal", "contains", "top_n"]
    value: Cell = None
    color: str = "F8CBAD"


class SheetChart(_Base):
    chart_type: Literal["bar", "column", "line", "pie", "doughnut", "area", "scatter"] = "column"
    title: str = ""
    category_column: str = ""
    value_columns: list[str] = Field(default_factory=list)
    anchor: str = Field("", description="Top-left cell, e.g. 'H2'. Default: right of the data")
    x_title: str = ""
    y_title: str = ""


class CellSpec(_Base):
    cell: str = Field(..., description="A1 reference")
    value: Cell = Field(None, description="Value or formula string starting with '='")
    bold: bool = False
    fill: str = ""
    number_format: str = ""
    comment: str = ""


class SheetSpec(_Base):
    name: str = "Sheet1"
    title: str = Field("", description="Optional title row above the table")
    columns: list[Union[str, ColumnSpec]] = Field(default_factory=list)
    rows: list[list[Cell]] = Field(default_factory=list)
    data_ref: Optional[str] = Field(None, description="Scratchpad key holding rows (list of objects or {columns, rows})")
    as_table: bool = Field(True, description="Format as Excel Table with filters")
    freeze_header: bool = True
    totals: dict[str, Literal["sum", "average", "count", "min", "max"]] = Field(
        default_factory=dict, description="Column header -> aggregate; written as live formulas")
    computed_columns: dict[str, str] = Field(
        default_factory=dict,
        description="New column header -> row formula using {Header} placeholders, e.g. {'Total': '={Qty}*{Price}'}")
    conditional_formats: list[ConditionalFormat] = Field(default_factory=list)
    charts: list[SheetChart] = Field(default_factory=list)
    cells: list[CellSpec] = Field(default_factory=list, description="Free-form cells (KPIs, notes, formulas)")
    notes: str = Field("", description="Source/notes line written under the table")


class WorkbookSpec(_Base):
    filename: str
    sheets: list[SheetSpec]
    theme: Theme = Field(default_factory=Theme)
    font: str = "Arial"


# --------------------------------------------------------------------------- PowerPoint
SlideLayout = Literal[
    "title", "section", "bullets", "two_column", "table", "chart", "image", "stats",
    "timeline", "quote", "comparison", "closing",
]


class Stat(_Base):
    value: str
    label: str
    detail: str = ""


class Step(_Base):
    title: str
    text: str = ""


class Column(_Base):
    heading: str = ""
    bullets: list[str] = Field(default_factory=list)
    text: str = ""


class SlideSpec(_Base):
    layout: SlideLayout
    title: str = ""
    subtitle: str = ""
    bullets: list[Union[str, ListItem]] = Field(default_factory=list)
    text: str = ""
    left: Optional[Column] = None
    right: Optional[Column] = None
    table: Optional[TableSpec] = None
    chart: Optional[ChartSpec] = None
    image: str = Field("", description="'screenshot:latest' or a workspace file name")
    stats: list[Stat] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    quote_author: str = ""
    notes: str = Field("", description="Speaker notes")
    source: str = Field("", description="Small source line at the bottom")


class PresentationSpec(_Base):
    filename: str
    title: str = ""
    theme: Theme = Field(default_factory=lambda: Theme(font_heading="Georgia", font_body="Calibri"))
    slide_numbers: bool = True
    slides: list[SlideSpec]


def dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump()
