"""Central configuration. Every knob is overridable via environment variables or backend/.env."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from dotenv import load_dotenv

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR / ".env")


def _split(value: str, sep: str = ",") -> list[str]:
    return [v.strip() for v in value.split(sep) if v.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---------- Server ----------
    host: str = "0.0.0.0"
    port: int = 8000                      # Cloud Foundry / ACA inject PORT
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:5173"
    frontend_dist: str = "../frontend/dist"  # served as static files if it exists

    # ---------- LLM (SAP AI Core via generative AI hub SDK) ----------
    # Credentials come from AICORE_* env vars, ~/.aicore/config.json, or VCAP_SERVICES (BTP binding)
    llm_model: str = "gpt-4.1"
    llm_deployment_id: str = ""           # optional: pin a specific AI Core deployment
    llm_temperature: float = 0.0
    llm_max_tokens: int = 8192            # document specs can be large
    llm_parallel_tool_calls: bool = False  # browser actions must be sequential

    # ---------- Agent ----------
    agent_max_steps: int = 60             # max model<->tool round trips per user turn
    tool_timeout_s: float = 120.0
    snapshot_keep_last: int = 1           # older page snapshots are trimmed before each model call
    snapshot_trim_chars: int = 600        # chars kept from a trimmed (old) tool result
    tool_result_max_chars: int = 40000    # hard cap for the latest tool result sent to the LLM
    auto_save_chars: int = 12000          # tool results above this are saved to the scratchpad in full
    loop_guard_repeats: int = 3           # identical consecutive tool calls before the agent is stopped/redirected
    working_memory_in_prompt: bool = True  # inject plan + scratchpad index + files into every model call
    ui_tool_output_chars: int = 2000      # tool output preview streamed to the UI

    # ---------- Custom tools ----------
    enable_document_tools: bool = True
    enable_web_search: bool = True

    # ---------- Playwright MCP ----------
    mcp_config_path: str = "mcp_servers.json"   # same format as Claude/VS Code mcpServers config
    mcp_server_name: str = "playwright"
    browser_isolation: Literal["shared", "per_thread"] = "shared"
    browser_idle_timeout_s: int = 900     # per_thread mode: close idle browsers
    browser_prewarm: bool = True          # shared mode: start browser at app startup
    pw_headless: bool = True
    pw_browser: str = ""                  # chrome | msedge | chromium | firefox | webkit ("" = MCP default)
    pw_isolated: bool = False             # in-memory profile (always true in per_thread mode)
    pw_viewport: str = "1366x768"
    pw_no_sandbox: bool = False           # set true in containers
    pw_tools_exclude: str = "browser_take_screenshot,browser_install,browser_resize"
    pw_secrets_file: str = ""             # optional dotenv file; values are redacted from tool output
    pw_extra_args: str = ""               # passed verbatim to the MCP server CLI
    allowed_origins: str = ""             # comma separated; empty = allow all

    # ---------- Artifacts ----------
    artifacts_dir: str = "artifacts"
    screenshot_mode: Literal["off", "after_action"] = "after_action"

    # ---------- Network ----------
    disable_ssl_verify: bool = True       # corporate SSL inspection: relax TLS for npx + browser

    # ---------- Memory ----------
    checkpointer: Literal["memory"] = "memory"   # "redis" planned

    # ---------- Derived helpers ----------
    def path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (BACKEND_DIR / p).resolve()

    @property
    def cors_origin_list(self) -> list[str]:
        return _split(self.cors_origins)

    @property
    def allowed_origin_list(self) -> list[str]:
        return _split(self.allowed_origins)

    @property
    def excluded_tools(self) -> set[str]:
        return set(_split(self.pw_tools_exclude))

    @property
    def screenshots_dir(self) -> Path:
        return self.path(self.artifacts_dir) / "screenshots"


@lru_cache
def get_settings() -> Settings:
    return Settings()
