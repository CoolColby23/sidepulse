from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ipc import send_hook_event


def format_hook_payload(
    provider: str,
    payload_text: str,
    *,
    logged_at: str | None = None,
) -> dict[str, Any]:
    timestamp = logged_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        payload: Any = json.loads(payload_text or "{}")
    except json.JSONDecodeError as exc:
        payload = {
            "hook_event_name": "ParseError",
            "raw": payload_text,
            "parse_error": str(exc),
        }

    if provider == "codex":
        return {"logged_at": timestamp, "event": payload}
    if isinstance(payload, dict):
        line = dict(payload)
        line["logged_at"] = line.get("logged_at") or timestamp
        return line
    return {"logged_at": timestamp, "event": payload}


def write_hook_line(log_path: Path, line: dict[str, Any]) -> None:
    log_path = log_path.expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, separators=(",", ":"), ensure_ascii=False) + "\n")


def write_hook_payload(provider: str, log_path: Path, payload_text: str) -> None:
    line = format_hook_payload(provider, payload_text)
    write_hook_line(log_path, line)


def hook_log_main(provider: str, log_path: Path) -> int:
    try:
        line = format_hook_payload(provider, sys.stdin.read())
        send_hook_event(provider, line)
        write_hook_line(log_path, line)
    except Exception:
        return 0
    return 0
