"""Port-file discovery without starting ATAS or making network requests."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
import atas_client  # noqa: E402


class PortDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write_port(self, base, directory, value):
        target = base / directory / "AiBridge.port"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding="utf-8")
        return target

    def test_macos_reads_atas_then_atas_x_under_application_support(self):
        base = self.root / "Library" / "Application Support"
        first = self.write_port(base, "ATAS", "34001\n")
        self.write_port(base, "ATAS X", "34002\n")

        with mock.patch.dict(os.environ, {"HOME": str(self.root)}, clear=True), \
             mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(atas_client.discover_port(), 34001)
            first.unlink()
            self.assertEqual(atas_client.discover_port(), 34002)

    def test_windows_reads_existing_appdata_candidates_in_order(self):
        appdata = self.root / "Roaming"
        first = self.write_port(appdata, "ATAS", "34003")
        self.write_port(appdata, "ATAS X", "34004")

        with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}, clear=True), \
             mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(atas_client.discover_port(), 34003)
            first.unlink()
            self.assertEqual(atas_client.discover_port(), 34004)

    def test_explicit_environment_port_takes_priority(self):
        base = self.root / "Library" / "Application Support"
        self.write_port(base, "ATAS", "34005")

        with mock.patch.dict(os.environ, {"HOME": str(self.root),
                                              "ATAS_AI_BRIDGE_PORT": " 34006 "}, clear=True), \
             mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(atas_client.discover_port(), 34006)

    def test_invalid_environment_and_port_files_fall_through(self):
        base = self.root / "Library" / "Application Support"
        first = self.write_port(base, "ATAS", "not a port")
        second = self.write_port(base, "ATAS X", "65535")

        with mock.patch.dict(os.environ, {"HOME": str(self.root)}, clear=True), \
             mock.patch.object(sys, "platform", "darwin"):
            for invalid in ("abc", "0", "65536", "  "):
                with self.subTest(invalid=invalid):
                    os.environ["ATAS_AI_BRIDGE_PORT"] = invalid
                    self.assertEqual(atas_client.discover_port(), 65535)

            second.write_text("65536", encoding="utf-8")
            self.assertEqual(atas_client.discover_port(), atas_client.DEFAULT_PORT)
            first.unlink()
            second.unlink()
            self.assertEqual(atas_client.discover_port(), atas_client.DEFAULT_PORT)

    def test_missing_appdata_does_not_read_relative_atas_file(self):
        self.write_port(self.root, "ATAS", "34007")
        original_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            with mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.object(sys, "platform", "win32"):
                self.assertEqual(atas_client.discover_port(), atas_client.DEFAULT_PORT)
        finally:
            os.chdir(original_cwd)

    def test_rediscover_recomputes_macos_home_and_respects_explicit_port(self):
        first_home = self.root / "first"
        second_home = self.root / "second"
        self.write_port(first_home / "Library" / "Application Support", "ATAS", "34008")
        self.write_port(second_home / "Library" / "Application Support", "ATAS X", "34009")

        with mock.patch.dict(os.environ, {"HOME": str(first_home)}, clear=True), \
             mock.patch.object(sys, "platform", "darwin"):
            discovered = atas_client.AtasBridgeClient()
            explicit = atas_client.AtasBridgeClient(port=34010)
            self.assertEqual(discovered.port, 34008)
            os.environ["HOME"] = str(second_home)
            discovered.rediscover()
            explicit.rediscover()
            self.assertEqual(discovered.port, 34009)
            self.assertEqual(explicit.port, 34010)

    def test_rediscover_recomputes_windows_appdata(self):
        first = self.root / "first"
        second = self.root / "second"
        self.write_port(first, "ATAS", "34011")
        self.write_port(second, "ATAS X", "34012")

        with mock.patch.dict(os.environ, {"APPDATA": str(first)}, clear=True), \
             mock.patch.object(sys, "platform", "win32"):
            client = atas_client.AtasBridgeClient()
            self.assertEqual(client.port, 34011)
            os.environ["APPDATA"] = str(second)
            client.rediscover()
            self.assertEqual(client.port, 34012)


if __name__ == "__main__":
    unittest.main()
