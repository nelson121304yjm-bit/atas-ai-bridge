# Create an isolated venv for the ATAS AI Bridge MCP server and install deps.
#
# ASCII-only on purpose: Windows PowerShell 5.1 decodes BOM-less .ps1 files with
# the system ANSI codepage (GBK on zh-CN), which corrupts non-ASCII text.
#
# Why a venv: the machine already has mcp 1.29.1 globally, and this server needs
# mcp >= 2.0 (mcp.server.mcpserver.MCPServer does not exist in 1.x). Installing
# into the venv avoids breaking other tools that depend on mcp 1.x.
$ErrorActionPreference = 'Stop'

$here = $PSScriptRoot
$venv = Join-Path $here '.venv'
$python = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path $python)) {
    Write-Host "creating venv: $venv" -ForegroundColor Cyan
    python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}

Write-Host 'installing requirements ...' -ForegroundColor Cyan
& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet -r (Join-Path $here 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host ''
Write-Host 'installed:' -ForegroundColor Green
& $python -c "import mcp, importlib.metadata as m; print('  mcp', m.version('mcp')); from mcp.server.mcpserver import MCPServer; print('  MCPServer import OK')"

Write-Host ''
Write-Host 'MCP client config (stdio) snippet:' -ForegroundColor Cyan
Write-Host ("  command: " + $python)
Write-Host ("  args:    " + (Join-Path $here 'server.py'))
Write-Host ''
Write-Host 'Trading tools stay hidden unless ATAS_TRADE_TOKEN is set in the env.' -ForegroundColor Yellow
