@echo off
rem Checks that the proxy in sandbox_settings.json supports every step the agent needs. Extra flags pass through.
setlocal
pushd "%~dp0"
set PYTHONUTF8=1
set "RC=1"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  goto finish
)
".venv\Scripts\python.exe" proxy_check.py %*
set "RC=%errorlevel%"

:finish
popd
if not defined NO_PAUSE pause
exit /b %RC%
