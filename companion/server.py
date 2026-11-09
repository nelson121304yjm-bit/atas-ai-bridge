"""Dedicated local server process started by the authenticated launcher."""

import argparse
import json
import os
from pathlib import Path
import signal
import tempfile
import threading

from .app import Workbench
from .http_server import make_server


def private_state(directory: Path) -> Path:
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def write_private(path: Path, value: bytes) -> None:
    descriptor, staged = tempfile.mkstemp(prefix=".runtime-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(value)
        os.chmod(staged, 0o600)
        os.replace(staged, path)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)


def serve(state_dir: Path, bridge_port: int | None = None) -> None:
    directory = private_state(state_dir)
    workbench = Workbench(directory)
    if bridge_port is not None:
        workbench.set_bridge_port(bridge_port)
    runtime_path = directory / "runtime.json"

    def on_bridge_port_change(value: int) -> None:
        changed = {**metadata, "bridge_port": value}
        write_private(runtime_path, json.dumps(changed).encode("utf-8"))
        workbench.set_bridge_port(value)
        metadata.update(changed)

    for port in range(8770, 8781):
        try:
            server = make_server(workbench.handle, directory, port=port,
                                 on_bridge_port_change=on_bridge_port_change)
            break
        except OSError:
            continue
    else:
        workbench.close()
        raise RuntimeError("No workbench port available on 127.0.0.1:8770-8780")

    metadata = {"pid": os.getpid(), "port": server.server_address[1],
                "control_token": server.auth.control_token, "bridge_port": bridge_port}
    server.bridge_port = bridge_port
    write_private(runtime_path, json.dumps(metadata).encode("utf-8"))

    def stop(_number, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        workbench.close()
        try:
            current = json.loads(runtime_path.read_text())
            if current.get("pid") == os.getpid():
                runtime_path.unlink()
        except (OSError, ValueError):
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="ATAS AI Review local server")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--bridge-port", type=int)
    arguments = parser.parse_args()
    serve(arguments.state_dir, arguments.bridge_port)


if __name__ == "__main__":
    main()
