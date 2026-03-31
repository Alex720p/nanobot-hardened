"""Daily runtime log helpers."""

import json
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from nanobot.config.paths import get_logs_dir

_WRITE_LOCK = threading.Lock()

def _serialize(value: Any) -> Any:
    """Convert runtime objects into JSON-serializable values."""
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _log_path(now: datetime) -> Path:
    """Resolve the daily log file path."""
    logs_dir = get_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / f"{now.strftime('%d-%m-%Y')}.log"


def write_runtime_log(event: str, **payload: Any) -> None:
    """Append one structured runtime event to the current daily log file."""
    try:
        now = datetime.now().astimezone()
        path = _log_path(now)
        prefix = f"[{now.strftime('%Z %z %H:%M:%S')}] "
        body = json.dumps({"event": event, **payload}, ensure_ascii=False, default=_serialize)
        with _WRITE_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(prefix + body + "\n")
    except Exception:
        return
