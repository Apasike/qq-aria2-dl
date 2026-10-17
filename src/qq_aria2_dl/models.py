from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ProgressSnapshot:
    gid: str
    aria2_status: str
    total_length: int = 0
    completed_length: int = 0
    download_speed: int = 0
    connections: int = 0
    files: list[dict[str, Any]] = field(default_factory=list)
    error_message: str = ""
    fetched_at: float = 0.0
    file_name: str = ""

    @classmethod
    def from_aria2(cls, result: dict[str, Any]) -> "ProgressSnapshot":
        files = result.get("files") or []
        file_name = ""
        if files:
            first = files[0] or {}
            path = str(first.get("path") or "")
            file_name = Path(path).name
        return cls(
            gid=str(result.get("gid") or ""),
            aria2_status=str(result.get("status") or ""),
            total_length=int(result.get("totalLength") or 0),
            completed_length=int(result.get("completedLength") or 0),
            download_speed=int(result.get("downloadSpeed") or 0),
            connections=int(result.get("connections") or 0),
            files=files,
            error_message=str(result.get("errorMessage") or ""),
            fetched_at=time.monotonic(),
            file_name=file_name,
        )