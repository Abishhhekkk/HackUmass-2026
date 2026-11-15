@echo off
rem Vantage cloud side (laptop): double-click to start the API and the public tunnel.
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
  rem Find a real Python 3 (the plain "python" command may only be the Microsoft Store shortcut)
  for /d %%D in ("%LOCALAPPDATA%\Python\pythoncore-3*" "%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*") do (
    if not defined PY if exist "%%~D\python.exe" set "PY=%%~D\python.exe"
  )
)
if not defined PY (
  where py >nul 2>nul && for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%P"
)
if not defined PY (
  echo Python 3 was not found on this laptop.
  echo Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), then run this again.
  goto :end
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating the project's Python environment ^(first run only^) with !PY!
  "!PY!" -m venv .venv || goto :end
)
".venv\Scripts\python.exe" scripts\launch_cloud.py %*
:end
echo.
pause
