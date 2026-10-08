@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_local.ps1"
if errorlevel 1 (
    echo Local server failed to stop. See the message above.
    pause
    exit /b 1
)
pause
