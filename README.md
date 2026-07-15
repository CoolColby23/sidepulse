# SidePulse

SidePulse is the overall product for small LED status devices and the local
agent-monitoring tools that drive them.

The SidePulse device is the eight-LED SD card slot version for MacBook Pro.
PulseDot is the tiny two-LED USB-C version.

They can display the status of an AI agent, battery level, or other system
signals.

The device mounts as a flash drive. Control the LEDs by writing to `LEDS.LED`
on current firmware. Older PulseDot firmware exposes the same control as
`LEDS.TXT`, and the CLI keeps that fallback.

The LED control DSL is described in [`LEDS_FORMAT.txt`](LEDS_FORMAT.txt).

Write an LED program directly to a mounted SidePulse or PulseDot device:

```sh
sidepulse write "off\n#ff3a00 1.6s pulse\nrepeat"
```

The CLI auto-detects mounted devices under `/Volumes` by looking for a
SidePulse/PulseDot-style volume name, an existing `LEDS.LED`, or a legacy
`LEDS.TXT`. If more than one device is possible, pass the mounted folder or
file explicitly:

```sh
sidepulse write "off\n#ff3a00 1.6s pulse\nrepeat" --device /Volumes/SidePulse
sidepulse write "off" --device /Volumes/SidePulse/LEDS.LED
sidepulse write "off" --device /Volumes/PulseDot/LEDS.TXT
```

The writer decodes simple escapes such as `\n`, then enforces the controller's
512-byte and 20-line limits before writing the LED control file.

## Battery LEDs

Show the current Mac battery state:

```sh
sidepulse battery status
sidepulse battery status --json
```

Mirror battery level to a mounted SidePulse/PulseDot:

```sh
sidepulse battery leds
sidepulse battery leds --once --dry-run
sidepulse battery leds --device /Volumes/SidePulse --full-watts 140
```

SidePulse uses all eight LEDs as a battery bar. At 50%, LEDs 0-3 are filled;
when charging, LED 4 is the pulsing frontier LED. Live updates ease the whole
strip into its new base state, then trigger one frontier pulse. The app owns
the animation cadence by rewriting that one-shot pulse; the device does not run
a repeated charging loop. Pulse length and rewrite frequency are based on
charger wattage divided by the laptop's full-speed wattage baseline, so slow
chargers produce occasional short blinks and full-speed chargers produce a
steady pulse.

Save the status-bar LED display preference:

```sh
sidepulse battery configure --display battery
sidepulse battery configure --display agent
sidepulse battery configure --full-watts auto
sidepulse battery configure --show-on-power-change yes --power-change-preview-seconds 7
```

## SidePulse Bridge

SidePulse Bridge is a companion app for macOS that controls SidePulse devices.

### Main Functionality

#### AI Agent Monitoring

SidePulse can monitor AI agents such as Codex and Claude through hooks, then
translate the current agent state into a small, glanceable LED status.

Agent status modes:

| Mode | Meaning | LED pattern |
| --- | --- | --- |
| Idle / Ready | The agent is available and not currently running a task. | Very dim idle pulse. |
| Working | The agent is thinking, generating, or otherwise actively processing. | Cyan rolling animation. |
| Tool Running | A shell command, API call, or external tool is in progress. | Cyan rolling animation. |
| Waiting for Input | The agent needs a user decision, approval, or additional context. | Slow amber pulse. |
| Long Task Progress | A longer job has measurable progress. | Cyan rolling animation. |
| Blocked / Error | The agent cannot continue, a tool failed, or a recoverable error needs attention. | Slow amber pulse. |
| Completed | The agent finished successfully. | Solid green. |

When multiple states are active, SidePulse should show the most actionable
mode first: Blocked / Error, Waiting for Input, Tool Running, Long Task
Progress, Working, then Idle / Ready.

For multiple agents, SidePulse aggregates their statuses into one global
display state. Each agent reports its own mode, and SidePulse renders the
highest-priority active mode across all non-stale agents. This keeps the device
useful at a glance: if any agent is blocked or waiting, the LEDs show that
actionable state instead of trying to show every agent separately.

Aggregation priority:

| Priority | Mode | Aggregated behavior |
| --- | --- | --- |
| 1 | Blocked / Error | Show immediately if any agent is blocked or has errored. |
| 2 | Waiting for Input | Show if any agent needs user input and no agent is blocked. |
| 3 | Tool Running | Show if any agent is running a tool and no higher-priority state is active. |
| 4 | Long Task Progress | Show the most recent or furthest-progressing long task. |
| 5 | Working | Show while one or more agents are actively processing. |
| 6 | Completed | Show briefly when the latest active agent completes successfully. |
| 7 | Idle / Ready | Show only when all known agents are idle or no fresh agent status exists. |

