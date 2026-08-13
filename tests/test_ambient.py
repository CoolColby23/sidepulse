from __future__ import annotations

import unittest

from sidepulse.ambient import (
    BatteryThresholdAlert,
    battery_warning_program,
    music_visualizer_program,
    rms_to_level,
)
from sidepulse.collector import COMPLETED_VISIBLE_SECONDS
from sidepulse.device_writer import validate_led_text
from sidepulse.models import AgentMode


class AmbientFeatureTests(unittest.TestCase):
    def test_completed_status_clears_after_fifteen_seconds(self) -> None:
        self.assertEqual(COMPLETED_VISIBLE_SECONDS, 15.0)

    def test_battery_thresholds_trigger_only_on_downward_crossings(self) -> None:
        alerts = BatteryThresholdAlert()
        self.assertIsNone(alerts.update(31, AgentMode.IDLE_READY, now=0))
        self.assertEqual(alerts.update(30, AgentMode.IDLE_READY, now=1), 30)
        self.assertTrue(alerts.active(now=15.9))
        self.assertFalse(alerts.active(now=16.0))
        self.assertIsNone(alerts.update(29, AgentMode.IDLE_READY, now=17))
        self.assertEqual(alerts.update(20, AgentMode.IDLE_READY, now=18), 20)

    def test_running_agent_uses_five_second_warning(self) -> None:
        alerts = BatteryThresholdAlert()
        alerts.update(21, AgentMode.IDLE_READY, now=0)
        self.assertEqual(alerts.update(20, AgentMode.WORKING, now=10), 20)
        self.assertTrue(alerts.active(now=14.9))
        self.assertFalse(alerts.active(now=15.0))

    def test_agent_start_caps_an_existing_idle_warning(self) -> None:
        alerts = BatteryThresholdAlert()
        alerts.update(31, AgentMode.IDLE_READY, now=0)
        alerts.update(30, AgentMode.IDLE_READY, now=1)
        alerts.adjust_for_mode(AgentMode.TOOL_RUNNING, now=3)
        self.assertTrue(alerts.active(now=5.9))
        self.assertFalse(alerts.active(now=6.0))

    def test_warning_and_music_programs_fit_device_dsl(self) -> None:
        validate_led_text(battery_warning_program())
        program = music_visualizer_program(0.625, led_count=8)
        validate_led_text(program)
        self.assertIn("0:#", program)
        self.assertIn("7:#", program)

    def test_audio_level_mapping_rejects_silence_and_scales_music(self) -> None:
        self.assertEqual(rms_to_level(0.0), 0.0)
        self.assertGreater(rms_to_level(0.05), 0.25)
        self.assertLessEqual(rms_to_level(1.0), 1.0)


if __name__ == "__main__":
    unittest.main()
