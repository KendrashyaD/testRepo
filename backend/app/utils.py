"""Small helpers shared across modules."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

SENSITIVE = re.compile(r"pass(word|code)?|pwd|secret|token|otp|pin\b|credential", re.I)
MASK = "••••••"


def content_to_text(content: Any) -> str:
    """Flatten LangChain/MCP message content (str or list of blocks) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, tuple):  # (content, artifact)
        return content_to_text(content[0])
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, str):
                out.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    out.append(block.get("text", ""))
                elif block.get("type") in ("image", "image_url"):
                    out.append("[image omitted]")
            elif hasattr(block, "text"):
                out.append(str(block.text))
        return "\n".join(out)
    return str(content)


def truncate(text: str, limit: int, note: str = "") -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]{(' ' + note) if note else ''}"


def redact_args(value: Any) -> Any:
    """Mask values that look like secrets before streaming/logging tool arguments."""
    if isinstance(value, list):
        return [redact_args(v) for v in value]
    if not isinstance(value, dict):
        return value
    label = " ".join(str(value.get(k, "")) for k in ("element", "name", "label", "field"))
    label_sensitive = bool(SENSITIVE.search(label))
    out = {}
    for k, v in value.items():
        if SENSITIVE.search(k) or (label_sensitive and k in ("text", "value")):
            out[k] = MASK
        else:
            out[k] = redact_args(v)
    return out


def origin_allowed(url: str | None, allowed: list[str]) -> bool:
    """Empty allow-list = everything allowed. Entries may be origins, hosts or *.domain wildcards."""
    if not allowed or not url:
        return True
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if parsed.scheme in ("about", "data", "blob"):
        return True
    host = (parsed.hostname or "").lower()
    origin = f"{parsed.scheme}://{parsed.netloc}".lower()
    for entry in allowed:
        e = entry.lower().rstrip("/")
        if "://" in e:
            if origin == e:
                return True
        elif e.startswith("*."):
            if host == e[2:] or host.endswith(e[1:]):
                return True
        elif host == e:
            return True
    return False
