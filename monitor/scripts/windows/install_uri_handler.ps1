# Register the atcs-monitor:// link for the current Windows user (no administrator rights).
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install_uri_handler.ps1 -DryRun   show what would be written
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install_uri_handler.ps1           register
#   ... -PythonW C:\path\to\pythonw.exe     use another interpreter (default: <monitor>\.venv-win\Scripts\pythonw.exe)
#   ... -Replace                            re-point a registration made by this script to this checkout
#
# Writes only HKCU\Software\Classes\atcs-monitor. The command it registers is fixed:
#   "<pythonw.exe>" "<monitor>\atcs_desktop_link.pyw" "%1"
# atcs_desktop_link.pyw accepts only atcs-monitor://open (docs/desktop_link.md). The installed
# ATCS Monitor program is not touched. Remove with uninstall_uri_handler.ps1.
param(
    [string]$MonitorDir = "",
    [string]$PythonW = "",
    [switch]$Replace,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"
$Scheme = "atcs-monitor"
$Key = "HKCU:\Software\Classes\$Scheme"
$Marker = "ATCSDesktopLink"

function Fail([string]$msg) { Write-Host "ATCS launcher: $msg" -ForegroundColor Red; exit 1 }

if (-not $MonitorDir) { $MonitorDir = Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }
$MonitorDir = [IO.Path]::GetFullPath($MonitorDir)
if ($MonitorDir.StartsWith("\\")) {
    Fail "the monitor folder is on a network or WSL share ($MonitorDir). Clone the repository to a local Windows drive first (docs/desktop_link.md, step 1)."
}
if (-not $PythonW) { $PythonW = Join-Path $MonitorDir ".venv-win\Scripts\pythonw.exe" }
$PythonW = [IO.Path]::GetFullPath($PythonW)
$Entry = Join-Path $MonitorDir "atcs_desktop_link.pyw"
$Launcher = Join-Path $MonitorDir "app\desktop_link\launcher.py"
$Icon = Join-Path $MonitorDir "app\resources\atcs_monitor.ico"

if ([IO.Path]::GetFileName($PythonW) -ne "pythonw.exe") { Fail "-PythonW must be a pythonw.exe (no console window): $PythonW" }
if ($PythonW.StartsWith("\\")) { Fail "pythonw.exe must be on a local drive, not $PythonW" }
foreach ($f in @($PythonW, $Entry, $Launcher)) {
    if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { Fail "expected file not found: $f" }
}
foreach ($p in @($PythonW, $Entry)) {
    # a quote would break the command line; % would be expanded by Windows
    if ($p -match '["%]') { Fail "unsupported character (quote or %) in path: $p" }
}
$Command = '"{0}" "{1}" "%1"' -f $PythonW, $Entry

$existing = Test-Path -LiteralPath $Key
if ($existing) {
    $owned = (Get-ItemProperty -LiteralPath $Key -ErrorAction SilentlyContinue).$Marker
    $current = (Get-ItemProperty -LiteralPath "$Key\shell\open\command" -ErrorAction SilentlyContinue).'(default)'
    if (-not $owned) {
        Fail "an $Scheme`: handler that this script did not create already exists. It was left unchanged."
    }
    if ($current -eq $Command) {
        Write-Host "The $Scheme`: link is already registered for this checkout:"
        Write-Host "  $Command"
        exit 0
    }
    if (-not $Replace) {
        Fail "the $Scheme`: link currently runs: $current`nRun again with -Replace to point it to $MonitorDir."
    }
}

Write-Host "Registry key : HKEY_CURRENT_USER\Software\Classes\$Scheme (current user only)"
Write-Host "Command      : $Command"
Write-Host "Accepted link: $Scheme`://open (anything else is refused by the launcher)"
if ($DryRun) {
    Write-Host "Dry run: nothing was written."
    exit 0
}

foreach ($k in @($Key, "$Key\DefaultIcon", "$Key\shell", "$Key\shell\open", "$Key\shell\open\command")) {
    # New-Item -Force would wipe an existing key, so only create what is missing
    if (-not (Test-Path -LiteralPath $k)) { New-Item -Path $k | Out-Null }
}
Set-Item -LiteralPath $Key -Value "URL:ATCS Monitor"
New-ItemProperty -LiteralPath $Key -Name "URL Protocol" -Value "" -PropertyType String -Force | Out-Null
New-ItemProperty -LiteralPath $Key -Name $Marker -Value $MonitorDir -PropertyType String -Force | Out-Null
if (Test-Path -LiteralPath $Icon -PathType Leaf) { Set-Item -LiteralPath "$Key\DefaultIcon" -Value ('"{0}",0' -f $Icon) }
Set-Item -LiteralPath "$Key\shell\open\command" -Value $Command

$check = (Get-ItemProperty -LiteralPath "$Key\shell\open\command").'(default)'
if ($check -ne $Command) { Fail "the registry value could not be verified: $check" }
Write-Host "Registered. Test it with: Start-Process '$Scheme`://open'  (your browser will ask before opening it)."
