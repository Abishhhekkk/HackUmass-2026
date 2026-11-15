@echo off
rem Vantage cloud side (laptop): double-click to start the API and the public tunnel.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating the Python environment ^(first run only^)...
  py -3 -m venv .venv 2>nul || python -m venv .venv
)
".venv\Scripts\python.exe" scripts\launch_cloud.py %*
echo.
pause
