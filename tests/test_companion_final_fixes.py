"""Final-review regressions: synthetic credentials and temporary process state only."""

import copy
import http.client
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from companion.app import Workbench
from companion.common import AppError
from companion.provider import ProviderClient
from tests.test_companion_workflow import FakeJournal, FakeMarket, FakeProvider, FakeVault


ROOT = Path(__file__).resolve().parents[1]
A = {"base_url": "https://synthetic-a.invalid/v1", "model": "requested-alias", "persist_key": False}
B = {"base_url": "https://synthetic-b.invalid/v1", "model": "other-alias", "persist_key": True}


class SettingsIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.vault, self.provider = FakeVault(), FakeProvider()
        self.app = Workbench(self.directory, market=FakeMarket(), journal=FakeJournal(),
                             vault=self.vault, provider=self.provider)
        self.addCleanup(self.app.close)
        self.app.store.save_settings(A)
        self.vault.values = {A["base_url"]: "synthetic-key-a", B["base_url"]: "synthetic-key-b"}

    def post(self, path, payload):
        return self.app.handle("POST", path, payload)

    def conflict(self, path, payload):
        before = copy.deepcopy(self.vault.values)
        self.vault.calls.clear()
        with self.assertRaises(AppError) as caught:
            self.post(path, payload)
        self.assertEqual((caught.exception.code, caught.exception.status), ("settings_conflict", 409))
        self.assertEqual(self.vault.calls, [], "identity must be checked before even reading credentials")
        self.assertEqual(self.vault.values, before)
        self.assertEqual(self.provider.calls, [])
        self.assertIsNone(self.app.jobs.active)

    def test_legacy_actions_without_displayed_identity_are_rejected_before_vault(self):
        self.app.store.save_settings(B)
        for path, payload in (("/api/settings", {"key": "synthetic-key-for-a"}),
                              ("/api/settings", {"delete_key": True}),
                              ("/api/settings", {"persist_key": False}),
                              ("/api/settings/test", {})):
            with self.subTest(payload=payload):
                self.conflict(path, payload)

    def test_two_clients_stale_save_delete_persistence_test_and_target_save_conflict(self):
        displayed = self.app.handle("GET", "/api/settings", {})["settings"]
        expected = {name: displayed[name] for name in A}
        self.post("/api/settings", {**B, "expected": expected})  # Other client changes shared config.
        actions = [("/api/settings", {"key": "synthetic-key-for-a", "persist_key": False}),
                   ("/api/settings", {"delete_key": True}),
                   ("/api/settings", {"persist_key": False}),
                   ("/api/settings", A), ("/api/settings/test", {})]
        for path, payload in actions:
            with self.subTest(path=path, fields=list(payload)):
                self.conflict(path, {**payload, "expected": expected})
                self.assertEqual(self.app.store.settings(), B)

    def test_expected_configuration_is_checked_inside_the_shared_lock(self):
        entered = threading.Event()
        outcome = []

        def stale_action():
            entered.set()
            try:
                self.post("/api/settings", {"key": "synthetic-key-for-a", "expected": A})
            except AppError as error:
                outcome.append(error.code)

        self.vault.calls.clear()
        with self.app._lock:
            worker = threading.Thread(target=stale_action)
            worker.start()
            self.assertTrue(entered.wait(1))
            self.app.store.save_settings(B)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, ["settings_conflict"])
        self.assertEqual(self.vault.calls, [])
        self.assertEqual(self.vault.values[B["base_url"]], "synthetic-key-b")

    def test_model_and_persistence_changes_invalidate_displayed_configuration(self):
        for changed in ({**A, "model": "new-model"}, {**A, "persist_key": True}):
            with self.subTest(changed=changed):
                self.app.store.save_settings(changed)
                self.conflict("/api/settings/test", {"expected": A})
                self.conflict("/api/settings", {"expected": A, "key": "synthetic-new-key"})

    def test_fresh_reconfirmation_mutates_only_current_address(self):
        self.app.store.save_settings(B)
        self.post("/api/settings", {"expected": B, "key": "synthetic-reconfirmed-b", "persist_key": False})
        current = {**B, "persist_key": False}
        self.assertEqual(self.vault.values[A["base_url"]], "synthetic-key-a")
        self.assertEqual(self.vault.values[B["base_url"]], "synthetic-reconfirmed-b")
        self.post("/api/settings", {"expected": current, "delete_key": True})
        self.assertEqual(self.vault.values, {A["base_url"]: "synthetic-key-a"})


