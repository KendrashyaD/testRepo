"""Runs the agent for one user turn and converts LangGraph stream output into UI events.

Event types pushed to the queue (and streamed as SSE):
  status     {message}                      - progress info (starting browser, waiting, ...)
  token      {text}                         - LLM text delta
  tool_start {id, name, args}               - tool call decided by the model (args redacted/condensed)
  tool_end   {id, name, status, output}     - tool result preview
  plan       {text}                         - task checklist updated
  file       {name, size, kind, url}        - a document was generated
  error      {message}
  done       {reason: completed|cancelled|error}
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError

from .config import get_settings
from .mcp_session import SessionManager
from .middleware import RUN_CONTEXT
from .utils import content_to_text, redact_args, truncate

log = logging.getLogger(__name__)

DOC_TOOLS = {"create_word_document", "create_pdf_document", "create_excel_workbook", "create_presentation"}


class RunRegistry:
    """Tracks the in-flight run per thread so it can be cancelled."""

    def __init__(self) -> None:
        self._runs: dict[str, asyncio.Task] = {}

    def is_running(self, thread_id: str) -> bool:
        task = self._runs.get(thread_id)
        return bool(task and not task.done())

    def add(self, thread_id: str, task: asyncio.Task) -> None:
        self._runs[thread_id] = task

    def remove(self, thread_id: str, task: asyncio.Task) -> None:
        if self._runs.get(thread_id) is task:
            self._runs.pop(thread_id, None)

    def cancel(self, thread_id: str) -> bool:
        task = self._runs.get(thread_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

    def cancel_all(self) -> None:
        for task in self._runs.values():
            task.cancel()


def ui_args(name: str, args: dict) -> dict:
    """Condense huge arguments (document specs) for the UI."""
    args = redact_args(args or {})
    if name in DOC_TOOLS:
        summary = {"filename": args.get("filename")}
        for k in ("blocks", "sheets", "slides"):
            if isinstance(args.get(k), list):
                summary[k] = len(args[k])
        return summary
    text = json.dumps(args, default=str)
    return args if len(text) < 4000 else {"_preview": truncate(text, 4000)}


async def run_turn(manager: SessionManager, thread_id: str, message: str, queue: asyncio.Queue) -> None:
    s = get_settings()

    async def emit(event_type: str, **data: Any) -> None:
        await queue.put({"type": event_type, "data": data})

    reason = "completed"
    try:
        await emit("status", message="Connecting to browser…")
        session = await manager.get(thread_id)

        if session.lock.locked():
            await emit("status", message="Browser is busy with another conversation, waiting…")
        async with session.lock:
            session.touch()
            RUN_CONTEXT.set({"thread_id": thread_id, "step": 0, "history": []})
            await emit("status", message="Thinking…")

            config = {
                "configurable": {"thread_id": thread_id},
                "recursion_limit": s.agent_max_steps * 2 + 5,
            }
            inputs = {"messages": [HumanMessage(content=message)]}

            async for mode, chunk in session.agent.astream(inputs, config, stream_mode=["messages", "updates"]):
                if mode == "messages":
                    msg, meta = chunk
                    if isinstance(msg, AIMessageChunk) and meta.get("langgraph_node") == "model":
                        text = content_to_text(msg.content)
                        if text:
                            await emit("token", text=text)
                    continue

                # mode == "updates": {node_name: state_update}
                for update in (chunk or {}).values():
                    if not isinstance(update, dict):
                        continue
                    for m in update.get("messages") or []:
                        if isinstance(m, AIMessage) and m.tool_calls:
                            for tc in m.tool_calls:
                                await emit("tool_start", id=tc["id"], name=tc["name"],
                                           args=ui_args(tc["name"], tc.get("args") or {}))
                        elif isinstance(m, ToolMessage):
                            text = content_to_text(m.content)
                            ok = m.status != "error"
                            await emit("tool_end", id=m.tool_call_id, name=m.name,
                                       status="success" if ok else "error",
                                       output=truncate(text, s.ui_tool_output_chars))
                            if ok and m.name == "update_plan":
                                await emit("plan", text=text)
                            artifact = getattr(m, "artifact", None)
                            if ok and isinstance(artifact, dict) and artifact.get("file"):
                                await emit("file", **artifact["file"])
            session.touch()

    except asyncio.CancelledError:
        reason = "cancelled"
        log.info("run cancelled thread=%s", thread_id)
        raise
    except GraphRecursionError:
        reason = "error"
        await emit("error", message=f"Stopped after reaching the step limit ({s.agent_max_steps} steps). "
                                    "Ask me to continue or narrow the task.")
    except Exception as exc:  # noqa: BLE001
        reason = "error"
        log.exception("run failed thread=%s", thread_id)
        await emit("error", message=str(exc) or exc.__class__.__name__)
    finally:
        # put_nowait: never block/await inside a cancelled task
        queue.put_nowait({"type": "done", "data": {"reason": reason}})
        queue.put_nowait(None)
