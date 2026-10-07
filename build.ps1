# Build ATAS.AiBridge and deploy it into the ATAS user plugin folder.
#
# NOTE: this file is intentionally ASCII-only. Windows PowerShell 5.1 decodes
# BOM-less .ps1 files using the system ANSI codepage (GBK on zh-CN), which
# corrupts non-ASCII characters and breaks parsing.
#
# The build only READS the ATAS assemblies (all Private=false in the
# csproj); the output goes to %APPDATA%\ATAS\Strategies only, so the ATAS
# installation directory is never modified.
#
# Usage:
#   .\build.ps1              # build + deploy
#   .\build.ps1 -NoDeploy    # build only
param(
    [switch]$NoDeploy,
    [switch]$Force,
    [string]$Configuration = 'Release'
)

$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$proj = Join-Path $root 'src\ATAS.AiBridge\ATAS.AiBridge.csproj'

$dotnet = Join-Path $env:USERPROFILE '.dotnet\dotnet.exe'
if (-not (Test-Path $dotnet)) { $dotnet = 'dotnet' }

Write-Host "dotnet: $dotnet" -ForegroundColor Cyan
& $dotnet build $proj -c $Configuration -v minimal
if ($LASTEXITCODE -ne 0) { throw "build failed (exit $LASTEXITCODE)" }

if ($NoDeploy) { Write-Host 'skip deploy' -ForegroundColor Yellow; exit 0 }

$dll = Join-Path $root 'out\ATAS.AiBridge.dll'
if (-not (Test-Path $dll)) { throw "output not found: $dll" }

# Overwriting a DLL that ATAS already loaded does NOT reload it inside the running
# process (and overwriting a memory-mapped image can leave that process inconsistent).
# Warn loudly so the operator knows a restart / re-add is required.
$atasRunning = Get-Process -Name 'OFT.Platform' -ErrorAction SilentlyContinue
if ($atasRunning -and -not $Force) {
    Write-Host ''
    Write-Host 'WARNING: ATAS is running (OFT.Platform pid ' -NoNewline -ForegroundColor Yellow
    Write-Host ($atasRunning.Id -join ',') -NoNewline -ForegroundColor Yellow
    Write-Host ').' -ForegroundColor Yellow
    Write-Host '  Deploying now replaces the file on disk but does NOT reload it in ATAS:' -ForegroundColor Yellow
    Write-Host '  -> restart ATAS (safest), or remove and re-add the strategy on the chart.' -ForegroundColor Yellow
    Write-Host '  Pass -Force to skip this check.' -ForegroundColor DarkGray
    Write-Host ''
}

$targets = @(
    (Join-Path $env:APPDATA 'ATAS\Strategies'),
    (Join-Path $env:APPDATA 'ATAS X\Strategies')
)

foreach ($t in $targets) {
    if ($t -like '*ATAS X*' -and -not (Test-Path (Split-Path $t -Parent))) {
        Write-Host "skip (ATAS X not installed): $t" -ForegroundColor DarkGray
        continue
    }
    if (-not (Test-Path $t)) { New-Item -ItemType Directory -Force -Path $t | Out-Null }
    Copy-Item $dll (Join-Path $t 'ATAS.AiBridge.dll') -Force
    Write-Host "deployed: $t\ATAS.AiBridge.dll" -ForegroundColor Green
}

Write-Host ''
Write-Host 'Next: start ATAS -> right-click chart -> Indicators/Strategies -> add "AI Bridge" -> check IsActivated' -ForegroundColor Cyan
Write-Host 'Verify: curl http://127.0.0.1:8790/api/health' -ForegroundColor Cyan
