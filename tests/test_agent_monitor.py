from __future__ import annotations

import json
import os
import plistlib
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent_monitor.battery import (
    BATTERY_CHARGING_MINT,
    BatteryLedController,
    BatterySnapshot,
    parse_ioreg_battery_plist,
    program_for_battery,
)
from agent_monitor import collector as collector_module
from agent_monitor import cli as cli_module
from agent_monitor.collector import (
    AgentMonitor,
    LiveAgentMonitor,
    MonitorSnapshot,
    SourceSpec,
    default_sources,
)
from agent_monitor.cli import build_parser, visible_watch_statuses
from agent_monitor.device_writer import (
    DeviceWriteError,
    discover_devices,
    normalize_led_text,
    validate_led_text,
    write_led_program,
)
from agent_monitor.hook import write_hook_payload
from agent_monitor.ipc import HookEventServer, send_hook_event
from agent_monitor.install import (
    install_claude_hooks,
    install_codex_hooks,
    uninstall_claude_hooks,
    uninstall_codex_hooks,
    update_codex_trusted_hashes,
)
from agent_monitor.keep_awake import KeepAwakeController, status_file_for_target
from agent_monitor.led_status import (
    AgentLedController,
    LedDisplayState,
    display_state_for_mode,
    led_count_for_target,
    program_for_display_state,
    write_mode_to_leds,
)
from agent_monitor.models import AgentMode, AgentStatus, AggregateStatus
from agent_monitor.providers import default_log_path, default_state_dir, parse_log_line
from agent_monitor.session_actions import session_deep_link, session_resume_command
from agent_monitor.settings import (
    AgentMonitorSettings,
    DeviceDisplaySetting,
    default_config_dir,
    default_settings_path,
    load_settings,
    save_settings,
)
from agent_monitor.status_bar_launch import LAUNCH_AGENT_LABEL, build_launch_agent_plist


class FakeProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def poll(self):
        return 0 if self.terminated or self.killed else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self) -> None:
        self.killed = True