Agent statuses should include a timestamp. SidePulse should ignore stale
statuses after a short timeout so disconnected or finished agents do not hold
the display indefinitely.

#### Agent Monitor Library

The `agent_monitor` Python package collects and normalizes local AI agent hook
events. The macOS status-bar app receives hook events through a lightweight
local Unix socket, keeps the latest agent states in memory, and writes only a
small `latest.json` restart snapshot plus provider JSONL debug logs. It does
not rescan historical logs or transcripts on every refresh.

The package can also mirror the aggregate state to a mounted SidePulse or
PulseDot by writing the current LED program to `LEDS.LED`, with `LEDS.TXT`
fallback for legacy firmware.

The monitor currently supports:

| Provider | Config | Detected log |
| --- | --- | --- |
| Codex | `~/.codex/config.toml` | `${XDG_STATE_HOME:-~/.local/state}/sidepulse/agent-monitor/codex.jsonl` |
| Claude | `~/.claude/settings.json` | `${XDG_STATE_HOME:-~/.local/state}/sidepulse/agent-monitor/claude.jsonl` |

For CLI snapshots, debugging, or recovery after missed hook events, the
file-based monitor can optionally read recent local transcripts as a fallback:

- Codex: `~/.codex/sessions/**/*.jsonl`
- Claude: `~/.claude/projects/**/*.jsonl`

Transcript monitoring is off by default and can be enabled in Settings. It can
catch active threads even when hook events are stale or missed. Claude
transcript files can be touched after their embedded event timestamps stop
moving, so a recent transcript mtime is treated as a Working heartbeat only
when the latest embedded event was already active. File mtimes never resurrect
a terminal `Stop` / `Completed` session. Internal Codex helper/suggestion
transcripts are ignored so app background work does not look like one of your
agents.

By default the monitor stores runtime logs under
`~/.local/state/sidepulse/agent-monitor/`, following the XDG state directory
convention. Set `XDG_STATE_HOME` to place them somewhere else.

Install locally for the `sidepulse` CLI:

```sh
python3 -m pip install -e .
```

To use the macOS status-bar app, install its Cocoa extra:

```sh
python3 -m pip install -e ".[status-bar]"
```

On Homebrew Python, use the user-site install form:

```sh
python3 -m pip install --user --break-system-packages -e .
ln -sf "$(python3 -m site --user-base)/bin/sidepulse" ~/.local/bin/sidepulse
```

Check the current hook configuration:

```sh
sidepulse agent-monitor doctor
```

Install or refresh the monitor hooks:

```sh
sidepulse agent-monitor install
sidepulse agent-monitor install codex
sidepulse agent-monitor install claude
```

Each hook invokes a small, standard-library-only Python entry point. It writes
the event to the monitor log and then makes a short best-effort local socket
delivery to the status-bar app.

Show current aggregated status:

```sh
sidepulse agent-monitor status
```

Watch a live dashboard of recently active agents:

```sh
sidepulse agent-monitor live
```

The dashboard refreshes every second and shows agents updated in the last hour
by default. Use `--recent-seconds` to change that window, or `--all` to
include stale/older sessions:

```sh
sidepulse agent-monitor live --recent-seconds 120
sidepulse agent-monitor live --all
```

By default, `Tool Running` events are not time-limited, so genuinely long tools
remain visible. If a provider drops completion hooks and you want protection
against stale tool starts, set `--tool-running-timeout`.

`Completed` remains visible for 20 minutes so the status bar and LEDs can show
Done long enough to be noticed. After that it drops out instead of counting as
an active session for the full stale window, and the LEDs return to the very
dim Idle pattern. Idle/session-start records also do not count as active
sessions.

Status detection is strongest when the agent tells the monitor its intended
handoff state explicitly. A final assistant message can include a hidden marker
line:

```text
<!-- sidepulse:ask -->
<!-- sidepulse:done -->
<!-- sidepulse:working -->
<!-- sidepulse:blocked -->
<!-- sidepulse:idle -->
```

Explicit markers win over text heuristics. If no marker is present, the monitor
falls back to provider events and then to conservative question detection in the
final assistant message. Casual closing questions such as "Anything else?" are
treated as Done unless the agent emits `<!-- sidepulse:ask -->`; concrete
follow-ups such as "Want me to push?" still count as Ask. Questions inside
markdown code spans or fenced code examples are ignored.

Codex `PermissionRequest` events are treated as Ask and remain sticky until the
matching tool command finishes. This prevents unrelated same-session activity
from hiding an approval prompt that is still waiting on the user.

For Codex or Claude projects that should report this reliably, add guidance like
this to the relevant agent instructions:

```text
When your final response needs user input, approval, or a decision, include
`<!-- sidepulse:ask -->` as a final hidden marker line. When the work is complete
and no user response is needed, include `<!-- sidepulse:done -->`.
```

Mirror the aggregate agent status to the LEDs in a foreground process:

