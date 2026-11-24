"""create_agent middleware that makes Playwright MCP usage efficient, robust and "reasoning-aware".

ContextBudgetMiddleware (around every model call, does NOT mutate stored history):
  - repairs dangling tool calls (e.g. after the user pressed Stop mid-tool)
  - trims all but the latest N large tool results (page snapshots) -> keeps the context small
  - trims huge tool-call arguments of older steps (e.g. big document specs)
  - injects WORKING MEMORY (plan, scratchpad index, created files) into the system message
  - forces sequential tool calls (refs from one snapshot are only valid for one action)

BrowserToolMiddleware (around every tool call):
  - allow-list check for navigation
  - loop guard: blocks the same call repeated N times in a row and asks the model to change approach
  - per-tool timeout + converts exceptions into ToolMessages the model can recover from
  - auto-saves large raw outputs (evaluate/network/console) to the scratchpad
  - captures a screenshot to disk after state-changing actions (no LLM tokens spent)
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool

from .config import Settings
from .utils import content_to_text, origin_allowed, redact_args, truncate

log = logging.getLogger(__name__)

# Set per run by the runner: {"thread_id": str, "step": int, "history": [...]}
RUN_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("run_context", default=None)

ACTION_TOOLS = {
    "browser_navigate", "browser_navigate_back", "browser_click", "browser_type",
    "browser_fill_form", "browser_select_option", "browser_press_key", "browser_drag",
    "browser_file_upload", "browser_handle_dialog", "browser_tabs",
}
AUTO_SAVE_TOOLS = {"browser_evaluate", "browser_network_requests", "browser_console_messages", "browser_run_code"}
NO_LOOP_GUARD = {"think", "update_plan", "scratchpad_list", "browser_wait_for"}
TRIM_NOTE = "[older output trimmed to save context - refs above are stale; use the latest snapshot or the scratchpad]"


def _override(request: Any, **changes: Any) -> Any:
    if hasattr(request, "override"):
        return request.override(**changes)
    for k, v in changes.items():  # very old langchain 1.0 pre-releases
        setattr(request, k, v)
    return request


class ContextBudgetMiddleware(AgentMiddleware):
    def __init__(self, settings: Settings):
        super().__init__()
        self.s = settings

    @staticmethod
    def _repair_dangling_tool_calls(messages: list) -> list:
        answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        out: list = []
        for m in messages:
            out.append(m)
            if isinstance(m, AIMessage) and m.tool_calls:
                for tc in m.tool_calls:
                    if tc.get("id") and tc["id"] not in answered:
                        out.append(ToolMessage(
                            content="Not executed: the run was cancelled by the user.",
                            tool_call_id=tc["id"], name=tc.get("name"), status="error",
                        ))
        return out

    def _trim(self, messages: list) -> list:
        out = list(messages)
        big = [i for i, m in enumerate(out)
               if isinstance(m, ToolMessage) and len(content_to_text(m.content)) > self.s.snapshot_trim_chars]
        keep = set(big[-self.s.snapshot_keep_last:]) if self.s.snapshot_keep_last > 0 else set()
        for i in big:
            text = content_to_text(out[i].content)
            if i in keep:
                if len(text) > self.s.tool_result_max_chars:
                    out[i] = out[i].model_copy(update={"content": truncate(
                        text, self.s.tool_result_max_chars, "(full output may be in the scratchpad)")})
            else:
                out[i] = out[i].model_copy(update={"content": text[: self.s.snapshot_trim_chars] + "\n…" + TRIM_NOTE})

        # Old, huge tool-call arguments (e.g. document specs) - keep the last 2 AI messages intact
        ai_idx = [i for i, m in enumerate(out) if isinstance(m, AIMessage) and m.tool_calls]
        for i in ai_idx[:-2]:
            calls, changed = [], False
            for tc in out[i].tool_calls:
                if len(json.dumps(tc.get("args", {}), default=str)) > 3000:
                    tc = {**tc, "args": {"_trimmed": "large arguments omitted from history"}}
                    changed = True
                calls.append(tc)
            if changed:
                out[i] = out[i].model_copy(update={"tool_calls": calls, "additional_kwargs": {}})
        return out

    def _working_memory(self) -> str:
        if not self.s.working_memory_in_prompt:
            return ""
        ctx = RUN_CONTEXT.get() or {}
        tid = ctx.get("thread_id")
        if not tid:
            return ""
        from .tools.workspace import SCRATCHPAD, list_files

        parts = []
        plan = SCRATCHPAD.get(tid, "plan")
        if plan:
            parts.append("Current plan:\n" + plan.content)
        index = [d for d in SCRATCHPAD.index(tid) if not d.startswith(("plan ", "thoughts "))]
        if index:
            parts.append("Scratchpad keys: " + "; ".join(index[-25:]))
        try:
            files = list_files(tid)
        except OSError:
            files = []
        if files:
            parts.append("Files created in this conversation: " + ", ".join(f["name"] for f in files[-15:]))
        return ("\n\n## WORKING MEMORY (auto-updated)\n" + "\n".join(parts)) if parts else ""

    async def awrap_model_call(self, request, handler):
        messages = self._trim(self._repair_dangling_tool_calls(list(request.messages)))
        changes: dict[str, Any] = {"messages": messages}
        if not self.s.llm_parallel_tool_calls:
            changes["model_settings"] = {**(getattr(request, "model_settings", None) or {}),
                                         "parallel_tool_calls": False}
        memory = self._working_memory()
        if memory:
            sys_msg = getattr(request, "system_message", None)
            if sys_msg is not None:
                changes["system_message"] = SystemMessage(content=content_to_text(sys_msg.content) + memory)
            elif isinstance(getattr(request, "system_prompt", None), str):
                changes["system_prompt"] = request.system_prompt + memory
        try:
            request = _override(request, **changes)
        except TypeError:  # older ModelRequest without some fields
            changes.pop("system_message", None)
            request = _override(request, **changes)
        return await handler(request)


class BrowserToolMiddleware(AgentMiddleware):
    def __init__(self, settings: Settings, screenshot_tool: BaseTool | None):
        super().__init__()
        self.s = settings
        self.screenshot_tool = screenshot_tool

    def _error(self, call: dict, text: str) -> ToolMessage:
        return ToolMessage(content=text, tool_call_id=call["id"], name=call["name"], status="error")

    def _loop_guard(self, name: str, args: dict) -> bool:
        """True if this exact call was already made N-1 times in a row."""
        if name in NO_LOOP_GUARD or self.s.loop_guard_repeats <= 1:
            return False
        ctx = RUN_CONTEXT.get()
        if ctx is None:
            return False
        sig = name + json.dumps(args, sort_keys=True, default=str)
        hist = ctx.setdefault("history", [])
        n = self.s.loop_guard_repeats - 1
        repeated = len(hist) >= n and all(h == sig for h in hist[-n:])
        hist.append(sig)
        del hist[:-20]
        return repeated

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        name, args = call["name"], call.get("args") or {}
        log.info("tool -> %s %s", name, redact_args(args))

        if name == "browser_navigate" and not origin_allowed(args.get("url"), self.s.allowed_origin_list):
            return self._error(call, f"Blocked: {args.get('url')} is not in the allowed sites "
                                     f"({', '.join(self.s.allowed_origin_list)}).")
        if self._loop_guard(name, args):
            return self._error(call, f"Loop detected: you already called {name} with identical arguments "
                                     f"{self.s.loop_guard_repeats - 1} times in a row and it did not help. Call `think`, "
                                     "then try a different approach (other element/tool, web_search for how this "
                                     "app works, or ask the user).")
        try:
            result = await asyncio.wait_for(handler(request), self.s.tool_timeout_s)
        except asyncio.TimeoutError:
            return self._error(call, f"Tool timed out after {self.s.tool_timeout_s:.0f}s. "
                                     "Check the page state with browser_snapshot and try another approach.")
        except Exception as exc:  # noqa: BLE001 - let the model recover
            log.warning("tool %s failed: %s", name, exc)
            return self._error(call, f"Tool error: {exc}. Take a fresh snapshot and try a different approach.")

        failed = isinstance(result, ToolMessage) and result.status == "error"
        if isinstance(result, ToolMessage) and not failed and name in AUTO_SAVE_TOOLS:
            result = self._auto_save(name, result)
        if not failed and name in ACTION_TOOLS:
            await self._screenshot(name)
        return result

    def _auto_save(self, name: str, result: ToolMessage) -> ToolMessage:
        text = content_to_text(result.content)
        ctx = RUN_CONTEXT.get() or {}
        if len(text) <= self.s.auto_save_chars or not ctx.get("thread_id"):
            return result
        from .tools.workspace import SCRATCHPAD

        ctx["auto"] = ctx.get("auto", 0) + 1
        key = f"auto/{name.removeprefix('browser_')}-{ctx['auto']}"
        SCRATCHPAD.write(ctx["thread_id"], key, text)
        note = (f"[Full output ({len(text):,} chars) saved to scratchpad key '{key}' - use scratchpad_read with "
                f"offset/grep, or data_query if it is JSON.]\n")
        return result.model_copy(update={"content": note + truncate(text, self.s.auto_save_chars)})

    async def _screenshot(self, tool_name: str) -> None:
        if self.s.screenshot_mode == "off" or self.screenshot_tool is None:
            return
        ctx = RUN_CONTEXT.get() or {}
        ctx["step"] = ctx.get("step", 0) + 1
        thread = (ctx.get("thread_id") or "run")[:12]
        filename = f"{thread}-{time.strftime('%Y%m%d-%H%M%S')}-{ctx['step']:03d}-{tool_name.removeprefix('browser_')}.png"
        try:
            await asyncio.wait_for(self.screenshot_tool.ainvoke({"filename": filename, "type": "png"}), 20)
        except Exception as exc:  # noqa: BLE001 - screenshots are best effort
            log.debug("screenshot failed: %s", exc)
