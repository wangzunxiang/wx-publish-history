@echo off
rem WeChat Official Accounts - publish history local viewer (Windows launcher)
chcp 65001 >nul
cd /d "%~dp0"
set PORT=%1
if "%PORT%"=="" set PORT=8765
if exist ".venv\Scripts\python.exe" (
  set PY=.venv\Scripts\python.exe
) else (
  echo [hint] .venv not found. Run once: python -m venv .venv ^&^& .venv\Scripts\pip install playwright
  set PY=python
)
%PY% wx_history.py %PORT%
echo [done] service window closed.
