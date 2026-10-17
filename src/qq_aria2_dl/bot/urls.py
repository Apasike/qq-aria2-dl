from __future__ import annotations

import os
import re
from urllib.parse import urlsplit, urlunsplit

_URL_RE = re.compile(r"https?://[^\s<>]+|magnet:\?xt=urn:btih:[^\s<>]+", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?'\"()[]{}<>，。；：！？）】》>"

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def extract_urls(text: str, allowed_schemes: list[str], max_urls: int = 3) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in _URL_RE.findall(text):
        url = normalize_url(raw, allowed_schemes)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
        if len(out) >= max_urls:
            break
    return out


def normalize_url(raw: str, allowed_schemes: list[str], max_length: int = 8192) -> str | None:
    if not raw:
        return None
    raw = raw.strip().strip("<>").strip()
    while raw and raw[-1] in _TRAILING_PUNCT:
        raw = raw[:-1]
    if not raw or len(raw) > max_length:
        return None

    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in allowed_schemes:
        return None

    if scheme == "magnet":
        return urlunsplit(("magnet", "", "", parts.query, ""))

    host = (parts.hostname or "").lower()
    if not host:
        return None
    netloc = host
    port = parts.port
    if port is not None:
        default = 443 if scheme == "https" else 80
        if port != default:
            netloc = f"{host}:{port}"
    # Drop fragment and userinfo; keep query untouched to avoid breaking signed URLs.
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def sanitize_filename(name: str, max_len: int = 200) -> str | None:
    if not name:
        return None
    name = str(name).strip().replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f")
    name = name.strip().strip(".")
    if not name or name in (".", ".."):
        return None
    for ch in '<>:"|?*':
        name = name.replace(ch, "_")

    if len(name) > max_len:
        stem, ext = os.path.splitext(name)
        keep = max_len - len(ext)
        if keep < 1:
            keep = 1
        name = stem[:keep] + ext
    return name


def format_bytes(n: int | float | None) -> str:
    if n is None or n < 0:
        return "未知"
    if n == 0:
        return "0 B"
    f = float(n)
    i = 0
    while f >= 1024 and i < len(_UNITS) - 1:
        f /= 1024
        i += 1
    return f"{f:.2f} {_UNITS[i]}"


def format_speed(n: int | float | None) -> str:
    if not n:
        return "0 B/s"
    return f"{format_bytes(n)}/s"


def format_duration(sec: int | float | None) -> str:
    if sec is None or sec < 0:
        return "未知"
    sec = int(sec)
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分 {sec % 60} 秒"
    return f"{sec // 3600} 小时 {(sec % 3600) // 60} 分钟"


def progress_bar(percent: float, width: int = 20) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(round(width * percent / 100))
    filled = max(0, min(width, filled))
    return "[" + "█" * filled + "░" * (width - filled) + "]"