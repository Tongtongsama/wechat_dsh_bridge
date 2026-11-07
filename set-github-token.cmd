@echo off
chcp 65001 >nul
setlocal
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
set "SECRET=%USERPROFILE%\.dsh\wechat-bridge\secret.env"
if not exist "%SECRET%" (
  echo secret.env not found: %SECRET%
  pause
  exit /b 1
)
echo ============================================================
echo  Editing secret keys: secret.env
echo  Replace the value after GITHUB_TOKEN= and save.
echo  Do not add quotes, do not delete other lines.
echo ============================================================
echo.
pause
notepad "%SECRET%"
echo.
"%PY%" "%~dp0tools\check_keys.py"
echo.
pause