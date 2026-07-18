from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    import objc
    from AppKit import (
        NSApp,
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSBackingStoreBuffered,
        NSBezelStyleRounded,
        NSButton,
        NSButtonTypeSwitch,
        NSFont,
        NSImage,
        NSMenu,
        NSMenuItem,
        NSOffState,
        NSOnState,
        NSScrollView,
        NSSlider,
        NSStatusBar,
        NSTextField,
        NSTextView,
        NSView,
        NSWorkspace,
        NSWindow,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskMiniaturizable,
        NSWindowStyleMaskTitled,
        NSVariableStatusItemLength,
    )
    from Foundation import NSObject, NSTimer, NSURL
except ImportError as exc:  # pragma: no cover - only exercised on non-macOS setups.
    raise SystemExit(
        "The status-bar app requires PyObjC/AppKit:\n"
        "  python3 -m pip install pyobjc-framework-Cocoa"
    ) from exc

from .battery import (
    BatteryLedController,
    BatterySnapshot,
    format_watts,
    read_battery_snapshot,
)
from .collector import LiveAgentMonitor, SourceSpec, read_recent_lines
from .device_writer import (
    DEFAULT_FILE_NAME,
    MOUNT_ROOT,
    DeviceCandidate,
    DeviceWriteError,
    discover_devices,
    normalize_led_text,
    path_exists,
    target_from_device_path,
    validate_led_text,
    write_led_program,
)
from .keep_awake import KEEPALIVE_FILE_NAME, KeepAwakeController
from .ipc import HookEventServer, default_event_socket_path, default_latest_state_path
from .install import (
    install_claude_hooks,
    install_codex_hooks,
    uninstall_claude_hooks,
    uninstall_codex_hooks,
)
from .led_status import (
    AgentLedController,
    apply_brightness,
    brightness_percent,
    normalize_brightness,
    normalized_device_name,
    write_mode_to_leds,
)
from .lid_sleep import (
    LID_POLL_SECONDS,
    ClosedLidAwakeController,
    read_lid_closed,
    sleep_helper_install_command,
    sleep_helper_installed,
)
from .models import AgentMode, AgentStatus
from .providers import (
    ProviderConfig,
    detect_claude_config,
    detect_codex_config,
    detect_log_path,
    parse_log_line,
)
from .session_actions import (
    available_session_open_actions,
    default_session_open_action,
    session_open_action_label,
    session_open_target,
)
from .settings import (
    CLOSED_LID_AWAKE_AGENTS,
    CLOSED_LID_AWAKE_ALWAYS,
    CLOSED_LID_AWAKE_CHOICES,
    CLOSED_LID_AWAKE_NEVER,
    LED_DISPLAY_AGENT,
    LED_DISPLAY_BATTERY,
    LID_ANIMATION_CLOSED,
    LID_ANIMATION_OPEN,
    LedAnimationSetting,
    default_settings_path,
    default_lid_animation,
    load_settings,
    normalize_animation_duration,
    save_settings,
)


@dataclass(frozen=True)
class StatusBarState:
    label: str
    symbol: str
    priority: int


@dataclass(frozen=True)
class StatusBarDevice:
    device_id: str
    name: str
    root: Path
    target: Path
    connected: bool
    display: str
    brightness: int = 255
    reason: str = ""


STATE_IDLE = StatusBarState("Idle", "circle", 4)
STATE_WORKING = StatusBarState("Working", "arrow.triangle.2.circlepath", 2)
STATE_DONE = StatusBarState("Done", "checkmark.circle", 3)
STATE_ASK = StatusBarState("Ask", "questionmark.circle", 1)
STATUS_BAR_DEVICE_PRIORITY = ("sidepulse", "pixiepulse", "pulsedot", "pixiedot")
STATUS_BAR_KEEPALIVE_VOLUME_NAMES = ("SidePulse", "PulseDot")
STATUS_BAR_REFRESH_SECONDS = 15.0
STATUS_BAR_MAX_LINES_PER_SOURCE = 500
STATUS_BAR_STARTUP_REPLAY_LINES = 200
LID_ANIMATION_RESTORE_FUDGE_SECONDS = 0.15
LID_ANIMATION_LABELS = {
    LID_ANIMATION_CLOSED: "Lid Closed",
    LID_ANIMATION_OPEN: "Lid Open",
}
CLOSED_LID_AWAKE_LABELS = {
    CLOSED_LID_AWAKE_NEVER: "Never",
    CLOSED_LID_AWAKE_AGENTS: "When Agents Work",
    CLOSED_LID_AWAKE_ALWAYS: "Always",
}


def state_for_mode(mode: AgentMode) -> StatusBarState:
    if mode in {AgentMode.WAITING_FOR_INPUT, AgentMode.BLOCKED_ERROR}:
        return STATE_ASK
    if mode in {
        AgentMode.WORKING,
        AgentMode.TOOL_RUNNING,
        AgentMode.LONG_TASK_PROGRESS,
    }:
        return STATE_WORKING
    if mode == AgentMode.COMPLETED:
        return STATE_DONE
    return STATE_IDLE


def replay_recent_debug_logs(
    monitor: LiveAgentMonitor,
    *,
    providers: tuple[str, ...] = ("codex", "claude"),
    max_lines: int = STATUS_BAR_STARTUP_REPLAY_LINES,
) -> int:
    replayed = 0
    for provider in providers:
        try:
            path = detect_log_path(provider)
            lines = read_recent_lines(path, max_lines) if path.exists() else []
        except Exception as exc:
            log_status_bar(f"startup_replay {provider} error: {exc}")
            continue

        for line in lines:
            record = parse_log_line(provider, line)
            if record is None:
                continue
            monitor.ingest_record(record)
            replayed += 1
    return replayed


