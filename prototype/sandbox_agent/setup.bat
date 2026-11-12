@echo off
rem One-time setup on Windows: a private Python environment for this prototype, then the settings file.
setlocal
pushd "%~dp0"
set PYTHONUTF8=1
set "RC=0"
set "PY="
where py >nul 2>nul
if not errorlevel 1 (
  py -3.12 -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>nul
  if not errorlevel 1 set "PY=py -3.12"
  if not defined PY (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
  )
)
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=python"
  )
)
if not defined PY (
  echo Python 3.12 or newer is required. Install it with the Python launcher or add it to PATH.
  set "RC=1"
  goto finish
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating the Python environment in .venv ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Could not create .venv.
    set "RC=1"
    goto finish
  )
)
echo Installing packages. Your PIP_INDEX_URL and PIP_CERT settings are used if set.
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo Package installation failed. Check the company package index settings and run setup.bat again.
  set "RC=1"
  goto finish
)
if not exist "sandbox_settings.json" (
  copy /y "sandbox_settings.example.json" "sandbox_settings.json" >nul
  echo.
  echo Created sandbox_settings.json. Put in your LiteLLM proxy address and model alias, save, then run check_proxy.bat.
  start "" notepad "sandbox_settings.json"
) else (
  echo Setup complete. Next: check_proxy.bat
)

:finish
popd
if not defined NO_PAUSE pause
exit /b %RC%
