@echo off
REM Double-click to start. You can also drag a .D folder onto this file to open it.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto deps

echo First run: setting up a private Python environment in this folder.
echo This needs an internet connection and takes a minute or two...
py -3 -m venv .venv 2>nul || python -m venv .venv
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo Could not find Python. Install Python 3.11 - 3.13 from python.org or Software Center,
  echo ticking "Add python.exe to PATH", then run this file again.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip

:deps
REM (re)install packages whenever requirements.txt has changed, e.g. after an update
fc /b requirements.txt ".venv\installed-requirements.txt" >nul 2>&1
if errorlevel 1 (
  echo Installing / updating packages...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Package installation failed - see the messages above.
    pause
    exit /b 1
  )
  copy /y requirements.txt ".venv\installed-requirements.txt" >nul
)

start "" ".venv\Scripts\pythonw.exe" wombat.py %*
