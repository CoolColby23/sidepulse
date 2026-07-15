from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .providers import default_state_dir

LAUNCH_AGENT_LABEL = "com.sidepulse.agentstatus"
LAUNCH_AGENT_FILENAME = f"{LAUNCH_AGENT_LABEL}.plist"


@dataclass(frozen=True)
class LaunchAgentResult:
    label: str
    plist_path: Path
    changed: bool
    started: bool = False
    stopped: bool = False


def launch_agent_path(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / "Library" / "LaunchAgents" / LAUNCH_AGENT_FILENAME


def build_launch_agent_plist(
    python_executable: Path | str | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> dict[str, Any]:
    executable = str(python_executable or sys.executable or "python3")
    state_dir = default_state_dir()
    stdout = stdout_path or state_dir / "status-bar.out.log"
    stderr = stderr_path or state_dir / "status-bar.err.log"

    plist: dict[str, Any] = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            executable,
            "-m",
            "agent_monitor",
            "status-bar",
            "--foreground",
        ],
        "RunAtLoad": True,
        "StandardOutPath": str(stdout),
        "StandardErrorPath": str(stderr),
        "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
            "PATH": launch_agent_path_env(executable),
        },
    }
    return plist


def install_launch_agent(
    *,
    start: bool = True,
    plist_path: Path | None = None,
    python_executable: Path | str | None = None,
) -> LaunchAgentResult:
    target = plist_path or launch_agent_path()
    plist = build_launch_agent_plist(python_executable=python_executable)
    data = plistlib.dumps(plist, sort_keys=False)
    existing = target.read_bytes() if target.exists() else None
    changed = existing != data

    target.parent.mkdir(parents=True, exist_ok=True)
    default_state_dir().mkdir(parents=True, exist_ok=True)
    if changed:
        target.write_bytes(data)

    started = False
    if start:
        restart_launch_agent(target)
        started = True

    return LaunchAgentResult(
        label=LAUNCH_AGENT_LABEL,
        plist_path=target,
        changed=changed,
        started=started,
    )


def uninstall_launch_agent(plist_path: Path | None = None) -> LaunchAgentResult:
    target = plist_path or launch_agent_path()
    bootout_launch_agent(target)
    changed = target.exists()
    if target.exists():
        target.unlink()
    return LaunchAgentResult(
        label=LAUNCH_AGENT_LABEL,
        plist_path=target,
        changed=changed,
        stopped=True,
    )


def restart_launch_agent(plist_path: Path) -> None:
    bootout_launch_agent(plist_path)
    subprocess.run(
        ["launchctl", "bootstrap", launch_domain(), str(plist_path)],
        check=True,
    )
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"{launch_domain()}/{LAUNCH_AGENT_LABEL}"],
        check=False,
    )


def bootout_launch_agent(plist_path: Path) -> None:
    subprocess.run(
        ["launchctl", "bootout", launch_domain(), str(plist_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def launch_domain() -> str:
    return f"gui/{os.getuid()}"


def launch_agent_path_env(python_executable: str) -> str:
    candidates = [
        Path.home() / ".local" / "bin",
        executable_parent(python_executable),
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
        Path("/usr/sbin"),
        Path("/sbin"),
        Path("/opt/anaconda3/bin"),
    ]
    seen: set[str] = set()
    result: list[str] = []
    for candidate in candidates:
        if candidate is None:
            continue
        text = str(candidate)
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return ":".join(result)


def executable_parent(python_executable: str) -> Path | None:
    path = Path(python_executable)
    if not path.is_absolute():
        return None
    return path.parent
