@echo off
chcp 65001 >nul
setlocal
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
echo ============================================================
echo  Edit bridge config: config.json
echo  After saving, this runs a format check.
echo ============================================================
echo.
pause
notepad "%~dp0config.json"
echo.
"%PY%" "%~dp0tools\check_config.py"
echo.
pause