"""Long-lived Playwright MCP sessions.

Why: MultiServerMCPClient.get_tools() opens a NEW MCP session (= new browser) per tool call,
which loses page state between steps. We keep one session open per "browser key" instead:
  - shared mode     -> key "shared" (one browser for all conversations)
  - per_thread mode -> key = thread_id (one isolated browser per conversation)

Each session lives inside its own asyncio task, because the stdio transport uses anyio cancel
scopes that must be entered and exited from the same task.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import time
from typing import Any, Callable

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from .config import Settings

log = logging.getLogger(__name__)

SHARED_KEY = "shared"


# --------------------------------------------------------------------------- config building
def _playwright_config(s: Settings, key: str) -> dict[str, Any]:
    """Playwright MCP JSON config (passed via --config). More stable than many CLI flags."""
    isolated = s.pw_isolated or s.browser_isolation == "per_thread"
    width, _, height = s.pw_viewport.partition("x")

    launch: dict[str, Any] = {"headless": s.pw_headless}
    browser: dict[str, Any] = {"isolated": isolated, "launchOptions": launch}

    name = s.pw_browser.strip().lower()
    if name in ("chrome", "msedge") or name.startswith(("chrome-", "msedge-")):
        browser["browserName"] = "chromium"
        launch["channel"] = name
    elif name in ("chromium", "firefox", "webkit"):
        browser["browserName"] = name

    if s.pw_no_sandbox:
        launch["chromiumSandbox"] = False
        launch["args"] = ["--disable-dev-shm-usage"]

    if not isolated:
        profile = s.path(s.artifacts_dir) / "browser-profile" / key
        profile.mkdir(parents=True, exist_ok=True)
        browser["userDataDir"] = str(profile)

    context: dict[str, Any] = {"ignoreHTTPSErrors": s.disable_ssl_verify, "acceptDownloads": True}
    if width.isdigit() and height.isdigit():
        context["viewport"] = {"width": int(width), "height": int(height)}
    browser["contextOptions"] = context

    cfg: dict[str, Any] = {
        "browser": browser,
        "imageResponses": "omit",          # screenshots go to disk, never into the LLM context
        "outputDir": str(s.screenshots_dir),
    }
    if s.allowed_origin_list:
        cfg["network"] = {"allowedOrigins": s.allowed_origin_list}
    return cfg


def build_connection(s: Settings, key: str) -> dict[str, Any]:
    """Turn the user's mcpServers JSON entry into a langchain-mcp-adapters stdio connection."""
    with open(s.path(s.mcp_config_path), encoding="utf-8") as f:
        server = json.load(f)["mcpServers"][s.mcp_server_name]

    command = server["command"]
    args = list(server.get("args", []))
    if os.path.basename(command).lower().startswith("npx") and "-y" not in args and "--yes" not in args:
        args.insert(0, "-y")                     # never block on the "install package?" prompt
    command = shutil.which(command) or command   # resolves npx.cmd on Windows

    s.screenshots_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir = s.path(s.artifacts_dir) / ".mcp"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / f"playwright-{key}.json"
    cfg_path.write_text(json.dumps(_playwright_config(s, key), indent=2), encoding="utf-8")

    args += ["--config", str(cfg_path)]
    if s.pw_headless:
        args.append("--headless")
    if s.pw_secrets_file:
        args += ["--secrets", str(s.path(s.pw_secrets_file))]
    if s.pw_extra_args:
        args += shlex.split(s.pw_extra_args)

    env = {**os.environ, **server.get("env", {})}
    if s.disable_ssl_verify:
        env.setdefault("NODE_TLS_REJECT_UNAUTHORIZED", "0")
        env.setdefault("NPM_CONFIG_STRICT_SSL", "false")

    return {"transport": "stdio", "command": command, "args": args, "env": env}


# --------------------------------------------------------------------------- session
class BrowserSession:
    def __init__(self, key: str, connection: dict[str, Any], server_name: str):
        self.key = key
        self._client = MultiServerMCPClient({server_name: connection})
        self._server_name = server_name
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._error: BaseException | None = None
        self.tools: dict[str, BaseTool] = {}
        self.agent: Any = None
        self.lock = asyncio.Lock()          # one agent run at a time per browser
        self.last_used = time.monotonic()

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    def touch(self) -> None:
        self.last_used = time.monotonic()

    async def start(self, timeout: float = 180) -> None:
        self._task = asyncio.create_task(self._run(), name=f"mcp-session-{self.key}")
        await asyncio.wait_for(self._ready.wait(), timeout)
        if self._error:
            raise RuntimeError(f"Playwright MCP failed to start: {self._error}") from self._error

    async def _run(self) -> None:
        try:
            async with self._client.session(self._server_name) as session:
                tools = await load_mcp_tools(session)
                self.tools = {t.name: t for t in tools}
                log.info("MCP session [%s] ready with %d tools", self.key, len(tools))
                self._ready.set()
                await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001 - surface any startup failure
            if not self._ready.is_set():
                self._error = exc
            log.warning("MCP session [%s] ended: %r", self.key, exc)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self._ready.set()

    async def close(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, 15)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._task.cancel()
        log.info("MCP session [%s] closed", self.key)


# --------------------------------------------------------------------------- manager
class SessionManager:
    """Creates, caches, reaps browser sessions. Switch modes with BROWSER_ISOLATION."""

    def __init__(self, settings: Settings, agent_factory: Callable[[BrowserSession], Any],
                 connection_factory: Callable[[Settings, str], dict] = build_connection):
        self.s = settings
        self._agent_factory = agent_factory
        self._connection_factory = connection_factory
        self._sessions: dict[str, BrowserSession] = {}
        self._create_lock = asyncio.Lock()

    def key_for(self, thread_id: str) -> str:
        return SHARED_KEY if self.s.browser_isolation == "shared" else thread_id

    async def get(self, thread_id: str) -> BrowserSession:
        key = self.key_for(thread_id)
        async with self._create_lock:
            sess = self._sessions.get(key)
            if sess and not sess.alive:
                log.info("MCP session [%s] is dead, recreating", key)
                await sess.close()
                sess = None
            if sess is None:
                sess = BrowserSession(key, self._connection_factory(self.s, key), self.s.mcp_server_name)
                await sess.start()
                sess.agent = self._agent_factory(sess)
                self._sessions[key] = sess
            sess.touch()
            return sess

    async def release_thread(self, thread_id: str) -> None:
        """Close the browser of a conversation (no-op in shared mode)."""
        if self.s.browser_isolation == "per_thread":
            sess = self._sessions.pop(thread_id, None)
            if sess:
                await sess.close()

    async def reap_idle_forever(self) -> None:
        while True:
            await asyncio.sleep(60)
            if self.s.browser_isolation != "per_thread":
                continue
            now = time.monotonic()
            for key, sess in list(self._sessions.items()):
                if not sess.lock.locked() and now - sess.last_used > self.s.browser_idle_timeout_s:
                    self._sessions.pop(key, None)
                    await sess.close()

    async def close_all(self) -> None:
        sessions, self._sessions = list(self._sessions.values()), {}
        await asyncio.gather(*(s.close() for s in sessions), return_exceptions=True)

    def stats(self) -> dict[str, Any]:
        return {"mode": self.s.browser_isolation, "open_browsers": len(self._sessions)}
