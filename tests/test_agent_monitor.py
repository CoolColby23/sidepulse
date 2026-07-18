from __future__ import annotations

import json
import os
import plistlib
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
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
from agent_monitor.lid_sleep import (
    ClosedLidAwakeController,
    SleepHelperRequiredError,
    closed_lid_awake_should_hold,
    parse_bool_ioreg_property,
    run_sudo_pmset_disablesleep,
    sleep_helper_sudoers_rule,
)
from agent_monitor.models import AgentMode, AgentStatus, AggregateStatus
from agent_monitor.providers import default_log_path, default_state_dir, parse_log_line
from agent_monitor.sd_eject_guard_launch import (
    SD_EJECT_GUARD_LABEL,
    SdEjectGuardInstallError,
    SdEjectGuardPaths,
    build_sd_eject_guard_plist,
    install_sd_eject_guard,
    stop_sd_eject_guard,
)
from agent_monitor.session_actions import (
    SESSION_OPEN_TERMINAL,
    default_session_open_action,
    session_deep_link,
    session_open_target,
    session_resume_command,
    session_vscode_link,
)
from agent_monitor.settings import (
    CLOSED_LID_AWAKE_AGENTS,
    CLOSED_LID_AWAKE_ALWAYS,
    CLOSED_LID_AWAKE_NEVER,
    LID_ANIMATION_CLOSED,
    LID_ANIMATION_OPEN,
    AgentMonitorSettings,
    DeviceDisplaySetting,
    default_config_dir,
    default_lid_animation,
    default_settings_path,
    load_settings,
    save_settings,
)
from agent_monitor.status_bar_launch import (
    LAUNCH_AGENT_LABEL,
    build_launch_agent_plist,
    install_launch_agent,
)


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

    def test_status_bar_session_menu_title_is_task_and_project(self) -> None:
        try:
            from agent_monitor import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        now = datetime.now(timezone.utc)
        status = AgentStatus(
            provider="codex",
            agent_id="codex:session:019ee395",
            display_name="pixiepulse-bridge: Refine README agent status modes (019ee395)",
            mode=AgentMode.COMPLETED,
            updated_at=now,
            event_name="Stop",
            session_id="019ee395",
            cwd="/Users/pero/pgit/pixiepulse-bridge",
        )

        self.assertEqual(
            status_bar.menu_title_for_status(status, now),
            "Done  Refine README agent status modes\npixiepulse-bridge",
        )
        self.assertEqual(
            status_bar.session_detail_for_status(status, now).split(" · ")[0],
            "Done",
        )

    def test_status_bar_session_row_has_inline_options(self) -> None:
        try:
            from agent_monitor import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        now = datetime.now(timezone.utc)
        status = AgentStatus(
            provider="claude",
            agent_id="claude:session:abc",
            display_name="Claude abc",
            mode=AgentMode.WAITING_FOR_INPUT,
            updated_at=now,
            event_name="Notification",
            session_id="1ca4348e-2aec-4147-9e81-d7d56364d257",
            cwd="/Users/pero/pgit/sdstatus_bitbang",
        )
        target = SimpleNamespace(settings=AgentMonitorSettings())

        row = status_bar.build_session_menu_item(status, now, target)
        submenu = row.submenu()
        titles = [
            submenu.itemAtIndex_(index).title()
            for index in range(submenu.numberOfItems())
            if submenu.itemAtIndex_(index).title()
        ]

        self.assertTrue(row.title().startswith("Ask  "))
        self.assertIsNotNone(row.image())
        self.assertIsNotNone(row.submenu())
        self.assertTrue(any(title.startswith("Ask  Claude abc") for title in titles))
        self.assertIn("Open in VS Code", titles)
        self.assertIn("Resume in Terminal", titles)

    def test_codex_session_options_are_codex_specific(self) -> None:
        try:
            from agent_monitor import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        now = datetime.now(timezone.utc)
        status = AgentStatus(
            provider="codex",
            agent_id="codex:session:abc",
            display_name="Codex abc",
            mode=AgentMode.WORKING,
            updated_at=now,
            event_name="PreToolUse",
            session_id="019ee395-2f64-7cc3-b566-afcc1d626160",
            cwd="/tmp/project with spaces",
        )
        target = SimpleNamespace(settings=AgentMonitorSettings())

        row = status_bar.build_session_menu_item(status, now, target)
        submenu = row.submenu()
        titles = [
            submenu.itemAtIndex_(index).title()
            for index in range(submenu.numberOfItems())
            if submenu.itemAtIndex_(index).title()
        ]

        self.assertTrue(row.title().startswith("Working  "))
        self.assertIsNotNone(row.image())
        self.assertTrue(any(title.startswith("Working  Codex abc") for title in titles))
        self.assertIn("Open in Codex", titles)
        self.assertIn("Resume in Terminal", titles)
        self.assertNotIn("Open in VS Code", titles)
        self.assertNotIn("Open Claude App", titles)

    def test_status_bar_device_submenu_has_brightness_slider(self) -> None:
        try:
            from agent_monitor import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        device = status_bar.StatusBarDevice(
            device_id="/Volumes/PulseDot",
            name="PulseDot",
            root=Path("/Volumes/PulseDot"),
            target=Path("/Volumes/PulseDot/LEDS.LED"),
            connected=True,
            display="agent",
            brightness=128,
        )

        item = status_bar.build_device_menu_item(device, SimpleNamespace())
        submenu = item.submenu()
        titles = [
            submenu.itemAtIndex_(index).title()
            for index in range(submenu.numberOfItems())
            if submenu.itemAtIndex_(index).title()
        ]
        custom_view_count = sum(
            1
            for index in range(submenu.numberOfItems())
            if submenu.itemAtIndex_(index).view() is not None
        )

        self.assertIn("Brightness 50%", titles)
        self.assertEqual(custom_view_count, 1)

    def test_status_bar_menu_has_closed_lid_awake_policy_choices(self) -> None:
        try:
            from agent_monitor import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        snapshot = SimpleNamespace(
            statuses=[],
            collected_at=datetime.now(timezone.utc),
        )
        target = SimpleNamespace(
            settings=AgentMonitorSettings(
                closed_lid_awake_policy=CLOSED_LID_AWAKE_AGENTS,
            ),
            closed_lid_awake=SimpleNamespace(last_error=None),
            status_bar_devices=lambda: [],
        )

        menu = status_bar.build_menu(snapshot, status_bar.STATE_IDLE, target)
        items = [menu.itemAtIndex_(index) for index in range(menu.numberOfItems())]
        by_title = {item.title(): item for item in items if item.title()}

        self.assertIn("Keep Awake With Lid Closed", by_title)
        self.assertEqual(by_title["Never"].state(), 0)
        self.assertEqual(by_title["When Agents Work"].state(), 1)
        self.assertEqual(by_title["Always"].state(), 0)
        self.assertIn("Strong Sleep Override...", by_title)
        self.assertEqual(by_title["Strong Sleep Override..."].state(), 0)

    def test_lid_animation_program_uses_device_brightness(self) -> None:
        try:
            from agent_monitor import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        animation = default_lid_animation(LID_ANIMATION_CLOSED)
        program = status_bar.program_for_lid_animation(animation, brightness=128)

        validate_led_text(program)
        self.assertTrue(program.startswith("brightness 128\n"))

    def test_lid_animation_restore_forces_led_resync(self) -> None:
        try:
            from agent_monitor import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        calls: list[tuple[str, object]] = []
        fake = SimpleNamespace(
            led_animation_token=42,
            led_animation_until_monotonic=100.0,
            last_snapshot=SimpleNamespace(
                aggregate=SimpleNamespace(mode=AgentMode.WORKING),
            ),
            last_battery_snapshot=object(),
            reset_led_controllers_for_display_change=lambda: calls.append(("reset", None)),
            active_led_display_kind=lambda snapshot: "agent",
            sync_leds=lambda mode, snapshot, display: calls.append(
                ("sync", (mode, snapshot, display))
            ),
            refresh_=lambda sender: calls.append(("refresh", sender)),
        )

        status_bar.restore_led_display(fake, "41")
        self.assertEqual(calls, [])
        self.assertEqual(fake.led_animation_until_monotonic, 100.0)

        status_bar.restore_led_display(fake, "42")
        self.assertEqual(fake.led_animation_until_monotonic, 0.0)
        self.assertEqual(calls[0], ("reset", None))
        self.assertEqual(calls[1][0], "sync")
        self.assertEqual(calls[1][1][0], AgentMode.WORKING)

    def test_status_bar_settings_window_has_lid_animation_controls(self) -> None:
        try:
            from agent_monitor import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        target = SimpleNamespace(settings_fields={}, settings_buttons={})

        window = status_bar.build_settings_window(target)

        self.assertEqual(window.title(), "SidePulse Agent Monitor Settings")
        self.assertIn("closed_animation_program", target.settings_fields)
        self.assertIn("closed_animation_duration", target.settings_fields)
        self.assertIn("open_animation_program", target.settings_fields)
        self.assertIn("open_animation_duration", target.settings_fields)
        self.assertIn("closed_lid_system_override", target.settings_buttons)

    def test_status_bar_open_session_remembers_action_by_provider(self) -> None:
        try:
            from agent_monitor import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

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
        fake = SimpleNamespace(
            settings=AgentMonitorSettings(),
            messages=[],
            set_settings_message=lambda message: None,
        )

        with (
            patch("agent_monitor.status_bar.open_terminal_command") as open_terminal,
            patch("agent_monitor.status_bar.save_settings") as save,
        ):
            status_bar.StatusBarController.open_session(
                fake,
                status,
                SESSION_OPEN_TERMINAL,
                remember=True,
            )

        open_terminal.assert_called_once_with(
            "cd /Users/pero/pgit/sdstatus_bitbang && claude --resume 1ca4348e-2aec-4147-9e81-d7d56364d257"
        )
        self.assertEqual(fake.settings.session_open_action("claude"), SESSION_OPEN_TERMINAL)
        save.assert_called_once_with(fake.settings)

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

    def test_sidepulse_status_bar_root_command_shape(self) -> None:
        parser = cli_module.build_sidepulse_parser()

        default = parser.parse_args(["status-bar"])
        start = parser.parse_args(["status-bar", "start", "--foreground"])
        stop = parser.parse_args(["status-bar", "stop"])
        helper = parser.parse_args(["status-bar", "install-sleep-helper", "--dry-run"])

        self.assertEqual(default.command, "status-bar")
        self.assertEqual(default.status_bar_command, "start")
        self.assertFalse(default.foreground)
        self.assertEqual(start.status_bar_command, "start")
        self.assertTrue(start.foreground)
        self.assertEqual(stop.status_bar_command, "stop")
        self.assertEqual(helper.status_bar_command, "install-sleep-helper")
        self.assertTrue(helper.dry_run)

    def test_sidepulse_sdejectguard_command_shape(self) -> None:
        parser = cli_module.build_sidepulse_parser()

        start = parser.parse_args(["sdejectguard", "start"])
        interactive = parser.parse_args(["sdejectguard", "start", "-it", "--scope", "user"])
        stop = parser.parse_args(["sdejectguard", "stop", "--scope", "system"])
        logs = parser.parse_args(["sdejectguard", "logs", "--lines", "12", "--follow"])

        self.assertEqual(start.command, "sdejectguard")
        self.assertEqual(start.sdejectguard_command, "start")
        self.assertEqual(start.scope, "auto")
        self.assertFalse(start.interactive)
        self.assertTrue(interactive.interactive)
        self.assertEqual(interactive.scope, "user")
        self.assertEqual(stop.sdejectguard_command, "stop")
        self.assertEqual(stop.scope, "system")
        self.assertEqual(logs.sdejectguard_command, "logs")
        self.assertEqual(logs.lines, 12)
        self.assertTrue(logs.follow)

    def test_sidepulse_sdejectguard_start_uses_launchd_installer(self) -> None:
        parser = cli_module.build_sidepulse_parser()
        args = parser.parse_args(["sdejectguard", "start", "--scope", "user"])
        guard_result = SimpleNamespace(
            dry_run=False,
            changed=True,
            started=True,
            scope="user",
            plist_path=Path("/tmp/io.sidepulse.sdejectguard.plist"),
            binary_path=Path("/tmp/sd_eject_guard"),
            cleanup_removed=None,
            cleanup_skipped=None,
        )

        with patch(
            "agent_monitor.sd_eject_guard_launch.install_sd_eject_guard",
            return_value=guard_result,
        ) as install:
            result = cli_module.cmd_sidepulse_sdejectguard_start(args)

        self.assertEqual(result, 0)
        install.assert_called_once_with(scope="user", dry_run=False)

    def test_sidepulse_sdejectguard_start_interactive_runs_foreground(self) -> None:
        parser = cli_module.build_sidepulse_parser()
        args = parser.parse_args(["sdejectguard", "start", "-it", "--scope", "user"])

        with patch(
            "agent_monitor.sd_eject_guard_launch.run_sd_eject_guard_interactive",
            return_value=0,
        ) as run:
            result = cli_module.cmd_sidepulse_sdejectguard_start(args)

        self.assertEqual(result, 0)
        run.assert_called_once_with(scope="user")

    def test_sidepulse_sdejectguard_stop_calls_guard_stop(self) -> None:
        parser = cli_module.build_sidepulse_parser()
        args = parser.parse_args(["sdejectguard", "stop", "--scope", "user", "--dry-run"])
        stop_result = SimpleNamespace(
            scope="user",
            plist_path=Path("/tmp/io.sidepulse.sdejectguard.plist"),
            stopped=True,
            skipped=None,
        )

        with patch(
            "agent_monitor.sd_eject_guard_launch.stop_sd_eject_guard",
            return_value=(stop_result,),
        ) as stop:
            result = cli_module.cmd_sidepulse_sdejectguard_stop(args)

        self.assertEqual(result, 0)
        stop.assert_called_once_with(scope="user", dry_run=True)

    def test_sidepulse_setup_command_shape(self) -> None:
        parser = cli_module.build_sidepulse_parser()

        default = parser.parse_args(["setup"])
        codex_only = parser.parse_args(
            [
                "setup",
                "codex",
                "--no-status-bar",
                "--dry-run",
                "--sd-eject-guard-scope",
                "user",
            ]
        )

        self.assertEqual(default.command, "setup")
        self.assertEqual(default.provider, "all")
        self.assertEqual(default.sd_eject_guard_scope, "auto")
        self.assertFalse(default.no_status_bar)
        self.assertFalse(default.dry_run)
        self.assertEqual(codex_only.provider, "codex")
        self.assertEqual(codex_only.sd_eject_guard_scope, "user")
        self.assertTrue(codex_only.no_status_bar)
        self.assertTrue(codex_only.dry_run)

    def test_sidepulse_setup_installs_hooks_guard_and_status_bar(self) -> None:
        parser = cli_module.build_sidepulse_parser()
        args = parser.parse_args(["setup"])
        codex_result = SimpleNamespace(
            provider="codex",
            config_path=Path("/tmp/codex.toml"),
            log_path=Path("/tmp/codex.jsonl"),
            changed=True,
            backup_path=None,
        )
        claude_result = SimpleNamespace(
            provider="claude",
            config_path=Path("/tmp/settings.json"),
            log_path=Path("/tmp/claude.jsonl"),
            changed=False,
            backup_path=None,
        )
        launch_result = SimpleNamespace(
            plist_path=Path("/tmp/io.sidepulse.agentstatus.plist"),
            changed=True,
            started=True,
        )
        guard_result = SimpleNamespace(
            dry_run=False,
            changed=True,
            started=True,
            scope="user",
            plist_path=Path("/tmp/io.sidepulse.sdejectguard.plist"),
            binary_path=Path("/tmp/sd_eject_guard"),
            cleanup_removed=None,
            cleanup_skipped=None,
        )

        with (
            patch.object(cli_module, "install_codex_hooks", return_value=codex_result) as codex,
            patch.object(cli_module, "install_claude_hooks", return_value=claude_result) as claude,
            patch(
                "agent_monitor.sd_eject_guard_launch.install_sd_eject_guard",
                return_value=guard_result,
            ) as guard,
            patch(
                "agent_monitor.status_bar_launch.install_launch_agent",
                return_value=launch_result,
            ) as launch,
        ):
            result = cli_module.cmd_sidepulse_setup(args)

        self.assertEqual(result, 0)
        codex.assert_called_once()
        claude.assert_called_once()
        guard.assert_called_once_with(scope="auto", dry_run=False)
        launch.assert_called_once_with(start=True)

    def test_sidepulse_setup_no_status_bar_still_installs_guard(self) -> None:
        parser = cli_module.build_sidepulse_parser()
        args = parser.parse_args(["setup", "--no-status-bar", "--sd-eject-guard-scope", "user"])
        hook_result = SimpleNamespace(
            provider="codex",
            config_path=Path("/tmp/codex.toml"),
            log_path=Path("/tmp/codex.jsonl"),
            changed=False,
            backup_path=None,
        )
        guard_result = SimpleNamespace(
            dry_run=False,
            changed=False,
            started=True,
            scope="user",
            plist_path=Path("/tmp/io.sidepulse.sdejectguard.plist"),
            binary_path=Path("/tmp/sd_eject_guard"),
            cleanup_removed=None,
            cleanup_skipped=None,
        )

        with (
            patch.object(cli_module, "install_hook_results", return_value=[hook_result]),
            patch(
                "agent_monitor.sd_eject_guard_launch.install_sd_eject_guard",
                return_value=guard_result,
            ) as guard,
            patch("agent_monitor.status_bar_launch.install_launch_agent") as launch,
        ):
            result = cli_module.cmd_sidepulse_setup(args)

        self.assertEqual(result, 0)
        guard.assert_called_once_with(scope="user", dry_run=False)
        launch.assert_not_called()

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

    def test_sidepulse_write_uses_leds_led_even_when_old_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "PulseDot"
            device.mkdir()
            (device / "LEDS.TXT").write_text("off")

            target = write_led_program(
                r"off\n#FF00FF pulse",
                device_path=device,
            )

            self.assertEqual(target, device / "LEDS.LED")
            self.assertEqual(target.read_text(), "off\n#FF00FF pulse")
            self.assertEqual((device / "LEDS.TXT").read_text(), "off")

    def test_sidepulse_write_discovers_pulsedot_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mount_root = Path(tmp)
            device = mount_root / "PulseDot"
            device.mkdir()

            candidates = discover_devices(mount_root=mount_root)

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].root, device)
            self.assertEqual(candidates[0].target, device / "LEDS.LED")

    def test_sidepulse_write_prefers_leds_led_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mount_root = Path(tmp)
            device = mount_root / "SidePulse"
            device.mkdir()
            (device / "LEDS.LED").write_text("off")

            candidates = discover_devices(mount_root=mount_root)

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].target, device / "LEDS.LED")

    def test_device_discovery_ignores_old_leds_txt_on_unnamed_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mount_root = Path(tmp)
            device = mount_root / "USB Drive"
            device.mkdir()
            (device / "LEDS.TXT").write_text("off")

            candidates = discover_devices(mount_root=mount_root)

            self.assertEqual(candidates, [])

    def test_device_discovery_skips_mount_io_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mount_root = Path(tmp)
            good = mount_root / "PulseDot"
            bad = mount_root / "SidePulse"
            good.mkdir()
            bad.mkdir()
            (good / "LEDS.LED").write_text("off")
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
            result = cli_module.sidepulse_main(
                ["write", r"off\n#FF00FF pulse", "--device", str(device)]
            )

            self.assertEqual(result, 0)
            self.assertEqual((device / "LEDS.LED").read_text(), "off\n#FF00FF pulse")

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
        self.assertEqual(
            program_for_display_state(LedDisplayState.DONE, brightness=128),
            "brightness 128\n#00FF66",
        )

    def test_write_mode_to_leds_uses_device_specific_program(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "PulseDot"
            device.mkdir()

            result = write_mode_to_leds(AgentMode.WORKING, device_path=device)

            self.assertEqual(result.state, LedDisplayState.WORKING)
            self.assertEqual(result.target, device / "LEDS.LED")
            self.assertEqual(
                (device / "LEDS.LED").read_text(),
                "off 160ms cosine\n"
                "0:#00E5FF 760ms pulse 0ms; 1:#00E5FF 760ms pulse 260ms\n"
                "repeat",
            )

            write_mode_to_leds(AgentMode.IDLE_READY, device_path=device)

            self.assertEqual(
                (device / "LEDS.LED").read_text(),
                "off\n#020204 6s pulse\nrepeat",
            )

            write_mode_to_leds(AgentMode.COMPLETED, device_path=device, brightness=64)

            self.assertEqual((device / "LEDS.LED").read_text(), "brightness 64\n#00FF66")

    def test_led_count_uses_product_name(self) -> None:
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

    def test_battery_program_uses_brightness_command(self) -> None:
        snapshot = BatterySnapshot(percent=57, is_plugged=False)

        program = program_for_battery(snapshot, led_count=8, brightness=128)

        validate_led_text(program)
        self.assertTrue(program.startswith("brightness 128\n"))

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

    def test_closed_lid_awake_policy_decisions(self) -> None:
        self.assertFalse(
            closed_lid_awake_should_hold(CLOSED_LID_AWAKE_NEVER, agents_active=True)
        )
        self.assertFalse(
            closed_lid_awake_should_hold(CLOSED_LID_AWAKE_AGENTS, agents_active=False)
        )
        self.assertTrue(
            closed_lid_awake_should_hold(CLOSED_LID_AWAKE_AGENTS, agents_active=True)
        )
        self.assertTrue(
            closed_lid_awake_should_hold(CLOSED_LID_AWAKE_ALWAYS, agents_active=False)
        )

    def test_closed_lid_awake_controller_sets_and_restores_system_disable(self) -> None:
        processes: list[FakeProcess] = []
        disabled_calls: list[bool] = []

        def factory(*_args, **_kwargs):
            process = FakeProcess()
            processes.append(process)
            return process

        controller = ClosedLidAwakeController(
            process_factory=factory,
            sleep_disabled_reader=lambda: False,
            sleep_disabled_setter=disabled_calls.append,
            use_system_disable=True,
        )

        self.assertTrue(
            controller.update(CLOSED_LID_AWAKE_ALWAYS, agents_active=False)
        )
        self.assertEqual(disabled_calls, [True])
        self.assertTrue(controller.changed_system_disable)
        self.assertEqual(len(processes), 1)

        controller.update(CLOSED_LID_AWAKE_ALWAYS, agents_active=False)
        self.assertEqual(disabled_calls, [True])

        self.assertFalse(
            controller.update(CLOSED_LID_AWAKE_NEVER, agents_active=False)
        )
        self.assertEqual(disabled_calls, [True, False])
        self.assertTrue(processes[0].terminated)

    def test_closed_lid_awake_controller_defaults_to_user_mode_only(self) -> None:
        disabled_calls: list[bool] = []
        controller = ClosedLidAwakeController(
            process_factory=lambda *_args, **_kwargs: FakeProcess(),
            sleep_disabled_reader=lambda: False,
            sleep_disabled_setter=disabled_calls.append,
        )

        self.assertTrue(
            controller.update(CLOSED_LID_AWAKE_ALWAYS, agents_active=False)
        )

        self.assertEqual(disabled_calls, [])
        self.assertTrue(controller.process_running())
        self.assertFalse(controller.changed_system_disable)

    def test_closed_lid_awake_controller_preserves_existing_system_disable(self) -> None:
        disabled_calls: list[bool] = []
        controller = ClosedLidAwakeController(
            process_factory=lambda *_args, **_kwargs: FakeProcess(),
            sleep_disabled_reader=lambda: True,
            sleep_disabled_setter=disabled_calls.append,
            use_system_disable=True,
        )

        controller.update(CLOSED_LID_AWAKE_ALWAYS, agents_active=False)
        controller.update(CLOSED_LID_AWAKE_NEVER, agents_active=False)

        self.assertEqual(disabled_calls, [])

    def test_sleep_override_uses_noninteractive_sudo(self) -> None:
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "", "")

        run_sudo_pmset_disablesleep(True, runner=runner)

        self.assertEqual(
            calls[0][0],
            ["/usr/bin/sudo", "-n", "/usr/bin/pmset", "-a", "disablesleep", "1"],
        )
        self.assertEqual(calls[0][1]["check"], False)

    def test_sleep_override_reports_missing_helper_without_prompting(self) -> None:
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                "sudo: a password is required",
            )

        with self.assertRaises(SleepHelperRequiredError) as ctx:
            run_sudo_pmset_disablesleep(False, runner=runner)

        self.assertIn("install-sleep-helper", str(ctx.exception))
        self.assertNotIn("/usr/bin/osascript", calls[0])

    def test_sleep_helper_sudoers_rule_is_narrow(self) -> None:
        self.assertEqual(
            sleep_helper_sudoers_rule("pero"),
            "pero ALL=(root) NOPASSWD: "
            "/usr/bin/pmset -a disablesleep 0, "
            "/usr/bin/pmset -a disablesleep 1\n",
        )
        with self.assertRaises(ValueError):
            sleep_helper_sudoers_rule("bad user")

    def test_lid_state_parser_reads_ioreg_booleans(self) -> None:
        self.assertTrue(
            parse_bool_ioreg_property('"AppleClamshellState" = Yes', "AppleClamshellState")
        )
        self.assertFalse(
            parse_bool_ioreg_property('"AppleClamshellState" = No', "AppleClamshellState")
        )
        self.assertTrue(parse_bool_ioreg_property('"SleepDisabled" = true', "SleepDisabled"))
        self.assertIsNone(parse_bool_ioreg_property('"Other" = Yes', "SleepDisabled"))

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
                        brightness=128,
                    ),
                )
            )

            save_settings(settings, settings_path)
            loaded = load_settings(settings_path)

            self.assertEqual(loaded.devices, settings.devices)
            self.assertEqual(loaded.display_for_device("/Volumes/SidePulse"), "agent")
            self.assertEqual(loaded.display_for_device("/Volumes/PulseDot"), "battery")
            self.assertEqual(loaded.brightness_for_device("/Volumes/PulseDot"), 128)

    def test_settings_round_trip_remembered_device_brightness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings = AgentMonitorSettings().with_device_brightness(
                "/Volumes/PulseDot",
                96,
                name="PulseDot",
                path="/Volumes/PulseDot",
            )

            save_settings(settings, settings_path)
            loaded = load_settings(settings_path)

            self.assertEqual(loaded.brightness_for_device("/Volumes/PulseDot"), 96)

    def test_settings_round_trip_session_open_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings = AgentMonitorSettings().with_session_open_action(
                "Claude",
                SESSION_OPEN_TERMINAL,
            )

            save_settings(settings, settings_path)
            loaded = load_settings(settings_path)

            self.assertEqual(loaded.session_open_action("claude"), SESSION_OPEN_TERMINAL)

    def test_settings_round_trip_closed_lid_policy_and_animations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings = AgentMonitorSettings().with_closed_lid_awake_policy(
                CLOSED_LID_AWAKE_AGENTS
            )
            settings = settings.with_lid_animation(
                LID_ANIMATION_CLOSED,
                program="off\n#FF3A00 200ms ease",
                duration_seconds=1.4,
            )
            settings = settings.with_lid_animation(
                LID_ANIMATION_OPEN,
                program="off\n#00FF66 200ms ease",
                duration_seconds=1.6,
            )

            save_settings(settings, settings_path)
            loaded = load_settings(settings_path)

            self.assertEqual(loaded.closed_lid_awake_policy, CLOSED_LID_AWAKE_AGENTS)
            self.assertEqual(
                loaded.lid_animation(LID_ANIMATION_CLOSED).program,
                "off\n#FF3A00 200ms ease",
            )
            self.assertEqual(
                loaded.lid_animation(LID_ANIMATION_OPEN).duration_seconds,
                1.6,
            )

            enabled = settings.with_closed_lid_system_override(True)
            save_settings(enabled, settings_path)
            loaded_enabled = load_settings(settings_path)
            self.assertTrue(loaded_enabled.closed_lid_system_override_enabled)

    def test_settings_migrate_missing_lid_fields_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings_path.write_text(json.dumps({"led_display": "agent"}))

            loaded = load_settings(settings_path)

            self.assertEqual(loaded.closed_lid_awake_policy, CLOSED_LID_AWAKE_NEVER)
            self.assertFalse(loaded.closed_lid_system_override_enabled)
            self.assertEqual(
                loaded.lid_animation(LID_ANIMATION_CLOSED),
                default_lid_animation(LID_ANIMATION_CLOSED),
            )

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
        self.assertEqual(remembered.brightness_for_device("/Volumes/PulseDot"), 255)

    def test_settings_remove_remembered_device(self) -> None:
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

        updated = settings.without_device("/Volumes/PulseDot")

        self.assertEqual([device.device_id for device in updated.devices], ["/Volumes/SidePulse"])
        self.assertEqual(updated.display_for_device("/Volumes/PulseDot"), "agent")

    def test_disconnected_device_menu_has_remove_option(self) -> None:
        try:
            from agent_monitor import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        device = status_bar.StatusBarDevice(
            device_id="/Volumes/SidePulse",
            name="SidePulse",
            root=Path("/Volumes/SidePulse"),
            target=Path("/Volumes/SidePulse/LEDS.LED"),
            connected=False,
            display="agent",
        )
        item = status_bar.build_device_menu_item(device, None)
        submenu = item.submenu()
        titles = [
            submenu.itemAtIndex_(index).title()
            for index in range(submenu.numberOfItems())
            if submenu.itemAtIndex_(index).title()
        ]

        self.assertIn("Not connected", titles)
        self.assertIn("Remove", titles)

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

    def test_status_bar_install_removes_legacy_com_sidepulse_plist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = base / "io.sidepulse.agentstatus.plist"
            legacy = base / "com.sidepulse.agentstatus.plist"
            legacy.write_bytes(b"old")

            with (
                patch("agent_monitor.status_bar_launch.default_state_dir", return_value=base / "state"),
                patch("agent_monitor.status_bar_launch.subprocess.run") as run,
            ):
                result = install_launch_agent(
                    start=False,
                    plist_path=target,
                    legacy_plist_path=legacy,
                    python_executable="/usr/bin/python3",
                )

            self.assertTrue(result.changed)
            self.assertTrue(target.exists())
            self.assertFalse(legacy.exists())
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0][0:2], ["launchctl", "bootout"])

    def test_sd_eject_guard_plist_shapes_for_user_and_system_scopes(self) -> None:
        for scope in ("user", "system"):
            paths = SdEjectGuardPaths(
                scope=scope,
                plist_path=Path(f"/tmp/{scope}/io.sidepulse.sdejectguard.plist"),
                binary_path=Path(f"/tmp/{scope}/sd_eject_guard"),
                stdout_path=Path(f"/tmp/{scope}/sd-eject-guard.out.log"),
                stderr_path=Path(f"/tmp/{scope}/sd-eject-guard.err.log"),
            )

            plist = build_sd_eject_guard_plist(paths)

            self.assertEqual(plist["Label"], SD_EJECT_GUARD_LABEL)
            self.assertEqual(plist["ProgramArguments"], [str(paths.binary_path)])
            self.assertTrue(plist["RunAtLoad"])
            self.assertTrue(plist["KeepAlive"])
            self.assertEqual(plist["StandardOutPath"], str(paths.stdout_path))
            self.assertEqual(plist["StandardErrorPath"], str(paths.stderr_path))

    def test_sd_eject_guard_auto_falls_back_to_user_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "sd_eject_guard.c"
            source.write_text("int main(void) { return 0; }\n")
            user_paths = SdEjectGuardPaths(
                scope="user",
                plist_path=base / "user" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "user" / "sd_eject_guard",
                stdout_path=base / "user" / "out.log",
                stderr_path=base / "user" / "err.log",
            )
            system_paths = SdEjectGuardPaths(
                scope="system",
                plist_path=base / "system" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "system" / "sd_eject_guard",
                stdout_path=base / "system" / "out.log",
                stderr_path=base / "system" / "err.log",
            )
            calls = []

            def fake_run(command, *args, **kwargs):
                calls.append(command)
                if command[0] == "clang":
                    Path(command[3]).write_bytes(b"binary")
                return subprocess.CompletedProcess(command, 0)

            with (
                patch("agent_monitor.sd_eject_guard_launch.os.geteuid", return_value=501),
                patch("agent_monitor.sd_eject_guard_launch.os.getuid", return_value=501),
                patch("agent_monitor.sd_eject_guard_launch.subprocess.run", side_effect=fake_run),
            ):
                result = install_sd_eject_guard(
                    scope="auto",
                    source_path=source,
                    user_paths=user_paths,
                    system_paths=system_paths,
                )

            self.assertEqual(result.scope, "user")
            self.assertTrue(result.compiled)
            self.assertTrue(result.started)
            self.assertTrue(user_paths.binary_path.exists())
            self.assertTrue(user_paths.plist_path.exists())
            self.assertEqual(calls[0][0:4], ["clang", "-O2", "-o", str(user_paths.binary_path.with_name("sd_eject_guard.tmp"))])
            self.assertIn("-framework", calls[0])
            self.assertIn(["launchctl", "bootstrap", "gui/501", str(user_paths.plist_path)], calls)

    def test_sd_eject_guard_user_install_reports_skipped_system_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "sd_eject_guard.c"
            source.write_text("int main(void) { return 0; }\n")
            user_paths = SdEjectGuardPaths(
                scope="user",
                plist_path=base / "user" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "user" / "sd_eject_guard",
                stdout_path=base / "user" / "out.log",
                stderr_path=base / "user" / "err.log",
            )
            system_paths = SdEjectGuardPaths(
                scope="system",
                plist_path=base / "system" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "system" / "sd_eject_guard",
                stdout_path=base / "system" / "out.log",
                stderr_path=base / "system" / "err.log",
            )
            system_paths.plist_path.parent.mkdir(parents=True)
            system_paths.plist_path.write_bytes(b"old")

            def fake_run(command, *args, **kwargs):
                if command[0] == "clang":
                    Path(command[3]).write_bytes(b"binary")
                return subprocess.CompletedProcess(command, 0)

            with (
                patch("agent_monitor.sd_eject_guard_launch.os.geteuid", return_value=501),
                patch("agent_monitor.sd_eject_guard_launch.os.getuid", return_value=501),
                patch("agent_monitor.sd_eject_guard_launch.subprocess.run", side_effect=fake_run),
            ):
                result = install_sd_eject_guard(
                    scope="user",
                    source_path=source,
                    user_paths=user_paths,
                    system_paths=system_paths,
                )

            self.assertIsNone(result.cleanup_removed)
            self.assertIn(str(system_paths.plist_path), result.cleanup_skipped or "")
            self.assertTrue(system_paths.plist_path.exists())

    def test_sd_eject_guard_stop_auto_stops_user_and_skips_system_without_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            user_paths = SdEjectGuardPaths(
                scope="user",
                plist_path=base / "user" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "user" / "sd_eject_guard",
                stdout_path=base / "user" / "out.log",
                stderr_path=base / "user" / "err.log",
            )
            system_paths = SdEjectGuardPaths(
                scope="system",
                plist_path=base / "system" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "system" / "sd_eject_guard",
                stdout_path=base / "system" / "out.log",
                stderr_path=base / "system" / "err.log",
            )
            user_paths.plist_path.parent.mkdir(parents=True)
            system_paths.plist_path.parent.mkdir(parents=True)
            user_paths.plist_path.write_bytes(b"user")
            system_paths.plist_path.write_bytes(b"system")

            with (
                patch("agent_monitor.sd_eject_guard_launch.os.geteuid", return_value=501),
                patch("agent_monitor.sd_eject_guard_launch.os.getuid", return_value=501),
                patch("agent_monitor.sd_eject_guard_launch.subprocess.run") as run,
            ):
                results = stop_sd_eject_guard(
                    scope="auto",
                    user_paths=user_paths,
                    system_paths=system_paths,
                )

            self.assertEqual(len(results), 2)
            self.assertTrue(results[0].stopped)
            self.assertFalse(results[1].stopped)
            self.assertIn("missing permissions", results[1].skipped or "")
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0][0:3], ["launchctl", "bootout", "gui/501"])

    def test_sd_eject_guard_system_scope_requires_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "sd_eject_guard.c"
            source.write_text("int main(void) { return 0; }\n")
            system_paths = SdEjectGuardPaths(
                scope="system",
                plist_path=base / "system" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "system" / "sd_eject_guard",
                stdout_path=base / "system" / "out.log",
                stderr_path=base / "system" / "err.log",
            )

            with patch("agent_monitor.sd_eject_guard_launch.os.geteuid", return_value=501):
                with self.assertRaisesRegex(SdEjectGuardInstallError, "requires root"):
                    install_sd_eject_guard(
                        scope="system",
                        source_path=source,
                        system_paths=system_paths,
                    )

    def test_sd_eject_guard_system_install_cleans_user_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "sd_eject_guard.c"
            source.write_text("int main(void) { return 0; }\n")
            user_paths = SdEjectGuardPaths(
                scope="user",
                plist_path=base / "user" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "user" / "sd_eject_guard",
                stdout_path=base / "user" / "out.log",
                stderr_path=base / "user" / "err.log",
            )
            system_paths = SdEjectGuardPaths(
                scope="system",
                plist_path=base / "system" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "system" / "sd_eject_guard",
                stdout_path=base / "system" / "out.log",
                stderr_path=base / "system" / "err.log",
            )
            user_paths.plist_path.parent.mkdir(parents=True)
            user_paths.plist_path.write_bytes(b"old")
            calls = []

            def fake_run(command, *args, **kwargs):
                calls.append(command)
                if command[0] == "clang":
                    Path(command[3]).write_bytes(b"binary")
                return subprocess.CompletedProcess(command, 0)

            with (
                patch("agent_monitor.sd_eject_guard_launch.os.geteuid", return_value=0),
                patch("agent_monitor.sd_eject_guard_launch.os.getuid", return_value=501),
                patch("agent_monitor.sd_eject_guard_launch.os.chown") as chown,
                patch("agent_monitor.sd_eject_guard_launch.subprocess.run", side_effect=fake_run),
            ):
                result = install_sd_eject_guard(
                    scope="system",
                    source_path=source,
                    user_paths=user_paths,
                    system_paths=system_paths,
                )

            self.assertEqual(result.scope, "system")
            self.assertEqual(result.cleanup_removed, user_paths.plist_path)
            self.assertFalse(user_paths.plist_path.exists())
            self.assertTrue(system_paths.plist_path.exists())
            self.assertIn(["launchctl", "bootstrap", "system", str(system_paths.plist_path)], calls)
            chown.assert_any_call(system_paths.binary_path, 0, 0)
            chown.assert_any_call(system_paths.plist_path, 0, 0)

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
        self.assertEqual(
            session_vscode_link(status),
            "vscode://anthropic.claude-code/open?session=1ca4348e-2aec-4147-9e81-d7d56364d257",
        )
        self.assertEqual(default_session_open_action(status), "vscode")
        self.assertEqual(
            session_open_target(status, "vscode"),
            (
                "url",
                "vscode://anthropic.claude-code/open?session=1ca4348e-2aec-4147-9e81-d7d56364d257",
            ),
        )


if __name__ == "__main__":
    unittest.main()
