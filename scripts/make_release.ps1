
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$releaseDir = Join-Path $root "release"
$zipPath = Join-Path $releaseDir "napominalka-2.2-win.zip"
$stage = Join-Path $releaseDir "_stage"

$files = @(
    "main.py",
    "database.py",
    "gui.py",
    "notifications.py",
    "templates.py",
    "process_builder.py",
    "autostart.py",
    "requirements.txt",
    "README.md",
    "INSTALL.md",
    "UPDATE-2.2.md"
)

$dist = Join-Path $root "process_builder_web\dist"
if (-not (Test-Path $dist)) {
    throw "Missing process_builder_web/dist. Run npm run build first."
}

if (Test-Path $stage) {
    Remove-Item -Recurse -Force $stage
}
New-Item -ItemType Directory -Path (Join-Path $stage "scripts") -Force | Out-Null
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null

foreach ($name in $files) {
    Copy-Item (Join-Path $root $name) (Join-Path $stage $name)
}
Copy-Item (Join-Path $root "scripts\autostart.ps1") (Join-Path $stage "scripts\autostart.ps1")

$webStage = Join-Path $stage "process_builder_web"
New-Item -ItemType Directory -Path (Join-Path $webStage "dist") -Force | Out-Null
Copy-Item (Join-Path $dist "*") (Join-Path $webStage "dist") -Recurse
Copy-Item (Join-Path $root "process_builder_web\package.json") (Join-Path $webStage "package.json")
Copy-Item (Join-Path $root "process_builder_web\vite.config.js") (Join-Path $webStage "vite.config.js")
Copy-Item (Join-Path $root "process_builder_web\index.html") (Join-Path $webStage "index.html")
Copy-Item (Join-Path $root "process_builder_web\src") (Join-Path $webStage "src") -Recurse

if (Test-Path $zipPath) {
    Remove-Item -Force $zipPath
}
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zipPath
Remove-Item -Recurse -Force $stage
Write-Host $zipPath