```sh
sidepulse agent-monitor leds
```

The LED mirror writes only when the aggregate display state changes. Use
`--once` to write the current state and exit, or `--dry-run` to inspect the LED
program:

```sh
sidepulse agent-monitor leds --once --dry-run
sidepulse agent-monitor leds --device /Volumes/PulseDot
```

PulseDot programs are generated for two LEDs. SidePulse programs are generated
for eight LEDs. The monitor detects this from the mounted device name and falls
back to the eight-LED SidePulse layout if the name is unknown.

Remove monitor hooks:

```sh
sidepulse agent-monitor uninstall
sidepulse agent-monitor uninstall codex
sidepulse agent-monitor uninstall claude
```

Install and start the macOS status-bar app:

```sh
sidepulse agent-monitor status-bar
```

This writes `~/Library/LaunchAgents/com.sidepulse.agentstatus.plist`, starts the
menu-bar app immediately, enables it at login, and mirrors the same aggregate
state to the LEDs. For debugging, run it in the foreground:

```sh
sidepulse agent-monitor status-bar --foreground
```

The status-bar item shows one of four collapsed states:

| Label | Meaning |
| --- | --- |
| Idle | No recent active agent work. |
| Working | One or more agents are thinking, running tools, or progressing. |
| Done | The most recent active agent completed successfully. |
| Ask | An agent needs input, permission, or attention. |

Click the status-bar item to expand the recent session list. Each session row
has two actions:

- `Deep Link` opens the provider app. Codex uses `codex://threads/<session-id>`.
- `Resume` opens Terminal in the session working directory and runs the
  provider CLI resume command.

The dropdown also includes a checked `Connect to Device` item. A checkmark means
the status-bar app is actively connected to a mounted SidePulse/PulseDot target.
If both devices are mounted, the status-bar app prefers SidePulse, then
PulseDot. Click the item to disconnect and turn the LEDs off; click it again to
reconnect.

The dropdown and Settings window can switch the LEDs between agent status and
battery status. When agent status is selected, `Show Battery on Plug/Unplug`
can briefly show the battery animation for seven seconds after the power source
changes.

Open `Settings...` from the dropdown to manage agent integrations. The settings
window can install or uninstall Codex and Claude hooks. The transcript
checkboxes control the file-based CLI/debug fallback; the status-bar app gets
live updates from the local hook event socket. Settings are stored at
`${XDG_CONFIG_HOME:-~/.config}/sidepulse/agent-monitor/settings.json`.

The checked `Keep Awake` item controls a macOS `caffeinate -dimsu` assertion.
When enabled, the status-bar app keeps the Mac awake while agents are Working /
Tool Running / Progressing. Done and Ask states keep the Mac awake for a
five-minute grace period, then release the assertion so normal sleep can resume.
While keep-awake is enabled and a device is connected, the app also touches a
`keepalive` file on each SidePulse/PulseDot volume at least once per minute to
keep the device volume active. Closed-lid sleep is still subject to macOS
clamshell rules.

The app is also installed as a user LaunchAgent at
`~/Library/LaunchAgents/com.sidepulse.agentstatus.plist`.

Stop and remove the LaunchAgent:

```sh
sidepulse agent-monitor status-bar --uninstall
```

Use it from another Python app:

```python
from agent_monitor import AgentMonitor, LiveAgentMonitor

snapshot = AgentMonitor.from_default_sources().snapshot()
print(snapshot.aggregate.mode.value)
for status in snapshot.statuses:
    print(status.provider, status.mode.value, status.cwd)

live = LiveAgentMonitor()
```

Publish a hook-shaped event to the status-bar app from another local process:

```python
from agent_monitor import send_hook_event

send_hook_event(
    "codex",
    {
        "logged_at": "2026-07-13T12:00:00Z",
        "event": {
            "hook_event_name": "Stop",
            "session_id": "example",
            "last_assistant_message": "Done.",
        },
    },
)
```

#### Audio Monitor Example

`examples/audio_monitor.py` turns microphone volume into a smooth LED level
bar. The LEDs stay dim at rest, run green through yellow to red, and brighten as
the audio level fills the bar.

Install the optional live-audio dependencies:

```sh
python3 -m pip install sounddevice numpy
```

Preview the meter in the terminal without touching a device:

```sh
python3 examples/audio_monitor.py --dry-run --terminal
```

Write to a mounted SidePulse or PulseDot:

```sh
python3 examples/audio_monitor.py --device /Volumes/SidePulse --terminal
python3 examples/audio_monitor.py --device /Volumes/PulseDot --terminal
```

List audio inputs or tune sensitivity:

```sh
python3 examples/audio_monitor.py --list-inputs
python3 examples/audio_monitor.py --device /Volumes/SidePulse --gain-db 8 --release 0.45
```

#### Battery Monitor

...

#### 