class StatusBarController(NSObject):
    def init(self):
        self = objc.super(StatusBarController, self).init()
        if self is None:
            return None

        self.settings = load_settings()
        self.monitor = self.build_monitor()
        self.event_server = None
        self.status_item = None
        self.timer = None
        self.lid_timer = None
        self.settings_window = None
        self.settings_fields = {}
        self.settings_buttons = {}
        self.last_snapshot = None
        self.last_battery_snapshot = None
        self.last_battery_error = None
        self.last_power_connected = None
        self.battery_preview_until = 0.0
        self.current_state = STATE_IDLE
        self.led_controller = AgentLedController()
        self.battery_led_controller = BatteryLedController()
        self.agent_led_controllers_by_device = {}
        self.battery_led_controllers_by_device = {}
        self.last_led_display_kind_by_device = {}
        self.device_errors = {}
        self.leds_enabled = True
        self.led_sync_in_flight = False
        self.last_led_error = None
        self.last_led_display_kind = LED_DISPLAY_AGENT
        self.keep_awake = KeepAwakeController()
        self.closed_lid_awake = ClosedLidAwakeController(
            use_system_disable=self.settings.closed_lid_system_override_enabled,
        )
        self.last_keep_awake_error = None
        self.last_closed_lid_awake_error = None
        self.last_status_read_error = None
        self.event_refresh_pending = False
        self.last_lid_closed = None
        self.last_lid_error = None
        self.led_animation_until_monotonic = 0.0
        self.led_animation_token = 0
        return self

    def applicationDidFinishLaunching_(self, _notification):
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        log_status_bar("launching status item")
        self.start_event_server()
        self.replay_debug_logs()

        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        button = self.status_item.button()
        button.setTitle_(" Idle")
        button.setImage_(image_for_symbol(STATE_IDLE.symbol, STATE_IDLE.label))
        button.setToolTip_("SidePulse Agent Monitor: Idle")
        log_status_bar("status item created")

        self.refresh_(None)
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            STATUS_BAR_REFRESH_SECONDS,
            self,
            "refresh:",
            None,
            True,
        )
        self.lid_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            LID_POLL_SECONDS,
            self,
            "pollLid:",
            None,
            True,
        )

    @objc.IBAction
    def refresh_(self, _sender):
        try:
            snapshot = self.monitor.snapshot(include_stale=False)
        except Exception as exc:
            log_status_bar(f"refresh error: {exc}")
            self.set_status(STATE_ASK)
            self.sync_keep_awake(AgentMode.BLOCKED_ERROR)
            self.sync_leds(AgentMode.BLOCKED_ERROR, None, LED_DISPLAY_AGENT)
            self.status_item.setMenu_(build_error_menu(exc))
            return

        self.last_snapshot = snapshot
        battery_snapshot = self.read_battery_snapshot()
        state = state_for_mode(snapshot.aggregate.mode)
        self.set_status(state)
        self.sync_keep_awake(snapshot.aggregate.mode)
        self.sync_leds(
            snapshot.aggregate.mode,
            battery_snapshot,
            self.active_led_display_kind(battery_snapshot),
        )
        self.status_item.setMenu_(build_menu(snapshot, state, self))

    @objc.IBAction
    def forceRefresh_(self, _sender):
        self.refresh_(None)

    @objc.IBAction
    def openDeepLink_(self, sender):
        url = sender.representedObject()
        if not url:
            return
        open_url(str(url))

    @objc.IBAction
    def resumeSession_(self, sender):
        command = sender.representedObject()
        if command:
            open_terminal_command(str(command))

    @objc.IBAction
    def openSession_(self, sender):
        self.open_session(sender.representedObject(), None, remember=False)

    @objc.IBAction
    def openSessionWithAction_(self, sender):
        payload = sender.representedObject()
        if not isinstance(payload, dict):
            return
        self.open_session(payload.get("status"), payload.get("action"), remember=True)

    @objc.IBAction
    def toggleDeviceConnection_(self, _sender):
        if self.device_connected():
            self.disconnect_device()
        else:
            self.connect_device()
        self.refresh_(None)

    @objc.IBAction
    def toggleKeepAwake_(self, _sender):
        self.keep_awake.set_enabled(not self.keep_awake.enabled)
        log_status_bar(f"keep_awake={'on' if self.keep_awake.enabled else 'off'}")
        self.refresh_(None)

    @objc.IBAction
    def setClosedLidAwakePolicy_(self, sender):
        self.set_closed_lid_awake_policy(sender.representedObject())

    @objc.IBAction
    def toggleClosedLidSystemOverride_(self, sender):
        if hasattr(sender, "state"):
            enabled = sender.state() != NSOnState
        else:
            enabled = not self.settings.closed_lid_system_override_enabled
        self.set_closed_lid_system_override(enabled)

    @objc.IBAction
    def openSettings_(self, _sender):
        self.show_settings_window()

    @objc.IBAction
    def installCodexHooks_(self, _sender):
        self.update_hooks("codex", install=True)

    @objc.IBAction
    def uninstallCodexHooks_(self, _sender):
        self.update_hooks("codex", install=False)

    @objc.IBAction
    def installClaudeHooks_(self, _sender):
        self.update_hooks("claude", install=True)

    @objc.IBAction
    def uninstallClaudeHooks_(self, _sender):
        self.update_hooks("claude", install=False)

    @objc.IBAction
    def toggleCodexTranscripts_(self, sender):
        self.set_transcript_monitoring("codex", sender.state() == NSOnState)

    @objc.IBAction
    def toggleClaudeTranscripts_(self, sender):
        self.set_transcript_monitoring("claude", sender.state() == NSOnState)

    @objc.IBAction
    def toggleBatteryLedDisplay_(self, _sender):
        self.set_battery_led_display(self.settings.led_display != LED_DISPLAY_BATTERY)

    @objc.IBAction
    def setBatteryLedDisplayFromCheckbox_(self, sender):
        self.set_battery_led_display(sender.state() == NSOnState)

    @objc.IBAction
    def toggleBatteryPowerPreview_(self, _sender):
        self.set_battery_power_preview(not self.settings.battery_show_on_power_change)

    @objc.IBAction
    def setBatteryPowerPreviewFromCheckbox_(self, sender):
        self.set_battery_power_preview(sender.state() == NSOnState)

    @objc.IBAction
    def setClosedLidSystemOverrideFromCheckbox_(self, sender):
        self.set_closed_lid_system_override(sender.state() == NSOnState)

    @objc.IBAction
    def setDeviceDisplayAgent_(self, sender):
        self.set_device_display(sender.representedObject(), LED_DISPLAY_AGENT)

    @objc.IBAction
    def setDeviceDisplayBattery_(self, sender):
        self.set_device_display(sender.representedObject(), LED_DISPLAY_BATTERY)

    @objc.IBAction
    def setDeviceBrightness_(self, sender):
        device_id = sender.identifier()
        if device_id is None:
            return
        self.set_device_brightness(str(device_id), sender.doubleValue())

    @objc.IBAction
    def saveLidAnimations_(self, _sender):
        self.save_lid_animations_from_fields()

    @objc.IBAction
    def previewLidClosedAnimation_(self, _sender):
        animation = self.lid_animation_from_fields(LID_ANIMATION_CLOSED)
        if animation is not None:
            self.play_lid_animation(LID_ANIMATION_CLOSED, animation=animation)

    @objc.IBAction
    def previewLidOpenAnimation_(self, _sender):
        animation = self.lid_animation_from_fields(LID_ANIMATION_OPEN)
        if animation is not None:
            self.play_lid_animation(LID_ANIMATION_OPEN, animation=animation)

    @objc.IBAction
    def resetLidClosedAnimation_(self, _sender):
        self.reset_lid_animation(LID_ANIMATION_CLOSED)

    @objc.IBAction
    def resetLidOpenAnimation_(self, _sender):
        self.reset_lid_animation(LID_ANIMATION_OPEN)

    @objc.IBAction
    def removeRememberedDevice_(self, sender):
        self.remove_remembered_device(sender.representedObject())

    @objc.IBAction
    def quit_(self, _sender):
        self.closed_lid_awake.release()
        self.keep_awake.release()
        NSApp.terminate_(self)

    def applicationWillTerminate_(self, _notification):
        self.stop_event_server()
        self.closed_lid_awake.release()
        self.keep_awake.release()

    def set_status(self, state: StatusBarState) -> None:
        previous = self.current_state
        self.current_state = state
        if self.status_item is None:
            return
        button = self.status_item.button()
        if button is None:
            return
        button.setTitle_(f" {state.label}")
        button.setImage_(image_for_symbol(state.symbol, state.label))
        button.setToolTip_(f"SidePulse Agent Monitor: {state.label}")
        if previous != state:
            log_status_bar(f"state={state.label}")

    def build_monitor(self) -> LiveAgentMonitor:
        socket_path = default_event_socket_path()
        return LiveAgentMonitor(
            sources=(SourceSpec("event-bus", socket_path),),
            stale_after_seconds=3600,
            latest_state_path=default_latest_state_path(),
        )

    def reload_monitor(self) -> None:
        self.monitor = self.build_monitor()

    def start_event_server(self) -> None:
        self.stop_event_server()
        self.event_server = HookEventServer(self.handle_hook_event_message)
        try:
            socket_path = self.event_server.start()
            log_status_bar(f"event_server listening={socket_path}")
        except Exception as exc:
            self.event_server = None
            log_status_bar(f"event_server error: {exc}")

    def stop_event_server(self) -> None:
        if self.event_server is not None:
            self.event_server.stop()
            self.event_server = None

    def handle_hook_event_message(self, provider: str, line: dict) -> None:
        try:
            record = parse_log_line(
                provider,
                json.dumps(line, separators=(",", ":"), ensure_ascii=False),
            )
            if record is not None:
                self.monitor.ingest_record(record)
                self.schedule_event_refresh()
        except Exception as exc:
            log_status_bar(f"event_server ingest error: {exc}")

    def schedule_event_refresh(self) -> None:
        if self.event_refresh_pending:
            return
        self.event_refresh_pending = True
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "refreshFromEvent:",
            None,
            False,
        )

    @objc.IBAction
    def refreshFromEvent_(self, _sender):
        self.event_refresh_pending = False
        self.refresh_(None)

    def replay_debug_logs(self) -> None:
        replayed = replay_recent_debug_logs(self.monitor)
        if replayed:
            log_status_bar(f"startup_replay events={replayed}")

    def show_settings_window(self) -> None:
        if self.settings_window is None:
            self.settings_window = build_settings_window(self)
        self.refresh_settings_window()
        self.settings_window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def refresh_settings_window(self) -> None:
        if self.settings_window is None:
            return

        codex = detect_codex_config()
        claude = detect_claude_config()
        set_field_value(
            self.settings_fields.get("codex_hook_status"),
            hook_status_text(codex),
        )
        set_field_value(
            self.settings_fields.get("claude_hook_status"),
            hook_status_text(claude),
        )
        set_field_value(
            self.settings_fields.get("settings_path"),
            f"Settings: {default_settings_path()}",
        )
        set_checkbox_state(
            self.settings_buttons.get("codex_transcripts"),
            self.settings.codex_transcripts_enabled,
        )
        set_checkbox_state(
            self.settings_buttons.get("claude_transcripts"),
            self.settings.claude_transcripts_enabled,
        )
        set_checkbox_state(
            self.settings_buttons.get("battery_leds"),
            self.settings.led_display == LED_DISPLAY_BATTERY,
        )
        set_checkbox_state(
            self.settings_buttons.get("battery_power_preview"),
            self.settings.battery_show_on_power_change,
        )
        set_checkbox_state(
            self.settings_buttons.get("closed_lid_system_override"),
            self.settings.closed_lid_system_override_enabled,
        )
        closed = self.settings.lid_closed_animation
        opened = self.settings.lid_open_animation
        set_text_control_value(
            self.settings_fields.get("closed_animation_program"),
            closed.program,
        )
        set_text_control_value(
            self.settings_fields.get("closed_animation_duration"),
            f"{closed.duration_seconds:g}",
        )
        set_text_control_value(
            self.settings_fields.get("open_animation_program"),
            opened.program,
        )
        set_text_control_value(
            self.settings_fields.get("open_animation_duration"),
            f"{opened.duration_seconds:g}",
        )

    def set_settings_message(self, message: str) -> None:
        set_field_value(self.settings_fields.get("message"), message)
        if message:
            log_status_bar(f"settings: {message}")

    def update_hooks(self, provider: str, *, install: bool) -> None:
        try:
            if provider == "codex" and install:
                result = install_codex_hooks()
            elif provider == "codex":
                result = uninstall_codex_hooks()
            elif provider == "claude" and install:
                result = install_claude_hooks()
            else:
                result = uninstall_claude_hooks()
        except Exception as exc:
            self.set_settings_message(f"{provider.title()} hooks failed: {exc}")
            self.refresh_settings_window()
            return

        action = "installed" if install else "removed"
        if not result.changed:
            action = "already installed" if install else "already removed"
        self.set_settings_message(f"{provider.title()} hooks {action}.")
        self.reload_monitor()
        self.refresh_settings_window()
        self.refresh_(None)

    def set_transcript_monitoring(self, provider: str, enabled: bool) -> None:
        try:
            self.settings = self.settings.with_transcript_provider(provider, enabled)
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not save settings: {exc}")
            self.settings = load_settings()
            self.refresh_settings_window()
            return

        self.reload_monitor()
        self.set_settings_message(
            f"{provider.title()} transcript CLI fallback {'enabled' if enabled else 'disabled'}."
        )
        self.refresh_settings_window()
        self.refresh_(None)

    def set_battery_led_display(self, enabled: bool) -> None:
        try:
            display = LED_DISPLAY_BATTERY if enabled else LED_DISPLAY_AGENT
            self.settings = self.settings.with_led_display(display)
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not save settings: {exc}")
            self.settings = load_settings()
            self.refresh_settings_window()
            return

        self.reset_led_controllers_for_display_change()
        self.set_settings_message(f"LED display set to {self.settings.led_display}.")
        self.refresh_settings_window()
        self.refresh_(None)

    def set_device_display(self, device_id: str | None, display: str) -> None:
        if not device_id:
            return
        device = next(
            (
                entry
                for entry in self.status_bar_devices(remember=False)
                if entry.device_id == str(device_id)
            ),
            None,
        )
        try:
            self.settings = self.settings.with_device_display(
                str(device_id),
                display,
                name=device.name if device else None,
                path=str(device.root) if device else None,
            )
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not save device display: {exc}")
            self.settings = load_settings()
            return

        self.reset_led_controllers_for_device(str(device_id))
        label = "Battery Level" if display == LED_DISPLAY_BATTERY else "Agent Status"
        self.set_settings_message(f"{device.name if device else device_id}: {label}.")
        self.refresh_settings_window()
        self.refresh_(None)

    def set_device_brightness(self, device_id: str | None, brightness: int | float) -> None:
        if not device_id:
            return
        device = next(
            (
                entry
                for entry in self.status_bar_devices(remember=False)
                if entry.device_id == str(device_id)
            ),
            None,
        )
        value = normalize_brightness(brightness)
        try:
            self.settings = self.settings.with_device_brightness(
                str(device_id),
                value,
                name=device.name if device else None,
                path=str(device.root) if device else None,
            )
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not save brightness: {exc}")
            self.settings = load_settings()
            return

        self.reset_led_controllers_for_device(str(device_id))
        self.set_settings_message(
            f"{device.name if device else device_id}: brightness {brightness_percent(value)}%."
        )
        self.refresh_settings_window()
        if self.last_snapshot is not None:
            self.sync_leds(
                self.last_snapshot.aggregate.mode,
                self.last_battery_snapshot,
                self.active_led_display_kind(self.last_battery_snapshot),
            )

    def set_closed_lid_awake_policy(self, policy: str | None) -> None:
        if policy not in CLOSED_LID_AWAKE_CHOICES:
            return
        try:
            self.settings = self.settings.with_closed_lid_awake_policy(str(policy))
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not save lid sleep setting: {exc}")
            self.settings = load_settings()
            return

        self.set_settings_message(
            f"Closed-lid awake: {CLOSED_LID_AWAKE_LABELS[self.settings.closed_lid_awake_policy]}."
        )
        self.sync_closed_lid_awake()
        self.refresh_settings_window()
        self.refresh_(None)

    def set_closed_lid_system_override(self, enabled: bool) -> None:
        try:
            self.settings = self.settings.with_closed_lid_system_override(enabled)
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not save sleep override setting: {exc}")
            self.settings = load_settings()
            self.refresh_settings_window()
            return

        self.closed_lid_awake.set_use_system_disable(
            self.settings.closed_lid_system_override_enabled
        )
        if self.settings.closed_lid_system_override_enabled and not sleep_helper_installed():
            self.set_settings_message(
                "Strong sleep override needs setup: "
                + sleep_helper_install_command()
            )
        else:
            label = "enabled" if self.settings.closed_lid_system_override_enabled else "disabled"
            self.set_settings_message(f"Strong sleep override {label}.")
        self.sync_closed_lid_awake()
        self.refresh_settings_window()
        self.refresh_(None)

    def lid_animation_from_fields(self, kind: str) -> LedAnimationSetting | None:
        program_field = self.settings_fields.get(f"{kind}_animation_program")
        duration_field = self.settings_fields.get(f"{kind}_animation_duration")
        current = self.settings.lid_animation(kind)
        program = text_control_value(program_field) or current.program
        duration_text = text_control_value(duration_field)
        try:
            duration = float(duration_text) if duration_text else current.duration_seconds
        except ValueError:
            self.set_settings_message(f"{LID_ANIMATION_LABELS[kind]} duration is not a number.")
            return None

        animation = LedAnimationSetting(
            program=normalize_led_text(program),
            duration_seconds=normalize_animation_duration(duration),
        )
        try:
            validate_lid_animation(animation)
        except DeviceWriteError as exc:
            self.set_settings_message(f"{LID_ANIMATION_LABELS[kind]} animation invalid: {exc}")
            return None
        return animation

    def save_lid_animations_from_fields(self) -> None:
        closed = self.lid_animation_from_fields(LID_ANIMATION_CLOSED)
        if closed is None:
            return
        opened = self.lid_animation_from_fields(LID_ANIMATION_OPEN)
        if opened is None:
            return
        try:
            self.settings = self.settings.with_lid_animation(
                LID_ANIMATION_CLOSED,
                program=closed.program,
                duration_seconds=closed.duration_seconds,
            )
            self.settings = self.settings.with_lid_animation(
                LID_ANIMATION_OPEN,
                program=opened.program,
                duration_seconds=opened.duration_seconds,
            )
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not save lid animations: {exc}")
            self.settings = load_settings()
            self.refresh_settings_window()
            return

        self.set_settings_message("Lid animations saved.")
        self.refresh_settings_window()

    def reset_lid_animation(self, kind: str) -> None:
        animation = default_lid_animation(kind)
        try:
            self.settings = self.settings.with_lid_animation(
                kind,
                program=animation.program,
                duration_seconds=animation.duration_seconds,
            )
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not reset {LID_ANIMATION_LABELS[kind]}: {exc}")
            self.settings = load_settings()
            self.refresh_settings_window()
            return

        self.set_settings_message(f"{LID_ANIMATION_LABELS[kind]} reset.")
        self.refresh_settings_window()

    def remove_remembered_device(self, device_id: str | None) -> None:
        if not device_id:
            return
        device = next(
            (
                entry
                for entry in self.status_bar_devices(remember=False)
                if entry.device_id == str(device_id)
            ),
            None,
        )
        try:
            self.settings = self.settings.without_device(str(device_id))
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not remove device: {exc}")
            self.settings = load_settings()
            return

        self.reset_led_controllers_for_device(str(device_id))
        self.set_settings_message(f"{device.name if device else device_id}: removed.")
        self.refresh_settings_window()
        self.refresh_(None)

    def open_session(self, status: AgentStatus | object, action: str | None, *, remember: bool) -> None:
        if not isinstance(status, AgentStatus):
            return
        provider = status.provider.lower()
        requested_action = (
            action
            or self.settings.session_open_action(provider)
            or default_session_open_action(status)
        )
        target = session_open_target(status, requested_action)
        if target is None:
            requested_action = default_session_open_action(status)
            target = session_open_target(status, requested_action)
        if target is None:
            self.set_settings_message(f"No open action available for {status.display_name}.")
            return

        kind, value = target
        if kind == "url":
            open_url(value)
        elif kind == "terminal":
            open_terminal_command(value)
        else:
            self.set_settings_message(f"Unknown open action for {status.display_name}.")
            return

        if remember:
            try:
                self.settings = self.settings.with_session_open_action(provider, requested_action)
                save_settings(self.settings)
            except Exception as exc:
                self.set_settings_message(f"Could not save open preference: {exc}")
                self.settings = load_settings()

    def set_battery_power_preview(self, enabled: bool) -> None:
        try:
            self.settings = self.settings.with_battery_power_change_preview(enabled=enabled)
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not save settings: {exc}")
            self.settings = load_settings()
            self.refresh_settings_window()
            return

        self.set_settings_message(
            f"Battery power-change preview {'enabled' if enabled else 'disabled'}."
        )
        self.refresh_settings_window()
        self.refresh_(None)

    def read_battery_snapshot(self) -> BatterySnapshot | None:
        try:
            snapshot = read_battery_snapshot(
                full_charge_watts=self.settings.battery_full_charge_watts,
            )
        except Exception as exc:
            error = str(exc)
            if error != self.last_battery_error:
                log_status_bar(f"battery error: {error}")
            self.last_battery_error = error
            return None

        self.last_battery_error = None
        self.last_battery_snapshot = snapshot
        self.update_battery_power_preview(snapshot)
        return snapshot

    def update_battery_power_preview(self, snapshot: BatterySnapshot) -> None:
        plugged = snapshot.is_plugged
        if self.last_power_connected is not None and self.last_power_connected != plugged:
            if self.settings.battery_show_on_power_change:
                self.battery_preview_until = (
                    time.monotonic()
                    + self.settings.battery_power_change_preview_seconds
                )
                log_status_bar(
                    f"battery preview power={'plugged' if plugged else 'unplugged'}"
                )
        self.last_power_connected = plugged

    def active_led_display_kind(self, snapshot: BatterySnapshot | None) -> str:
        if self.settings.led_display == LED_DISPLAY_BATTERY:
            return LED_DISPLAY_BATTERY
        if snapshot is not None and time.monotonic() < self.battery_preview_until:
            return LED_DISPLAY_BATTERY
        return LED_DISPLAY_AGENT

    def reset_led_controllers_for_display_change(self) -> None:
        self.led_controller.reset()
        self.battery_led_controller.reset()
        for controller in self.agent_led_controllers_by_device.values():
            controller.reset()
        for controller in self.battery_led_controllers_by_device.values():
            controller.reset()
        self.last_led_display_kind_by_device.clear()
        self.last_led_error = None

    def reset_led_controllers_for_device(self, device_id: str) -> None:
        agent_controller = self.agent_led_controllers_by_device.get(device_id)
        if agent_controller is not None:
            agent_controller.reset()
        battery_controller = self.battery_led_controllers_by_device.get(device_id)
        if battery_controller is not None:
            battery_controller.reset()
        self.last_led_display_kind_by_device.pop(device_id, None)
        self.device_errors.pop(device_id, None)
        self.last_led_error = None

    def agent_controller_for_device(self, device: StatusBarDevice) -> AgentLedController:
        controller = self.agent_led_controllers_by_device.get(device.device_id)
        if controller is None:
            controller = AgentLedController(device_path=device.target)
            self.agent_led_controllers_by_device[device.device_id] = controller
        controller.device_path = device.target
        controller.brightness = device.brightness
        return controller

    def battery_controller_for_device(self, device: StatusBarDevice) -> BatteryLedController:
        controller = self.battery_led_controllers_by_device.get(device.device_id)
        if controller is None:
            controller = BatteryLedController(device_path=device.target)
            self.battery_led_controllers_by_device[device.device_id] = controller
        controller.device_path = device.target
        controller.brightness = device.brightness
        return controller

    def status_bar_devices(self, *, remember: bool = True) -> list[StatusBarDevice]:
        entries_by_id: dict[str, StatusBarDevice] = {}
        try:
            candidates = discover_devices()
        except Exception as exc:
            log_status_bar(f"device discovery error: {exc}")
            candidates = []

        for candidate in candidates:
            device_id = device_id_for_root(candidate.root)
            name = device_display_name(candidate.root.name)
            entries_by_id[device_id] = StatusBarDevice(
                device_id=device_id,
                name=name,
                root=candidate.root,
                target=candidate.target,
                connected=True,
                display=self.settings.display_for_device(device_id),
                brightness=self.settings.brightness_for_device(device_id),
                reason=candidate.reason,
            )

        for device in self.settings.devices:
            if device.device_id in entries_by_id:
                continue
            root = Path(device.path).expanduser()
            entries_by_id[device.device_id] = StatusBarDevice(
                device_id=device.device_id,
                name=device.name,
                root=root,
                target=target_from_device_path(root, DEFAULT_FILE_NAME),
                connected=False,
                display=device.led_display,
                brightness=device.brightness,
                reason="previously connected",
            )

        entries = sorted(
            entries_by_id.values(),
            key=lambda item: (not item.connected, normalized_device_name(item.name), str(item.root)),
        )
        entries = disambiguate_device_names(entries)
        if remember:
            self.remember_connected_devices(entries)
        return entries

    def remember_connected_devices(self, devices: list[StatusBarDevice]) -> None:
        settings = self.settings
        for device in devices:
            if not device.connected:
                continue
            settings = settings.with_remembered_device(
                device_id=device.device_id,
                name=device.name,
                path=str(device.root),
            )
        if settings != self.settings:
            self.settings = settings
            try:
                save_settings(self.settings)
            except Exception as exc:
                log_status_bar(f"device remember error: {exc}")

    def active_led_display_kind_for_device(
        self,
        device: StatusBarDevice,
        battery_snapshot: BatterySnapshot | None,
    ) -> str:
        if device.display == LED_DISPLAY_BATTERY:
            return LED_DISPLAY_BATTERY
        if battery_snapshot is not None and time.monotonic() < self.battery_preview_until:
            return LED_DISPLAY_BATTERY
        return LED_DISPLAY_AGENT

    def sync_leds(
        self,
        mode: AgentMode,
        battery_snapshot: BatterySnapshot | None,
        display_kind: str,
    ) -> None:
        if not self.leds_enabled:
            return

        if time.monotonic() < self.led_animation_until_monotonic:
            return

        if self.led_sync_in_flight:
            return
        self.led_sync_in_flight = True
        thread = threading.Thread(
            target=self.sync_leds_worker,
            args=(mode, battery_snapshot, display_kind),
            daemon=True,
        )
        thread.start()

    def sync_leds_worker(
        self,
        mode: AgentMode,
        battery_snapshot: BatterySnapshot | None,
        display_kind: str,
    ) -> None:
        try:
            self.sync_leds_now(mode, battery_snapshot, display_kind)
        finally:
            self.led_sync_in_flight = False

    def sync_leds_now(
        self,
        mode: AgentMode,
        battery_snapshot: BatterySnapshot | None,
        display_kind: str,
    ) -> None:
        devices = [device for device in self.status_bar_devices() if device.connected]
        if not devices:
            self.ensure_device_selection()
            devices = [
                device for device in self.status_bar_devices(remember=False) if device.connected
            ]
        if not devices:
            self.last_led_error = None
            return

        active_errors: dict[str, str] = {}
        for device in devices:
            device_display_kind = self.active_led_display_kind_for_device(
                device,
                battery_snapshot,
            )
            if self.last_led_display_kind_by_device.get(device.device_id) != device_display_kind:
                self.reset_led_controllers_for_device(device.device_id)
                self.last_led_display_kind_by_device[device.device_id] = device_display_kind

            if device_display_kind == LED_DISPLAY_BATTERY and battery_snapshot is not None:
                result = self.battery_controller_for_device(device).sync_snapshot(battery_snapshot)
                label = (
                    f"{device.name} Battery {battery_snapshot.percent}% "
                    f"{format_watts(battery_snapshot.adapter_power)}"
                )
            else:
                result = self.agent_controller_for_device(device).sync_mode(mode)
                label = f"{device.name} {result.label}"

            if result.error:
                active_errors[device.device_id] = result.error
                previous_error = self.device_errors.get(device.device_id)
                if result.error != previous_error:
                    log_status_bar(f"led error {device.name}: {result.error}")
                continue

            self.device_errors.pop(device.device_id, None)
            if result.changed:
                target = result.target if result.target is not None else "-"
                log_status_bar(f"leds={label} target={target}")

        self.device_errors.update(active_errors)
        self.last_led_error = next(iter(self.device_errors.values()), None)

    def play_lid_animation(
        self,
        kind: str,
        *,
        animation: LedAnimationSetting | None = None,
    ) -> None:
        if not self.leds_enabled:
            return
        animation = animation or self.settings.lid_animation(kind)
        try:
            validate_lid_animation(animation)
        except DeviceWriteError as exc:
            self.set_settings_message(f"{LID_ANIMATION_LABELS[kind]} animation invalid: {exc}")
            return

        devices = [device for device in self.status_bar_devices() if device.connected]
        if not devices:
            return

        self.led_animation_token += 1
        token = self.led_animation_token
        duration = animation.duration_seconds + LID_ANIMATION_RESTORE_FUDGE_SECONDS
        self.led_animation_until_monotonic = time.monotonic() + duration
        thread = threading.Thread(
            target=self.play_lid_animation_worker,
            args=(kind, animation, devices, token),
            daemon=True,
        )
        thread.start()

    def play_lid_animation_worker(
        self,
        kind: str,
        animation: LedAnimationSetting,
        devices: list[StatusBarDevice],
        token: int,
    ) -> None:
        label = LID_ANIMATION_LABELS[kind]
        for device in devices:
            try:
                program = program_for_lid_animation(animation, brightness=device.brightness)
                target = write_led_program(program, device_path=device.target)
                log_status_bar(f"animation={label} device={device.name} target={target}")
            except Exception as exc:
                log_status_bar(f"animation error {label} {device.name}: {exc}")

        time.sleep(animation.duration_seconds + LID_ANIMATION_RESTORE_FUDGE_SECONDS)
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "restoreLedDisplay:",
            str(token),
            False,
        )

    @objc.IBAction
    def restoreLedDisplay_(self, token_value):
        restore_led_display(self, token_value)

    def connect_device(self) -> None:
        self.leds_enabled = True
        self.status_bar_devices()
        self.reset_led_controllers_for_display_change()
        self.last_led_error = None
        self.last_status_read_error = None
        log_status_bar("device connect requested")

    def disconnect_device(self) -> None:
        self.leds_enabled = False
        targets = self.current_led_targets()
        if not targets:
            targets = [device.target for device in self.status_bar_devices() if device.connected]
        for target in targets:
            try:
                result = write_mode_to_leds(AgentMode.IDLE_READY, device_path=target)
                log_status_bar(f"device disconnected target={result.target}")
            except Exception as exc:
                log_status_bar(f"device disconnect error: {exc}")
        self.reset_led_controllers_for_display_change()
        self.last_led_error = None
        self.last_status_read_error = None

    def device_connected(self) -> bool:
        connected = [
            device
            for device in self.status_bar_devices(remember=False)
            if device.connected
        ]
        return (
            self.leds_enabled
            and bool(self.current_led_targets() or connected)
            and self.last_led_error is None
            and not self.device_errors
        )

    def current_led_target(self) -> Path | None:
        targets = self.current_led_targets()
        return targets[0] if targets else None

    def current_led_targets(self) -> list[Path]:
        targets: list[Path] = []
        seen: set[str] = set()
        for controller in (
            *self.battery_led_controllers_by_device.values(),
            *self.agent_led_controllers_by_device.values(),
            self.battery_led_controller,
            self.led_controller,
        ):
            target = controller.last_target or controller.device_path
            if target is None:
                continue
            if not path_exists(Path(target).parent):
                continue
            key = str(target)
            if key in seen:
                continue
            targets.append(Path(target))
            seen.add(key)
        return targets

    def ensure_device_selection(self) -> None:
        selected = self.led_controller.device_path or self.battery_led_controller.device_path
        if selected is not None:
            target = target_from_device_path(
                Path(selected),
                DEFAULT_FILE_NAME,
            )
            if path_exists(target.parent):
                self.led_controller.device_path = target
                self.battery_led_controller.device_path = target
                return
            self.led_controller.device_path = None
            self.battery_led_controller.device_path = None
            self.reset_led_controllers_for_display_change()
            self.last_led_error = None

        candidates = discover_devices()
        if not candidates:
            return
        target = preferred_status_bar_device(candidates).target
        self.led_controller.device_path = target
        self.battery_led_controller.device_path = target

    @objc.IBAction
    def pollLid_(self, _sender):
        try:
            closed = read_lid_closed()
        except Exception as exc:
            error = str(exc)
            if error != self.last_lid_error:
                self.last_lid_error = error
                log_status_bar(f"lid_state error: {error}")
            return

        if closed is None:
            return
        self.last_lid_error = None
        if self.last_lid_closed is None:
            self.last_lid_closed = closed
            return
        if closed == self.last_lid_closed:
            return

        self.last_lid_closed = closed
        kind = LID_ANIMATION_CLOSED if closed else LID_ANIMATION_OPEN
        log_status_bar(f"lid_state={'closed' if closed else 'open'}")
        self.play_lid_animation(kind)

    def sync_keep_awake(self, mode: AgentMode) -> None:
        was_running = self.keep_awake.process_running()
        self.keep_awake.update(mode)
        is_running = self.keep_awake.process_running()
        if was_running != is_running:
            log_status_bar(f"keep_awake={'active' if is_running else 'released'}")
        if self.keep_awake.last_error != self.last_keep_awake_error:
            self.last_keep_awake_error = self.keep_awake.last_error
            if self.last_keep_awake_error:
                log_status_bar(f"keep_awake error: {self.last_keep_awake_error}")

        self.sync_closed_lid_awake()

        if not self.leds_enabled:
            return
        read_any = False
        for target in self.status_keepalive_targets():
            status_path = self.keep_awake.poke_status_file(target)
            if status_path is not None:
                read_any = True
                log_status_bar(f"sd_keepalive touch={status_path}")
        if not read_any and self.keep_awake.last_status_error != self.last_status_read_error:
            self.last_status_read_error = self.keep_awake.last_status_error
            if self.last_status_read_error:
                log_status_bar(f"sd_keepalive error: {self.last_status_read_error}")

    def sync_closed_lid_awake(self) -> None:
        was_active = self.closed_lid_awake.active()
        self.closed_lid_awake.set_use_system_disable(
            self.settings.closed_lid_system_override_enabled
        )
        self.closed_lid_awake.update(
            self.settings.closed_lid_awake_policy,
            agents_active=self.keep_awake.holding_requested,
        )
        is_active = self.closed_lid_awake.active()
        if was_active != is_active:
            log_status_bar(
                f"closed_lid_awake={'active' if is_active else 'released'} "
                f"policy={self.settings.closed_lid_awake_policy}"
            )
        if self.closed_lid_awake.last_error != self.last_closed_lid_awake_error:
            self.last_closed_lid_awake_error = self.closed_lid_awake.last_error
            if self.last_closed_lid_awake_error:
                log_status_bar(
                    f"closed_lid_awake error: {self.last_closed_lid_awake_error}"
                )

    def status_keepalive_targets(self) -> list[Path]:
        targets = self.current_led_targets()
        if targets:
            return targets
        connected_targets = [
            device.target for device in self.status_bar_devices(remember=False) if device.connected
        ]
        if connected_targets:
            return connected_targets
        return [MOUNT_ROOT / name / KEEPALIVE_FILE_NAME for name in STATUS_BAR_KEEPALIVE_VOLUME_NAMES]


