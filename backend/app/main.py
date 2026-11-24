"""FastAPI app: REST + streamed responses (Server-Sent Events over a POST body). No WebSockets."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from urllib.parse import quote
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import build_agent, get_checkpointer
from .config import get_settings
from .mcp_session import SHARED_KEY, SessionManager
from .runner import RunRegistry, run_turn
from .tools.document_tools import MIME
from .tools.workspace import SCRATCHPAD, files_dir, list_files

settings = get_settings()
logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("web_agent")

HEARTBEAT_S = 15
THREAD_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


@asynccontextmanager
async def lifespan(app: FastAPI):
    manager = getattr(app.state, "manager", None) or SessionManager(settings, build_agent)
    app.state.manager = manager
    app.state.runs = RunRegistry()
    background = [asyncio.create_task(manager.reap_idle_forever())]

    if settings.browser_isolation == "shared" and settings.browser_prewarm:
        async def prewarm():
            try:
                await manager.get(SHARED_KEY)
            except Exception as exc:  # noqa: BLE001 - retried lazily on first chat
                log.warning("Browser prewarm failed (will retry on first request): %s", exc)
        background.append(asyncio.create_task(prewarm()))

    yield

    app.state.runs.cancel_all()
    for task in background:
        task.cancel()
    await manager.close_all()


app = FastAPI(title="Web Agent", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origin_list,
    allow_methods=["GET", "POST", "DELETE"], allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    thread_id: str | None = Field(default=None, pattern=THREAD_PATTERN)


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _check_thread(thread_id: str) -> None:
    import re

    if not re.match(THREAD_PATTERN, thread_id):
        raise HTTPException(400, "invalid thread id")


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/info")
async def info(request: Request):
    return {
        "model": settings.llm_model,
        "headless": settings.pw_headless,
        "browser": settings.pw_browser or "default",
        "allowed_origins": settings.allowed_origin_list,
        "document_tools": settings.enable_document_tools,
        "web_search": settings.enable_web_search,
        **request.app.state.manager.stats(),
    }


@app.post("/api/chat")
async def chat(body: ChatRequest, request: Request):
    runs: RunRegistry = request.app.state.runs
    thread_id = body.thread_id or uuid.uuid4().hex
    if runs.is_running(thread_id):
        raise HTTPException(409, "A run is already in progress for this conversation.")

    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(run_turn(request.app.state.manager, thread_id, body.message, queue))
    runs.add(thread_id, task)

    async def stream():
        try:
            yield sse("meta", {"thread_id": thread_id, "model": settings.llm_model})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), HEARTBEAT_S)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"   # keeps CF gorouter / ACA ingress / proxies from idling out
                    continue
                if event is None:
                    break
                yield sse(event["type"], event["data"])
        finally:
            if not task.done():          # client disconnected or aborted
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
            runs.remove(thread_id, task)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


@app.post("/api/chat/{thread_id}/cancel")
async def cancel(thread_id: str, request: Request):
    return {"cancelled": request.app.state.runs.cancel(thread_id)}


@app.delete("/api/threads/{thread_id}")
async def delete_thread(thread_id: str, request: Request):
    _check_thread(thread_id)
    request.app.state.runs.cancel(thread_id)
    with suppress(Exception):
        await get_checkpointer().adelete_thread(thread_id)
    SCRATCHPAD.clear(thread_id)
    await request.app.state.manager.release_thread(thread_id)
    return {"deleted": thread_id}


# ---- Generated files ----
@app.get("/api/threads/{thread_id}/files")
async def thread_files(thread_id: str):
    _check_thread(thread_id)
    return [{**f, "url": f"/api/files/{thread_id}/{quote(f['name'])}"} for f in list_files(thread_id)]


@app.get("/api/files/{thread_id}/{filename}")
async def download(thread_id: str, filename: str):
    _check_thread(thread_id)
    base = files_dir(thread_id).resolve()
    path = (base / filename).resolve()
    if path.parent != base or not path.is_file():   # blocks path traversal
        raise HTTPException(404, "file not found")
    return FileResponse(path, filename=path.name,
                        media_type=MIME.get(path.suffix.lstrip("."), "application/octet-stream"))


# ---- Serve the built React app (single deployable unit) ----
_dist = settings.path(settings.frontend_dist)
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="ui")
    log.info("Serving UI from %s", _dist)
