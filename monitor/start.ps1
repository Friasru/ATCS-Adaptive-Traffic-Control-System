# Launch ATCS Monitor on Windows: .\start.ps1 [--video FILE [--start]]
# (or double-click / run start.cmd, which bypasses the PowerShell execution policy for this script only)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Py = Join-Path $Root ".venv-win\Scripts\python.exe"

function Fail([string]$msg) { Write-Host "ATCS Monitor: $msg" -ForegroundColor Red; exit 1 }

# Keep third-party config/cache writes inside the project (same as app/paths.py).
if (-not $env:YOLO_CONFIG_DIR) { $env:YOLO_CONFIG_DIR = Join-Path $Root "data\lib_config\ultralytics" }
if (-not $env:MPLCONFIGDIR) { $env:MPLCONFIGDIR = Join-Path $Root "data\lib_config\matplotlib" }
if (-not $env:YOLO_OFFLINE) { $env:YOLO_OFFLINE = "true" }
$env:YOLO_AUTOINSTALL = "false"
New-Item -ItemType Directory -Force -Path $env:YOLO_CONFIG_DIR, $env:MPLCONFIGDIR | Out-Null

if (-not (Test-Path $Py)) {
    Fail "virtual environment not found at $Root\.venv-win - run: powershell -ExecutionPolicy Bypass -File scripts\setup.ps1"
}

$ErrorActionPreference = "Continue"  # native stderr must not abort the checks below
$importErr = & $Py -c "import PySide6.QtWidgets, cv2, ultralytics, torch" 2>&1
if ($LASTEXITCODE -ne 0) {
    Fail "dependencies are missing or broken in .venv-win - run scripts\setup.ps1 again.`n$($importErr | Select-Object -Last 1)"
}

$model = & $Py -c "import sys; sys.path.insert(0, r'$Root'); from app.settings import load_settings; print(load_settings().model_name)" 2>$null
if ($LASTEXITCODE -ne 0 -or -not $model) { $model = "yolo26n.pt" }
if (-not (Test-Path (Join-Path $Root "models\$model"))) {
    Write-Host "ATCS Monitor: note - models\$model is missing; the app will offer to download it on Start."
}

Set-Location $Root
& $Py -m app.main @args
exit $LASTEXITCODE
