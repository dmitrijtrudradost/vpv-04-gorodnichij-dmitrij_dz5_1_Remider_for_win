

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$releaseDir = Join-Path $root "release"
$zipPath = Join-Path $releaseDir "napominalka-2.1-win.zip"
$stage = Join-Path $releaseDir "_stage"

$files = @(
    "main.py",
    "database.py",
    "gui.py",
    "notifications.py",
    "templates.py",
    "autostart.py",
    "requirements.txt",
    "README.md",
    "INSTALL.md"
)

if (Test-Path $stage) {
    Remove-Item -Recurse -Force $stage
}
New-Item -ItemType Directory -Path (Join-Path $stage "scripts") -Force | Out-Null
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null

foreach ($name in $files) {
    Copy-Item (Join-Path $root $name) (Join-Path $stage $name)
}
Copy-Item (Join-Path $root "scripts\autostart.ps1") (Join-Path $stage "scripts\autostart.ps1")

if (Test-Path $zipPath) {
    Remove-Item -Force $zipPath
}
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zipPath
Remove-Item -Recurse -Force $stage
Write-Host $zipPath
