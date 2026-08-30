param(
    [switch]$Install,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$autostart = Join-Path $root "autostart.py"

if ($Install) {
    python $autostart install
    exit $LASTEXITCODE
}

if ($Uninstall) {
    python $autostart uninstall
    exit $LASTEXITCODE
}

Write-Host "Использование: autostart.ps1 -Install  или  autostart.ps1 -Uninstall"
exit 1