class ReturnedModelTests(unittest.TestCase):
    def completion(self, value, include=True):
        payload = {"choices": [{"message": {"content": "Synthetic [E:candles]"}}]}
        if include:
            payload["model"] = value
        response = io.BytesIO(json.dumps(payload).encode())
        response.status, response.headers = 200, {}
        with patch("companion.provider._open_no_redirect", return_value=response):
            return ProviderClient().complete(A["base_url"], A["model"], "synthetic-key-a", [])

    def test_transport_preserves_bounded_returned_model_without_rewriting_requested_alias(self):
        self.assertEqual(self.completion("actual-resolved-model").get("model"), "actual-resolved-model")
        self.assertEqual(self.completion("m" * 256).get("model"), "m" * 256)

    def test_missing_invalid_returned_models_stay_unknown(self):
        for value in (None, 7, {}, [], "", " ", " model ", "x" * 257, "x\ny", "x\x00y", "x\x7fy", "x\u0085y", "x\u2028y"):
            with self.subTest(value=value):
                self.assertNotIn("model", self.completion(value))
        self.assertNotIn("model", self.completion(None, include=False))

    def test_real_adapter_metadata_in_report_and_connection_test_and_credential_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = FakeVault()
            vault.values[A["base_url"]] = "synthetic-key-a"
            app = Workbench(directory, market=FakeMarket(), journal=FakeJournal(), vault=vault)
            self.addCleanup(app.close)
            app.store.save_settings(A)
            for actual in ("actual-resolved-model", "synthetic-key-a", None):
                payload = {"choices": [{"message": {"content": "Synthetic [E:candles]"}}], "model": actual}
                def response(*_):
                    value = io.BytesIO(json.dumps(payload).encode())
                    value.status, value.headers = 200, {}
                    return value
                with patch("companion.provider._open_no_redirect", side_effect=response):
                    for connection_test in (False, True):
                        if connection_test:
                            job = app.handle("POST", "/api/settings/test", {"expected": A})["job"]
                        else:
                            preview = app.handle("POST", "/api/preview", {"source": "market", "question": "Synthetic review"})["preview"]
                            job = app.handle("POST", "/api/analyze", {"preview_id": preview["id"]})["job"]
                        app.jobs.thread.join(3)
                        completed = app.jobs.get(job["id"])
                        self.assertEqual(completed["state"], "succeeded", completed)
                        record = completed["result"] if connection_test else app.store.get("reports", completed["result"]["report_id"])
                        self.assertEqual(record["provider"]["requested_model"], "requested-alias")
                        expected = "[credential redacted]" if actual == "synthetic-key-a" else actual
                        self.assertEqual(record["provider"]["actual_model"], expected)
                        self.assertNotIn("synthetic-key-a", json.dumps(record))
            app.close()
            self.assertNotIn(b"synthetic-key-a", (Path(directory) / "workbench.sqlite3").read_bytes())


# A real server process with controllable slow local settings and synthetic provider jobs.
OWNER_SCRIPT = r'''
import json, os, sys, time
from pathlib import Path
from companion import server
from companion.app import Workbench
from tests.test_companion_workflow import FakeMarket, FakeJournal, FakeVault
state = Path(sys.argv[1])
def wait_for(name):
    deadline = time.monotonic() + 15
    while not (state / name).exists() and time.monotonic() < deadline:
        time.sleep(.02)
class Provider:
    def complete(self, *args):
        with (state / "provider-calls").open("a") as file:
            file.write(str(os.getpid()) + "\n")
        wait_for("release-provider")
        return {"text": "synthetic"}
class SlowWorkbench(Workbench):
    def __init__(self, directory):
        super().__init__(directory, market=FakeMarket(), journal=FakeJournal(), vault=FakeVault(), provider=Provider())
        self.store.save_settings({"base_url":"https://synthetic-a.invalid/v1","model":"requested-alias","persist_key":False})
        self.vault.values["https://synthetic-a.invalid/v1"] = "synthetic-key-a"
    def set_bridge_port(self, port):
        if port == 34001:
            with self._lock:
                (state / "slow-control-entered").touch()
                wait_for("release-control")
        super().set_bridge_port(port)
server.Workbench = SlowWorkbench
original_make_server = server.make_server
def observed_server(*args, **kwargs):
    instance = original_make_server(*args, **kwargs)
    handler = instance.RequestHandlerClass
    setup, payload = handler.setup, handler._payload
    def observed_setup(self):
        setup(self)
        (state / ("accepted-" + str(self.client_address[1]))).touch()
    def observed_payload(self):
        (state / ("body-read-" + str(self.client_address[1]))).touch()
        return payload(self)
    handler.setup, handler._payload = observed_setup, observed_payload
    return instance
server.make_server = observed_server
server.serve(state, 34000)
'''

