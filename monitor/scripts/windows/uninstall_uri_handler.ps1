# Remove the atcs-monitor:// link registered by install_uri_handler.ps1 (current user only).
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall_uri_handler.ps1 -DryRun
#   powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall_uri_handler.ps1
#
# Removes only HKCU\Software\Classes\atcs-monitor, and only if install_uri_handler.ps1 created it.
# The app, its settings, history, recordings, the desktop link token and outbox are not touched.
param([switch]$DryRun)
$ErrorActionPreference = "Stop"
$Key = "HKCU:\Software\Classes\atcs-monitor"
$Marker = "ATCSDesktopLink"

if (-not (Test-Path -LiteralPath $Key)) {
    Write-Host "The atcs-monitor: link is not registered for this user. Nothing to do."
    exit 0
}
$owned = (Get-ItemProperty -LiteralPath $Key -ErrorAction SilentlyContinue).$Marker
if (-not $owned) {
    Write-Host "ATCS launcher: the atcs-monitor: handler was not created by install_uri_handler.ps1; it was left unchanged." -ForegroundColor Red
    exit 1
}
$current = (Get-ItemProperty -LiteralPath "$Key\shell\open\command" -ErrorAction SilentlyContinue).'(default)'
Write-Host "Registry key : HKEY_CURRENT_USER\Software\Classes\atcs-monitor"
Write-Host "Command      : $current"
if ($DryRun) {
    Write-Host "Dry run: nothing was removed."
    exit 0
}
Remove-Item -LiteralPath $Key -Recurse
Write-Host "Removed. atcs-monitor:// links no longer open anything."