def build_menu(snapshot, state: StatusBarState, target: StatusBarController) -> NSMenu:
    menu = NSMenu.alloc().init()

    menu.addItem_(disabled_menu_item("SidePulse"))
    menu.addItem_(NSMenuItem.separatorItem())

    menu.addItem_(disabled_menu_item("Devices"))
    devices = target.status_bar_devices()
    if devices:
        for device in devices:
            menu.addItem_(build_device_menu_item(device, target))
    else:
        menu.addItem_(disabled_menu_item("No devices"))

    menu.addItem_(NSMenuItem.separatorItem())
    menu.addItem_(disabled_menu_item("Keep Awake With Lid Closed"))
    for policy in CLOSED_LID_AWAKE_CHOICES:
        menu.addItem_(build_closed_lid_awake_policy_item(policy, target))
    menu.addItem_(build_closed_lid_system_override_item(target))
    helper_title = (
        "Sleep Helper Installed"
        if sleep_helper_installed()
        else "Sleep Helper Missing"
    )
    menu.addItem_(disabled_menu_item(helper_title))
    if target.closed_lid_awake.last_error:
        menu.addItem_(disabled_menu_item(f"Sleep warning: {target.closed_lid_awake.last_error}"))

    menu.addItem_(NSMenuItem.separatorItem())
    menu.addItem_(disabled_menu_item("Agents"))

    statuses = recent_statuses(snapshot)
    if not statuses:
        menu.addItem_(disabled_menu_item("No recent sessions"))
    else:
        for status in statuses:
            menu.addItem_(build_session_menu_item(status, snapshot.collected_at, target))

    menu.addItem_(NSMenuItem.separatorItem())
    settings = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Settings...",
        "openSettings:",
        ",",
    )
    settings.setTarget_(target)
    menu.addItem_(settings)

    quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Quit",
        "quit:",
        "q",
    )
    quit_item.setTarget_(target)
    menu.addItem_(quit_item)

    return menu


