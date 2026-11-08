#!/bin/bash
# macOS: build by default; deployment requires --install.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bash build.sh [--install] [--force] [--configuration Release|Debug]
                     [--atas-base PATH] [--atas-user-dir PATH]

Builds with the installed ATAS X SDK. No files are installed by default.
  --install          Copy only ATAS.AiBridge.dll into the user's Strategies folder.
                     An existing DLL is backed up before replacement.
  --force            With --install, allow replacement while ATAS is running.
                     Restart ATAS afterwards; this does not reload the plugin.
  --atas-base PATH    SDK directory (default: /Applications/ATAS X.app/Contents/MonoBundle).
  --atas-user-dir PATH  ATAS user-data directory (default: ~/Library/Application Support/ATAS).
  --configuration    Build configuration (default: Release).
  --help             Print this help.

Environment overrides: DOTNET (executable), ATAS_BASE, ATAS_USER_DIR.
EOF
}

fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
need_value() { [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || fail "$1 requires a value"; }

root="$(CDPATH='' cd -- "$(dirname "$0")" && pwd)"
atas_base="${ATAS_BASE:-/Applications/ATAS X.app/Contents/MonoBundle}"
atas_user_dir="${ATAS_USER_DIR:-$HOME/Library/Application Support/ATAS}"
dotnet="${DOTNET:-dotnet}"
configuration=Release
install=0
force=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --install) install=1; shift ;;
        --force) force=1; shift ;;
        --configuration) need_value "$@"; configuration="$2"; shift 2 ;;
        --atas-base) need_value "$@"; atas_base="$2"; shift 2 ;;
        --atas-user-dir) need_value "$@"; atas_user_dir="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) fail "Unknown option: $1 (see --help)" ;;
    esac
done

[[ "$(uname -s)" == Darwin ]] || fail "build.sh is for macOS; use build.ps1 on Windows."
[[ $force -eq 0 || $install -eq 1 ]] || fail "--force requires --install"
case "$configuration" in Release|Debug) ;; *) fail "Configuration must be Release or Debug" ;; esac
command -v "$dotnet" >/dev/null 2>&1 || fail "Install .NET 10 SDK or set DOTNET to its executable."
for name in ATAS.Indicators ATAS.DataFeedsCore ATAS.Strategies ATAS.Types Utils.Common; do
    [[ -f "$atas_base/$name.dll" ]] || fail "Missing $atas_base/$name.dll; use --atas-base to select the ATAS X SDK."
done
# MSBuild resolves HintPath relative to the project, not the caller's cwd.
atas_base="$(CDPATH='' cd -- "$atas_base" && pwd)"

check_running() {
    if [[ $install -eq 1 && $force -eq 0 ]]; then
        if pgrep -x 'ATAS X' >/dev/null 2>&1 || pgrep -x 'OFT.Platform' >/dev/null 2>&1; then
            fail "ATAS is running. Quit it before installing, or explicitly use --install --force and restart afterwards."
        fi
    fi
}

check_running
printf 'SDK: %s\n' "$atas_base"
"$dotnet" build "$root/src/ATAS.AiBridge/ATAS.AiBridge.csproj" \
    --configuration "$configuration" "-p:ATAS_BASE=$atas_base"

dll="$root/out/ATAS.AiBridge.dll"
[[ -f "$dll" ]] || fail "Build did not produce $dll"
if [[ $install -eq 0 ]]; then
    printf '\nBuilt: %s\nNo plugin installed. Quit ATAS, then run bash build.sh --install.\n' "$dll"
    exit 0
fi

# The platform may have started during compilation.
check_running
target_dir="$atas_user_dir/Strategies"
mkdir -p "$target_dir"
target="$target_dir/ATAS.AiBridge.dll"
if [[ -e "$target" ]]; then
    backup="$(mktemp "$target.bak.XXXXXX")"
    cp -p "$target" "$backup"
    printf 'Backup: %s\n' "$backup"
fi
staged="$(mktemp "$target_dir/.ATAS.AiBridge.XXXXXX")"
trap 'rm -f "$staged"' EXIT
cp -p "$dll" "$staged"
mv -f "$staged" "$target"
printf '\nInstalled: %s\nStart/restart ATAS and add the AI Bridge chart strategy.\n' "$target"
