"""Composite browser tools built on the Playwright MCP session.

web_search           - Google search (Bing / DuckDuckGo fallback) in a separate tab, returns compact results
                       and restores the user's tab, so research never loses the task page.
extract_page_content - pulls tables / text / links from the current page via one evaluate call and stores the
                       full result in the scratchpad (only a preview goes back to the LLM).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal, Optional
from urllib.parse import parse_qs, quote_plus, urlparse

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from ..config import get_settings
from ..utils import content_to_text, origin_allowed
from .scratchpad_tools import md_table
from .workspace import SCRATCHPAD, thread_id_from

log = logging.getLogger(__name__)

ENGINES = {
    "google": "https://www.google.com/search?hl=en&num={n}&q={q}",
    "bing": "https://www.bing.com/search?setlang=en&count={n}&q={q}",
    "duckduckgo": "https://html.duckduckgo.com/html/?q={q}",
}

SEARCH_JS = r"""() => {
  const host = location.hostname, out = [], seen = new Set();
  const add = (title, url, snippet) => {
    if (!title || !url || seen.has(url)) return;
    try { const u = new URL(url, location.href);
      if (u.hostname.endsWith('duckduckgo.com') && u.searchParams.get('uddg')) url = u.searchParams.get('uddg');
      else url = u.href; } catch (e) { return; }
    if (/^https?:\/\/(www\.)?(google|bing)\.[a-z.]+\/(search|aclk|url\?)/.test(url)) return;
    seen.add(url);
    out.push({title: title.trim().slice(0, 200), url, snippet: (snippet || '').replace(/\s+/g, ' ').trim().slice(0, 320)});
  };
  let blocked = false, answer = '';
  if (host.includes('google.')) {
    blocked = /\/sorry\/|consent\./.test(location.href) || !!document.querySelector('#captcha-form, iframe[src*="recaptcha"]');
    const ab = document.querySelector('[data-attrid="wa:/description"], .hgKElc, .IZ6rdc, .Z0LcW');
    if (ab) answer = ab.innerText;
    document.querySelectorAll('#search a h3, #rso a h3').forEach(h => {
      const a = h.closest('a'); const box = a && (a.closest('div.MjjYud, div.g, div[data-hveid]') || a.parentElement);
      const sn = box && box.querySelector('div.VwiC3b, [data-sncf], div[style*="line-clamp"], span.aCOpRe');
      add(h.innerText, a && a.href, sn ? sn.innerText : '');
    });
  } else if (host.includes('bing.')) {
    blocked = !!document.querySelector('#b_captcha, .captcha');
    document.querySelectorAll('li.b_algo').forEach(li => {
      const a = li.querySelector('h2 a'); const sn = li.querySelector('.b_caption p, .b_lineclamp2, .b_lineclamp3, p');
      add(a && a.innerText, a && a.href, sn && sn.innerText);
    });
  } else {
    blocked = !!document.querySelector('.anomaly-modal, #challenge-form');
    document.querySelectorAll('.result').forEach(r => {
      const a = r.querySelector('.result__a'); const sn = r.querySelector('.result__snippet');
      add(a && a.innerText, a && a.getAttribute('href'), sn && sn.innerText);
    });
  }
  return {url: location.href, blocked, answer: answer.slice(0, 600), results: out};
}"""

EXTRACT_JS = r"""() => {
  const KIND = %(kind)s, SELECTOR = %(selector)s, MAXCHARS = %(maxchars)d;
  const root = SELECTOR ? document.querySelector(SELECTOR) : document;
  if (!root) return {error: 'selector not found: ' + SELECTOR};
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  if (KIND === 'tables') {
    const tables = [];
    root.querySelectorAll('table').forEach((t, ti) => {
      if (!visible(t)) return;
      let headers = [...t.querySelectorAll('thead tr:last-child th, thead tr:last-child td')].map(c => clean(c.innerText));
      let rows = [...t.querySelectorAll('tbody tr, :scope > tr')].map(r => [...r.children].map(c => clean(c.innerText)));
      if (!headers.length && rows.length && t.querySelector('tr th')) {
        const first = t.querySelector('tr'); headers = [...first.children].map(c => clean(c.innerText));
        rows = rows.filter((r, i) => i > 0 || r.join('|') !== headers.join('|'));
      }
      rows = rows.filter(r => r.some(c => c));
      if (rows.length) tables.push({caption: clean(t.caption && t.caption.innerText) || clean(t.getAttribute('aria-label')), headers, rows});
    });
    root.querySelectorAll('[role="grid"], [role="table"], [role="treegrid"]').forEach(g => {
      if (g.tagName === 'TABLE' || !visible(g)) return;
      const headers = [...g.querySelectorAll('[role="columnheader"]')].map(c => clean(c.innerText));
      const rows = [...g.querySelectorAll('[role="row"]')].map(r => [...r.querySelectorAll('[role="gridcell"], [role="cell"]')]
        .map(c => clean(c.innerText))).filter(r => r.length && r.some(c => c));
      if (rows.length) tables.push({caption: clean(g.getAttribute('aria-label')), headers, rows});
    });
    return {url: location.href, title: document.title, tables};
  }
  if (KIND === 'links') {
    const links = [...root.querySelectorAll('a[href]')].filter(visible)
      .map(a => ({text: clean(a.innerText || a.getAttribute('aria-label')), url: a.href}))
      .filter(l => l.text && !l.url.startsWith('javascript:'));
    return {url: location.href, title: document.title, links};
  }
  const main = SELECTOR ? root : (document.querySelector('main, [role="main"], article') || document.body);
  return {url: location.href, title: document.title, text: (main.innerText || '').slice(0, MAXCHARS)};
}"""


# --------------------------------------------------------------------------- MCP helpers
def parse_eval_result(raw: Any) -> Any:
    """Extract the JSON value from a Playwright MCP browser_evaluate response."""
    text = content_to_text(raw) if not isinstance(raw, str) else raw
    m = re.search(r"###\s*Result\s*\n(.*?)(?:\n###\s|\Z)", text, re.S)
    body = (m.group(1) if m else text).strip()
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body).strip()
    for candidate in (body, body[body.find("{"):] if "{" in body else ""):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
            if isinstance(value, str) and value[:1] in "[{":
                value = json.loads(value)
            return value
        except ValueError:
            continue
    raise ValueError(f"Could not parse evaluate result: {text[:300]}")


class Browser:
    """Thin wrapper over the session's MCP tools (handles old/new tab tool names)."""

    def __init__(self, tools: dict[str, BaseTool]):
        self.t = tools

    async def call(self, name: str, args: dict) -> str:
        if name not in self.t:
            raise RuntimeError(f"Playwright MCP tool '{name}' is not available")
        return content_to_text(await self.t[name].ainvoke(args))

    async def evaluate(self, js: str) -> Any:
        return parse_eval_result(await self.call("browser_evaluate", {"function": js}))

    async def tabs(self) -> tuple[list[str], Optional[int]]:
        if "browser_tabs" in self.t:
            text = await self.call("browser_tabs", {"action": "list"})
        elif "browser_tab_list" in self.t:
            text = await self.call("browser_tab_list", {})
        else:
            return [], None
        lines = [l for l in text.splitlines() if re.match(r"\s*-\s*\d+:", l)]
        current = next((int(re.match(r"\s*-\s*(\d+):", l).group(1)) for l in lines if "(current)" in l), None)
        return lines, current

    async def new_tab(self) -> bool:
        if "browser_tabs" in self.t:
            await self.call("browser_tabs", {"action": "new"})
            return True
        if "browser_tab_new" in self.t:
            await self.call("browser_tab_new", {})
            return True
        return False

    async def close_current_and_select(self, index: Optional[int]) -> None:
        _, cur = await self.tabs()
        if "browser_tabs" in self.t:
            await self.call("browser_tabs", {"action": "close", **({"index": cur} if cur is not None else {})})
            if index is not None:
                await self.call("browser_tabs", {"action": "select", "index": index})
        elif "browser_tab_close" in self.t:
            await self.call("browser_tab_close", {**({"index": cur} if cur is not None else {})})
            if index is not None and "browser_tab_select" in self.t:
                await self.call("browser_tab_select", {"index": index})


