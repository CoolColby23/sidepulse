from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import HookEvent, parse_datetime

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility.
    tomllib = None

CODEX_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "Stop",
)

CLAUDE_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "Notification",
    "PreCompact",
    "PostCompact",
    "SubagentStop",
    "Stop",
    "SessionEnd",
)


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    config_path: Path
    exists: bool
    hooks_enabled: bool
    hook_events: tuple[str, ...]
    log_paths: tuple[Path, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "config_path": str(self.config_path),
            "exists": self.exists,
            "hooks_enabled": self.hooks_enabled,
            "hook_events": list(self.hook_events),
            "log_paths": [str(path) for path in self.log_paths],
        }


def default_state_dir(home: Path | None = None) -> Path:
    if home is None:
        xdg_state_home = os.environ.get("XDG_STATE_HOME")
        if xdg_state_home:
            return Path(xdg_state_home).expanduser() / "sidepulse" / "agent-monitor"

    base = home or Path.home()
    return base / ".local" / "state" / "sidepulse" / "agent-monitor"


def default_log_path(provider: str, home: Path | None = None) -> Path:
    suffix = "jsonl"
    return default_state_dir(home) / f"{provider}.{suffix}"


def detect_provider_configs(home: Path | None = None) -> list[ProviderConfig]:
    return [detect_codex_config(home), detect_claude_config(home)]


def detect_codex_config(home: Path | None = None) -> ProviderConfig:
    base = home or Path.home()
    config_path = base / ".codex" / "config.toml"
    if not config_path.exists():
        return ProviderConfig("codex", config_path, False, False, (), ())

    text = config_path.read_text()
    try:
        if tomllib is None:
            raise RuntimeError("tomllib unavailable")
        data = tomllib.loads(text)
    except Exception:
        return detect_codex_config_from_text(config_path, text)

    features = data.get("features") or {}
    hooks = data.get("hooks") or {}
    hook_events: list[str] = []
    paths: list[Path] = []

    if isinstance(hooks, dict):
        for event_name, entries in hooks.items():
            if event_name not in CODEX_EVENTS or not isinstance(entries, list):
                continue
            hook_events.append(event_name)
            paths.extend(_paths_from_hook_entries(entries))

    return ProviderConfig(
        "codex",
        config_path,
        True,
        bool(features.get("hooks")),
        tuple(sorted(set(hook_events))),
        _dedupe_paths(paths),
    )


def detect_codex_config_from_text(config_path: Path, text: str) -> ProviderConfig:
    hook_events = tuple(
        sorted(
            {
                match.group(1)
                for match in re.finditer(r"^\s*\[\[hooks\.([A-Za-z0-9_]+)\]\]\s*$", text, re.MULTILINE)
                if match.group(1) in CODEX_EVENTS
            }
        )
    )

    paths: list[Path] = []
    for match in re.finditer(r"command\s*=\s*'''(.*?)'''", text, re.DOTALL):
        paths.extend(extract_log_paths_from_command(match.group(1)))
    for match in re.finditer(r'command\s*=\s*"(.*?)"', text):
        paths.extend(extract_log_paths_from_command(match.group(1)))

    return ProviderConfig(
        "codex",
        config_path,
        True,
        codex_hooks_feature_enabled(text),
        hook_events,
        _dedupe_paths(paths),
    )


def codex_hooks_feature_enabled(text: str) -> bool:
    match = re.search(r"^\s*\[features\]\s*$(.*?)(?=^\s*\[|\Z)", text, re.MULTILINE | re.DOTALL)
    if not match:
        return False
    return bool(re.search(r"^\s*hooks\s*=\s*true\s*$", match.group(1), re.MULTILINE))


def detect_claude_config(home: Path | None = None) -> ProviderConfig:
    base = home or Path.home()
    config_path = base / ".claude" / "settings.json"
    if not config_path.exists():
        return ProviderConfig("claude", config_path, False, False, (), ())

    try:
        data = json.loads(config_path.read_text())
    except Exception:
        return ProviderConfig("claude", config_path, True, False, (), ())

    hooks = data.get("hooks") or {}
    hook_events: list[str] = []
    paths: list[Path] = []

    if isinstance(hooks, dict):
        for event_name, entries in hooks.items():
            if event_name not in CLAUDE_EVENTS or not isinstance(entries, list):
                continue
            hook_events.append(event_name)
            paths.extend(_paths_from_hook_entries(entries))

    return ProviderConfig(
        "claude",
        config_path,
        True,
        bool(hook_events),
        tuple(sorted(set(hook_events))),
        _dedupe_paths(paths),
    )


def detect_log_path(provider: str, home: Path | None = None) -> Path:
    config = detect_codex_config(home) if provider == "codex" else detect_claude_config(home)
    if config.log_paths:
        return config.log_paths[0]
    return default_log_path(provider, home)


def parse_log_line(provider: str, line: str) -> HookEvent | None:
    line = line.strip()
    if not line:
        return None

    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None

    if not isinstance(obj, dict):
        return None

    if provider == "codex" and isinstance(obj.get("event"), dict):
        raw = obj["event"]
        logged_at = obj.get("logged_at") or raw.get("logged_at")
    else:
        raw = obj
        logged_at = raw.get("logged_at")

    event_name = raw.get("hook_event_name")
    if not event_name:
        return None

    return HookEvent(
        provider=provider,
        logged_at=parse_datetime(logged_at),
        event_name=str(event_name),
        raw=raw,
        session_id=_string_or_none(raw.get("session_id")),
        turn_id=_string_or_none(raw.get("turn_id")),
        agent_id=_string_or_none(raw.get("agent_id")),
        cwd=_string_or_none(raw.get("cwd")),
        tool_name=_string_or_none(raw.get("tool_name")),
        message=_string_or_none(raw.get("message") or raw.get("last_assistant_message")),
    )


def _paths_from_hook_entries(entries: list[Any]) -> list[Path]:
    paths: list[Path] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks") or []:
            if not isinstance(hook, dict):
                continue
            command = hook.get("command")
            if isinstance(command, str):
                paths.extend(extract_log_paths_from_command(command))
    return paths


def extract_log_paths_from_command(command: str) -> list[Path]:
    paths: list[Path] = []

    for match in re.finditer(r">>\s+(['\"]?)([^'\"\s]+)\1", command):
        paths.append(Path(match.group(2)).expanduser())

    try:
        parts = shlex.split(command)
    except ValueError:
        parts = []

    for index, part in enumerate(parts):
        if part == "--log" and index + 1 < len(parts):
            paths.append(Path(parts[index + 1]).expanduser())
        elif part.startswith("--log="):
            paths.append(Path(part.split("=", 1)[1]).expanduser())

    return _dedupe_paths(paths)


def _dedupe_paths(paths: list[Path]) -> tuple[Path, ...]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return tuple(result)


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
