from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .device_writer import (
    DEFAULT_FILE_NAME,
    DeviceWriteError,
    resolve_target_path,
    write_led_program,
)
from .models import AgentMode


class LedDisplayState(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    DONE = "done"
    ASK = "ask"


LED_STATE_LABELS: dict[LedDisplayState, str] = {
    LedDisplayState.IDLE: "Idle",
    LedDisplayState.WORKING: "Working",
    LedDisplayState.DONE: "Done",
    LedDisplayState.ASK: "Ask",
}


ASK_AMBER = "#FF3A00"
WORKING_CYAN = "#00E5FF"
DONE_GREEN = "#00FF66"
IDLE_DIM = "#020204"
DEVICE_LED_COUNTS = {
    "pulsedot": 2,
    "pixiedot": 2,
    "sidepulse": 8,
    "pixiepulse": 8,
}


@dataclass(frozen=True)
class LedStatusWrite:
    state: LedDisplayState
    target: Path | None
    program: str
    changed: bool
    error: str | None = None

    @property
    def label(self) -> str:
        return LED_STATE_LABELS[self.state]


def display_state_for_mode(mode: AgentMode) -> LedDisplayState:
    if mode in {AgentMode.WAITING_FOR_INPUT, AgentMode.BLOCKED_ERROR}:
        return LedDisplayState.ASK
    if mode in {
        AgentMode.WORKING,
        AgentMode.TOOL_RUNNING,
        AgentMode.LONG_TASK_PROGRESS,
    }:
        return LedDisplayState.WORKING
    if mode == AgentMode.COMPLETED:
        return LedDisplayState.DONE
    return LedDisplayState.IDLE


def program_for_display_state(
    state: LedDisplayState,
    *,
    led_count: int = 8,
) -> str:
    if state == LedDisplayState.IDLE:
        return "\n".join(
            [
                "off",
                f"{IDLE_DIM} 6s pulse",
                "repeat",
            ]
        )
    if state == LedDisplayState.ASK:
        return "\n".join(
            [
                "off",
                f"{ASK_AMBER} 1.6s pulse",
                "repeat",
            ]
        )
    if state == LedDisplayState.DONE:
        return DONE_GREEN
    if state == LedDisplayState.WORKING:
        return rolling_program(WORKING_CYAN, led_count=led_count)
    raise ValueError(f"Unknown LED display state: {state}")


def rolling_program(color: str, *, led_count: int = 8) -> str:
    count = max(2, min(8, int(led_count)))
    delay_ms = 260 if count == 2 else 95
    duration_ms = 760
    segments: list[str] = []
    for active_index in range(count):
        delay = active_index * delay_ms
        segments.append(f"{active_index}:{color} {duration_ms}ms pulse {delay}ms")
    return "\n".join(
        [
            "off 160ms cosine",
            "; ".join(segments),
            "repeat",
        ]
    )


def write_mode_to_leds(
    mode: AgentMode,
    *,
    device_path: Path | None = None,
    file_name: str = DEFAULT_FILE_NAME,
    dry_run: bool = False,
) -> LedStatusWrite:
    target = resolve_target_path(device_path=device_path, file_name=file_name)
    state = display_state_for_mode(mode)
    program = program_for_display_state(
        state,
        led_count=led_count_for_target(target),
    )
    written_target = write_led_program(
        program,
        device_path=target,
        file_name=file_name,
        dry_run=dry_run,
    )
    return LedStatusWrite(
        state=state,
        target=written_target,
        program=program,
        changed=True,
    )


def led_count_for_target(target: Path) -> int:
    name = normalized_device_name(target.parent.name)
    for hint, led_count in DEVICE_LED_COUNTS.items():
        if hint in name:
            return led_count
    return 8


def normalized_device_name(name: str) -> str:
    return "".join(char for char in name.lower() if char.isalnum())


class AgentLedController:
    def __init__(
        self,
        *,
        device_path: Path | None = None,
        file_name: str = DEFAULT_FILE_NAME,
        dry_run: bool = False,
        error_retry_seconds: float = 10.0,
    ) -> None:
        self.device_path = device_path
        self.file_name = file_name
        self.dry_run = dry_run
        self.error_retry_seconds = error_retry_seconds
        self.last_state: LedDisplayState | None = None
        self.last_error: str | None = None
        self.last_target: Path | None = None
        self.last_attempt_monotonic = 0.0

    def reset(self) -> None:
        self.last_state = None
        self.last_error = None
        self.last_target = None
        self.last_attempt_monotonic = 0.0

    def sync_mode(self, mode: AgentMode) -> LedStatusWrite:
        state = display_state_for_mode(mode)
        now = time.monotonic()
        if state == self.last_state and self.last_error is None:
            return LedStatusWrite(
                state=state,
                target=self.last_target,
                program="",
                changed=False,
            )
        if (
            state == self.last_state
            and self.last_error is not None
            and now - self.last_attempt_monotonic < self.error_retry_seconds
        ):
            return LedStatusWrite(
                state=state,
                target=self.last_target,
                program="",
                changed=False,
                error=self.last_error,
            )

        self.last_attempt_monotonic = now
        try:
            result = write_mode_to_leds(
                mode,
                device_path=self.device_path,
                file_name=self.file_name,
                dry_run=self.dry_run,
            )
        except (DeviceWriteError, OSError) as exc:
            self.last_state = state
            self.last_error = str(exc)
            return LedStatusWrite(
                state=state,
                target=self.last_target,
                program="",
                changed=False,
                error=self.last_error,
            )

        self.last_state = state
        self.last_error = None
        self.last_target = result.target
        return result
