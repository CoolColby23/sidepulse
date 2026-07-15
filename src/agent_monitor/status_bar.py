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
        NSImage,
        NSMenu,
        NSMenuItem,
        NSOffState,
        NSOnState,
        NSStatusBar,
        NSTextField,
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
    battery_state_label,
    format_watts,
    read_battery_snapshot,
)
from .collector import LiveAgentMonitor, SourceSpec, read_recent_lines
from .device_writer import (
    DEFAULT_FILE_NAME,
    MOUNT_ROOT,
    DeviceCandidate,
    discover_devices,
    path_exists,
    target_from_device_path,
)
from .keep_awake import KEEPALIVE_FILE_NAME, KeepAwakeController
from .ipc import HookEventServer, default_event_socket_path, default_latest_state_path
from .install import (
    install_claude_hooks,
    install_codex_hooks,
    uninstall_claude_hooks,
    uninstall_codex_hooks,
)
from .led_status import AgentLedController, normalized_device_name, write_mode_to_leds
from .models import AgentMode, AgentStatus
from .providers import (
    ProviderConfig,
    detect_claude_config,
    detect_codex_config,
    detect_log_path,
    parse_log_line,
)
from .session_actions import session_deep_link, session_resume_command
from .settings import (
    LED_DISPLAY_AGENT,
    LED_DISPLAY_BATTERY,
    default_settings_path,
    load_settings,
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
        self.last_keep_awake_error = None
        self.last_status_read_error = None
        self.event_refresh_pending = False
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
        ns_url = NSURL.URLWithString_(str(url))
        if ns_url is not None:
            NSWorkspace.sharedWorkspace().openURL_(ns_url)

    @objc.IBAction
    def resumeSession_(self, sender):
        command = sender.representedObject()
        if command:
            open_terminal_command(str(command))

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
    def setDeviceDisplayAgent_(self, sender):
        self.set_device_display(sender.representedObject(), LED_DISPLAY_AGENT)

    @objc.IBAction
    def setDeviceDisplayBattery_(self, sender):
        self.set_device_display(sender.representedObject(), LED_DISPLAY_BATTERY)

    @objc.IBAction
    def quit_(self, _sender):
        self.keep_awake.release()
        NSApp.terminate_(self)

    def applicationWillTerminate_(self, _notification):
        self.stop_event_server()
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
        return controller

    def battery_controller_for_device(self, device: StatusBarDevice) -> BatteryLedController:
        controller = self.battery_led_controllers_by_device.get(device.device_id)
        if controller is None:
            controller = BatteryLedController(device_path=device.target)
            self.battery_led_controllers_by_device[device.device_id] = controller
        controller.device_path = device.target
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

    title = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"Agent Status: {state.label}",
        None,
        "",
    )
    title.setEnabled_(False)
    menu.addItem_(title)

    updated = snapshot.collected_at.astimezone().strftime("%H:%M:%S")
    counts = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"{snapshot.aggregate.active_count} active, {snapshot.aggregate.stale_count} stale - {updated}",
        None,
        "",
    )
    counts.setEnabled_(False)
    menu.addItem_(counts)
    menu.addItem_(NSMenuItem.separatorItem())

    devices_title = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Devices",
        None,
        "",
    )
    devices_title.setEnabled_(False)
    menu.addItem_(devices_title)

    device_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "LED Output",
        "toggleDeviceConnection:",
        "",
    )
    device_item.setTarget_(target)
    device_item.setState_(1 if target.device_connected() else 0)
    menu.addItem_(device_item)

    detail = device_connection_detail(target)
    if detail:
        detail_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            detail,
            None,
            "",
        )
        detail_item.setEnabled_(False)
        menu.addItem_(detail_item)

    devices = target.status_bar_devices()
    if devices:
        connected_devices = [device for device in devices if device.connected]
        previous_devices = [device for device in devices if not device.connected]
        if connected_devices:
            menu.addItem_(disabled_menu_item("Connected"))
        for device in connected_devices:
            menu.addItem_(build_device_menu_item(device, target))
        if previous_devices:
            menu.addItem_(disabled_menu_item("Previously Connected"))
        for device in previous_devices:
            menu.addItem_(build_device_menu_item(device, target))
    else:
        no_devices = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "No SidePulse or PulseDot devices seen",
            None,
            "",
        )
        no_devices.setEnabled_(False)
        menu.addItem_(no_devices)

    menu.addItem_(NSMenuItem.separatorItem())

    keep_awake_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Keep Awake",
        "toggleKeepAwake:",
        "",
    )
    keep_awake_item.setTarget_(target)
    keep_awake_item.setState_(1 if target.keep_awake.enabled else 0)
    menu.addItem_(keep_awake_item)

    keep_awake_detail_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        target.keep_awake.detail(),
        None,
        "",
    )
    keep_awake_detail_item.setEnabled_(False)
    menu.addItem_(keep_awake_detail_item)

    battery_preview_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Show Battery on Plug/Unplug",
        "toggleBatteryPowerPreview:",
        "",
    )
    battery_preview_item.setTarget_(target)
    battery_preview_item.setState_(1 if target.settings.battery_show_on_power_change else 0)
    menu.addItem_(battery_preview_item)

    battery_detail = battery_status_detail(target)
    if battery_detail:
        battery_detail_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            battery_detail,
            None,
            "",
        )
        battery_detail_item.setEnabled_(False)
        menu.addItem_(battery_detail_item)

    menu.addItem_(NSMenuItem.separatorItem())

    statuses = recent_statuses(snapshot)
    if not statuses:
        empty = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "No recent sessions",
            None,
            "",
        )
        empty.setEnabled_(False)
        menu.addItem_(empty)
    else:
        for status in statuses:
            menu.addItem_(build_session_menu_item(status, snapshot.collected_at, target))

    menu.addItem_(NSMenuItem.separatorItem())
    sources_title = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Sources",
        None,
        "",
    )
    sources_title.setEnabled_(False)
    menu.addItem_(sources_title)
    for source in snapshot.sources:
        marker = "OK" if source.path.exists() else "Missing"
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"{marker}  {source.provider}: {source.path}",
            None,
            "",
        )
        item.setEnabled_(False)
        menu.addItem_(item)

    menu.addItem_(NSMenuItem.separatorItem())
    settings = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Settings...",
        "openSettings:",
        ",",
    )
    settings.setTarget_(target)
    menu.addItem_(settings)

    refresh = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Refresh",
        "forceRefresh:",
        "r",
    )
    refresh.setTarget_(target)
    menu.addItem_(refresh)

    quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Quit Agent Status",
        "quit:",
        "q",
    )
    quit_item.setTarget_(target)
    menu.addItem_(quit_item)

    return menu


