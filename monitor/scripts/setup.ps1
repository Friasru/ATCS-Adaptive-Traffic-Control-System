# Create the Windows virtual environment (.venv-win) and install dependencies.
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1          auto: CUDA 12.8 torch if nvidia-smi works
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Cpu     force CPU-only torch (much smaller)
#   ... -Python "C:\Path\to\python.exe"                                  use a specific interpreter
#   ... -Cpu -Venv .venv-win-cpu                                          separate CPU-only environment (for the
#                                                                          lightweight build: build.ps1 -Venv .venv-win-cpu)
# The Linux/WSL environment (.venv, scripts/setup.sh) is separate and unaffected.
param(
    [switch]$Cpu,
    [string]$Python = "",
    [string]$Venv = ".venv-win"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
if (-not [IO.Path]::IsPathRooted($Venv)) { $Venv = Join-Path $Root $Venv }
$VPy = Join-Path $Venv "Scripts\python.exe"

function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "command failed ($LASTEXITCODE): $Exe $($Arguments -join ' ')" }
}

function Find-Python {
    if ($Python) { return $Python }
    # Prefer the py launcher; any 64-bit CPython 3.10+ with wheels for the pinned packages works
    # (tested: 3.14). Fall back to whatever "python" is on PATH.
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in @("3.14", "3.13", "3.12", "3.11", "3.10")) {
            $out = & py "-$v" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) { return "$out".Trim() }
        }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "No Python found. Install 64-bit Python 3.10+ from https://www.python.org/downloads/windows/ (or 'winget install Python.Python.3.14')."
}

if (-not (Test-Path $VPy)) {
    $py = Find-Python
    $check = & $py -c "import sys, struct; print(sys.version_info >= (3, 10) and struct.calcsize('P') == 8)"
    if ("$check".Trim() -ne "True") { throw "$py is not a 64-bit Python 3.10+" }
    Write-Host "Creating $Venv with $py"
    Invoke-Checked $py @("-m", "venv", $Venv)
}

$TorchIndex = "https://download.pytorch.org/whl/cpu"
if (-not $Cpu) {
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($smi) {
        & $smi.Source -L *> $null
        if ($LASTEXITCODE -eq 0) { $TorchIndex = "https://download.pytorch.org/whl/cu128" }
    }
}
Write-Host "PyTorch index: $TorchIndex"

Invoke-Checked $VPy @("-m", "pip", "install", "--upgrade", "pip")
Invoke-Checked $VPy @("-m", "pip", "install", "torch==2.11.0", "torchvision==0.26.0", "--index-url", $TorchIndex)
Invoke-Checked $VPy @("-m", "pip", "install", "-r", "requirements-dev.txt")
# ultralytics pulls in opencv-python, whose bundled Qt clashes with PySide6.
& $VPy -m pip uninstall -y opencv-python *> $null
Invoke-Checked $VPy @("-m", "pip", "install", "--force-reinstall", "--no-deps", "opencv-python-headless==5.0.0.93")
Invoke-Checked $VPy @("scripts\download_model.py")
Invoke-Checked $VPy @("-c", "import torch; print('torch', torch.__version__, '| CUDA available:', torch.cuda.is_available(), '|', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU only')")
Write-Host "Setup complete. Start the app with .\start.cmd (or .\start.ps1)"