class AgentMonitorTests(unittest.TestCase):
    def test_aggregates_highest_priority_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            codex = base / "codex.jsonl"
            claude = base / "claude.jsonl"

            codex.write_text(
                json.dumps(
                    {
                        "logged_at": "2026-06-20T06:00:00Z",
                        "event": {
                            "hook_event_name": "PreToolUse",
                            "session_id": "codex-session",
                            "tool_name": "Bash",
                        },
                    }
                )
                + "\n"
            )
            claude.write_text(
                json.dumps(
                    {
                        "logged_at": "2026-06-20T06:00:01Z",
                        "hook_event_name": "Notification",
                        "session_id": "claude-session",
                        "notification_type": "idle_prompt",
                        "message": "Claude is waiting for your input",
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", codex), SourceSpec("claude", claude)),
                stale_after_seconds=999999999,
                tool_running_timeout_seconds=0,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)
            self.assertEqual(len(snapshot.statuses), 2)

    def test_hook_log_writes_provider_formats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            codex = base / "codex.jsonl"
            claude = base / "claude.jsonl"

            write_hook_payload(
                "codex",
                codex,
                '{"hook_event_name":"Stop","session_id":"abc"}',
            )
            write_hook_payload(
                "claude",
                claude,
                '{"hook_event_name":"Stop","session_id":"xyz"}',
            )

            codex_obj = json.loads(codex.read_text())
            claude_obj = json.loads(claude.read_text())

            self.assertIn("event", codex_obj)
            self.assertEqual(codex_obj["event"]["session_id"], "abc")
            self.assertNotIn("event", claude_obj)
            self.assertEqual(claude_obj["session_id"], "xyz")
            self.assertTrue(
                datetime.fromisoformat(codex_obj["logged_at"].replace("Z", "+00:00")).tzinfo
                is not None
            )

    def test_hook_event_server_receives_socket_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            received: list[tuple[str, dict]] = []
            server = HookEventServer(
                lambda provider, line: received.append((provider, line)),
                socket_path=Path(tmp) / "events.sock",
            )
            try:
                server.start()
                sent = send_hook_event(
                    "codex",
                    {
                        "logged_at": "2026-06-20T06:00:00Z",
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "Done.",
                        },
                    },
                    socket_path=server.socket_path,
                    timeout=0.5,
                )

                deadline = time.time() + 1
                while sent and not received and time.time() < deadline:
                    time.sleep(0.01)

                self.assertTrue(sent)
                self.assertTrue(received)
                self.assertEqual(received[0][0], "codex")
                self.assertEqual(
                    received[0][1]["event"]["hook_event_name"],
                    "Stop",
                )
            finally:
                server.stop()

    def test_live_agent_monitor_ingests_events_and_persists_latest_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            latest = base / "latest.json"
            source = SourceSpec("event-bus", base / "events.sock")
            monitor = LiveAgentMonitor(
                sources=(source,),
                stale_after_seconds=3600,
                latest_state_path=latest,
            )
            line = {
                "logged_at": datetime.now(timezone.utc).isoformat(),
                "event": {
                    "hook_event_name": "PreToolUse",
                    "session_id": "codex-session",
                    "cwd": "/tmp/project",
                    "tool_name": "Bash",
                },
            }
            record = parse_log_line("codex", json.dumps(line))

            self.assertIsNotNone(record)
            monitor.ingest_record(record)
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.TOOL_RUNNING)
            self.assertEqual(snapshot.statuses[0].tool_name, "Bash")
            self.assertTrue(latest.exists())

            reloaded = LiveAgentMonitor(
                sources=(source,),
                stale_after_seconds=3600,
                latest_state_path=latest,
            )
            self.assertEqual(reloaded.snapshot().aggregate.mode, AgentMode.TOOL_RUNNING)

    def test_status_bar_startup_replay_ingests_recent_debug_logs(self) -> None:
        try:
            from agent_monitor import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            log = base / "codex.jsonl"
            session_id = "eeeeeeee-ffff-7aaa-8bbb-cccccccccccc"
            log.write_text(
                json.dumps(
                    {
                        "logged_at": datetime.now(timezone.utc).isoformat(),
                        "event": {
                            "hook_event_name": "UserPromptSubmit",
                            "session_id": session_id,
                            "cwd": "/tmp/project",
                            "prompt": "startup replay should restore this",
                        },
                    }
                )
                + "\n"
            )
            monitor = LiveAgentMonitor()

            with patch(
                "agent_monitor.status_bar.detect_log_path",
                return_value=log,
            ):
                replayed = status_bar.replay_recent_debug_logs(
                    monitor,
                    providers=("codex",),
                    max_lines=20,
                )

            snapshot = monitor.snapshot()
            self.assertEqual(replayed, 1)
            self.assertEqual(snapshot.aggregate.mode, AgentMode.WORKING)
            self.assertIn("startup replay", snapshot.statuses[0].display_name)

    def test_codex_installer_replaces_monitor_hook_and_preserves_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "config.toml"
            log = base / "codex.jsonl"
            config.write_text(
                "\n".join(
                    [
                        '[features]',
                        'js_repl = false',
                        '',
                        '[[hooks.PreToolUse]]',
                        '[[hooks.PreToolUse.hooks]]',
                        'type = "command"',
                        f"command = '''echo old >> {log}'''",
                        '',
                        '[hooks.state]',
                        'source = "keep-me"',
                        '',
                    ]
                )
            )

            result = install_codex_hooks(
                log_path=log,
                config_path=config,
                python_executable="python3",
            )

            self.assertTrue(result.changed)
            text = config.read_text()
            self.assertIn("hooks = true", text)
            self.assertIn('[hooks.state]', text)
            self.assertIn('source = "keep-me"', text)
            self.assertIn("hook_entry.py", text)
            self.assertIn("--provider codex", text)
            self.assertIn(str(log), text)
            self.assertNotIn("echo old", text)

    def test_codex_installer_refreshes_managed_hook_trust_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "config.toml"
            log = base / "codex.jsonl"
            key = f"{config}:pre_tool_use:0:0"
            config.write_text("[features]\nhooks = true\n")

            with patch("agent_monitor.install.should_refresh_codex_hook_trust", return_value=True):
                with patch(
                    "agent_monitor.install.resolve_codex_hook_hashes",
                    return_value={key: "sha256:new-current-hash"},
                ):
                    result = install_codex_hooks(
                        log_path=log,
                        config_path=config,
                        python_executable="python3",
                    )

            self.assertTrue(result.changed)
            text = config.read_text()
            self.assertIn(f'[hooks.state."{key}"]', text)
            self.assertIn('trusted_hash = "sha256:new-current-hash"', text)

    def test_update_codex_trusted_hashes_preserves_other_state(self) -> None:
        text = "\n".join(
            [
                "[hooks.state]",
                'source = "keep-me"',
                "",
                '[hooks.state."/tmp/config.toml:pre_tool_use:0:0"]',
                'trusted_hash = "sha256:old"',
                "",
            ]
        )

        updated = update_codex_trusted_hashes(
            text,
            {
                "/tmp/config.toml:pre_tool_use:0:0": "sha256:new",
                "/tmp/config.toml:stop:0:0": "sha256:stop",
            },
        )

        self.assertIn('source = "keep-me"', updated)
        self.assertIn('trusted_hash = "sha256:new"', updated)
        self.assertIn('[hooks.state."/tmp/config.toml:stop:0:0"]', updated)
        self.assertIn('trusted_hash = "sha256:stop"', updated)
        self.assertNotIn("sha256:old", updated)

    def test_claude_installer_replaces_target_hook_and_preserves_other_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "settings.json"
            log = base / "claude.jsonl"
            config.write_text(
                json.dumps(
                    {
                        "permissions": {"allow": ["Bash(date)"]},
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": "*",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": f"jq -c . >> {log}",
                                        },
                                        {
                                            "type": "command",
                                            "command": "echo keep >> /tmp/other.log",
                                        },
                                    ],
                                }
                            ]
                        },
                    }
                )
            )

            result = install_claude_hooks(
                log_path=log,
                config_path=config,
                python_executable="python3",
            )

            self.assertTrue(result.changed)
            data = json.loads(config.read_text())
            commands = [
                hook["command"]
                for entry in data["hooks"]["PreToolUse"]
                for hook in entry["hooks"]
            ]
            self.assertIn("echo keep >> /tmp/other.log", commands)
            self.assertTrue(any("hook_entry.py" in command for command in commands))
            self.assertFalse(any(command.startswith("jq -c") for command in commands))
            self.assertEqual(data["permissions"]["allow"], ["Bash(date)"])

    def test_codex_uninstaller_removes_monitor_hooks_and_preserves_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "config.toml"
            log = base / "codex.jsonl"
            config.write_text(
                "\n".join(
                    [
                        "[features]",
                        "js_repl = false",
                        "",
                        "[hooks.state]",
                        'source = "keep-me"',
                        "",
                    ]
                )
            )
            install_codex_hooks(log_path=log, config_path=config, python_executable="python3")

            result = uninstall_codex_hooks(log_path=log, config_path=config)

            self.assertTrue(result.changed)
            text = config.read_text()
            self.assertIn("[features]", text)
            self.assertIn("js_repl = false", text)
            self.assertIn("[hooks.state]", text)
            self.assertIn('source = "keep-me"', text)
            self.assertNotIn("agent-monitor hooks", text)
            self.assertNotIn(str(log), text)

    def test_claude_uninstaller_removes_monitor_hooks_and_preserves_other_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "settings.json"
            log = base / "claude.jsonl"
            config.write_text(
                json.dumps(
                    {
                        "permissions": {"allow": ["Bash(date)"]},
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": "*",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "echo keep >> /tmp/other.log",
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                )
            )
            install_claude_hooks(log_path=log, config_path=config, python_executable="python3")

            result = uninstall_claude_hooks(log_path=log, config_path=config)

            self.assertTrue(result.changed)
            data = json.loads(config.read_text())
            commands = [
                hook["command"]
                for entry in data["hooks"]["PreToolUse"]
                for hook in entry["hooks"]
            ]
            self.assertEqual(commands, ["echo keep >> /tmp/other.log"])
            self.assertEqual(data["permissions"]["allow"], ["Bash(date)"])

    def test_sidepulse_agent_monitor_command_shape(self) -> None:
        parser = build_parser(prog="sidepulse agent-monitor")

        install = parser.parse_args(["install"])
        live = parser.parse_args(["live", "--recent-seconds", "120"])
        leds = parser.parse_args(["leds", "--once", "--dry-run"])
        uninstall = parser.parse_args(["uninstall"])
        status_bar = parser.parse_args(["status-bar"])
        status_bar_foreground = parser.parse_args(["status-bar", "--foreground"])

        self.assertEqual(install.provider, "all")
        self.assertEqual(live.command, "live")
        self.assertEqual(live.recent_seconds, 120)
        self.assertEqual(leds.command, "leds")
        self.assertTrue(leds.once)
        self.assertTrue(leds.dry_run)
        self.assertEqual(uninstall.provider, "all")
        self.assertEqual(status_bar.command, "status-bar")
        self.assertFalse(status_bar.foreground)
        self.assertFalse(status_bar.uninstall)
        self.assertTrue(status_bar_foreground.foreground)
        self.assertIn("sidepulse agent-monitor", parser.format_usage())

    def test_sidepulse_entrypoint_dispatches_to_agent_monitor(self) -> None:
        with patch.object(cli_module, "main", return_value=17) as main:
            result = cli_module.sidepulse_main(["agent-monitor", "live"])

        self.assertEqual(result, 17)
        main.assert_called_once_with(["live"], prog="sidepulse agent-monitor")

    def test_sidepulse_battery_command_shape(self) -> None:
        parser = cli_module.build_sidepulse_parser()

        status = parser.parse_args(["battery", "status", "--json"])
        leds = parser.parse_args(["battery", "leds", "--once", "--dry-run", "--full-watts", "140"])
        configure = parser.parse_args(["battery", "configure", "--display", "battery"])

        self.assertEqual(status.command, "battery")
        self.assertEqual(status.battery_command, "status")
        self.assertTrue(status.json)
        self.assertEqual(leds.battery_command, "leds")
        self.assertTrue(leds.once)
        self.assertTrue(leds.dry_run)
        self.assertEqual(leds.full_watts, "140")
        self.assertEqual(configure.battery_command, "configure")
        self.assertEqual(configure.display, "battery")

    def test_sidepulse_write_decodes_escaped_newlines_and_writes_leds_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "SidePulse"
            device.mkdir()

            target = write_led_program(
                r"off\n#FF00FF pulse",
                device_path=device,
            )

            self.assertEqual(target, device / "LEDS.LED")
            self.assertEqual(target.read_text(), "off\n#FF00FF pulse")

    def test_sidepulse_write_falls_back_to_existing_legacy_leds_txt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "PulseDot"
            device.mkdir()
            (device / "LEDS.TXT").write_text("off")

            target = write_led_program(
                r"off\n#FF00FF pulse",
                device_path=device,
            )

            self.assertEqual(target, device / "LEDS.TXT")
            self.assertEqual(target.read_text(), "off\n#FF00FF pulse")

    def test_sidepulse_write_discovers_pulsedot_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mount_root = Path(tmp)
            device = mount_root / "PulseDot"
            device.mkdir()
            (device / "LEDS.TXT").write_text("off")

            candidates = discover_devices(mount_root=mount_root)

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].root, device)
            self.assertEqual(candidates[0].target, device / "LEDS.TXT")

    def test_sidepulse_write_prefers_leds_led_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mount_root = Path(tmp)
            device = mount_root / "SidePulse"
            device.mkdir()
            (device / "LEDS.LED").write_text("off")
            (device / "LEDS.TXT").write_text("legacy")

            candidates = discover_devices(mount_root=mount_root)

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].target, device / "LEDS.LED")

    def test_device_discovery_skips_mount_io_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mount_root = Path(tmp)
            good = mount_root / "PulseDot"
            bad = mount_root / "SidePulse"
            good.mkdir()
            bad.mkdir()
            (good / "LEDS.TXT").write_text("off")
            original_is_dir = Path.is_dir

            def flaky_is_dir(path: Path) -> bool:
                if path == bad:
                    raise OSError("offline")
                return original_is_dir(path)

            with patch.object(Path, "is_dir", flaky_is_dir):
                candidates = discover_devices(mount_root=mount_root)

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].root, good)

    def test_sidepulse_write_validates_device_limits(self) -> None:
        self.assertEqual(normalize_led_text(r"off\n#FF00FF pulse"), "off\n#FF00FF pulse")
        with self.assertRaises(DeviceWriteError):
            write_led_program("x" * 513, device_path=Path("/tmp/device"), dry_run=True)
        write_led_program("\n".join(["off"] * 20), device_path=Path("/tmp/device"), dry_run=True)
        with self.assertRaises(DeviceWriteError):
            write_led_program("\n".join(["off"] * 21), device_path=Path("/tmp/device"), dry_run=True)

    def test_sidepulse_write_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "PulseDot"
            device.mkdir()
            (device / "LEDS.TXT").write_text("off")
            result = cli_module.sidepulse_main(
                ["write", r"off\n#FF00FF pulse", "--device", str(device)]
            )

            self.assertEqual(result, 0)
            self.assertEqual((device / "LEDS.TXT").read_text(), "off\n#FF00FF pulse")

    def test_led_status_maps_agent_modes_to_programs(self) -> None:
        self.assertEqual(
            display_state_for_mode(AgentMode.WAITING_FOR_INPUT),
            LedDisplayState.ASK,
        )
        self.assertEqual(
            display_state_for_mode(AgentMode.TOOL_RUNNING),
            LedDisplayState.WORKING,
        )
        self.assertEqual(
            display_state_for_mode(AgentMode.COMPLETED),
            LedDisplayState.DONE,
        )
        self.assertEqual(
            display_state_for_mode(AgentMode.IDLE_READY),
            LedDisplayState.IDLE,
        )

        self.assertEqual(
            program_for_display_state(LedDisplayState.IDLE),
            "off\n#020204 6s pulse\nrepeat",
        )
        self.assertEqual(program_for_display_state(LedDisplayState.DONE), "#00FF66")
        self.assertIn("#FF3A00 1.6s pulse", program_for_display_state(LedDisplayState.ASK))
        self.assertEqual(
            program_for_display_state(LedDisplayState.WORKING, led_count=2).splitlines(),
            [
                "off 160ms cosine",
                "0:#00E5FF 760ms pulse 0ms; 1:#00E5FF 760ms pulse 260ms",
                "repeat",
            ],
        )
        self.assertEqual(
            len(program_for_display_state(LedDisplayState.WORKING, led_count=8).splitlines()),
            3,
        )

    def test_write_mode_to_leds_uses_device_specific_program(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "PulseDot"
            device.mkdir()
            (device / "LEDS.TXT").write_text("off")

            result = write_mode_to_leds(AgentMode.WORKING, device_path=device)

            self.assertEqual(result.state, LedDisplayState.WORKING)
            self.assertEqual(result.target, device / "LEDS.TXT")
            self.assertEqual(
                (device / "LEDS.TXT").read_text(),
                "off 160ms cosine\n"
                "0:#00E5FF 760ms pulse 0ms; 1:#00E5FF 760ms pulse 260ms\n"
                "repeat",
            )

            write_mode_to_leds(AgentMode.IDLE_READY, device_path=device)

            self.assertEqual(
                (device / "LEDS.TXT").read_text(),
                "off\n#020204 6s pulse\nrepeat",
            )

    def test_led_count_uses_product_name(self) -> None:
        self.assertEqual(led_count_for_target(Path("/Volumes/PulseDot/LEDS.TXT")), 2)
        self.assertEqual(led_count_for_target(Path("/Volumes/PulseDot/LEDS.LED")), 2)
        self.assertEqual(led_count_for_target(Path("/Volumes/SidePulse/LEDS.LED")), 8)

    def test_sidepulse_working_program_uses_eight_leds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "SidePulse"
            device.mkdir()

            write_mode_to_leds(AgentMode.WORKING, device_path=device)

            lines = (device / "LEDS.LED").read_text().splitlines()
            self.assertEqual(len(lines), 3)
            self.assertEqual(lines[0], "off 160ms cosine")
            self.assertIn("0:#00E5FF 760ms pulse 0ms", lines[1])
            self.assertIn("5:#00E5FF 760ms pulse 475ms", lines[1])
            self.assertIn("7:#00E5FF 760ms pulse 665ms", lines[1])
            self.assertEqual(lines[-1], "repeat")

    def test_agent_led_controller_skips_unchanged_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "SidePulse"
            device.mkdir()
            controller = AgentLedController(device_path=device)

            first = controller.sync_mode(AgentMode.COMPLETED)
            second = controller.sync_mode(AgentMode.COMPLETED)
            third = controller.sync_mode(AgentMode.WAITING_FOR_INPUT)

            self.assertTrue(first.changed)
            self.assertFalse(second.changed)
            self.assertTrue(third.changed)
            self.assertIn("#FF3A00 1.6s pulse", (device / "LEDS.LED").read_text())

    def test_battery_parser_uses_adapter_watts_and_raw_capacity(self) -> None:
        payload = plistlib.dumps(
            [
                {
                    "CurrentCapacity": 50,
                    "ExternalConnected": True,
                    "IsCharging": True,
                    "FullyCharged": False,
                    "Voltage": 12000,
                    "Amperage": 1000,
                    "AppleRawCurrentCapacity": 4000,
                    "AppleRawMaxCapacity": 8000,
                    "DesignCapacity": 10000,
                    "CycleCount": 12,
                    "AdapterDetails": {
                        "Watts": 96,
                        "AdapterVoltage": 20000,
                        "Current": 4800,
                        "UsbHvcMenu": [
                            {"MaxVoltage": 5000, "MaxCurrent": 3000},
                            {"MaxVoltage": 20000, "MaxCurrent": 4800},
                        ],
                    },
                }
            ]
        )

        snapshot = parse_ioreg_battery_plist(payload)

        self.assertEqual(snapshot.percent, 50)
        self.assertTrue(snapshot.is_plugged)
        self.assertTrue(snapshot.is_charging)
        self.assertEqual(snapshot.adapter_power, 96)
        self.assertEqual(snapshot.health_percent, 80)
        self.assertEqual(snapshot.current_capacity_mah, 4000)
        self.assertEqual(len(snapshot.pd_profiles), 2)

    def test_battery_program_matches_simulator_frontier_pulse(self) -> None:
        snapshot = BatterySnapshot(
            percent=50,
            is_plugged=True,
            is_charging=True,
            adapter_watts=70,
            full_charge_watts=140,
        )

        program = program_for_battery(snapshot, led_count=8)

        validate_led_text(program)
        lines = program.splitlines()
        self.assertIn(f"0:{BATTERY_CHARGING_MINT} 360ms ease", lines[0])
        self.assertIn(f"3:{BATTERY_CHARGING_MINT} 360ms ease", lines[0])
        self.assertIn("4:#000000 360ms ease", lines[0])
        self.assertEqual(lines[1], f"4:{BATTERY_CHARGING_MINT} 790ms pulse")
        self.assertEqual(len(lines), 2)
        self.assertNotIn("repeat", program)
        self.assertNotIn("\noff", program)

    def test_unplugged_battery_program_eases_to_static_level(self) -> None:
        snapshot = BatterySnapshot(percent=50, is_plugged=False)

        program = program_for_battery(snapshot, led_count=8)

        validate_led_text(program)
        self.assertEqual(len(program.splitlines()), 1)
        self.assertIn("0:#FFB000 360ms ease", program)
        self.assertIn("3:#FFB000 360ms ease", program)
        self.assertIn("4:#000000 360ms ease", program)
        self.assertNotIn("repeat", program)

    def test_battery_program_uses_partial_next_led(self) -> None:
        snapshot = BatterySnapshot(percent=57, is_plugged=False)

        program = program_for_battery(snapshot, led_count=8)

        validate_led_text(program)
        segments = program.split(";")
        self.assertEqual(segments[0], "0:#00FF66 360ms ease")
        self.assertEqual(segments[3], "3:#00FF66 360ms ease")
        self.assertEqual(segments[4], "4:#008F39 360ms ease")
        self.assertEqual(segments[5], "5:#000000 360ms ease")

    def test_battery_program_uses_full_speed_steady_pulse(self) -> None:
        snapshot = BatterySnapshot(
            percent=80,
            is_plugged=True,
            is_charging=True,
            adapter_watts=140,
            full_charge_watts=140,
        )

        program = program_for_battery(snapshot, led_count=8)

        validate_led_text(program)
        self.assertIn(f"6:{BATTERY_CHARGING_MINT} 1400ms pulse", program)
        self.assertNotIn("repeat", program)
        self.assertNotIn("none", program)

    def test_battery_led_controller_animates_charging_on_cadence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "SidePulse"
            device.mkdir()
            controller = BatteryLedController(device_path=device)
            snapshot = BatterySnapshot(
                percent=50,
                is_plugged=True,
                is_charging=True,
                adapter_watts=70,
                full_charge_watts=140,
            )

            with patch(
                "agent_monitor.battery.time.monotonic",
                side_effect=[0.0, 0.5, 2.0],
            ):
                first = controller.sync_snapshot(snapshot)
                second = controller.sync_snapshot(snapshot)
                third = controller.sync_snapshot(snapshot)

            self.assertTrue(first.changed)
            self.assertFalse(second.changed)
            self.assertTrue(third.changed)

    def test_battery_led_controller_skips_unchanged_static_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "SidePulse"
            device.mkdir()
            controller = BatteryLedController(device_path=device)
            snapshot = BatterySnapshot(percent=50, is_plugged=False)

            with patch(
                "agent_monitor.battery.time.monotonic",
                side_effect=[0.0, 10.0],
            ):
                first = controller.sync_snapshot(snapshot)
                second = controller.sync_snapshot(snapshot)

            self.assertTrue(first.changed)
            self.assertFalse(second.changed)

    def test_keep_awake_holds_working_then_graces_done(self) -> None:
        processes: list[FakeProcess] = []

        def factory(*_args, **_kwargs):
            process = FakeProcess()
            processes.append(process)
            return process

        controller = KeepAwakeController(
            grace_seconds=300,
            process_factory=factory,
        )

        self.assertTrue(controller.update(AgentMode.WORKING, now=100))
        self.assertEqual(len(processes), 1)
        self.assertTrue(controller.process_running())

        self.assertTrue(controller.update(AgentMode.COMPLETED, now=110))
        self.assertIn("grace", controller.detail(now=110))

        self.assertTrue(controller.update(AgentMode.IDLE_READY, now=200))
        self.assertTrue(controller.process_running())

        self.assertFalse(controller.update(AgentMode.IDLE_READY, now=411))
        self.assertFalse(controller.process_running())
        self.assertTrue(processes[0].terminated)

    def test_keep_awake_ask_grace_expires_without_refresh_extension(self) -> None:
        processes: list[FakeProcess] = []

        def factory(*_args, **_kwargs):
            process = FakeProcess()
            processes.append(process)
            return process

        controller = KeepAwakeController(
            grace_seconds=300,
            process_factory=factory,
        )

        self.assertTrue(controller.update(AgentMode.WAITING_FOR_INPUT, now=100))
        self.assertTrue(controller.update(AgentMode.WAITING_FOR_INPUT, now=350))
        self.assertFalse(controller.update(AgentMode.WAITING_FOR_INPUT, now=401))
        self.assertEqual(len(processes), 1)
        self.assertTrue(processes[0].terminated)

    def test_keep_awake_touches_keepalive_file_once_per_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "SidePulse"
            device.mkdir()
            status_path = device / "keepalive"
            reads: list[Path] = []

            controller = KeepAwakeController(
                status_read_seconds=60,
                status_reader=lambda path: reads.append(path),
                status_read_async=False,
            )

            self.assertEqual(status_file_for_target(device / "LEDS.LED"), status_path)
            self.assertEqual(status_file_for_target(device / "LEDS.TXT"), status_path)
            self.assertEqual(status_file_for_target(device / "STATUS.TXT"), status_path)
            self.assertEqual(
                controller.poke_status_file(device / "LEDS.LED", now=0),
                status_path,
            )
            self.assertIsNone(controller.poke_status_file(device / "LEDS.LED", now=30))
            self.assertEqual(
                controller.poke_status_file(device / "LEDS.LED", now=61),
                status_path,
            )
            self.assertEqual(reads, [status_path, status_path])

    def test_default_logs_use_sidepulse_xdg_state_dir(self) -> None:
        home = Path("/Users/example")

        self.assertEqual(
            default_state_dir(home),
            home / ".local" / "state" / "sidepulse" / "agent-monitor",
        )
        self.assertEqual(
            default_log_path("codex", home),
            home / ".local" / "state" / "sidepulse" / "agent-monitor" / "codex.jsonl",
        )

        with patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/xdg-state"}):
            self.assertEqual(
                default_state_dir(),
                Path("/tmp/xdg-state") / "sidepulse" / "agent-monitor",
            )

    def test_install_defaults_to_standard_state_log_path(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["install", "codex"])

        with patch.object(
            cli_module,
            "default_log_path",
            return_value=Path("/tmp/state/sidepulse/agent-monitor/codex.jsonl"),
        ):
            self.assertEqual(
                cli_module.install_log_path("codex", args),
                Path("/tmp/state/sidepulse/agent-monitor/codex.jsonl"),
            )

    def test_settings_use_xdg_config_dir_and_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_home = Path(tmp) / "xdg-config"
            settings_path = config_home / "sidepulse" / "agent-monitor" / "settings.json"

            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home)}):
                self.assertEqual(default_config_dir(), settings_path.parent)
                self.assertEqual(default_settings_path(), settings_path)

                saved = AgentMonitorSettings(
                    codex_transcripts_enabled=False,
                    claude_transcripts_enabled=True,
                )
                self.assertEqual(save_settings(saved), settings_path)
                self.assertEqual(load_settings(), saved)

    def test_default_sources_respect_transcript_settings(self) -> None:
        settings = AgentMonitorSettings(
            codex_transcripts_enabled=False,
            claude_transcripts_enabled=True,
        )

        providers = [source.provider for source in default_sources(settings)]

        self.assertNotIn("codex-transcripts", providers)
        self.assertIn("claude-transcripts", providers)

    def test_default_sources_are_hook_only_by_default(self) -> None:
        providers = [source.provider for source in default_sources(AgentMonitorSettings())]

        self.assertIn("codex", providers)
        self.assertIn("claude", providers)
        self.assertNotIn("codex-transcripts", providers)
        self.assertNotIn("claude-transcripts", providers)

    def test_settings_round_trip_remembered_device_display_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings = AgentMonitorSettings(
                devices=(
                    DeviceDisplaySetting(
                        device_id="/Volumes/SidePulse",
                        name="SidePulse",
                        path="/Volumes/SidePulse",
                        led_display="agent",
                    ),
                    DeviceDisplaySetting(
                        device_id="/Volumes/PulseDot",
                        name="PulseDot",
                        path="/Volumes/PulseDot",
                        led_display="battery",
                    ),
                )
            )

            save_settings(settings, settings_path)
            loaded = load_settings(settings_path)

            self.assertEqual(loaded.devices, settings.devices)
            self.assertEqual(loaded.display_for_device("/Volumes/SidePulse"), "agent")
            self.assertEqual(loaded.display_for_device("/Volumes/PulseDot"), "battery")

    def test_settings_remember_device_preserves_existing_display_choice(self) -> None:
        settings = AgentMonitorSettings().with_device_display(
            "/Volumes/PulseDot",
            "battery",
            name="PulseDot",
            path="/Volumes/PulseDot",
        )

        remembered = settings.with_remembered_device(
            device_id="/Volumes/PulseDot",
            name="PulseDot",
            path="/Volumes/PulseDot",
        )

        self.assertEqual(remembered.display_for_device("/Volumes/PulseDot"), "battery")

    def test_status_bar_launch_agent_plist_runs_foreground_command(self) -> None:
        plist = build_launch_agent_plist(
            python_executable="/usr/bin/python3",
            stdout_path=Path("/tmp/sidepulse.out.log"),
            stderr_path=Path("/tmp/sidepulse.err.log"),
        )

        self.assertEqual(plist["Label"], LAUNCH_AGENT_LABEL)
        self.assertEqual(
            plist["ProgramArguments"],
            [
                "/usr/bin/python3",
                "-m",
                "agent_monitor",
                "status-bar",
                "--foreground",
            ],
        )
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(plist["StandardOutPath"], "/tmp/sidepulse.out.log")
        self.assertEqual(plist["StandardErrorPath"], "/tmp/sidepulse.err.log")
        self.assertNotIn("KeepAlive", plist)

    def test_watch_filters_to_recent_statuses(self) -> None:
        now = datetime.now(timezone.utc)
        recent = AgentStatus(
            provider="codex",
            agent_id="recent",
            display_name="Recent",
            mode=AgentMode.WORKING,
            updated_at=now - timedelta(seconds=20),
            event_name="PostToolUse",
        )
        older = AgentStatus(
            provider="claude",
            agent_id="older",
            display_name="Older",
            mode=AgentMode.COMPLETED,
            updated_at=now - timedelta(seconds=600),
            event_name="Stop",
        )
        snapshot = MonitorSnapshot(
            aggregate=AggregateStatus(AgentMode.WORKING, 2, 0, recent),
            statuses=(recent, older),
            stale_statuses=(),
            sources=(),
            collected_at=now,
        )

        visible = visible_watch_statuses(snapshot, recent_seconds=120, include_stale=False)

        self.assertEqual([status.agent_id for status in visible], ["recent"])

    def test_orphaned_tool_running_expires_before_session_stale_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            old = datetime.now(timezone.utc) - timedelta(seconds=180)
            log.write_text(
                json.dumps(
                    {
                        "logged_at": old.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "event": {
                            "hook_event_name": "PreToolUse",
                            "session_id": "codex-session",
                            "tool_name": "Bash",
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=300,
                tool_running_timeout_seconds=120,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.IDLE_READY)
            self.assertEqual(snapshot.statuses, ())
            self.assertEqual(len(snapshot.stale_statuses), 1)
            self.assertEqual(snapshot.stale_statuses[0].mode, AgentMode.TOOL_RUNNING)

    def test_completed_status_expires_before_session_stale_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            old = datetime.now(timezone.utc) - timedelta(seconds=60)
            log.write_text(
                json.dumps(
                    {
                        "logged_at": old.isoformat(),
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "Done.",
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=3600,
                completed_visible_seconds=15,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.IDLE_READY)
            self.assertEqual(snapshot.statuses, ())
            self.assertEqual(len(snapshot.stale_statuses), 1)
            self.assertEqual(snapshot.stale_statuses[0].mode, AgentMode.COMPLETED)

    def test_completed_status_stays_visible_for_twenty_minutes_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            recent_done = datetime.now(timezone.utc) - timedelta(minutes=19)
            log.write_text(
                json.dumps(
                    {
                        "logged_at": recent_done.isoformat(),
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "Done.",
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.COMPLETED)
            self.assertEqual(len(snapshot.statuses), 1)

    def test_completed_status_is_hidden_when_active_work_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "logged_at": now,
                                "event": {
                                    "hook_event_name": "Stop",
                                    "session_id": "done-session",
                                    "last_assistant_message": "Done.",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "logged_at": now,
                                "event": {
                                    "hook_event_name": "PreToolUse",
                                    "session_id": "working-session",
                                    "tool_name": "Bash",
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=3600,
                completed_visible_seconds=15,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.active_count, 1)
            self.assertEqual([status.session_id for status in snapshot.statuses], ["working-session"])
            self.assertEqual(snapshot.stale_statuses[0].session_id, "done-session")

    def test_idle_notification_does_not_resurrect_completed_claude_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "claude.jsonl"
            old = datetime.now(timezone.utc) - timedelta(minutes=25)
            log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "logged_at": old.isoformat(),
                                "hook_event_name": "Stop",
                                "session_id": "claude-session",
                                "cwd": "/tmp/project",
                                "last_assistant_message": "Done and verified.",
                                "background_tasks": [],
                                "session_crons": [],
                            }
                        ),
                        json.dumps(
                            {
                                "logged_at": (old + timedelta(seconds=60)).isoformat(),
                                "hook_event_name": "Notification",
                                "session_id": "claude-session",
                                "cwd": "/tmp/project",
                                "notification_type": "idle_prompt",
                                "message": "Claude is waiting for your input",
                            }
                        ),
                    ]
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("claude", log),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.IDLE_READY)
            self.assertEqual(snapshot.statuses, ())
            self.assertEqual(snapshot.stale_statuses[0].mode, AgentMode.COMPLETED)

    def test_codex_permission_request_stays_ask_during_unrelated_tool_activity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            now = datetime.now(timezone.utc)
            session_id = "019f179b-7fdc-7eb0-a3af-1ca3eb128eee"
            server_command = ".venv/bin/bambucuts server --host 127.0.0.1 --port 5425"
            curl_command = "curl -s http://127.0.0.1:5425/api/status | head -c 1000"
            events = [
                {
                    "logged_at": now.isoformat(),
                    "event": {
                        "hook_event_name": "PreToolUse",
                        "session_id": session_id,
                        "cwd": "/Users/pero/pgit/a1plotter",
                        "tool_name": "Bash",
                        "tool_input": {"command": server_command},
                    },
                },
                {
                    "logged_at": (now + timedelta(seconds=1)).isoformat(),
                    "event": {
                        "hook_event_name": "PermissionRequest",
                        "session_id": session_id,
                        "cwd": "/Users/pero/pgit/a1plotter",
                        "tool_name": "Bash",
                        "tool_input": {"command": server_command},
                    },
                },
                {
                    "logged_at": (now + timedelta(seconds=2)).isoformat(),
                    "event": {
                        "hook_event_name": "PreToolUse",
                        "session_id": session_id,
                        "cwd": "/Users/pero/pgit/a1plotter",
                        "tool_name": "Bash",
                        "tool_input": {"command": curl_command},
                    },
                },
                {
                    "logged_at": (now + timedelta(seconds=3)).isoformat(),
                    "event": {
                        "hook_event_name": "PostToolUse",
                        "session_id": session_id,
                        "cwd": "/Users/pero/pgit/a1plotter",
                        "tool_name": "Bash",
                        "tool_input": {"command": curl_command},
                        "tool_response": "{}",
                    },
                },
            ]
            log.write_text("\n".join(json.dumps(event) for event in events) + "\n")

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)
            self.assertEqual(snapshot.statuses[0].event_name, "PermissionRequest")

    def test_codex_permission_request_clears_when_matching_tool_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            now = datetime.now(timezone.utc)
            session_id = "019f179b-7fdc-7eb0-a3af-1ca3eb128eee"
            command = "curl -s http://127.0.0.1:5425/api/status | head -c 1000"
            events = [
                {
                    "logged_at": now.isoformat(),
                    "event": {
                        "hook_event_name": "PreToolUse",
                        "session_id": session_id,
                        "cwd": "/Users/pero/pgit/a1plotter",
                        "tool_name": "Bash",
                        "tool_input": {"command": command},
                    },
                },
                {
                    "logged_at": (now + timedelta(seconds=1)).isoformat(),
                    "event": {
                        "hook_event_name": "PermissionRequest",
                        "session_id": session_id,
                        "cwd": "/Users/pero/pgit/a1plotter",
                        "tool_name": "Bash",
                        "tool_input": {"command": command},
                    },
                },
                {
                    "logged_at": (now + timedelta(seconds=2)).isoformat(),
                    "event": {
                        "hook_event_name": "PostToolUse",
                        "session_id": session_id,
                        "cwd": "/Users/pero/pgit/a1plotter",
                        "tool_name": "Bash",
                        "tool_input": {"command": command},
                        "tool_response": "{}",
                    },
                },
            ]
            log.write_text("\n".join(json.dumps(event) for event in events) + "\n")

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.WORKING)
            self.assertEqual(snapshot.statuses[0].event_name, "PostToolUse")

    def test_internal_codex_helper_sessions_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "logged_at": now,
                                "event": {
                                    "hook_event_name": "UserPromptSubmit",
                                    "session_id": "codex-helper",
                                    "cwd": "/Users/pero/pgit/pixiepulse-bridge",
                                    "prompt": "Overview\nGenerate 0 to 3 hyperpersonalized suggestions for what this user might do.",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "logged_at": now,
                                "event": {
                                    "hook_event_name": "PostToolUse",
                                    "session_id": "codex-helper",
                                    "cwd": "/Users/pero/pgit/pixiepulse-bridge",
                                    "tool_name": "mcp__codex_apps__gmail__batch_read_email",
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.IDLE_READY)
            self.assertEqual(snapshot.statuses, ())

    def test_codex_transcript_fallback_marks_recent_user_turn_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "aaaaaaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee"
            path = root / "2026" / "06" / "29" / f"rollout-2026-06-29T08-27-42-{session_id}.jsonl"
            path.parent.mkdir(parents=True)
            now = datetime.now(timezone.utc).isoformat()
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": now,
                                "type": "turn_context",
                                "payload": {
                                    "turn_id": "turn-1",
                                    "cwd": "/Users/pero/pgit/pixiepulse-bridge",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": now,
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "input_text",
                                            "text": "it didnt catch this conversation",
                                        }
                                    ],
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex-transcripts", root),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.WORKING)
            self.assertEqual(snapshot.statuses[0].session_id, session_id)
            self.assertIn("pixiepulse-bridge", snapshot.statuses[0].display_name)
            self.assertIn("it didnt catch", snapshot.statuses[0].display_name)

    def test_transcript_records_are_cached_until_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "019ee395-2f64-7cc3-b566-afcc1d626160"
            path = root / f"rollout-2026-06-29T08-27-42-{session_id}.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            path.write_text(
                json.dumps(
                    {
                        "timestamp": now,
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "call_id": "call-1",
                            "arguments": "{}",
                        },
                    }
                )
                + "\n"
            )
            monitor = AgentMonitor(
                sources=(SourceSpec("codex-transcripts", root),),
                stale_after_seconds=3600,
            )
            calls: list[Path] = []
            original_read_recent_lines = collector_module.read_recent_lines

            def counting_read_recent_lines(read_path: Path, max_lines: int) -> list[str]:
                if read_path == path:
                    calls.append(read_path)
                return original_read_recent_lines(read_path, max_lines)

            with patch(
                "agent_monitor.collector.read_recent_lines",
                side_effect=counting_read_recent_lines,
            ):
                self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.TOOL_RUNNING)
                self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.TOOL_RUNNING)
                self.assertEqual(calls, [path])

                with path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "type": "response_item",
                                "payload": {
                                    "type": "function_call_output",
                                    "call_id": "call-1",
                                    "output": "{}",
                                },
                            }
                        )
                        + "\n"
                    )

                self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.WORKING)
                self.assertEqual(calls, [path, path])

    def test_hook_log_records_are_cached_until_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            log.write_text(
                json.dumps(
                    {
                        "logged_at": now,
                        "event": {
                            "hook_event_name": "PreToolUse",
                            "session_id": "codex-session",
                            "tool_name": "Bash",
                        },
                    }
                )
                + "\n"
            )
            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=3600,
            )
            calls: list[Path] = []
            original_read_recent_lines = collector_module.read_recent_lines

            def counting_read_recent_lines(read_path: Path, max_lines: int) -> list[str]:
                if read_path == log:
                    calls.append(read_path)
                return original_read_recent_lines(read_path, max_lines)

            with patch(
                "agent_monitor.collector.read_recent_lines",
                side_effect=counting_read_recent_lines,
            ):
                self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.TOOL_RUNNING)
                self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.TOOL_RUNNING)
                self.assertEqual(calls, [log])

                with log.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "logged_at": datetime.now(timezone.utc).isoformat(),
                                "event": {
                                    "hook_event_name": "PostToolUse",
                                    "session_id": "codex-session",
                                    "tool_name": "Bash",
                                },
                            }
                        )
                        + "\n"
                    )

                self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.WORKING)
                self.assertEqual(calls, [log, log])

    def test_snapshot_reuses_latest_statuses_when_inputs_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            log.write_text(
                json.dumps(
                    {
                        "logged_at": datetime.now(timezone.utc).isoformat(),
                        "event": {
                            "hook_event_name": "PreToolUse",
                            "session_id": "codex-session",
                            "tool_name": "Bash",
                        },
                    }
                )
                + "\n"
            )
            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=3600,
            )

            with patch(
                "agent_monitor.collector.status_from_event",
                wraps=collector_module.status_from_event,
            ) as status_from_event:
                self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.TOOL_RUNNING)
                first_count = status_from_event.call_count
                self.assertGreater(first_count, 0)

                self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.TOOL_RUNNING)
                self.assertEqual(status_from_event.call_count, first_count)

    def test_codex_transcript_fallback_marks_tool_calls_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "019ee395-2f64-7cc3-b566-afcc1d626160"
            path = root / "rollout-2026-06-29T08-27-42-" / f"{session_id}.jsonl"
            path.parent.mkdir(parents=True)
            now = datetime.now(timezone.utc).isoformat()
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": now,
                                "type": "turn_context",
                                "payload": {
                                    "turn_id": "turn-1",
                                    "cwd": "/tmp/project",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": now,
                                "type": "response_item",
                                "payload": {
                                    "type": "function_call",
                                    "name": "exec_command",
                                    "call_id": "call-1",
                                    "arguments": "{}",
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex-transcripts", root),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.TOOL_RUNNING)
            self.assertEqual(snapshot.statuses[0].tool_name, "exec_command")

    def test_codex_transcript_task_complete_overrides_last_tool_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "019f179b-7fdc-7eb0-a3af-1ca3eb128eee"
            path = root / f"rollout-2026-06-30T01-18-14-{session_id}.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": now,
                                "type": "turn_context",
                                "payload": {"cwd": "/tmp/project"},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": now,
                                "type": "response_item",
                                "payload": {
                                    "type": "function_call_output",
                                    "call_id": "call-1",
                                    "output": "ok",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": now,
                                "type": "event_msg",
                                "payload": {
                                    "type": "task_complete",
                                    "last_agent_message": "All set.",
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex-transcripts", root),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.COMPLETED)
            self.assertEqual(snapshot.statuses[0].event_name, "Stop")

    def test_claude_transcript_fallback_marks_tool_calls_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "e289f361-f64f-415e-8dd3-01ed835f7869"
            path = root / "-Users-pero-pgit-sdrgb" / f"{session_id}.jsonl"
            path.parent.mkdir(parents=True)
            now = datetime.now(timezone.utc).isoformat()
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": now,
                                "type": "user",
                                "sessionId": session_id,
                                "cwd": "/Users/pero/pgit/sdrgb",
                                "message": {
                                    "role": "user",
                                    "content": "make a pull request",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": now,
                                "type": "assistant",
                                "sessionId": session_id,
                                "cwd": "/Users/pero/pgit/sdrgb",
                                "message": {
                                    "role": "assistant",
                                    "stop_reason": "tool_use",
                                    "content": [
                                        {
                                            "type": "tool_use",
                                            "id": "toolu_1",
                                            "name": "Edit",
                                            "input": {"file_path": "README.md"},
                                        }
                                    ],
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("claude-transcripts", root),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.TOOL_RUNNING)
            self.assertEqual(snapshot.statuses[0].provider, "claude")
            self.assertEqual(snapshot.statuses[0].tool_name, "Edit")

    def test_claude_transcript_mtime_extends_active_file_activity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "e289f361-f64f-415e-8dd3-01ed835f7869"
            path = root / f"{session_id}.jsonl"
            old = datetime.now(timezone.utc) - timedelta(minutes=25)
            now = datetime.now(timezone.utc)
            path.write_text(
                json.dumps(
                    {
                        "timestamp": old.isoformat(),
                        "type": "user",
                        "sessionId": session_id,
                        "cwd": "/Users/pero/pgit/sdrgb",
                        "message": {
                            "role": "user",
                            "content": "keep going",
                        },
                    }
                )
                + "\n"
            )
            os.utime(path, (now.timestamp(), now.timestamp()))

            monitor = AgentMonitor(
                sources=(SourceSpec("claude-transcripts", root),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.WORKING)
            self.assertEqual(snapshot.statuses[0].event_name, "Notification")

    def test_claude_transcript_mtime_does_not_resurrect_completed_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "e289f361-f64f-415e-8dd3-01ed835f7869"
            path = root / f"{session_id}.jsonl"
            old = datetime.now(timezone.utc) - timedelta(minutes=25)
            now = datetime.now(timezone.utc)
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": old.isoformat(),
                                "type": "user",
                                "sessionId": session_id,
                                "cwd": "/Users/pero/pgit/sdrgb",
                                "message": {
                                    "role": "user",
                                    "content": "it's ok, done",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": (old + timedelta(seconds=10)).isoformat(),
                                "type": "assistant",
                                "sessionId": session_id,
                                "cwd": "/Users/pero/pgit/sdrgb",
                                "message": {
                                    "role": "assistant",
                                    "stop_reason": "end_turn",
                                    "content": [{"type": "text", "text": "Great, thanks for handling it."}],
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )
            os.utime(path, (now.timestamp(), now.timestamp()))

            monitor = AgentMonitor(
                sources=(SourceSpec("claude-transcripts", root),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.IDLE_READY)
            self.assertEqual(snapshot.statuses, ())
            self.assertEqual(snapshot.stale_statuses[0].mode, AgentMode.COMPLETED)

    def test_final_question_maps_to_waiting_for_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            log.write_text(
                json.dumps(
                    {
                        "logged_at": "2026-06-20T06:00:00Z",
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "Which mode do you see now?",
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=999999999,
                tool_running_timeout_seconds=0,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)

    def test_anything_else_prompt_maps_to_completed_before_recaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            log.write_text(
                json.dumps(
                    {
                        "logged_at": now,
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "\n".join(
                                [
                                    "Anything else you want to tweak?",
                                    "",
                                    "* Cogitated for 40s - 1 shell still running",
                                    "※ recap: We built and deployed the SidePulse/PulseDot product status.",
                                ]
                            ),
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=999999999,
                tool_running_timeout_seconds=0,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.COMPLETED)

    def test_concrete_followup_question_maps_to_waiting_for_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "claude.jsonl"
            log.write_text(
                json.dumps(
                    {
                        "logged_at": "2026-06-20T06:00:00Z",
                        "hook_event_name": "Stop",
                        "session_id": "claude-session",
                        "last_assistant_message": (
                            "Committed as `67b0208` but not pushed. "
                            "Want me to push?"
                        ),
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("claude", log),),
                stale_after_seconds=999999999,
                tool_running_timeout_seconds=0,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)

    def test_question_examples_in_inline_code_do_not_map_to_waiting_for_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            log.write_text(
                json.dumps(
                    {
                        "logged_at": now,
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "\n".join(
                                [
                                    "Now:",
                                    "- `Committed but not pushed. Want me to push?` => `Ask`",
                                    "- `Which mode do you see now?` => `Ask`",
                                    "",
                                    "Verified: `42` tests pass.",
                                ]
                            ),
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=999999999,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.COMPLETED)

    def test_real_question_with_inline_code_maps_to_waiting_for_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            log.write_text(
                json.dumps(
                    {
                        "logged_at": "2026-06-20T06:00:00Z",
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "Want me to run `git push`?",
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=999999999,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)

    def test_answer_heading_does_not_map_to_waiting_for_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            log.write_text(
                json.dumps(
                    {
                        "logged_at": now,
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "\n".join(
                                [
                                    "No. Nothing in this payload exposes live XYZ.",
                                    "",
                                    "What we can infer from this:",
                                    "",
                                    "- MQTT print status is useful for uploaded jobs.",
                                ]
                            ),
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=999999999,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.COMPLETED)

    def test_explicit_sidepulse_marker_maps_to_waiting_for_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            log.write_text(
                json.dumps(
                    {
                        "logged_at": "2026-06-20T06:00:00Z",
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "I need your choice.\n<!-- sidepulse:ask -->",
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=999999999,
                tool_running_timeout_seconds=0,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)

    def test_explicit_sidepulse_marker_overrides_question_heuristic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            log.write_text(
                json.dumps(
                    {
                        "logged_at": now,
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "Anything else to tweak?\n<!-- sidepulse:done -->",
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=999999999,
                tool_running_timeout_seconds=0,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.COMPLETED)

    def test_explicit_sidepulse_field_maps_to_waiting_for_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "claude.jsonl"
            log.write_text(
                json.dumps(
                    {
                        "logged_at": "2026-06-20T06:00:00Z",
                        "hook_event_name": "Stop",
                        "session_id": "claude-session",
                        "last_assistant_message": "Done-ish.",
                        "sidepulse_status": "ask",
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("claude", log),),
                stale_after_seconds=999999999,
                tool_running_timeout_seconds=0,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)

    def test_explicit_marker_inside_code_block_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            log.write_text(
                json.dumps(
                    {
                        "logged_at": now,
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "Use:\n```text\n<!-- sidepulse:ask -->\n```",
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=999999999,
                tool_running_timeout_seconds=0,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.COMPLETED)

    def test_session_display_name_uses_prompt_context_after_later_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            session_id = "dddddddd-eeee-7fff-8aaa-bbbbbbbbbbbb"
            prompt = """
# Files mentioned by the user:

## codex-clipboard.png: /var/folders/tmp/codex-clipboard.png

## My request for Codex:
team id 5QJ7W2AQ8H, push key '/Users/pero/Dropbox/keys/PixieDotPushKey.p8'
"""
            log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "logged_at": "2026-06-22T19:50:58Z",
                                "event": {
                                    "hook_event_name": "UserPromptSubmit",
                                    "session_id": session_id,
                                    "cwd": "/Users/pero/pgit/pixiepulse-bridge",
                                    "prompt": prompt,
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "logged_at": "2026-06-22T19:51:09Z",
                                "event": {
                                    "hook_event_name": "PreToolUse",
                                    "session_id": session_id,
                                    "cwd": "/Users/pero/pgit/pixiepulse-bridge",
                                    "tool_name": "Bash",
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=999999999,
                tool_running_timeout_seconds=0,
            )
            snapshot = monitor.snapshot()
            status = snapshot.statuses[0]

            self.assertIn("pixiepulse-bridge", status.display_name)
            self.assertIn("team id 5QJ7W2AQ8H", status.display_name)
            self.assertIn(session_id[:8], status.display_name)
            self.assertNotIn("/Users/pero", status.display_name)

    def test_codex_display_name_uses_session_index_thread_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            session_id = "bbbbbbbb-cccc-7ddd-8eee-ffffffffffff"
            (codex_home / "session_index.jsonl").write_text(
                json.dumps(
                    {
                        "id": session_id,
                        "thread_name": "Refine README agent status modes",
                        "updated_at": "2026-06-20T05:52:21.985091Z",
                    }
                )
                + "\n"
            )
            log = base / "codex.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "logged_at": now,
                                "event": {
                                    "hook_event_name": "UserPromptSubmit",
                                    "session_id": session_id,
                                    "cwd": "/Users/pero/pgit/pixiepulse-bridge",
                                    "prompt": "Why are we burning so much CPU",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "logged_at": now,
                                "event": {
                                    "hook_event_name": "PreToolUse",
                                    "session_id": session_id,
                                    "cwd": "/Users/pero/pgit/pixiepulse-bridge",
                                    "tool_name": "Bash",
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )

            with patch("agent_monitor.collector.Path.home", return_value=home):
                monitor = AgentMonitor(
                    sources=(SourceSpec("codex", log),),
                    stale_after_seconds=999999999,
                )
                snapshot = monitor.snapshot()

            name = snapshot.statuses[0].display_name
            self.assertIn("pixiepulse-bridge", name)
            self.assertIn("Refine README agent status modes", name)
            self.assertNotIn("Why are we burning", name)

    def test_live_monitor_refreshes_loaded_codex_display_name_from_session_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            session_id = "cccccccc-dddd-7eee-8fff-aaaaaaaaaaaa"
            (codex_home / "session_index.jsonl").write_text(
                json.dumps(
                    {
                        "id": session_id,
                        "thread_name": "Refine README agent status modes",
                    }
                )
                + "\n"
            )
            latest = base / "latest.json"
            now = datetime.now(timezone.utc)
            latest.write_text(
                json.dumps(
                    {
                        "updated_at": now.isoformat(),
                        "statuses": [
                            {
                                "provider": "codex",
                                "agent_id": f"codex:session:{session_id}",
                                "display_name": (
                                    "pixiepulse-bridge: Why are we burning so much CPU "
                                    f"({session_id[:8]})"
                                ),
                                "mode": "working",
                                "updated_at": now.isoformat(),
                                "event_name": "UserPromptSubmit",
                                "session_id": session_id,
                                "cwd": "/Users/pero/pgit/pixiepulse-bridge",
                            }
                        ],
                    }
                )
                + "\n"
            )

            with patch("agent_monitor.collector.Path.home", return_value=home):
                monitor = LiveAgentMonitor(latest_state_path=latest)
                snapshot = monitor.snapshot()

            name = snapshot.statuses[0].display_name
            self.assertIn("Refine README agent status modes", name)
            self.assertNotIn("Why are we burning", name)

    def test_task_notification_does_not_replace_session_display_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "claude.jsonl"
            session_id = "1ca4348e-2aec-4147-9e81-d7d56364d257"
            log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "logged_at": "2026-06-22T07:20:00Z",
                                "hook_event_name": "UserPromptSubmit",
                                "session_id": session_id,
                                "cwd": "/Users/pero/pgit/sdstatus_bitbang",
                                "prompt": "convert these videos to mp4",
                            }
                        ),
                        json.dumps(
                            {
                                "logged_at": "2026-06-22T07:24:05Z",
                                "hook_event_name": "UserPromptSubmit",
                                "session_id": session_id,
                                "cwd": "/Users/pero/pgit/sdstatus_bitbang",
                                "prompt": "<task-notification><status>completed</status></task-notification>",
                            }
                        ),
                    ]
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("claude", log),),
                stale_after_seconds=999999999,
            )
            snapshot = monitor.snapshot()

            self.assertIn("convert these videos", snapshot.statuses[0].display_name)
            self.assertNotIn("task-notification", snapshot.statuses[0].display_name)

    def test_codex_session_actions_build_deeplink_and_resume_command(self) -> None:
        status = AgentStatus(
            provider="codex",
            agent_id="codex:session:abc",
            display_name="Codex abc",
            mode=AgentMode.WORKING,
            updated_at=datetime.now(timezone.utc),
            event_name="PreToolUse",
            session_id="019ee395-2f64-7cc3-b566-afcc1d626160",
            cwd="/tmp/project with spaces",
        )

        self.assertEqual(
            session_deep_link(status),
            "codex://threads/019ee395-2f64-7cc3-b566-afcc1d626160",
        )
        self.assertEqual(
            session_resume_command(status),
            "cd '/tmp/project with spaces' && codex resume 019ee395-2f64-7cc3-b566-afcc1d626160",
        )

    def test_claude_session_actions_build_app_link_and_resume_command(self) -> None:
        status = AgentStatus(
            provider="claude",
            agent_id="claude:session:abc",
            display_name="Claude abc",
            mode=AgentMode.WAITING_FOR_INPUT,
            updated_at=datetime.now(timezone.utc),
            event_name="Notification",
            session_id="1ca4348e-2aec-4147-9e81-d7d56364d257",
            cwd="/Users/pero/pgit/sdstatus_bitbang",
        )

        self.assertEqual(session_deep_link(status), "claude://")
        self.assertEqual(
            session_resume_command(status),
            "cd /Users/pero/pgit/sdstatus_bitbang && claude --resume 1ca4348e-2aec-4147-9e81-d7d56364d257",
        )


if __name__ == "__main__":
    unittest.main()
