"""Per-conversation workspace: generated files on disk + an in-memory scratchpad.

The scratchpad is the agent's working memory for large data (extracted tables, page text,
search results, notes, plans). Big data is stored here ONCE and referenced by key
(`data_ref`) from the document tools, so it never has to be re-sent through the LLM.

Storage is in-process today; ScratchpadStore has a tiny interface so a Redis-backed
implementation can be swapped in later.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig

from ..config import get_settings

_SAFE = re.compile(r"[^A-Za-z0-9._ -]+")


# --------------------------------------------------------------------------- thread id
def thread_id_from(config: RunnableConfig | None) -> str:
    """Resolve the conversation id inside a tool (LangChain injects RunnableConfig)."""
    tid = ((config or {}).get("configurable") or {}).get("thread_id")
    if tid:
        return str(tid)
    from ..middleware import RUN_CONTEXT  # fallback when called outside the graph

    ctx = RUN_CONTEXT.get() or {}
    return str(ctx.get("thread_id") or "default")


# --------------------------------------------------------------------------- files
def files_dir(thread_id: str) -> Path:
    s = get_settings()
    d = s.path(s.artifacts_dir) / "files" / _SAFE.sub("_", thread_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_filename(name: str, ext: str) -> str:
    stem = Path(name or "output").stem
    stem = _SAFE.sub("_", stem).strip(" ._") or "output"
    return f"{stem[:80]}.{ext}"


def unique_path(directory: Path, filename: str) -> Path:
    path = directory / filename
    n = 2
    while path.exists():
        path = directory / f"{Path(filename).stem}-{n}{Path(filename).suffix}"
        n += 1
    return path


def list_files(thread_id: str) -> list[dict[str, Any]]:
    d = files_dir(thread_id)
    return [
        {"name": p.name, "size": p.stat().st_size, "modified": p.stat().st_mtime}
        for p in sorted(d.iterdir(), key=lambda p: p.stat().st_mtime)
        if p.is_file()
    ]


def screenshots_for(thread_id: str) -> list[Path]:
    d = get_settings().screenshots_dir
    if not d.exists():
        return []
    prefix = thread_id[:12]
    return sorted(d.glob(f"{prefix}-*.png"), key=lambda p: p.stat().st_mtime)


def resolve_image(thread_id: str, ref: str) -> Path | None:
    """Image refs: 'latest' / 'screenshot:latest' / 'screenshot:-2' / a screenshot or file name."""
    ref = (ref or "").strip()
    shots = screenshots_for(thread_id)
    if ref.startswith("screenshot:"):
        ref = ref.split(":", 1)[1] or "latest"
    if ref in ("latest", "last"):
        return shots[-1] if shots else None
    if re.fullmatch(r"-?\d+", ref):
        try:
            return shots[int(ref)]
        except IndexError:
            return None
    s = get_settings()
    name = Path(ref).name  # never allow paths outside the artifacts folder
    for candidate in (s.screenshots_dir / name, files_dir(thread_id) / name):
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------- scratchpad
@dataclass
class Entry:
    content: str
    kind: str = "note"            # note | data
    updated: float = field(default_factory=time.time)

    def describe(self, key: str) -> str:
        extra = ""
        if self.kind == "data":
            data = parse_json(self.content)
            if isinstance(data, list):
                extra = f", {len(data)} rows"
            elif isinstance(data, dict) and isinstance(data.get("rows"), list):
                extra = f", {len(data['rows'])} rows"
        return f"{key} ({self.kind}, {len(self.content):,} chars{extra})"


class ScratchpadStore:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, Entry]] = {}
        self._lock = threading.Lock()

    def _pad(self, thread_id: str) -> dict[str, Entry]:
        return self._data.setdefault(thread_id, {})

    def write(self, thread_id: str, key: str, content: str, append: bool = False) -> Entry:
        key = key.strip()
        with self._lock:
            pad = self._pad(thread_id)
            if append and key in pad:
                content = pad[key].content + ("\n" if not pad[key].content.endswith("\n") else "") + content
            kind = "data" if isinstance(parse_json(content), (list, dict)) else "note"
            pad[key] = Entry(content=content, kind=kind)
            return pad[key]

    def get(self, thread_id: str, key: str) -> Entry | None:
        return self._pad(thread_id).get(key.strip())

    def delete(self, thread_id: str, key: str) -> bool:
        with self._lock:
            return self._pad(thread_id).pop(key.strip(), None) is not None

    def keys(self, thread_id: str) -> list[str]:
        return list(self._pad(thread_id).keys())

    def index(self, thread_id: str) -> list[str]:
        return [e.describe(k) for k, e in self._pad(thread_id).items()]

    def clear(self, thread_id: str) -> None:
        with self._lock:
            self._data.pop(thread_id, None)


SCRATCHPAD = ScratchpadStore()


def parse_json(text: str) -> Any:
    text = (text or "").strip()
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def load_data_ref(thread_id: str, key: str) -> Any:
    """Return parsed JSON stored under a scratchpad key (raises a helpful error otherwise)."""
    entry = SCRATCHPAD.get(thread_id, key)
    if entry is None:
        keys = ", ".join(SCRATCHPAD.keys(thread_id)) or "none"
        raise ValueError(f"data_ref '{key}' not found in scratchpad. Available keys: {keys}")
    data = parse_json(entry.content)
    if data is None:
        raise ValueError(f"data_ref '{key}' does not contain JSON data (it is a text note).")
    return data
