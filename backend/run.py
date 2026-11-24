"""Entry point for local, Docker, BTP and ACA.

Uses `python run.py` instead of `uvicorn --reload` because on Windows uvicorn's reload/workers
mode switches to the Selector event loop, which cannot spawn the Playwright MCP subprocess.
"""
import asyncio
import os
import sys

import uvicorn

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

if __name__ == "__main__":
    from app.config import get_settings

    s = get_settings()
    uvicorn.run(
        "app.main:app",
        host=s.host,
        port=int(os.getenv("PORT", s.port)),
        log_level=s.log_level.lower(),
        reload=False,
        timeout_graceful_shutdown=10,
    )
