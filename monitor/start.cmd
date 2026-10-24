@echo off
rem Launch ATCS Monitor on Windows (double-click, or: start.cmd --video FILE --start)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
exit /b %ERRORLEVEL%