def build_closed_lid_awake_policy_item(policy: str, target: StatusBarController) -> NSMenuItem:
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        CLOSED_LID_AWAKE_LABELS[policy],
        "setClosedLidAwakePolicy:",
        "",
    )
    item.setTarget_(target)
    item.setRepresentedObject_(policy)
    item.setState_(1 if target.settings.closed_lid_awake_policy == policy else 0)
    return item


def build_closed_lid_system_override_item(target: StatusBarController) -> NSMenuItem:
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Strong Sleep Override...",
        "toggleClosedLidSystemOverride:",
        "",
    )
    item.setTarget_(target)
    item.setState_(1 if target.settings.closed_lid_system_override_enabled else 0)
    return item


def build_device_menu_item(device: StatusBarDevice, target: StatusBarController) -> NSMenuItem:
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(device.name, None, "")
    item.setState_(1 if device.connected else 0)
    submenu = NSMenu.alloc().init()

    agent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Agent Status",
        "setDeviceDisplayAgent:",
        "",
    )
    agent.setTarget_(target)
    agent.setRepresentedObject_(device.device_id)
    agent.setState_(1 if device.display == LED_DISPLAY_AGENT else 0)
    submenu.addItem_(agent)

    battery = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Battery Level",
        "setDeviceDisplayBattery:",
        "",
    )
    battery.setTarget_(target)
    battery.setRepresentedObject_(device.device_id)
    battery.setState_(1 if device.display == LED_DISPLAY_BATTERY else 0)
    submenu.addItem_(battery)

    submenu.addItem_(NSMenuItem.separatorItem())
    submenu.addItem_(disabled_menu_item(f"Brightness {brightness_percent(device.brightness)}%"))
    submenu.addItem_(build_brightness_slider_item(device, target))

    if not device.connected:
        submenu.addItem_(NSMenuItem.separatorItem())
        submenu.addItem_(disabled_menu_item("Not connected"))
        remove = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Remove",
            "removeRememberedDevice:",
            "",
        )
        remove.setTarget_(target)
        remove.setRepresentedObject_(device.device_id)
        submenu.addItem_(remove)

    item.setSubmenu_(submenu)
    return item


