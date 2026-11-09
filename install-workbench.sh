#!/bin/bash
# Explicitly install the local workbench launcher; never modify or restart ATAS.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bash install-workbench.sh --install [--python PATH] [--state-dir PATH]

Writes a private launcher and installation metadata. This does not install
the ATAS indicator; use build.sh --install separately after quitting ATAS.
The chart button reads installation metadata only from the default state
directory. --state-dir creates a launcher for manual use or isolated tests;
it does not move the chart button's installation metadata lookup.
EOF
}
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
need_value() { [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || fail "$1 requires a value"; }

root="$(CDPATH='' cd -- "$(dirname "$0")" && pwd)"
python="$(command -v python3 || true)"
state_dir="$HOME/Library/Application Support/ATAS AI Review"
install=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --install) install=1; shift ;;
        --python) need_value "$@"; python="$2"; shift 2 ;;
        --state-dir) need_value "$@"; state_dir="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) fail "Unknown option: $1" ;;
    esac
done
[[ $install -eq 1 ]] || { usage >&2; fail "--install is required"; }
[[ -x "$python" ]] || fail "Python executable not found: $python"
"$python" - "$state_dir" "$root/companion/launch.py" <<'PY'
import json
import os
from pathlib import Path
import shlex
import sys

state = Path(sys.argv[1]).expanduser().resolve()
launcher = Path(sys.argv[2]).resolve()
python = Path(sys.executable).resolve()
if not launcher.is_file():
    raise SystemExit("Workbench launcher is missing")
state.mkdir(parents=True, mode=0o700, exist_ok=True)
state.chmod(0o700)
metadata = state / "installation.json"
with metadata.open("w", encoding="utf-8") as file:
    os.fchmod(file.fileno(), 0o600)
    json.dump({"python": str(python), "launcher": str(launcher)}, file)
user_launcher = state / "Open ATAS AI Review.command"
with user_launcher.open("w", encoding="utf-8") as file:
    os.fchmod(file.fileno(), 0o700)
    file.write("#!/bin/sh\nexec " + shlex.quote(str(python)) + " " +
               shlex.quote(str(launcher)) + " --open --state-dir " +
               shlex.quote(str(state)) + " \"$@\"\n")
print("Workbench launcher installed: " + str(user_launcher))
default_state = Path.home() / "Library" / "Application Support" / "ATAS AI Review"
if state != default_state.resolve():
    print("Custom state directory: open this launcher manually; the ATAS chart button "
          "reads the default installation directory only.")
PY
