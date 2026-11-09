#!/bin/bash
# Explicitly install the local workbench launcher; never modify or restart ATAS.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bash install-workbench.sh --install [--python PATH] [--state-dir PATH]
       [--journal-python PATH --journal-script PATH --journal-db PATH --journal-account ID]
       [--disable-journal]

Writes a private launcher and installation metadata. This does not install
the ATAS indicator; use build.sh --install separately after quitting ATAS.
The chart button reads installation metadata only from the default state
directory. --state-dir creates a launcher for manual use or isolated tests;
it does not move the chart button's installation metadata lookup.
Python 3.12+ is required. A dedicated venv is created in the state directory.
Journal arguments are optional but must be supplied together. An upgrade
preserves existing journal configuration unless --disable-journal is explicit.
EOF
}
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
need_value() { [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || fail "$1 requires a value"; }

root="$(CDPATH='' cd -- "$(dirname "$0")" && pwd)"
python="$(command -v python3 || true)"
state_dir="$HOME/Library/Application Support/ATAS AI Review"
install=0
journal_python=""
journal_script=""
journal_db=""
journal_account=""
disable_journal=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --install) install=1; shift ;;
        --python) need_value "$@"; python="$2"; shift 2 ;;
        --state-dir) need_value "$@"; state_dir="$2"; shift 2 ;;
        --journal-python) need_value "$@"; journal_python="$2"; shift 2 ;;
        --journal-script) need_value "$@"; journal_script="$2"; shift 2 ;;
        --journal-db) need_value "$@"; journal_db="$2"; shift 2 ;;
        --journal-account) need_value "$@"; journal_account="$2"; shift 2 ;;
        --disable-journal) disable_journal=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) fail "Unknown option: $1" ;;
    esac
done
[[ $install -eq 1 ]] || { usage >&2; fail "--install is required"; }
[[ -x "$python" ]] || fail "Python executable not found: $python"
count=0
for value in "$journal_python" "$journal_script" "$journal_db" "$journal_account"; do
    [[ -z "$value" ]] || count=$((count + 1))
done
[[ $count -eq 0 || $count -eq 4 ]] || fail "All four journal options must be supplied together"
[[ $disable_journal -eq 0 || $count -eq 0 ]] || fail "--disable-journal cannot be combined with journal options"
"$python" - "$state_dir" "$root/companion/launch.py" "$journal_python" "$journal_script" "$journal_db" "$journal_account" "$disable_journal" <<'PY'
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

if sys.version_info < (3, 12):
    raise SystemExit("Python 3.12+ required; pass --python /path/to/python3.12")
state = Path(sys.argv[1]).expanduser().resolve()
launcher = Path(sys.argv[2]).resolve()
if not launcher.is_file():
    raise SystemExit("Workbench launcher is missing")
journal_values = sys.argv[3:7]
disable_journal = sys.argv[7] == "1"
journal_config = None
if all(journal_values):
    try:
        python_path, script_path, db_path = [Path(value).expanduser().resolve(strict=True)
                                              for value in journal_values[:3]]
    except (OSError, RuntimeError) as exc:
        raise SystemExit("Journal paths must be existing files") from exc
    if not all(path.is_file() for path in (python_path, script_path, db_path)):
        raise SystemExit("Journal paths must be existing files")
    if not os.access(python_path, os.X_OK):
        raise SystemExit("Journal Python must be executable")
    account = journal_values[3]
    if not account.strip() or len(account) > 256:
        raise SystemExit("Journal account ID must contain 1..256 characters")
    journal_config = {"python_path": str(python_path), "script_path": str(script_path),
                      "db_path": str(db_path), "account_id": account}

os.umask(0o077)
state.mkdir(parents=True, mode=0o700, exist_ok=True)
state.chmod(0o700)
venv = state / ".venv"
if venv.is_symlink():
    raise SystemExit("Virtual environment path must not be a symlink")
venv_python = venv / "bin" / "python"
if not venv.exists():
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
if not venv_python.is_file() or not os.access(venv_python, os.X_OK):
    raise SystemExit("Workbench virtual environment is incomplete")
check = subprocess.run([str(venv_python), "-c", "import sys; assert sys.version_info >= (3,12)"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
if check.returncode:
    raise SystemExit("Workbench virtual environment requires Python 3.12+")

def private_json(path, value):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=state, delete=False) as file:
        os.fchmod(file.fileno(), 0o600)
        json.dump(value, file)
        temporary = Path(file.name)
    os.replace(temporary, path)

def private_text(path, value, mode):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=state, delete=False) as file:
        os.fchmod(file.fileno(), mode)
        file.write(value)
        temporary = Path(file.name)
    os.replace(temporary, path)

metadata = state / "installation.json"
private_json(metadata, {"python": str(venv_python), "launcher": str(launcher)})
config_path = state / "journal.json"
if journal_config is not None:
    private_json(config_path, journal_config)
elif disable_journal:
    config_path.unlink(missing_ok=True)
user_launcher = state / "Open ATAS AI Review.command"
private_text(user_launcher,
             "#!/bin/sh\nexec " + shlex.quote(str(venv_python)) + " " +
             shlex.quote(str(launcher)) + " --open --state-dir " +
             shlex.quote(str(state)) + " \"$@\"\n", 0o700)
print("Workbench launcher installed: " + str(user_launcher))
default_state = Path.home() / "Library" / "Application Support" / "ATAS AI Review"
if state != default_state.resolve():
    print("Custom state directory: open this launcher manually; the ATAS chart button "
          "reads the default installation directory only.")
PY
