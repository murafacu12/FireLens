# Build the FIRELENS desktop app for Windows, from the repository.
#   powershell -ExecutionPolicy Bypass -File backend\desktop\build.ps1 [-Version 1.0.0]
# Produces, in backend\dist\:
#   FIRELENS\FIRELENS.exe                 the app folder
#   FIRELENS-<version>-windows-x64.zip    portable: unzip and run FIRELENS.exe
#   FIRELENS-Setup-<version>.exe          installer (only if Inno Setup 6 is installed)
# Needs: Node 20+, and backend\.venv with the app installed (pip install -e ".[desktop]").
param([string]$Version = "1.0.0")
$ErrorActionPreference = "Stop"

$backend = Split-Path -Parent $PSScriptRoot
$root = Split-Path -Parent $backend
$py = Join-Path $backend ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "backend\.venv not found: create it and run pip install -e "".[desktop]""" }

Write-Host "== Website (frontend\dist)"
Push-Location (Join-Path $root "frontend")
try {
    if (-not (Test-Path "node_modules")) { npm ci; if ($LASTEXITCODE) { throw "npm ci failed" } }
    $env:VITE_API_BASE = ""   # same address as the API
    npm run build; if ($LASTEXITCODE) { throw "website build failed" }
} finally { Pop-Location }

Write-Host "== App (PyInstaller)"
Push-Location $backend
try {
    & $py -m PyInstaller firelens.spec --noconfirm; if ($LASTEXITCODE) { throw "PyInstaller failed" }
} finally { Pop-Location }

$dist = Join-Path $backend "dist"
$zip = Join-Path $dist "FIRELENS-$Version-windows-x64.zip"
Write-Host "== Portable zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $dist "FIRELENS") -DestinationPath $zip -CompressionLevel Optimal
Write-Host ("   {0} ({1:N0} MB)" -f $zip, ((Get-Item $zip).Length / 1MB))

$iscc = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    Write-Host "== Installer (Inno Setup)"
    & $iscc "/DAppVersion=$Version" (Join-Path $PSScriptRoot "installer.iss"); if ($LASTEXITCODE) { throw "Inno Setup failed" }
} else {
    Write-Host "== Installer skipped: Inno Setup 6 not found (winget install JRSoftware.InnoSetup)"
}
Write-Host "Done: $dist"
