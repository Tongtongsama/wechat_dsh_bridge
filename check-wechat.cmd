@echo off
chcp 65001 >nul
setlocal
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
echo ============================================================
echo  WeChat UI health check
echo  Run this when the bridge stays silent.
echo ============================================================
echo.
"%PY%" "%~dp0tools\check_wechat.py"
echo.
pause