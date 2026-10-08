@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_local.ps1" -OpenControl
if errorlevel 1 (
    echo Local server failed to start. See the message above.
    pause
    exit /b 1
)
echo Local server is ready: http://127.0.0.1:18080/control
pause
