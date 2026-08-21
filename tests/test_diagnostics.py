from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sidepulse.cli import cmd_doctor, diagnostics_bundle_preview, path_diagnostic, write_diagnostics_bundle


class DiagnosticsTests(unittest.TestCase):
    def test_doctor_reports_every_detected_provider(self) -> None:
        configs = [
            SimpleNamespace(provider=name, to_dict=lambda name=name: {"provider": name})
            for name in ("codex", "claude", "grok", "antigravity", "cursor", "kiro", "opencode")
        ]
        args = SimpleNamespace(json=True, verbose=False, bundle=None, preview_bundle=False)
        with (
            patch("sidepulse.cli.detect_provider_configs", return_value=configs),
            patch("builtins.print") as output,
        ):
            self.assertEqual(cmd_doctor(args), 0)
        report = json.loads(output.call_args.args[0])
        self.assertEqual(
            [provider["provider"] for provider in report["providers"]],
            [config.provider for config in configs],
        )

    def test_path_diagnostic_reports_missing_and_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(path_diagnostic(root / "missing")["state"], "missing")
            empty = root / "empty.log"
            empty.touch()
            self.assertEqual(path_diagnostic(empty)["state"], "empty")

    def test_bundle_contains_machine_readable_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = write_diagnostics_bundle(
                Path(tmp) / "support",
                {"providers": [], "runtime": {"event_socket": {"state": "missing"}}},
            )
            self.assertEqual(target.suffix, ".zip")
            with zipfile.ZipFile(target) as archive:
                report = json.loads(archive.read("doctor.json"))
            self.assertEqual(report["runtime"]["event_socket"]["state"], "missing")

    def test_privacy_preview_names_included_and_excluded_categories(self) -> None:
        preview = diagnostics_bundle_preview({"providers": [], "runtime": {}})
        archive_paths = {item["archive_path"] for item in preview["files"]}
        self.assertIn("doctor.json", archive_paths)
        self.assertIn("logs/status-bar.err.log", archive_paths)
        self.assertIn("provider event logs", preview["excluded"])
        self.assertIn("agent transcripts", preview["excluded"])
        self.assertEqual(preview["doctor_json"], {"providers": [], "runtime": {}})
        self.assertNotIn("bundle_preview", preview["doctor_json"])


if __name__ == "__main__":
    unittest.main()
