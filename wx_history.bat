@echo off
rem WeChat Official Accounts - publish history local viewer (Windows launcher)
rem If .venv is missing, delegate to install_and_run.bat (one-click installer).
chcp 65001 >nul
cd /d "%~dp0"
set PORT=%1
if "%PORT%"=="" set PORT=8765
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" wx_history.py %PORT%
  echo [done] service window closed.
) else (
  echo [hint] .venv not found - launching one-click installer...
  call install_and_run.bat %PORT%
)
