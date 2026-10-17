from __future__ import annotations

from typing import Any


def extract_text(message: Any) -> str:
    """Extract plain text from a OneBot v11 message (string or segment array)."""
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts: list[str] = []
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "text":
                parts.append(str(seg.get("data", {}).get("text", "")))
        return "".join(parts)
    return ""


def extract_at_ids(message: Any) -> list[str]:
    if not isinstance(message, list):
        return []
    ids: list[str] = []
    for seg in message:
        if isinstance(seg, dict) and seg.get("type") == "at":
            ids.append(str(seg.get("data", {}).get("qq", "")))
    return ids


def is_at_self(message: Any, self_id: int | str) -> bool:
    return str(self_id) in extract_at_ids(message)


def text_segments(text: str, reply_message_id: int | str | None = None) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    if reply_message_id is not None:
        segments.append({"type": "reply", "data": {"id": reply_message_id}})
    segments.append({"type": "text", "data": {"text": text}})
    return segments