def build_device_menu_item(device: StatusBarDevice, target: StatusBarController) -> NSMenuItem:
    title = f"{device.name} - {device_display_label(device.display)}"
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
    item.setImage_(
        image_for_symbol(
            device_display_symbol(device.display),
            device_display_label(device.display),
        )
    )
    submenu = NSMenu.alloc().init()

    status_text = "Connected" if device.connected else "Not Connected"
    submenu.addItem_(disabled_menu_item(status_text))
    location_text = f"Mounted at {device.root}" if device.connected else f"Last seen at {device.root}"
    submenu.addItem_(disabled_menu_item(location_text))

    if device.connected and device.reason:
        submenu.addItem_(disabled_menu_item(f"Detected by {device.reason}"))

    submenu.addItem_(NSMenuItem.separatorItem())
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

    item.setSubmenu_(submenu)
    return item


def build_settings_window(target: StatusBarController) -> NSWindow:
    width = 600
    height = 460
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

    add_label(content, "Agent Hooks", 24, 412, 200, 24)
    add_label(content, "Codex", 32, 370, 80, 22)
    codex_status = add_label(content, "", 112, 370, 230, 22)
    add_button(content, "Install", 352, 366, 90, 28, target, "installCodexHooks:")
    add_button(content, "Uninstall", 452, 366, 100, 28, target, "uninstallCodexHooks:")

    add_label(content, "Claude", 32, 328, 80, 22)
    claude_status = add_label(content, "", 112, 328, 230, 22)
    add_button(content, "Install", 352, 324, 90, 28, target, "installClaudeHooks:")
    add_button(content, "Uninstall", 452, 324, 100, 28, target, "uninstallClaudeHooks:")

    add_separator(content, 24, 296, width - 48)
    add_label(content, "Transcript Monitoring", 24, 262, 240, 24)
    codex_transcripts = add_checkbox(
        content,
        "CLI fallback: Codex transcripts",
        32,
        228,
        260,
        24,
        target,
        "toggleCodexTranscripts:",
    )
    claude_transcripts = add_checkbox(
        content,
        "CLI fallback: Claude transcripts",
        32,
        196,
        260,
        24,
        target,
        "toggleClaudeTranscripts:",
    )

    add_separator(content, 24, 176, width - 48)
    add_label(content, "LED Display", 24, 142, 240, 24)
    battery_leds = add_checkbox(
        content,
        "Show battery on LEDs",
        32,
        108,
        260,
        24,
        target,
        "setBatteryLedDisplayFromCheckbox:",
    )
    battery_power_preview = add_checkbox(
        content,
        "Show battery for 7s on plug/unplug",
        32,
        76,
        320,
        24,
        target,
        "setBatteryPowerPreviewFromCheckbox:",
    )

    add_separator(content, 24, 56, width - 48)
    message = add_label(content, "", 24, 30, width - 48, 22)
    settings_path = add_label(content, "", 24, 8, width - 48, 18)

    target.settings_fields = {
        "codex_hook_status": codex_status,
        "claude_hook_status": claude_status,
        "message": message,
        "settings_path": settings_path,
    }
    target.settings_buttons = {
        "codex_transcripts": codex_transcripts,
        "claude_transcripts": claude_transcripts,
        "battery_leds": battery_leds,
        "battery_power_preview": battery_power_preview,
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


def set_checkbox_state(button, enabled: bool) -> None:
    if button is not None:
        button.setState_(NSOnState if enabled else NSOffState)


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


def device_display_symbol(display: str) -> str:
    if display == LED_DISPLAY_BATTERY:
        return "battery.100"
    return "waveform.path.ecg"


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


def device_connection_detail(target: StatusBarController) -> str:
    if not target.leds_enabled:
        return "LED output disabled"
    if target.last_led_error:
        return f"Device error: {target.last_led_error}"
    if target.led_controller.last_error:
        return f"Device error: {target.led_controller.last_error}"
    if target.battery_led_controller.last_error:
        return f"Device error: {target.battery_led_controller.last_error}"
    devices = target.status_bar_devices(remember=False)
    connected = [device for device in devices if device.connected]
    previous_count = max(0, len(devices) - len(connected))
    if connected:
        suffix = "device" if len(connected) == 1 else "devices"
        if previous_count:
            return f"{len(connected)} connected {suffix}, {previous_count} remembered"
        return f"{len(connected)} connected {suffix}"
    if previous_count:
        suffix = "device" if previous_count == 1 else "devices"
        return f"No devices connected, {previous_count} remembered {suffix}"
    return "No devices connected"


def battery_status_detail(target: StatusBarController) -> str:
    if target.last_battery_error:
        return f"Battery error: {target.last_battery_error}"
    snapshot = target.last_battery_snapshot
    if snapshot is None:
        return ""
    state = battery_state_label(snapshot)
    if snapshot.is_plugged:
        return (
            f"Battery: {snapshot.percent}% {state}, "
            f"adapter {format_watts(snapshot.adapter_power)}"
        )
    return f"Battery: {snapshot.percent}% {state}"


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
    item.setEnabled_(True)

    submenu = NSMenu.alloc().init()
    add_action_item(
        submenu,
        "Deep Link",
        "openDeepLink:",
        session_deep_link(status),
        target,
    )
    add_action_item(
        submenu,
        "Resume",
        "resumeSession:",
        session_resume_command(status),
        target,
    )
    item.setSubmenu_(submenu)
    return item


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
    age = format_age(status.age_seconds(now))
    tool = f" - {status.tool_name}" if status.tool_name else ""
    cwd = compact_path(status.cwd) if status.cwd else "-"
    return f"{state.label:7}  {status.display_name}  {age}  {status.event_name}{tool}  {cwd}"


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
