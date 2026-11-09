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
            self.assertEqual(metadata["python"], str(state.resolve() / ".venv/bin/python"))
            self.assertEqual(subprocess.run([metadata["python"], "--version"],
                            capture_output=True).returncode, 0)
            self.assertEqual(metadata["launcher"], str((ROOT / "companion/launch.py").resolve()))
            self.assertTrue(Path(metadata["launcher"]).is_file())
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
            self.assertEqual(metadata_path.stat().st_mode & 0o777, 0o600)
            user_launcher = state / "Open ATAS AI Review.command"
            self.assertTrue(os.access(user_launcher, os.X_OK))
            self.assertIn("--open", user_launcher.read_text())
            self.assertIn("--state-dir", user_launcher.read_text())
            self.assertIn(str(state), user_launcher.read_text())
            self.assertIn("chart button", result.stdout.lower())
            help_result = subprocess.run(["/bin/bash", str(ROOT / "install-workbench.sh"),
                                          "--help"], capture_output=True, text=True)
            self.assertIn("chart button", help_result.stdout.lower())

    def test_optional_journal_config_is_private_and_preserved_on_upgrade(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            script = root / "journal.py"
            database = root / "journal.sqlite"
            script.write_text("# synthetic test fixture\n")
            database.write_bytes(b"synthetic")
            base = ["/bin/bash", str(ROOT / "install-workbench.sh"), "--install",
                    "--state-dir", str(state), "--python", sys.executable]
            extra = ["--journal-python", sys.executable, "--journal-script", str(script),
                     "--journal-db", str(database), "--journal-account", "SIM"]
            first = subprocess.run(base + extra, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            path = state / "journal.json"
            expected = {"python_path": str(Path(sys.executable).resolve()),
                        "script_path": str(script.resolve()), "db_path": str(database.resolve()),
                        "account_id": "SIM"}
            self.assertEqual(json.loads(path.read_text()), expected)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            second = subprocess.run(base, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(path.read_text()), expected)
            removed = subprocess.run(base + ["--disable-journal"], capture_output=True, text=True)
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertFalse(path.exists())
            self.assertTrue(database.exists(), "disabling adapter must preserve journal data")

    def test_incomplete_or_invalid_journal_config_does_not_write_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            base = ["/bin/bash", str(ROOT / "install-workbench.sh"), "--install",
                    "--state-dir", str(state), "--python", sys.executable]
            for extra in (["--journal-account", "SIM"],
                          ["--journal-python", sys.executable, "--journal-script", __file__,
                           "--journal-db", "/nonexistent/journal.sqlite", "--journal-account", "SIM"],
                          ["--disable-journal", "--journal-account", "SIM"]):
                with self.subTest(extra=extra):
                    result = subprocess.run(base + extra, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse((state / "installation.json").exists())


if __name__ == "__main__":
    unittest.main()
