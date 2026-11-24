"""Offline end-to-end test of the full stack: FastAPI SSE -> create_agent -> middleware -> custom tools ->
real MCP stdio session (fake Playwright server) -> file download. Uses a scripted LLM (no AI Core needed).

    cd backend
    python -m tests.e2e_fake_agent
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TMP = Path(tempfile.mkdtemp(prefix="webagent-e2e-"))
os.environ.update({"ARTIFACTS_DIR": str(TMP), "BROWSER_PREWARM": "false", "SCREENSHOT_MODE": "after_action",
                   "AUTO_SAVE_CHARS": "5000", "LOOP_GUARD_REPEATS": "3"})

import httpx  # noqa: E402
from langchain_core.language_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.mcp_session import SessionManager  # noqa: E402

SEEN_SYSTEM: list[str] = []
SEEN_TOOL_RESULTS: list[str] = []


def call(name: str, args: dict, i: int) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{i}", "type": "tool_call"}])


SCRIPT = [
    ("update_plan", {"goal": "Download sales report data to Excel + Word summary", "steps": [
        {"step": "Find the report", "status": "in_progress"}, {"step": "Extract data"},
        {"step": "Create files"}]}),
    ("web_search", {"query": "fake sales portal where is the sales report"}),
    ("browser_navigate", {"url": "https://example.com/analytics/sales"}),
    ("extract_page_content", {"kind": "tables", "save_as": "sales"}),
    ("data_query", {"data_ref": "sales", "group_by": ["Region"],
                    "aggregations": {"Revenue": "sum", "Units": "sum"}, "sort_by": "sum(Revenue)",
                    "descending": True, "save_as": "by_region"}),
    ("browser_evaluate", {"function": "() => bigData()"}),
    ("create_excel_workbook", {"filename": "sales report", "sheets": [
        {"name": "Raw", "data_ref": "sales", "totals": {"Units": "sum", "Revenue": "sum"},
         "charts": [{"chart_type": "column", "category_column": "Month", "value_columns": ["Units"]}],
         "notes": "Source: https://example.com/analytics/sales"},
        {"name": "By region", "data_ref": "by_region"}]}),
    ("create_word_document", {"filename": "summary", "title": "Sales summary", "blocks": [
        {"type": "paragraph", "text": "Top region is **North**."},
        {"type": "table", "table": {"data_ref": "by_region"}},
        {"type": "chart", "chart": {"chart_type": "bar", "data_ref": "by_region", "category_column": "Region",
                                    "value_columns": ["sum(Revenue)"]}},
        {"type": "image", "image": "screenshot:latest", "caption": "Report page"}]}),
    ("create_presentation", {"filename": "deck", "slides": [
        {"layout": "title", "title": "Sales"},
        {"layout": "table", "title": "By region", "table": {"data_ref": "by_region"}}]}),
    ("create_pdf_document", {"filename": "summary", "title": "Sales summary", "blocks": [
        {"type": "table", "table": {"data_ref": "by_region"}}]}),
    ("browser_click", {"element": "Next", "ref": "e1"}),
    ("browser_click", {"element": "Next", "ref": "e1"}),
    ("browser_click", {"element": "Next", "ref": "e1"}),   # 3rd identical call -> loop guard
]


class ScriptedModel(BaseChatModel):
    step: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        SEEN_SYSTEM.append(str(messages[0].content))
        SEEN_TOOL_RESULTS.append(" ".join(str(m.content) for m in messages[1:]))
        if self.step < len(SCRIPT):
            name, args = SCRIPT[self.step]
            msg = call(name, args, self.step)
        else:
            msg = AIMessage(content="Done. Created **sales report.xlsx** and **summary.docx**.")
        self.step += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])


def fake_connection(s, key):
    return {"transport": "stdio", "command": sys.executable,
            "args": [str(Path(__file__).parent / "fake_playwright_mcp.py")],
            "env": {**os.environ, "FAKE_OUTPUT_DIR": str(s.screenshots_dir)}}


def parse_sse(raw: str) -> list[tuple[str, Any]]:
    events = []
    for block in raw.split("\n\n"):
        ev, data = "message", []
        for line in block.splitlines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if data:
            events.append((ev, json.loads("\n".join(data))))
    return events


async def main() -> int:
    from app import main as app_main
    from app.agent import build_agent

    model = ScriptedModel()
    app = app_main.app
    app.state.manager = SessionManager(get_settings(), lambda sess: build_agent(sess, model=model),
                                       connection_factory=fake_connection)
    async with app_main.lifespan(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=180) as client:
            r = await client.post("/api/chat", json={"message": "Review the sales report and download it as xlsx"})
            events = parse_sse(r.text)
            kinds = [e for e, _ in events]
            thread = events[0][1]["thread_id"]
            files = [d for e, d in events if e == "file"]
            tool_ends = {d["id"]: d for e, d in events if e == "tool_end"}
            errors = [d for e, d in events if e == "error"]

            print("events:", {k: kinds.count(k) for k in dict.fromkeys(kinds)})
            for d in tool_ends.values():
                print(f"  {d['status']:7s} {d['name']:24s} {d['output'][:110]!r}")
            assert not errors, errors
            assert kinds[-1] == "done"
            assert len(files) == 4, files
            assert "plan" in kinds
            # web_search: google blocked -> fallback to bing; tab restored
            assert "via bing" in tool_ends["call_1"]["output"], tool_ends["call_1"]["output"]
            # extraction stored 24 rows
            assert "24 rows" in tool_ends["call_3"]["output"]
            # data_query grouped 4 regions
            assert tool_ends["call_4"]["output"].startswith("4 rows")
            # large evaluate output auto-saved
            assert "auto/evaluate-1" in tool_ends["call_5"]["output"]
            # loop guard
            assert tool_ends["call_12"]["status"] == "error" and "Loop detected" in tool_ends["call_12"]["output"]
            # working memory injected into the system message after update_plan
            assert any("WORKING MEMORY" in s and "Find the report" in s for s in SEEN_SYSTEM[1:]), "memory"
            assert any("Scratchpad keys:" in s and "by_region" in s for s in SEEN_SYSTEM), "scratchpad index"
            # old snapshots trimmed in later model calls
            assert any("older output trimmed" in t for t in SEEN_TOOL_RESULTS), "snapshot trimming"

            for f in files:
                d = await client.get(f["url"])
                assert d.status_code == 200 and len(d.content) > 1000, (f, d.status_code)
                print(f"  downloaded {f['name']:22s} {len(d.content) / 1024:6.1f} KB  {d.headers['content-type'][:40]}")
            listing = (await client.get(f"/api/threads/{thread}/files")).json()
            assert len(listing) == 4
            assert (await client.get(f"/api/files/{thread}/..%2F..%2Fsecret")).status_code == 404

            # second turn on same thread (memory + scratchpad survive)
            r2 = await client.post("/api/chat", json={"message": "thanks", "thread_id": thread})
            assert parse_sse(r2.text)[-1][0] == "done"
            assert (await client.delete(f"/api/threads/{thread}")).status_code == 200

    shots = list((TMP / "screenshots").glob("*.png"))
    print(f"screenshots captured: {len(shots)}")
    assert shots, "no screenshots"
    out = Path(__file__).parent / "out" / "e2e"
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(TMP / "files", out)
    print("E2E PASSED. Files copied to", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
