"""Provider-neutral AI agent status monitoring."""

from .battery import (
    BatteryLedController,
    BatterySnapshot,
    program_for_battery,
    read_battery_snapshot,
)
from .collector import AgentMonitor, LiveAgentMonitor, MonitorSnapshot, SourceSpec
from .ipc import (
    HookEventServer,
    default_event_socket_path,
    default_latest_state_path,
    send_hook_event,
)
from .led_status import (
    AgentLedController,
    LedDisplayState,
    display_state_for_mode,
    program_for_display_state,
    write_mode_to_leds,
)
from .models import AgentMode, AgentStatus, AggregateStatus, HookEvent

__all__ = [
    "AgentLedController",
    "AgentMode",
    "AgentStatus",
    "AggregateStatus",
    "BatteryLedController",
    "BatterySnapshot",
    "AgentMonitor",
    "LiveAgentMonitor",
    "HookEvent",
    "HookEventServer",
    "LedDisplayState",
    "MonitorSnapshot",
    "SourceSpec",
    "default_event_socket_path",
    "default_latest_state_path",
    "display_state_for_mode",
    "program_for_battery",
    "read_battery_snapshot",
    "send_hook_event",
    "program_for_display_state",
    "write_mode_to_leds",
]

__version__ = "0.1.0"