CONTENDER_SCRIPT = r'''
import sys
from pathlib import Path
from companion import server
from companion.app import Workbench
state = Path(sys.argv[1])
class ObservedWorkbench(Workbench):
    def __init__(self, directory):
        (state / "contender-opened-workbench").touch()
        super().__init__(directory)
server.Workbench = ObservedWorkbench
server.serve(state)
'''


class LifetimeOwnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.children = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        (self.state / "release-control").touch()
        (self.state / "release-provider").touch()
        # RED runs may expose an incorrectly spawned detached backend; kill only temp-state owner.
        try:
            pid = self.runtime()["pid"]
            if pid not in [child.pid for child in self.children]:
                os.kill(pid, 15)
        except (OSError, KeyError, ValueError):
            pass
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(4)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(3)

    def spawn(self, code):
        child = subprocess.Popen([sys.executable, "-c", code, str(self.state)], cwd=ROOT,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        return child

    def runtime(self):
        return json.loads((self.state / "runtime.json").read_text())

    def wait_until(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except (OSError, ValueError):
                pass
            time.sleep(.02)
        self.fail("Temporary process did not reach expected state")

    def request(self, metadata, path, payload, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", metadata["port"], timeout=3)
        try:
            connection.request("POST", path, json.dumps(payload), headers={"Content-Type": "application/json", **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read()), response.getheader("Set-Cookie")
        finally:
            connection.close()

    def authenticated(self, metadata):
        status, issued, _ = self.request(metadata, "/__launcher/issue", {}, {"X-Launcher-Token": metadata["control_token"]})
        self.assertEqual(status, 200)
        status, session, cookie = self.request(metadata, "/api/bootstrap", {"token": issued["token"]})
        self.assertEqual(status, 200)
        return {"Cookie": cookie.split(";", 1)[0], "X-CSRF-Token": session["csrf"]}

    def launch(self, bridge_port=None):
        command = [sys.executable, "-m", "companion.launch", "--state-dir", str(self.state),
                   "--url-file", str(self.state / "url")]
        if bridge_port is not None:
            command.extend(["--bridge-port", str(bridge_port)])
        return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=8)

    def test_slow_control_does_not_spawn_or_replace_live_owner_and_retry_reuses_it(self):
        owner = self.spawn(OWNER_SCRIPT)
        self.wait_until(lambda: self.runtime()["pid"] == owner.pid)
        before = self.runtime()
        completed = self.launch(34001)
        self.assertNotEqual(completed.returncode, 0, "slow existing owner must report busy, never launch a replacement")
        self.assertIn("稍后重试", completed.stderr)
        self.assertIsNone(owner.poll())
        self.assertEqual(self.runtime(), before, "metadata must not be replaced or updated before port change completes")
        self.assertTrue((self.state / "slow-control-entered").exists())
        (self.state / "release-control").touch()
        self.wait_until(lambda: self.runtime()["bridge_port"] == 34001)
        retried = self.launch(34001)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertIn("/#token=", (self.state / "url").read_text())
        self.assertEqual(self.runtime()["pid"], owner.pid)

    def test_contender_exits_before_workbench_open_and_cannot_duplicate_active_job(self):
        owner = self.spawn(OWNER_SCRIPT)
        self.wait_until(lambda: self.runtime()["pid"] == owner.pid)
        metadata = self.runtime()
        headers = self.authenticated(metadata)
        self.assertEqual(self.request(metadata, "/api/settings/test", {"expected": A}, headers)[0], 200)
        self.wait_until(lambda: (self.state / "provider-calls").exists())
        contender = self.spawn(CONTENDER_SCRIPT)
        self.wait_until(lambda: contender.poll() is not None or (self.state / "contender-opened-workbench").exists())
        self.assertFalse((self.state / "contender-opened-workbench").exists(), "lifetime ownership must precede Workbench and Store construction")
        self.assertNotEqual(contender.wait(2), 0)
        self.assertEqual(self.runtime(), metadata)
        status, body, _ = self.request(metadata, "/api/settings/test", {"expected": A}, headers)
        self.assertEqual((status, body["error"]["code"]), (409, "provider_busy"))
        self.assertEqual((self.state / "provider-calls").read_text().splitlines(), [str(owner.pid)])

    def test_process_death_releases_lock_and_stale_runtime_can_restart(self):
        owner = self.spawn(OWNER_SCRIPT)
        self.wait_until(lambda: self.runtime()["pid"] == owner.pid)
        owner.kill()
        owner.wait(3)
        completed = self.launch()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("/#token=", (self.state / "url").read_text())
        replacement = self.runtime()
        self.assertNotEqual(replacement["pid"], owner.pid)
        self.assertEqual((self.state / "instance.lock").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.request(replacement, "/__launcher/issue", {}, {"X-Launcher-Token": replacement["control_token"]})[0], 200)

    def test_shutdown_retains_lock_while_provider_finishes_even_without_runtime(self):
        owner = self.spawn(OWNER_SCRIPT)
        self.wait_until(lambda: self.runtime()["pid"] == owner.pid)
        metadata = self.runtime()
        headers = self.authenticated(metadata)
        self.assertEqual(self.request(metadata, "/api/settings/test", {"expected": A}, headers)[0], 200)
        self.wait_until(lambda: (self.state / "provider-calls").exists())
        owner.terminate()
        self.wait_until(lambda: not (self.state / "runtime.json").exists())
        self.assertIsNone(owner.poll(), "owner must finish its active provider job before exiting")
        completed = self.launch()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("稍后重试", completed.stderr)
        contender = self.spawn(CONTENDER_SCRIPT)
        self.wait_until(lambda: contender.poll() is not None or (self.state / "contender-opened-workbench").exists())
        self.assertFalse((self.state / "contender-opened-workbench").exists())
        self.assertNotEqual(contender.wait(2), 0)
        (self.state / "release-provider").touch()
        self.assertEqual(owner.wait(3), 0)
        self.assertEqual(self.launch().returncode, 0)

    def assert_shutdown_with_unfinished_request(self, body=False):
        owner = self.spawn(OWNER_SCRIPT)
        self.wait_until(lambda: self.runtime()["pid"] == owner.pid)
        metadata = self.runtime()
        peer = socket.create_connection(("127.0.0.1", metadata["port"]), timeout=3)
        self.addCleanup(peer.close)
        client_port = peer.getsockname()[1]
        self.wait_until(lambda: (self.state / ("accepted-" + str(client_port))).exists())
        if body:
            peer.sendall(("POST /api/bootstrap HTTP/1.1\r\n"
                          f"Host: 127.0.0.1:{metadata['port']}\r\n"
                          "Content-Type: application/json\r\nContent-Length: 128\r\n\r\n{").encode())
            self.wait_until(lambda: (self.state / ("body-read-" + str(client_port))).exists())
        owner.terminate()
        try:
            code = owner.wait(4)
        except subprocess.TimeoutExpired:
            self.fail("An accepted idle/incomplete request blocked shutdown while its peer stayed open")
        self.assertEqual(code, 0)
        self.assertFalse((self.state / "runtime.json").exists())
        self.assertFalse((self.state / "provider-calls").exists())
        self.assertFalse((self.state / "errors.log").exists())
        received = peer.recv(4096)
        if body:
            self.assertTrue(received.startswith(b"HTTP/1.0 408"), "incomplete JSON receives a safe timeout response")
            while received:
                received = peer.recv(4096)
        self.assertEqual(received, b"", "server must close its side of the unfinished request")
        # Deliberately leave peer open: ownership release must not depend on client cleanup.
        completed = self.launch()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotEqual(self.runtime()["pid"], owner.pid)

    def test_shutdown_bounds_accepted_idle_browser_connection(self):
        self.assert_shutdown_with_unfinished_request()

    def test_shutdown_bounds_incomplete_json_body(self):
        self.assert_shutdown_with_unfinished_request(body=True)


if __name__ == "__main__":
    unittest.main()
