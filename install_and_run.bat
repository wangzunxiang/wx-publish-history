@echo off
rem ============================================================
rem  wx-publish-history one-click installer + launcher (Windows)
rem  - auto-detects a Python 3.8+
rem  - creates .venv, installs playwright, downloads chromium
rem  - idempotent: safe to double-click repeatedly
rem  - mirrors first (China-friendly), falls back to official
rem ============================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   wx-publish-history - one-click install and start
echo ============================================================
echo.

rem ---------- 1. pick a Python version ----------
set PYCMD=
py -3.13 --version >nul 2>&1 && set PYCMD=py -3.13
if not defined PYCMD py -3.12 --version >nul 2>&1 && set PYCMD=py -3.12
if not defined PYCMD py -3.11 --version >nul 2>&1 && set PYCMD=py -3.11
if not defined PYCMD py -3.10 --version >nul 2>&1 && set PYCMD=py -3.10
if not defined PYCMD py -3 --version >nul 2>&1 && set PYCMD=py -3
if not defined PYCMD python --version >nul 2>&1 && set PYCMD=python
if not defined PYCMD (
  echo [ERROR] No Python found on this machine.
  echo         Install Python 3.10+ from https://www.python.org/downloads/
  echo         and check "Add python.exe to PATH" during installation,
  echo         then double-click this script again.
  pause
  exit /b 1
)
echo Using Python: %PYCMD%
%PYCMD% --version
echo.

rem ---------- 2. create venv ----------
if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating virtual environment .venv ...
  %PYCMD% -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Failed to create .venv.
    pause
    exit /b 1
  )
) else (
  echo [1/3] .venv already exists, skipping creation.
)

rem ---------- 3. install playwright (mirror first, official fallback) ----------
echo [2/3] Installing playwright via pip (may take a few minutes) ...
".venv\Scripts\pip.exe" install -i https://pypi.tuna.tsinghua.edu.cn/simple playwright
if errorlevel 1 (
  echo [WARN] Tsinghua mirror failed, retrying with official PyPI ...
  ".venv\Scripts\pip.exe" install playwright
)
if errorlevel 1 (
  echo [ERROR] pip install playwright failed. Check your network and retry.
  pause
  exit /b 1
)

rem ---------- 4. download chromium kernel (mirror first, official fallback) ----------
echo [3/3] Downloading Chromium kernel (~150MB) ...
set PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
".venv\Scripts\playwright.exe" install chromium
if errorlevel 1 (
  echo [WARN] Mirror download failed, retrying with official source ...
  set PLAYWRIGHT_DOWNLOAD_HOST=
  ".venv\Scripts\playwright.exe" install chromium
)
if errorlevel 1 (
  echo [WARN] Chromium download failed - not fatal.
  echo        The tool falls back to your installed Chrome / Edge.
  echo        Retry later from any terminal in this folder:
  echo        .venv\Scripts\playwright install chromium
)

echo.
echo ============================================================
echo   Install finished. Starting the service ...
echo   Your browser will open http://127.0.0.1:8765
echo   Scan the QR code with WeChat to log in.
echo   Close this window to stop the service.
echo ============================================================
echo.
".venv\Scripts\python.exe" wx_history.py %1
echo.
echo [done] Service window closed.
pause
endlocal
