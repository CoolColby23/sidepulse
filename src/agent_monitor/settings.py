from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .battery import DEFAULT_POWER_CHANGE_PREVIEW_SECONDS


LED_DISPLAY_AGENT = "agent"
LED_DISPLAY_BATTERY = "battery"
LED_DISPLAY_CHOICES = (LED_DISPLAY_AGENT, LED_DISPLAY_BATTERY)


@dataclass(frozen=True)
class DeviceDisplaySetting:
    device_id: str
    name: str
    path: str
    led_display: str = LED_DISPLAY_AGENT

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.device_id,
            "name": self.name,
            "path": self.path,
            "led_display": self.led_display,
        }


@dataclass(frozen=True)
class AgentMonitorSettings:
    codex_transcripts_enabled: bool = False
    claude_transcripts_enabled: bool = False
    led_display: str = LED_DISPLAY_AGENT
    devices: tuple[DeviceDisplaySetting, ...] = ()
    battery_full_charge_watts: float | None = None
    battery_show_on_power_change: bool = True
    battery_power_change_preview_seconds: float = DEFAULT_POWER_CHANGE_PREVIEW_SECONDS

    def transcript_enabled(self, provider: str) -> bool:
        if provider == "codex":
            return self.codex_transcripts_enabled
        if provider == "claude":
            return self.claude_transcripts_enabled
        return False

    def with_transcript_provider(self, provider: str, enabled: bool) -> "AgentMonitorSettings":
        if provider == "codex":
            return replace(self, codex_transcripts_enabled=enabled)
        if provider == "claude":
            return replace(self, claude_transcripts_enabled=enabled)
        raise ValueError(f"Unknown transcript provider: {provider}")

    def with_led_display(self, display: str) -> "AgentMonitorSettings":
        if display not in LED_DISPLAY_CHOICES:
            raise ValueError(f"Unknown LED display: {display}")
        return replace(self, led_display=display)

    def display_for_device(self, device_id: str) -> str:
        for device in self.devices:
            if device.device_id == device_id:
                return device.led_display
        return self.led_display

    def with_device_display(
        self,
        device_id: str,
        display: str,
        *,
        name: str | None = None,
        path: str | None = None,
    ) -> "AgentMonitorSettings":
        if display not in LED_DISPLAY_CHOICES:
            raise ValueError(f"Unknown LED display: {display}")

        devices: list[DeviceDisplaySetting] = []
        updated = False
        for device in self.devices:
            if device.device_id == device_id:
                devices.append(
                    DeviceDisplaySetting(
                        device_id=device.device_id,
                        name=name or device.name,
                        path=path or device.path,
                        led_display=display,
                    )
                )
                updated = True
            else:
                devices.append(device)
        if not updated:
            devices.append(
                DeviceDisplaySetting(
                    device_id=device_id,
                    name=name or device_id,
                    path=path or device_id,
                    led_display=display,
                )
            )
        return replace(self, devices=tuple(devices))

    def with_remembered_device(
        self,
        *,
        device_id: str,
        name: str,
        path: str,
    ) -> "AgentMonitorSettings":
        return self.with_device_display(
            device_id,
            self.display_for_device(device_id),
            name=name,
            path=path,
        )

    def with_battery_full_charge_watts(self, watts: float | None) -> "AgentMonitorSettings":
        if watts is not None and watts <= 0:
            watts = None
        return replace(self, battery_full_charge_watts=watts)

    def with_battery_power_change_preview(
        self,
        *,
        enabled: bool | None = None,
        seconds: float | None = None,
    ) -> "AgentMonitorSettings":
        preview_seconds = self.battery_power_change_preview_seconds
        if seconds is not None:
            preview_seconds = max(0.0, float(seconds))
        return replace(
            self,
            battery_show_on_power_change=(
                self.battery_show_on_power_change if enabled is None else enabled
            ),
            battery_power_change_preview_seconds=preview_seconds,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "led_display": self.led_display,
            "devices": [device.to_dict() for device in self.devices],
            "transcript_monitoring": {
                "codex": self.codex_transcripts_enabled,
                "claude": self.claude_transcripts_enabled,
            },
            "battery_monitoring": {
                "full_charge_watts": self.battery_full_charge_watts,
                "show_on_power_change": self.battery_show_on_power_change,
                "power_change_preview_seconds": self.battery_power_change_preview_seconds,
            },
        }


def default_config_dir(home: Path | None = None) -> Path:
    if home is None:
        xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
        if xdg_config_home:
            return Path(xdg_config_home).expanduser() / "sidepulse" / "agent-monitor"

    base = home or Path.home()
    return base / ".config" / "sidepulse" / "agent-monitor"


def default_settings_path(home: Path | None = None) -> Path:
    return default_config_dir(home) / "settings.json"


def load_settings(path: Path | None = None) -> AgentMonitorSettings:
    target = (path or default_settings_path()).expanduser()
    if not target.exists():
        return AgentMonitorSettings()

    try:
        data = json.loads(target.read_text())
    except Exception:
        return AgentMonitorSettings()

    if not isinstance(data, dict):
        return AgentMonitorSettings()

    transcript = data.get("transcript_monitoring")
    if not isinstance(transcript, dict):
        transcript = {}

    battery = data.get("battery_monitoring")
    if not isinstance(battery, dict):
        battery = {}

    led_display = _led_display_setting(data.get("led_display"), LED_DISPLAY_AGENT)
    return AgentMonitorSettings(
        codex_transcripts_enabled=_bool_setting(transcript.get("codex"), False),
        claude_transcripts_enabled=_bool_setting(transcript.get("claude"), False),
        led_display=led_display,
        devices=_device_display_settings(data.get("devices"), led_display),
        battery_full_charge_watts=_optional_float_setting(
            battery.get("full_charge_watts"),
        ),
        battery_show_on_power_change=_bool_setting(
            battery.get("show_on_power_change"),
            True,
        ),
        battery_power_change_preview_seconds=_float_setting(
            battery.get("power_change_preview_seconds"),
            DEFAULT_POWER_CHANGE_PREVIEW_SECONDS,
        ),
    )


def save_settings(
    settings: AgentMonitorSettings,
    path: Path | None = None,
) -> Path:
    target = (path or default_settings_path()).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(settings.to_dict(), indent=2, sort_keys=True) + "\n")
    return target


def _bool_setting(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _led_display_setting(value: object, default: str) -> str:
    if isinstance(value, str) and value in LED_DISPLAY_CHOICES:
        return value
    return default


def _device_display_settings(value: object, default_display: str) -> tuple[DeviceDisplaySetting, ...]:
    if not isinstance(value, list):
        return ()

    devices: list[DeviceDisplaySetting] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        device_id = item.get("id")
        path = item.get("path")
        if not isinstance(device_id, str) or not device_id:
            continue
        if not isinstance(path, str) or not path:
            path = device_id
        if device_id in seen:
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            name = Path(path).name or device_id
        display = _led_display_setting(item.get("led_display"), default_display)
        devices.append(
            DeviceDisplaySetting(
                device_id=device_id,
                name=name,
                path=path,
                led_display=display,
            )
        )
        seen.add(device_id)
    return tuple(devices)


def _optional_float_setting(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _float_setting(value: object, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return default
