"""Scratchpad + data tools: the agent's working memory for large data and intermediate reasoning."""
from __future__ import annotations

import json
import re
from typing import Any, Literal, Optional, Union

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from .docgen.common import format_cell, to_columns_rows, to_number
from .workspace import SCRATCHPAD, load_data_ref, thread_id_from

PREVIEW_ROWS = 15


def md_table(columns: list[str], rows: list[list[Any]], limit: int = PREVIEW_ROWS) -> str:
    if not columns:
        return "(empty)"
    esc = lambda v: format_cell(v).replace("|", "\\|").replace("\n", " ")[:80]  # noqa: E731
    lines = ["| " + " | ".join(esc(c) for c in columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(esc(v) for v in r) + " |" for r in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"... {len(rows) - limit} more rows")
    return "\n".join(lines)


# --------------------------------------------------------------------------- scratchpad
class WriteArgs(BaseModel):
    key: str = Field(..., description="Short key, e.g. 'notes', 'requirements', 'orders_table'")
    content: str = Field(..., description="Text, or JSON (list of objects / {columns, rows}) for tabular data")
    mode: Literal["replace", "append"] = "replace"


class ReadArgs(BaseModel):
    key: str
    offset: int = Field(0, description="Character offset for paging through long entries")
    limit: int = Field(6000, le=20000, description="Max characters to return")
    grep: Optional[str] = Field(None, description="Only return lines containing this text/regex (case-insensitive)")


class KeyArgs(BaseModel):
    key: str


class NoArgs(BaseModel):
    pass


async def _write(config: RunnableConfig, key: str, content: str, mode: str = "replace") -> str:
    entry = SCRATCHPAD.write(thread_id_from(config), key, content, append=mode == "append")
    return f"Saved '{key}' -> {entry.describe(key)}"


async def _read(config: RunnableConfig, key: str, offset: int = 0, limit: int = 6000,
                grep: Optional[str] = None) -> str:
    tid = thread_id_from(config)
    entry = SCRATCHPAD.get(tid, key)
    if entry is None:
        return f"No entry '{key}'. Keys: {', '.join(SCRATCHPAD.keys(tid)) or 'none'}"
    text = entry.content
    if grep:
        try:
            rx = re.compile(grep, re.I)
        except re.error:
            rx = re.compile(re.escape(grep), re.I)
        hits = [f"{i + 1}: {line}" for i, line in enumerate(text.splitlines()) if rx.search(line)]
        text = "\n".join(hits) or f"No lines match '{grep}'."
    chunk = text[offset: offset + limit]
    more = len(text) - (offset + len(chunk))
    tail = f"\n…[{more:,} more chars - call again with offset={offset + len(chunk)}]" if more > 0 else ""
    return chunk + tail


async def _list(config: RunnableConfig) -> str:
    idx = SCRATCHPAD.index(thread_id_from(config))
    return "Scratchpad:\n- " + "\n- ".join(idx) if idx else "Scratchpad is empty."


async def _delete(config: RunnableConfig, key: str) -> str:
    return f"Deleted '{key}'." if SCRATCHPAD.delete(thread_id_from(config), key) else f"No entry '{key}'."


# --------------------------------------------------------------------------- data query
class Condition(BaseModel):
    column: str
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "contains", "not_contains", "in", "empty", "not_empty"]
    value: Union[str, float, int, list, None] = None


class QueryArgs(BaseModel):
    data_ref: str = Field(..., description="Scratchpad key with tabular data")
    select: list[str] = Field(default_factory=list, description="Columns to keep (default all)")
    where: list[Condition] = Field(default_factory=list, description="All conditions must match")
    group_by: list[str] = Field(default_factory=list)
    aggregations: dict[str, Literal["sum", "avg", "count", "min", "max", "distinct"]] = Field(
        default_factory=dict, description="column -> function (used with group_by, or for whole-table totals)")
    sort_by: Optional[str] = None
    descending: bool = False
    limit: Optional[int] = None
    dedupe: bool = False
    save_as: Optional[str] = Field(None, description="Store the result as a new scratchpad key (for data_ref)")


