#!/bin/bash
# Isolated Python environment for the ATAS AI Bridge MCP server on macOS.
set -euo pipefail

if [[ $# -gt 0 ]]; then
    if [[ $# -eq 1 && ( "$1" == --help || "$1" == -h ) ]]; then
        printf 'Usage: bash mcp/setup-venv.sh\nSet PYTHON to override python3. Installs into mcp/.venv.\n'
        exit 0
    fi
    printf 'Error: unexpected arguments (see --help).\n' >&2
    exit 1
fi

here="$(CDPATH='' cd -- "$(dirname "$0")" && pwd)"
python="${PYTHON:-python3}"
venv_python="$here/.venv/bin/python"
command -v "$python" >/dev/null 2>&1 || { printf 'Error: install Python 3.10+ or set PYTHON.\n' >&2; exit 1; }
"$python" -c 'import sys; sys.exit("Python 3.10+ is required") if sys.version_info < (3, 10) else None'
if [[ ! -x "$venv_python" ]]; then
    "$python" -m venv "$here/.venv"
fi
"$venv_python" -m pip install -r "$here/requirements.txt"
"$venv_python" -c 'from importlib.metadata import version; from mcp.server.mcpserver import MCPServer; print("mcp", version("mcp"), "- MCPServer import OK")'

"$venv_python" - "$here" <<'PY'
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
print("\nMCP client config (stdio):")
print(json.dumps({"mcpServers": {"atas": {
    "command": str(root / ".venv/bin/python"),
    "args": [str(root / "server.py")],
}}}, indent=2))
print("\nTrading tools stay hidden unless ATAS_TRADE_TOKEN is explicitly set.")
PY
