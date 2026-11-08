"""Optional real stdio handshake; lists tools without contacting an ATAS bridge."""

import importlib.util
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import unittest


try:
    HAS_MCP_V2 = importlib.util.find_spec("mcp.server.mcpserver") is not None
except (ImportError, ModuleNotFoundError):
    HAS_MCP_V2 = False


@unittest.skipUnless(HAS_MCP_V2 and os.name == "posix",
                     "requires the MCP v2 venv and POSIX pipe polling")
class McpSmokeTests(unittest.TestCase):
    def test_stdio_initializes_and_lists_default_tools(self):
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env.pop("ATAS_TRADE_TOKEN", None)
        # Avoid reading the user's port files. No tools/call requests are made.
        env["ATAS_AI_BRIDGE_PORT"] = "1"
        proc = subprocess.Popen([sys.executable, str(root / "mcp/server.py"),
                                 "--transport", "stdio"], cwd=root, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            def send(message):
                proc.stdin.write(json.dumps(message) + "\n")
                proc.stdin.flush()

            def receive(request_id):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))
                    if not ready:
                        break
                    line = proc.stdout.readline()
                    if not line:
                        self.fail("MCP exited before replying")
                    message = json.loads(line)
                    if message.get("id") == request_id:
                        self.assertNotIn("error", message, message)
                        return message["result"]
                self.fail("Timed out waiting for MCP response")

            send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "atas-bridge-smoke-test", "version": "1.0"},
            }})
            initialized = receive(1)
            self.assertEqual(initialized["serverInfo"]["name"], "ATAS")
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            names = {tool["name"] for tool in receive(2)["tools"]}
            self.assertTrue({"atas_status", "atas_dom", "atas_footprint", "atas_cvd",
                             "atas_profile", "atas_get_settings"}.issubset(names), names)
            self.assertFalse(names & {"atas_place_order", "atas_cancel_order", "atas_close_position"})
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            proc.stdout.close()
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
