"""Agent + checkpointer factories (LangChain v1 create_agent)."""
from __future__ import annotations

import logging
from functools import lru_cache

from langchain.agents import create_agent
from langgraph.checkpoint.memory import InMemorySaver

from .config import Settings, get_settings
from .llm import get_llm
from .mcp_session import BrowserSession
from .middleware import BrowserToolMiddleware, ContextBudgetMiddleware
from .prompts import build_system_prompt

log = logging.getLogger(__name__)


@lru_cache
def get_checkpointer():
    s = get_settings()
    if s.checkpointer == "memory":
        return InMemorySaver()
    # Future: from langgraph.checkpoint.redis.aio import AsyncRedisSaver
    raise ValueError(f"Unsupported checkpointer: {s.checkpointer}")


def _secret_keys(s: Settings) -> list[str]:
    if not s.pw_secrets_file:
        return []
    path = s.path(s.pw_secrets_file)
    if not path.exists():
        return []
    keys = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.append(line.split("=", 1)[0].strip())
    return keys


def build_tools(session: BrowserSession) -> list:
    s = get_settings()
    from .tools.document_tools import document_tools
    from .tools.reasoning_tools import reasoning_tools
    from .tools.research_tools import research_tools
    from .tools.scratchpad_tools import scratchpad_tools

    tools = [t for name, t in session.tools.items() if name not in s.excluded_tools]
    tools += reasoning_tools() + scratchpad_tools()
    research = research_tools(session)
    if not s.enable_web_search:
        research = [t for t in research if t.name != "web_search"]
    tools += research
    if s.enable_document_tools:
        tools += document_tools()
    return tools


def build_agent(session: BrowserSession, model=None):
    s = get_settings()
    tools = build_tools(session)
    log.info("Agent tools (%d): %s", len(tools), ", ".join(sorted(t.name for t in tools)))
    return create_agent(
        model=model or get_llm(),
        tools=tools,
        system_prompt=build_system_prompt(s, _secret_keys(s)),
        middleware=[
            ContextBudgetMiddleware(s),
            BrowserToolMiddleware(s, session.tools.get("browser_take_screenshot")),
        ],
        checkpointer=get_checkpointer(),
        name="web_agent",
    )
