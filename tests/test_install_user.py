from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


class UserInstallerTests(unittest.TestCase):
    def test_installer_uses_isolated_venv_and_user_bin(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "install-user.sh"
        text = script.read_text()
        self.assertIn('"$PYTHON_BIN" -m venv "$VENV_DIR"', text)
        self.assertIn('"$VENV_DIR/bin/python" -m pip install', text)
        self.assertNotIn("--break-system-packages", text)

    def test_installer_shell_syntax(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["sh", "-n", str(root / "scripts" / "install-user.sh")],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
