@echo off
rem Vantage, cloud side (laptop): double-click. Starts the cloud API and the Cloudflare tunnel.
title Vantage laptop (cloud side)
cd /d "%~dp0"
where git >nul 2>nul && git pull --ff-only -q
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
)
%PY% scripts\vantage_laptop.py %*
echo.
pause
