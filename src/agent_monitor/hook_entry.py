"""Minimal, dependency-free process entry point for Codex and Claude hooks."""

import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path


provider, log_path = sys.argv[sys.argv.index("--provider") + 1], Path(sys.argv[sys.argv.index("--log") + 1]).expanduser()
try:
    event = json.loads(sys.stdin.read() or "{}")
except json.JSONDecodeError:
    event = {"hook_event_name": "ParseError"}
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
line = {"logged_at": now, "event": event} if provider == "codex" else dict(event, logged_at=event.get("logged_at", now)) if isinstance(event, dict) else {"logged_at": now, "event": event}
log_path.parent.mkdir(parents=True, exist_ok=True)
with log_path.open("a", encoding="utf-8") as output:
    output.write(json.dumps(line, separators=(",", ":"), ensure_ascii=False) + "\n")
state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "sidepulse/agent-monitor/events.sock"
try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(0.05)
        client.connect(str(state))
        client.sendall(json.dumps({"provider": provider, "line": line}, separators=(",", ":"), ensure_ascii=False).encode())
except OSError:
    pass
