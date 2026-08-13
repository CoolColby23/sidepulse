from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from sidepulse.cli import path_diagnostic, write_diagnostics_bundle


class DiagnosticsTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