def build_brightness_slider_item(
    device: StatusBarDevice,
    target: StatusBarController,
) -> NSMenuItem:
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, "")
    view = NSView.alloc().initWithFrame_(((0, 0), (230, 34)))
    slider = NSSlider.alloc().initWithFrame_(((14, 6), (202, 22)))
    slider.setMinValue_(0.0)
    slider.setMaxValue_(255.0)
    slider.setDoubleValue_(float(normalize_brightness(device.brightness)))
    slider.setContinuous_(False)
    slider.setTarget_(target)
    slider.setAction_("setDeviceBrightness:")
    slider.setIdentifier_(device.device_id)
    view.addSubview_(slider)
    item.setView_(view)
    return item


def build_settings_window(target: StatusBarController) -> NSWindow:
    width = 680
    height = 840
    style = (
        NSWindowStyleMaskTitled
        | NSWindowStyleMaskClosable
        | NSWindowStyleMaskMiniaturizable
    )
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        ((0, 0), (width, height)),
        style,
        NSBackingStoreBuffered,
        False,
    )
    window.setTitle_("SidePulse Agent Monitor Settings")
    window.center()
    content = window.contentView()

    add_label(content, "Agent Hooks", 24, 792, 200, 24)
    add_label(content, "Codex", 32, 750, 80, 22)
    codex_status = add_label(content, "", 112, 750, 270, 22)
    add_button(content, "Install", 432, 746, 90, 28, target, "installCodexHooks:")
    add_button(content, "Uninstall", 532, 746, 100, 28, target, "uninstallCodexHooks:")

    add_label(content, "Claude", 32, 708, 80, 22)
    claude_status = add_label(content, "", 112, 708, 270, 22)
    add_button(content, "Install", 432, 704, 90, 28, target, "installClaudeHooks:")
    add_button(content, "Uninstall", 532, 704, 100, 28, target, "uninstallClaudeHooks:")

    add_separator(content, 24, 676, width - 48)
    add_label(content, "Transcript Monitoring", 24, 642, 240, 24)
    codex_transcripts = add_checkbox(
        content,
        "CLI fallback: Codex transcripts",
        32,
        608,
        260,
        24,
        target,
        "toggleCodexTranscripts:",
    )
    claude_transcripts = add_checkbox(
        content,
        "CLI fallback: Claude transcripts",
        32,
        576,
        260,
        24,
        target,
        "toggleClaudeTranscripts:",
    )

    add_separator(content, 24, 556, width - 48)
    add_label(content, "LED Display", 24, 522, 240, 24)
    battery_leds = add_checkbox(
        content,
        "Show battery on LEDs",
        32,
        488,
        260,
        24,
        target,
        "setBatteryLedDisplayFromCheckbox:",
    )
    battery_power_preview = add_checkbox(
        content,
        "Show battery for 7s on plug/unplug",
        32,
        456,
        320,
        24,
        target,
        "setBatteryPowerPreviewFromCheckbox:",
    )

    add_separator(content, 24, 436, width - 48)
    add_label(content, "Lid & Sleep", 24, 402, 240, 24)
    closed_lid_system_override = add_checkbox(
        content,
        "Strong sleep override (requires helper)",
        300,
        402,
        300,
        24,
        target,
        "setClosedLidSystemOverrideFromCheckbox:",
    )
    add_label(content, "Lid Closed", 32, 368, 120, 22)
    add_label(content, "Duration", 520, 368, 70, 22)
    closed_duration = add_editable_field(content, "", 590, 366, 48, 24)
    closed_program = add_text_view(content, "", 32, 276, 606, 82)
    add_button(content, "Preview", 32, 238, 90, 28, target, "previewLidClosedAnimation:")
    add_button(content, "Reset", 132, 238, 90, 28, target, "resetLidClosedAnimation:")

    add_label(content, "Lid Open", 32, 202, 120, 22)
    add_label(content, "Duration", 520, 202, 70, 22)
    open_duration = add_editable_field(content, "", 590, 200, 48, 24)
    open_program = add_text_view(content, "", 32, 110, 606, 82)
    add_button(content, "Preview", 32, 72, 90, 28, target, "previewLidOpenAnimation:")
    add_button(content, "Reset", 132, 72, 90, 28, target, "resetLidOpenAnimation:")
    add_button(content, "Save Animations", 492, 72, 146, 28, target, "saveLidAnimations:")

    message = add_label(content, "", 24, 34, width - 48, 22)
    settings_path = add_label(content, "", 24, 12, width - 48, 18)

    target.settings_fields = {
        "codex_hook_status": codex_status,
        "claude_hook_status": claude_status,
        "closed_animation_program": closed_program,
        "closed_animation_duration": closed_duration,
        "open_animation_program": open_program,
        "open_animation_duration": open_duration,
        "message": message,
        "settings_path": settings_path,
    }
    target.settings_buttons = {
        "codex_transcripts": codex_transcripts,
        "claude_transcripts": claude_transcripts,
        "battery_leds": battery_leds,
        "battery_power_preview": battery_power_preview,
        "closed_lid_system_override": closed_lid_system_override,
    }
    return window


