"""Open or obtain a fresh one-use URL for the private local workbench."""

from __future__ import annotations

import argparse
import fcntl
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import quote
import webbrowser

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from companion.server import private_state  # noqa: E402


DEFAULT_STATE = Path.home() / "Library" / "Application Support" / "ATAS AI Review"


def _read_runtime(directory: Path) -> dict | None:
    try:
        value = json.loads((directory / "runtime.json").read_text(encoding="utf-8"))
        if (isinstance(value.get("port"), int) and 8770 <= value["port"] <= 8780
                and isinstance(value.get("control_token"), str)):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _issue(metadata: dict) -> str | None:
    connection = http.client.HTTPConnection("127.0.0.1", metadata["port"], timeout=1)
    try:
        connection.request("POST", "/__launcher/issue", headers={
            "X-Launcher-Token": metadata["control_token"]})
        response = connection.getresponse()
        if response.status != 200:
            return None
        token = json.loads(response.read())["token"]
        return f"http://127.0.0.1:{metadata['port']}/#token={quote(token)}"
    except (OSError, ValueError, KeyError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def issue_url(directory: Path, bridge_port: int | None = None) -> str:
    directory = private_state(directory)
    lock_path = directory / "launcher.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        metadata = _read_runtime(directory)
        url = _issue(metadata) if metadata else None
        if url:
            return url
        args = [sys.executable, "-m", "companion.server", "--state-dir", str(directory)]
        if bridge_port is not None:
            args.extend(["--bridge-port", str(bridge_port)])
        subprocess.Popen(args, cwd=Path(__file__).resolve().parents[1],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(100):
            time.sleep(0.05)
            metadata = _read_runtime(directory)
            url = _issue(metadata) if metadata else None
            if url:
                return url
        raise RuntimeError("本地工作台未能启动，请检查端口 8770–8780。")
    finally:
        os.close(descriptor)


def _write_url(path: Path, url: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        os.fchmod(file.fileno(), 0o600)
        file.write(url + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Open ATAS AI Review")
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--open", action="store_true")
    destination.add_argument("--url-file", type=Path, help="Private test/automation output")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--bridge-port", type=int)
    arguments = parser.parse_args()
    if arguments.bridge_port is not None and not 1 <= arguments.bridge_port <= 65535:
        parser.error("--bridge-port must be in 1..65535")
    url = issue_url(arguments.state_dir, arguments.bridge_port)
    if arguments.url_file:
        _write_url(arguments.url_file, url)
    elif not webbrowser.open(url):
        raise SystemExit("无法打开浏览器。请检查默认浏览器设置。")


if __name__ == "__main__":
    main()
