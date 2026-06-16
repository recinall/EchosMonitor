# Build a one-dir PyInstaller bundle of EchosMonitor (M7-B). Windows.
#
# Output: dist\echosmonitor\echosmonitor.exe  (a self-contained launcher).
# Requires the dev dependency group (pyinstaller); `uv sync` installs it.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\build.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\build.ps1 -NoClean
param(
    [switch]$NoClean
)
$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

if (-not $NoClean) {
    Write-Host ">> cleaning build\ and dist\echosmonitor"
    if (Test-Path build) { Remove-Item -Recurse -Force build }
    if (Test-Path dist\echosmonitor) { Remove-Item -Recurse -Force dist\echosmonitor }
}

Write-Host ">> ensuring build deps are installed (uv sync)"
uv sync

Write-Host ">> running PyInstaller (packaging\echosmonitor.spec)"
uv run pyinstaller packaging\echosmonitor.spec --noconfirm --workpath build --distpath dist

$Bin = "dist\echosmonitor\echosmonitor.exe"
$env:QT_QPA_PLATFORM = "offscreen"
# --check is the portable smoke: a Windows GUI-subsystem .exe has no attached
# stdout, so --version cannot be asserted on, but --check exits by code after
# constructing config + the main window in the freeze.
Write-Host ">> smoke: $Bin --check (headless start/quit)"
& $Bin --check
if ($LASTEXITCODE -ne 0) { throw "packaged --check failed with exit $LASTEXITCODE" }

Write-Host ">> build OK -> dist\echosmonitor\"
