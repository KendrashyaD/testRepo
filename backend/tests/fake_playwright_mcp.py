"""A tiny stdio MCP server that mimics Playwright MCP tool names/response shapes (for offline e2e tests)."""
import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from PIL import Image, ImageDraw

mcp = FastMCP("fake-playwright")
STATE = {"url": "about:blank", "tabs": ["about:blank"], "current": 0}
OUT = Path(os.environ.get("FAKE_OUTPUT_DIR", "."))

SALES_HTML_TABLE = {
    "headers": ["Month", "Region", "Units", "Revenue"],
    "rows": [[f"2026-0{m}", r, f"{1000 + m * 37 + i * 11:,}", f"₹{(m * 91_000 + i * 13_500):,}.00"]
             for m in range(1, 7) for i, r in enumerate(["North", "South", "East", "West"])],
}


def _snapshot() -> str:
    return (f"### Page state\n- Page URL: {STATE['url']}\n- Page Title: Fake\n```yaml\n"
            + "\n".join(f"- row \"line {i}\" [ref=e{i}]" for i in range(400)) + "\n```")


def _result(value) -> str:
    return f"### Result\n{json.dumps(value)}\n\n### Ran Playwright code\n```js\nawait page.evaluate(...)\n```"


@mcp.tool()
def browser_navigate(url: str) -> str:
    """Navigate to a URL"""
    STATE["url"] = url
    STATE["tabs"][STATE["current"]] = url
    return _snapshot()


@mcp.tool()
def browser_snapshot() -> str:
    """Capture accessibility snapshot"""
    return _snapshot()


@mcp.tool()
def browser_click(element: str, ref: str) -> str:
    """Click"""
    return _snapshot()


@mcp.tool()
def browser_tabs(action: str, index: int | None = None) -> str:
    """List, create, close, or select a browser tab."""
    if action == "new":
        STATE["tabs"].append("about:blank")
        STATE["current"] = len(STATE["tabs"]) - 1
    elif action == "close":
        i = STATE["current"] if index is None else index
        STATE["tabs"].pop(i)
        STATE["current"] = max(0, min(STATE["current"], len(STATE["tabs"]) - 1))
    elif action == "select" and index is not None:
        STATE["current"] = index
    STATE["url"] = STATE["tabs"][STATE["current"]]
    return "### Open tabs\n" + "\n".join(
        f"- {i}:{' (current)' if i == STATE['current'] else ''} [Fake] ({u})" for i, u in enumerate(STATE["tabs"]))


@mcp.tool()
def browser_evaluate(function: str) -> str:
    """Evaluate JavaScript"""
    if "results: out" in function:
        if "google." in STATE["url"]:
            return _result({"url": STATE["url"], "blocked": True, "answer": "", "results": []})  # simulate captcha
        return _result({"url": STATE["url"], "blocked": False, "answer": "",
                        "results": [{"title": "Sales portal help", "url": "https://example.com/help",
                                     "snippet": "Reports live under Analytics > Sales."}]})
    if "KIND" in function:
        if '"tables"' in function:
            return _result({"url": STATE["url"], "title": "Sales report", "tables": [SALES_HTML_TABLE]})
        return _result({"url": STATE["url"], "title": "Sales report", "text": "Lorem report text " * 50})
    if "location.href" in function:
        return _result(STATE["url"])
    return _result({"big": ["x" * 100] * 300})  # large output -> auto-save to scratchpad


@mcp.tool()
def browser_take_screenshot(filename: str, type: str = "png") -> str:
    """Take a screenshot"""
    OUT.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1366, 768), (240, 244, 248))
    ImageDraw.Draw(img).rectangle([0, 0, 1366, 60], fill=(11, 60, 93))
    img.save(OUT / filename)
    return f"Took screenshot {filename}"


if __name__ == "__main__":
    mcp.run()
