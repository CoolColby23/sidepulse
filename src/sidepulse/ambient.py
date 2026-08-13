from __future__ import annotations

import math
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .device_writer import validate_led_text
from .led_status import apply_brightness
from .models import AgentMode


BATTERY_WARNING_THRESHOLDS = (30, 20, 15, 10)
BATTERY_WARNING_IDLE_SECONDS = 15.0
BATTERY_WARNING_ACTIVE_SECONDS = 5.0
BATTERY_WARNING_RED = "#FF0000"
AUDIO_AUDIBLE_HOLD_SECONDS = 1.25
AUDIO_MIN_RMS = 1e-9


def agent_is_running(mode: AgentMode) -> bool:
    return mode in {
        AgentMode.WORKING,
        AgentMode.TOOL_RUNNING,
        AgentMode.LONG_TASK_PROGRESS,
    }


class BatteryThresholdAlert:
    def __init__(self, thresholds: tuple[int, ...] = BATTERY_WARNING_THRESHOLDS):
        self.thresholds = tuple(sorted({int(value) for value in thresholds}, reverse=True))
        self.previous_percent: int | None = None
        self.warning_threshold: int | None = None
        self.warning_started = 0.0
        self.warning_until = 0.0

    def update(self, percent: int, mode: AgentMode, *, now: float | None = None) -> int | None:
        timestamp = time.monotonic() if now is None else float(now)
        current = max(0, min(100, int(percent)))
        previous = self.previous_percent
        self.previous_percent = current
        if previous is None or current >= previous:
            return None

        crossed = next(
            (threshold for threshold in self.thresholds if previous > threshold >= current),
            None,
        )
        if crossed is None:
            return None

        duration = (
            BATTERY_WARNING_ACTIVE_SECONDS
            if agent_is_running(mode)
            else BATTERY_WARNING_IDLE_SECONDS
        )
        self.warning_threshold = crossed
        self.warning_started = timestamp
        self.warning_until = timestamp + duration
        return crossed

    def active(self, *, now: float | None = None) -> bool:
        timestamp = time.monotonic() if now is None else float(now)
        return self.warning_threshold is not None and timestamp < self.warning_until

    def adjust_for_mode(self, mode: AgentMode, *, now: float | None = None) -> None:
        timestamp = time.monotonic() if now is None else float(now)
        if not self.active(now=timestamp) or not agent_is_running(mode):
            return
        self.warning_until = min(
            self.warning_until,
            self.warning_started + BATTERY_WARNING_ACTIVE_SECONDS,
        )


def battery_warning_program(*, brightness: int | float = 255) -> str:
    return apply_brightness(
        "\n".join(["off", f"{BATTERY_WARNING_RED} 700ms pulse", "repeat"]),
        brightness,
    )


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def rms_to_level(
    rms: float,
    *,
    noise_floor_db: float = -56.0,
    peak_db: float = -8.0,
    gain_db: float = 4.0,
    curve: float = 0.72,
) -> float:
    db = 20.0 * math.log10(max(float(rms), AUDIO_MIN_RMS)) + gain_db
    normalized = clamp((db - noise_floor_db) / (peak_db - noise_floor_db))
    return normalized ** max(0.05, float(curve))


def smooth_level(previous: float, target: float, elapsed: float) -> float:
    tau = 0.045 if target > previous else 0.30
    alpha = 1.0 - math.exp(-max(0.0, elapsed) / tau)
    return clamp(previous + ((target - previous) * alpha))


def music_visualizer_program(
    level: float,
    *,
    led_count: int = 8,
    brightness: int | float = 255,
) -> str:
    count = max(1, min(8, int(led_count)))
    fill = clamp(level) * count
    segments: list[str] = []
    for index in range(count):
        segment_level = clamp(fill - index)
        position = 0.0 if count == 1 else index / (count - 1)
        if position <= 0.5:
            red = round(255 * (position / 0.5))
            green = 255
        else:
            red = 255
            green = round(255 * (1.0 - ((position - 0.5) / 0.5)))
        idle = 0.025
        intensity = idle + ((1.0 - idle) * segment_level)
        color = f"#{round(red * intensity):02X}{round(green * intensity):02X}00"
        segments.append(f"{index}:{color} 100ms cosine")
    program = apply_brightness("; ".join(segments), brightness)
    validate_led_text(program)
    return program


class SystemAudioMeter:
    def __init__(
        self,
        executable: Path,
        *,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.executable = executable.expanduser()
        self.logger = logger
        self.process: subprocess.Popen[str] | None = None
        self.latest_level = 0.0
        self.latest_sample_at = 0.0
        self.audible_until = 0.0
        self.last_error = ""
        self._lock = threading.Lock()

    def start(self) -> bool:
        if self.process is not None and self.process.poll() is None:
            return True
        if not self.executable.exists():
            self.last_error = f"system audio helper missing: {self.executable}"
            return False
        try:
            self.process = subprocess.Popen(
                [str(self.executable)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self.last_error = str(exc)
            return False
        threading.Thread(target=self._read_levels, daemon=True).start()
        threading.Thread(target=self._read_errors, daemon=True).start()
        return True

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            process.terminate()

    def level(self, *, now: float | None = None) -> float | None:
        timestamp = time.monotonic() if now is None else float(now)
        with self._lock:
            if timestamp - self.latest_sample_at > 1.0:
                return None
            if timestamp >= self.audible_until:
                return None
            return self.latest_level

    def _read_levels(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        previous_at = time.monotonic()
        for line in process.stdout:
            try:
                rms = max(0.0, float(line.strip()))
            except ValueError:
                continue
            now = time.monotonic()
            target = rms_to_level(rms)
            with self._lock:
                self.latest_level = smooth_level(
                    self.latest_level,
                    target,
                    now - previous_at,
                )
                self.latest_sample_at = now
                if target >= 0.035:
                    self.audible_until = now + AUDIO_AUDIBLE_HOLD_SECONDS
            previous_at = now

    def _read_errors(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            message = line.strip()
            if not message:
                continue
            self.last_error = message
            if self.logger is not None:
                self.logger(message)