def add_label(parent, text: str, x: int, y: int, width: int, height: int):
    label = NSTextField.alloc().initWithFrame_(((x, y), (width, height)))
    label.setStringValue_(text)
    label.setBezeled_(False)
    label.setDrawsBackground_(False)
    label.setEditable_(False)
    label.setSelectable_(False)
    parent.addSubview_(label)
    return label


def add_button(
    parent,
    title: str,
    x: int,
    y: int,
    width: int,
    height: int,
    target: StatusBarController,
    selector: str,
):
    button = NSButton.alloc().initWithFrame_(((x, y), (width, height)))
    button.setTitle_(title)
    button.setBezelStyle_(NSBezelStyleRounded)
    button.setTarget_(target)
    button.setAction_(selector)
    parent.addSubview_(button)
    return button


def add_checkbox(
    parent,
    title: str,
    x: int,
    y: int,
    width: int,
    height: int,
    target: StatusBarController,
    selector: str,
):
    checkbox = NSButton.alloc().initWithFrame_(((x, y), (width, height)))
    checkbox.setButtonType_(NSButtonTypeSwitch)
    checkbox.setTitle_(title)
    checkbox.setTarget_(target)
    checkbox.setAction_(selector)
    parent.addSubview_(checkbox)
    return checkbox


def add_editable_field(parent, text: str, x: int, y: int, width: int, height: int):
    field = NSTextField.alloc().initWithFrame_(((x, y), (width, height)))
    field.setStringValue_(text)
    field.setEditable_(True)
    field.setSelectable_(True)
    parent.addSubview_(field)
    return field


