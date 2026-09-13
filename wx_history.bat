@echo off
chcp 65001 >nul
rem 公众号发布历史查询工具 启动脚本 (Windows)
cd /d "%~dp0"
set PORT=%1
if "%PORT%"=="" set PORT=8765
if exist ".venv\Scripts\python.exe" (
  set PY=.venv\Scripts\python.exe
) else (
  echo [提示] 未找到 .venv，先运行一次: python -m venv .venv && .venv\Scripts\pip install playwright
  set PY=python
)
%PY% wx_history.py %PORT%