# --------------------------------------------------------------------------- tools
class SearchArgs(BaseModel):
    query: str = Field(..., description="Search query, as you would type it into Google")
    max_results: int = Field(6, ge=1, le=10)
    engine: Literal["auto", "google", "bing", "duckduckgo"] = "auto"


class ExtractArgs(BaseModel):
    kind: Literal["tables", "text", "links"] = "tables"
    save_as: str = Field(..., description="Scratchpad key for the full result (tables: one key per table, "
                                          "suffixed _1, _2 ... when several)")
    selector: Optional[str] = Field(None, description="Optional CSS selector to limit extraction to one region")
    append: bool = Field(False, description="Append rows to an existing key (e.g. paginated tables)")


def research_tools(session) -> list[BaseTool]:
    s = get_settings()

    async def web_search(config: RunnableConfig, query: str, max_results: int = 6, engine: str = "auto") -> str:
        br = Browser(session.tools)
        order = [engine] if engine != "auto" else ["google", "bing", "duckduckgo"]
        order = [e for e in order if origin_allowed(ENGINES[e], s.allowed_origin_list)]
        if not order:
            return "Web search is unavailable: no search engine is in ALLOWED_ORIGINS."
        _, original = await br.tabs()
        opened = await br.new_tab()
        back_url = None
        if not opened:  # very old MCP: search in the same tab and navigate back afterwards
            back_url = (await br.evaluate("() => location.href"))
        notes, data = [], None
        try:
            for eng in order:
                url = ENGINES[eng].format(q=quote_plus(query), n=max_results + 2)
                try:
                    await br.call("browser_navigate", {"url": url})
                    data = await br.evaluate(SEARCH_JS)
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"{eng}: {exc}")
                    continue
                if data.get("results") and not data.get("blocked"):
                    data["engine"] = eng
                    break
                notes.append(f"{eng}: {'blocked/captcha' if data.get('blocked') else 'no results'}")
                data = None
        finally:
            try:
                if opened:
                    await br.close_current_and_select(original)
                elif back_url:
                    await br.call("browser_navigate", {"url": back_url})
            except Exception as exc:  # noqa: BLE001
                log.warning("could not restore tab after search: %s", exc)
        if not data:
            return f"No search results for '{query}' ({'; '.join(notes)}). Try rephrasing or another engine."
        results = data["results"][:max_results]
        SCRATCHPAD.write(thread_id_from(config), f"search/{re.sub(r'[^a-z0-9]+', '_', query.lower())[:40]}",
                         json.dumps(results, ensure_ascii=False))
        lines = [f"Search results for '{query}' via {data['engine']} (your task tab was restored):"]
        if data.get("answer"):
            lines.append(f"Answer box: {data['answer']}")
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}")
        lines.append("To read a result, open it with browser_tabs(action='new') + browser_navigate, "
                     "or extract_page_content(kind='text').")
        return "\n".join(lines)

    async def extract_page_content(config: RunnableConfig, kind: str = "tables", save_as: str = "page",
                                   selector: Optional[str] = None, append: bool = False) -> str:
        br = Browser(session.tools)
        js = EXTRACT_JS % {"kind": json.dumps(kind), "selector": json.dumps(selector), "maxchars": 200_000}
        try:
            data = await br.evaluate(js)
        except Exception as exc:  # noqa: BLE001
            return f"Extraction failed: {exc}"
        if data.get("error"):
            return f"Extraction failed: {data['error']}"
        tid = thread_id_from(config)
        src = f"Source: {data.get('title', '')} ({data.get('url', '')})"

        if kind == "text":
            text = data.get("text", "")
            SCRATCHPAD.write(tid, save_as, text, append=append)
            return (f"{src}\nSaved {len(text):,} chars of page text to '{save_as}'. Preview:\n{text[:1500]}"
                    + ("\n… use scratchpad_read(key, offset/grep) for more." if len(text) > 1500 else ""))
        if kind == "links":
            links = data.get("links", [])
            SCRATCHPAD.write(tid, save_as, json.dumps(links, ensure_ascii=False))
            return f"{src}\nSaved {len(links)} links to '{save_as}'.\n" + md_table(
                ["text", "url"], [[l["text"], l["url"]] for l in links], 20)

        tables = data.get("tables", [])
        if not tables:
            return (f"{src}\nNo HTML/ARIA tables found. For card or list layouts use kind='text', or "
                    "browser_evaluate with custom JS returning a JSON array of objects, then scratchpad_write.")
        out = [src, f"Found {len(tables)} table(s):"]
        for i, t in enumerate(tables, 1):
            key = save_as if len(tables) == 1 else f"{save_as}_{i}"
            headers = t["headers"] or [f"Column {k + 1}" for k in range(max(len(r) for r in t["rows"]))]
            width = len(headers)
            rows = [(r + [""] * width)[:width] for r in t["rows"]]
            existing = SCRATCHPAD.get(tid, key)
            if append and existing:
                try:
                    prev = json.loads(existing.content)
                    if prev.get("columns") == headers:
                        rows = prev["rows"] + rows
                except (ValueError, AttributeError):
                    pass
            SCRATCHPAD.write(tid, key, json.dumps({"columns": headers, "rows": rows}, ensure_ascii=False))
            cap = f" - {t['caption']}" if t.get("caption") else ""
            out.append(f"\nTable {i}{cap} -> '{key}' ({len(rows)} rows total)\n" + md_table(headers, rows, 5))
        out.append("\nUse these keys as data_ref in data_query / create_* tools.")
        return "\n".join(out)

    return [
        StructuredTool.from_function(
            coroutine=web_search, name="web_search", args_schema=SearchArgs,
            description="Search the web (Google, falls back to Bing/DuckDuckGo) in a separate browser tab and "
                        "return titles, URLs and snippets; the current task page is preserved. Use it whenever "
                        "the task is unclear: unknown terms, how a web app works, where a page/report lives, "
                        "official URLs, or facts you need to verify."),
        StructuredTool.from_function(
            coroutine=extract_page_content, name="extract_page_content", args_schema=ExtractArgs,
            description="Extract ALL tables (HTML + ARIA grids, e.g. SAP UI5), page text or links from the "
                        "current page in one step and store the full data in the scratchpad. Returns a short "
                        "preview. Prefer this over reading large snapshots when you need data for a report/file. "
                        "For paginated tables: extract page 1, go to the next page, extract with append=true."),
    ]
