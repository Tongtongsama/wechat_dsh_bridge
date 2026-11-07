@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bridge.ps1" -Action status
echo.
pause
