@echo off
rem Starts the TG Cleaner local service.
rem   start.bat            normal use (runs one-time setup first if needed)
rem   start.bat --demo     fake data, never contacts Telegram
rem   start.bat setup      re-enter API ID / hash
rem   start.bat logout     log out of Telegram and delete the local session
setlocal
cd /d "%~dp0service"

where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
  echo Python 3.10 or newer is required. Install it from https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PY% -m venv .venv || goto :fail
)
fc /b requirements.txt .venv\.installed-requirements >nul 2>nul
if errorlevel 1 (
  echo Installing dependencies...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :fail
  copy /y requirements.txt .venv\.installed-requirements >nul
)

if /i "%~1"=="setup" goto :passthrough
if /i "%~1"=="logout" goto :passthrough
if /i "%~1"=="reset" goto :passthrough
if /i "%~1"=="--demo" (
  ".venv\Scripts\python.exe" -m tgcleaner serve --demo
  goto :end
)

".venv\Scripts\python.exe" -c "from tgcleaner.config import load_config; import sys; sys.exit(0 if load_config() else 1)"
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m tgcleaner setup || goto :fail
)
".venv\Scripts\python.exe" -m tgcleaner serve %*
goto :end

:passthrough
".venv\Scripts\python.exe" -m tgcleaner %*
goto :end

:fail
echo Something went wrong. See the messages above.
pause
exit /b 1

:end
endlocal
