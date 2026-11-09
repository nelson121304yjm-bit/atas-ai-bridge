"""Installation metadata is private and explicit, without touching ATAS."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def test_installer_requires_explicit_install_and_writes_private_launcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "ATAS AI Review"
            command = ["/bin/bash", str(ROOT / "install-workbench.sh"),
                       "--state-dir", str(state), "--python", sys.executable]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(state.exists())
            result = subprocess.run(command + ["--install"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            metadata_path = state / "installation.json"
            metadata = json.loads(metadata_path.read_text())
            self.assertEqual(metadata["python"], str(Path(sys.executable).resolve()))
            self.assertEqual(metadata["launcher"], str((ROOT / "companion/launch.py").resolve()))
            self.assertTrue(Path(metadata["launcher"]).is_file())
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
            self.assertEqual(metadata_path.stat().st_mode & 0o777, 0o600)
            user_launcher = state / "Open ATAS AI Review.command"
            self.assertTrue(os.access(user_launcher, os.X_OK))
            self.assertIn("--open", user_launcher.read_text())
            self.assertIn("--state-dir", user_launcher.read_text())
            self.assertIn(str(state), user_launcher.read_text())


if __name__ == "__main__":
    unittest.main()
