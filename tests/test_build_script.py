"""Exercise the macOS installer against temporary SDK/tool stubs, never ATAS."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SDK_DLLS = ("ATAS.Indicators", "ATAS.DataFeedsCore", "ATAS.Strategies",
            "ATAS.Types", "Utils.Common")


class BuildScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="atas build test ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "source with spaces"
        self.repo.mkdir()
        shutil.copy2(ROOT / "build.sh", self.repo / "build.sh")
        project = self.repo / "src" / "ATAS.AiBridge"
        project.mkdir(parents=True)
        (project / "ATAS.AiBridge.csproj").write_text("<Project />")
        self.sdk = self.root / "ATAS X.app" / "Contents" / "MonoBundle"
        self.sdk.mkdir(parents=True)
        for name in SDK_DLLS:
            (self.sdk / (name + ".dll")).touch()
        self.user_dir = self.root / "Application Support" / "ATAS"
        self.target = self.user_dir / "Strategies" / "ATAS.AiBridge.dll"
        self.log = self.root / "build-args.json"
        tools = self.root / "tools"
        tools.mkdir()
        self.dotnet = tools / "dotnet"
        self._tool(self.dotnet, """
import json, os, pathlib, sys
pathlib.Path(os.environ['TEST_BUILD_LOG']).write_text(json.dumps(sys.argv[1:]))
if os.environ.get('TEST_BUILD_FAIL'):
    sys.exit(7)
if not os.environ.get('TEST_NO_OUTPUT'):
    out = pathlib.Path(os.environ['TEST_REPO']) / 'out'
    out.mkdir(exist_ok=True)
    (out / 'ATAS.AiBridge.dll').write_bytes(b'new plugin')
""")
        self._tool(tools / "uname", "print('Darwin')")
        self._tool(tools / "pgrep", """
import os, sys
sys.exit(0 if sys.argv[-1] == os.environ.get('TEST_RUNNING_PROCESS') else 1)
""")
        self.env = dict(os.environ)
        self.env.update(PATH=str(tools) + os.pathsep + os.defpath,
                        DOTNET=str(self.dotnet), ATAS_BASE=str(self.sdk),
                        ATAS_USER_DIR=str(self.user_dir), TEST_REPO=str(self.repo),
                        TEST_BUILD_LOG=str(self.log))

    @staticmethod
    def _tool(path, body):
        path.write_text("#!" + sys.executable + "\n" + body.lstrip())
        path.chmod(0o755)

    def run_script(self, *args, **env):
        return subprocess.run(["/bin/bash", str(self.repo / "build.sh"), *args],
                              cwd=self.root, env=self.env | env,
                              text=True, capture_output=True, timeout=15)

    def test_default_build_does_not_install(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.repo / "out" / "ATAS.AiBridge.dll").exists())
        self.assertFalse(self.user_dir.exists())
        args = json.loads(self.log.read_text())
        self.assertIn("-p:ATAS_BASE=" + str(self.sdk), args)
        self.assertIn(str(self.repo / "src/ATAS.AiBridge/ATAS.AiBridge.csproj"), args)

    def test_explicit_install_only_copies_plugin(self):
        result = self.run_script("--install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.read_bytes(), b"new plugin")
        self.assertEqual([p.name for p in self.target.parent.iterdir()],
                         ["ATAS.AiBridge.dll"])

    def test_existing_plugin_is_backed_up(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"old plugin")
        result = self.run_script("--install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.read_bytes(), b"new plugin")
        backups = list(self.target.parent.glob("ATAS.AiBridge.dll.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"old plugin")

    def test_install_refused_for_both_platform_process_names(self):
        for process in ("ATAS X", "OFT.Platform"):
            with self.subTest(process=process):
                result = self.run_script("--install", TEST_RUNNING_PROCESS=process)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("running", result.stderr)
                self.assertFalse(self.target.exists())

    def test_force_install_requires_explicit_flag(self):
        result = self.run_script("--install", "--force", TEST_RUNNING_PROCESS="ATAS X")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.target.exists())

    def test_build_failure_leaves_existing_plugin_untouched(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"old plugin")
        result = self.run_script("--install", TEST_BUILD_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.target.read_bytes(), b"old plugin")
        self.assertEqual(len(list(self.target.parent.iterdir())), 1)

    def test_missing_sdk_does_not_invoke_dotnet(self):
        (self.sdk / "ATAS.Indicators.dll").unlink()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ATAS.Indicators.dll", result.stderr)
        self.assertFalse(self.log.exists())

    def test_no_build_output_does_not_install(self):
        result = self.run_script("--install", TEST_NO_OUTPUT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.target.exists())

    def test_options_accept_paths_with_spaces(self):
        result = self.run_script("--configuration", "Debug", "--atas-base", str(self.sdk),
                                 "--atas-user-dir", str(self.user_dir), "--install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Debug", json.loads(self.log.read_text()))
        self.assertTrue(self.target.exists())

    def test_relative_sdk_path_is_resolved_from_callers_directory(self):
        relative = os.path.relpath(self.sdk, self.root)
        result = self.run_script("--atas-base", relative)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.log.read_text())
        passed = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("-p:ATAS_BASE=")))
        self.assertTrue(passed.is_absolute())
        self.assertEqual(passed.resolve(), self.sdk.resolve())

    def test_bad_options_fail_before_build(self):
        for args in (("--unknown",), ("--configuration",), ("--atas-base",),
                     ("--atas-user-dir",), ("--force",)):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.log.exists())

    def test_help_needs_neither_sdk_nor_dotnet(self):
        result = self.run_script("--help", ATAS_BASE="/missing", DOTNET="/missing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--install", result.stdout)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