def _match(v: Any, c: Condition) -> bool:
    if c.op == "empty":
        return v in (None, "")
    if c.op == "not_empty":
        return v not in (None, "")
    if c.op in ("contains", "not_contains"):
        hit = str(c.value).lower() in str(v or "").lower()
        return hit if c.op == "contains" else not hit
    if c.op == "in":
        vals = c.value if isinstance(c.value, list) else [c.value]
        return str(v) in {str(x) for x in vals}
    a, b = to_number(v), to_number(c.value)
    if a is None or b is None:
        a, b = str(v or "").lower(), str(c.value or "").lower()
    return {"eq": a == b, "ne": a != b, "gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[c.op]


def _agg(values: list[Any], fn: str) -> Any:
    if fn == "count":
        return sum(1 for v in values if v not in (None, ""))
    if fn == "distinct":
        return len({str(v) for v in values if v not in (None, "")})
    nums = [n for n in (to_number(v) for v in values) if n is not None]
    if not nums:
        return None
    return {"sum": sum(nums), "avg": round(sum(nums) / len(nums), 4), "min": min(nums), "max": max(nums)}[fn]


async def _query(config: RunnableConfig, **kw: Any) -> str:
    q = QueryArgs(**kw)
    tid = thread_id_from(config)
    try:
        columns, rows = to_columns_rows(load_data_ref(tid, q.data_ref))
    except ValueError as exc:
        return f"Error: {exc}"
    lower = {c.lower(): i for i, c in enumerate(columns)}

    def col(name: str) -> int:
        if name in columns:
            return columns.index(name)
        if name.lower() in lower:
            return lower[name.lower()]
        raise ValueError(f"Unknown column '{name}'. Columns: {columns}")

    try:
        total = len(rows)
        for c in q.where:
            i = col(c.column)
            rows = [r for r in rows if _match(r[i], c)]
        if q.group_by or q.aggregations:
            gi = [col(g) for g in q.group_by]
            groups: dict[tuple, list[list[Any]]] = {}
            for r in rows:
                groups.setdefault(tuple(r[i] for i in gi), []).append(r)
            aggs = q.aggregations or {}
            columns_out = list(q.group_by) + [f"{fn}({c})" for c, fn in aggs.items()] + ["rows"]
            out = []
            for key, grp in groups.items():
                out.append(list(key) + [_agg([r[col(c)] for r in grp], fn) for c, fn in aggs.items()] + [len(grp)])
            columns, rows = columns_out, out
        elif q.select:
            si = [col(s) for s in q.select]
            columns, rows = [columns[i] for i in si], [[r[i] for i in si] for r in rows]
        if q.dedupe:
            seen, uniq = set(), []
            for r in rows:
                k = json.dumps(r, default=str)
                if k not in seen:
                    seen.add(k)
                    uniq.append(r)
            rows = uniq
        if q.sort_by:
            si = columns.index(q.sort_by) if q.sort_by in columns else col(q.sort_by)
            rows.sort(key=lambda r: (to_number(r[si]) is None, to_number(r[si]) if to_number(r[si]) is not None
                                     else str(r[si] or "").lower()), reverse=q.descending)
            if q.descending:  # keep empty values last
                rows.sort(key=lambda r: r[si] in (None, ""))
        if q.limit:
            rows = rows[: q.limit]
    except ValueError as exc:
        return f"Error: {exc}"

    msg = f"{len(rows)} rows (from {total}). Columns: {columns}\n" + md_table(columns, rows)
    if q.save_as:
        SCRATCHPAD.write(tid, q.save_as, json.dumps({"columns": columns, "rows": rows}, default=str))
        msg += f"\nSaved as '{q.save_as}' (use as data_ref)."
    return msg


def scratchpad_tools() -> list[BaseTool]:
    return [
        StructuredTool.from_function(
            coroutine=_write, name="scratchpad_write", args_schema=WriteArgs,
            description="Save notes, requirements, intermediate findings or JSON data to your scratchpad so they "
                        "survive context trimming. Use 'append' to accumulate data across pages."),
        StructuredTool.from_function(
            coroutine=_read, name="scratchpad_read", args_schema=ReadArgs,
            description="Read a scratchpad entry. Page through long entries with offset/limit, or grep for lines."),
        StructuredTool.from_function(coroutine=_list, name="scratchpad_list", args_schema=NoArgs,
                                     description="List scratchpad keys with sizes/row counts."),
        StructuredTool.from_function(coroutine=_delete, name="scratchpad_delete", args_schema=KeyArgs,
                                     description="Delete a scratchpad entry."),
        StructuredTool.from_function(
            coroutine=_query, name="data_query", args_schema=QueryArgs,
            description="Analyse tabular scratchpad data WITHOUT reading it all: filter, select, group/aggregate, "
                        "sort, limit, dedupe. Returns a preview; save_as stores the result for document tools. "
                        "Call with only data_ref to see columns and the first rows."),
    ]
