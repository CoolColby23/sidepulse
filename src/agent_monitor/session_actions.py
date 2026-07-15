from __future__ import annotations

import shlex
from pathlib import Path
from urllib.parse import quote

from .models import AgentStatus


def session_deep_link(status: AgentStatus) -> str | None:
    provider = status.provider.lower()
    session_id = status.session_id

    if provider == "codex" and session_id:
        return f"codex://threads/{quote(session_id, safe='')}"
    if provider == "claude":
        return "claude://"
    return None


def session_resume_command(status: AgentStatus) -> str | None:
    if not status.session_id:
        return None

    provider = status.provider.lower()
    cwd = shlex.quote(status.cwd or str(Path.home()))
    session_id = shlex.quote(status.session_id)

    if provider == "codex":
        return f"cd {cwd} && codex resume {session_id}"
    if provider == "claude":
        return f"cd {cwd} && claude --resume {session_id}"
    return None