def add_text_view(parent, text: str, x: int, y: int, width: int, height: int):
    scroll = NSScrollView.alloc().initWithFrame_(((x, y), (width, height)))
    scroll.setHasVerticalScroller_(True)
    scroll.setHasHorizontalScroller_(False)
    text_view = NSTextView.alloc().initWithFrame_(((0, 0), (width, height)))
    text_view.setString_(text)
    text_view.setVerticallyResizable_(True)
    text_view.setHorizontallyResizable_(False)
    try:
        text_view.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11.0, 0.0))
    except Exception:
        pass
    scroll.setDocumentView_(text_view)
    parent.addSubview_(scroll)
    return text_view


def add_separator(parent, x: int, y: int, width: int):
    separator = NSTextField.alloc().initWithFrame_(((x, y), (width, 1)))
    separator.setStringValue_("")
    separator.setBezeled_(False)
    separator.setEditable_(False)
    separator.setDrawsBackground_(True)
    parent.addSubview_(separator)
    return separator


def set_field_value(field, value: str) -> None:
    if field is not None:
        field.setStringValue_(value)


def set_text_control_value(control, value: str) -> None:
    if control is None:
        return
    if hasattr(control, "setString_"):
        control.setString_(value)
    else:
        control.setStringValue_(value)


def text_control_value(control) -> str:
    if control is None:
        return ""
    if hasattr(control, "string"):
        return str(control.string())
    return str(control.stringValue())


def set_checkbox_state(button, enabled: bool) -> None:
    if button is not None:
        button.setState_(NSOnState if enabled else NSOffState)


def validate_lid_animation(animation: LedAnimationSetting) -> None:
    program = normalize_led_text(animation.program)
    validate_led_text(program)
    validate_led_text(apply_brightness(program, 1))
    normalize_animation_duration(animation.duration_seconds)


def program_for_lid_animation(
    animation: LedAnimationSetting,
    *,
    brightness: int | float = 255,
) -> str:
    validate_lid_animation(animation)
    return apply_brightness(normalize_led_text(animation.program), brightness)


def restore_led_display(target, token_value) -> None:
    try:
        token = int(str(token_value))
    except ValueError:
        token = target.led_animation_token
    if token != target.led_animation_token:
        return
    target.led_animation_until_monotonic = 0.0
    target.reset_led_controllers_for_display_change()
    if target.last_snapshot is not None:
        target.sync_leds(
            target.last_snapshot.aggregate.mode,
            target.last_battery_snapshot,
            target.active_led_display_kind(target.last_battery_snapshot),
        )
    else:
        target.refresh_(None)


def hook_status_text(config: ProviderConfig) -> str:
    if not config.exists:
        return f"Not installed - config will be created at {config.config_path}"
    if config.hook_events:
        event_count = len(config.hook_events)
        suffix = "event" if event_count == 1 else "events"
        return f"Installed ({event_count} {suffix})"
    return "Not installed"


def device_id_for_root(root: Path) -> str:
    return str(root.expanduser())


def device_display_name(name: str) -> str:
    normalized = normalized_device_name(name)
    if "pulsedot" in normalized or "pixiedot" in normalized:
        return "PulseDot"
    if "sidepulse" in normalized or "pixiepulse" in normalized:
        return "SidePulse"
    return name or "SidePulse Device"


def device_display_label(display: str) -> str:
    if display == LED_DISPLAY_BATTERY:
        return "Battery Level"
    return "Agent Status"


def disabled_menu_item(title: str) -> NSMenuItem:
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
    item.setEnabled_(False)
    return item


def disambiguate_device_names(devices: list[StatusBarDevice]) -> list[StatusBarDevice]:
    counts: dict[str, int] = {}
    for device in devices:
        counts[device.name] = counts.get(device.name, 0) + 1
    if all(count == 1 for count in counts.values()):
        return devices

    result: list[StatusBarDevice] = []
    for device in devices:
        if counts.get(device.name, 0) <= 1:
            result.append(device)
            continue
        suffix = duplicate_device_suffix(device)
        result.append(
            StatusBarDevice(
                device_id=device.device_id,
                name=f"{device.name} {suffix}" if suffix else device.name,
                root=device.root,
                target=device.target,
                connected=device.connected,
                display=device.display,
                brightness=device.brightness,
                reason=device.reason,
            )
        )
    return result


def duplicate_device_suffix(device: StatusBarDevice) -> str:
    root_name = device.root.name
    normalized_root = normalized_device_name(root_name)
    normalized_name = normalized_device_name(device.name)
    if normalized_root.startswith(normalized_name):
        suffix = root_name[len(device.name) :].strip()
        return suffix
    return root_name


def preferred_status_bar_device(candidates: list[DeviceCandidate]) -> DeviceCandidate:
    return sorted(candidates, key=status_bar_device_sort_key)[0]


def status_bar_device_sort_key(candidate: DeviceCandidate) -> tuple[int, str]:
    name = normalized_device_name(candidate.root.name)
    for index, hint in enumerate(STATUS_BAR_DEVICE_PRIORITY):
        if hint in name:
            return (index, candidate.root.name.lower())
    return (len(STATUS_BAR_DEVICE_PRIORITY), candidate.root.name.lower())


def build_session_menu_item(
    status: AgentStatus,
    now: datetime,
    target: StatusBarController,
) -> NSMenuItem:
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        menu_title_for_status(status, now),
        None,
        "",
    )
    item.setImage_(provider_icon_for_status(status))
    submenu = NSMenu.alloc().init()
    submenu.addItem_(disabled_menu_item(flatten_menu_title(menu_title_for_status(status, now))))
    submenu.addItem_(disabled_menu_item(session_detail_for_status(status, now)))
    submenu.addItem_(NSMenuItem.separatorItem())

    selected_action = getattr(target, "settings", None)
    if selected_action is not None:
        selected = target.settings.session_open_action(status.provider)
    else:
        selected = None
    selected = selected or default_session_open_action(status)
    for action in available_session_open_actions(status):
        add_session_open_action_item(
            submenu,
            session_open_action_label(status, action),
            status,
            action,
            target,
            selected=action == selected,
        )
    item.setSubmenu_(submenu)
    item.setEnabled_(True)
    return item


def add_session_open_action_item(
    menu: NSMenu,
    title: str,
    status: AgentStatus,
    action: str,
    target: StatusBarController,
    *,
    selected: bool,
) -> None:
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        title,
        "openSessionWithAction:",
        "",
    )
    item.setTarget_(target)
    item.setRepresentedObject_({"status": status, "action": action})
    item.setState_(1 if selected else 0)
    menu.addItem_(item)


def provider_icon_for_status(status: AgentStatus):
    provider = status.provider.lower()
    if provider == "codex":
        for path in ("/Applications/Codex.app", "/Applications/ChatGPT.app"):
            image = app_icon(path)
            if image is not None:
                return image
        return image_for_symbol("sparkles", "Codex")
    if provider == "claude":
        image = app_icon("/Applications/Claude.app")
        if image is not None:
            return image
        return image_for_symbol("brain.head.profile", "Claude")
    return image_for_symbol("terminal", status.provider.title() or "Agent")


def app_icon(path: str):
    if not Path(path).exists():
        return None
    image = NSWorkspace.sharedWorkspace().iconForFile_(path)
    if image is not None:
        image.setSize_((18, 18))
    return image


def flatten_menu_title(title: str) -> str:
    return " · ".join(part.strip() for part in title.splitlines() if part.strip())


def add_action_item(
    menu: NSMenu,
    title: str,
    selector: str,
    represented_object: str | None,
    target: StatusBarController,
) -> None:
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        title if represented_object else f"{title} unavailable",
        selector,
        "",
    )
    item.setTarget_(target)
    item.setEnabled_(represented_object is not None)
    if represented_object is not None:
        item.setRepresentedObject_(represented_object)
    menu.addItem_(item)


def build_error_menu(exc: Exception) -> NSMenu:
    menu = NSMenu.alloc().init()
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"Agent monitor error: {exc}",
        None,
        "",
    )
    item.setEnabled_(False)
    menu.addItem_(item)
    return menu


def recent_statuses(snapshot) -> list[AgentStatus]:
    statuses = list(snapshot.statuses)
    statuses.sort(key=lambda status: (status.priority, -status.updated_at.timestamp()))
    return statuses[:12]


def menu_title_for_status(status: AgentStatus, now: datetime) -> str:
    state = state_for_mode(status.mode)
    title, project = session_title_parts(status)
    first_line = f"{state.label}  {title}"
    if project:
        return f"{first_line}\n{project}"
    return first_line


def session_detail_for_status(status: AgentStatus, now: datetime) -> str:
    state = state_for_mode(status.mode)
    age = format_age(status.age_seconds(now))
    details = [state.label, age]
    if status.tool_name:
        details.append(status.tool_name)
    return " · ".join(details)


def session_title_parts(status: AgentStatus) -> tuple[str, str | None]:
    project = project_name_from_cwd(status.cwd)
    title = strip_session_short_id(status.display_name, status.session_id)
    if project and title.startswith(f"{project}: "):
        title = title[len(project) + 2 :]
    elif ": " in title:
        maybe_project, maybe_title = title.split(": ", 1)
        if not project:
            project = maybe_project
        title = maybe_title
    return title or status.display_name, project


def strip_session_short_id(display_name: str, session_id: str | None) -> str:
    text = display_name.strip()
    if session_id:
        suffix = f" ({session_id[:8]})"
        if text.endswith(suffix):
            return text[: -len(suffix)].strip()
    if text.endswith(")") and " (" in text:
        prefix, suffix = text.rsplit(" (", 1)
        token = suffix[:-1]
        if 6 <= len(token) <= 12 and all(char.isalnum() or char == "-" for char in token):
            return prefix.strip()
    return text


def project_name_from_cwd(cwd: str | None) -> str | None:
    if not cwd:
        return None
    name = Path(cwd).name
    return name or cwd


def image_for_symbol(symbol: str, description: str):
    try:
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            symbol,
            description,
        )
        if image is not None:
            image.setTemplate_(True)
    except Exception:
        image = None
    return image


def log_status_bar(message: str) -> None:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"{timestamp} {message}", flush=True)


def compact_path(path: str, max_len: int = 48) -> str:
    if len(path) <= max_len:
        return path
    keep = max_len - 1
    return "." + path[-keep:]


def format_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def open_url(url: str) -> None:
    ns_url = NSURL.URLWithString_(url)
    if ns_url is not None:
        NSWorkspace.sharedWorkspace().openURL_(ns_url)


def open_terminal_command(command: str) -> None:
    script = "\n".join(
        [
            'tell application "Terminal"',
            "  activate",
            f"  do script {applescript_quote(command)}",
            "end tell",
        ]
    )
    subprocess.Popen(
        ["/usr/bin/osascript", "-e", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def applescript_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_status_bar() -> None:
    app = NSApplication.sharedApplication()
    controller = StatusBarController.alloc().init()
    app.setDelegate_(controller)
    app.run()


def main() -> int:
    run_status_bar()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
