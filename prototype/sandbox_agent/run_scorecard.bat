@echo off
rem Runs the agent on every synthetic test packet through the proxy and writes a comparison table.
rem   run_scorecard.bat                    shapes A, B and C once each (about an hour; paid)
rem   run_scorecard.bat --shapes B         one shape only
setlocal
pushd "%~dp0"
set PYTHONUTF8=1
set "RC=1"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  goto finish
)
".venv\Scripts\python.exe" -m sandbox_agent packets runs\packets
if errorlevel 1 goto finish
".venv\Scripts\python.exe" -m sandbox_agent experiment --packets runs\packets %*
set "RC=%errorlevel%"

:finish
popd
if not defined NO_PAUSE pause
exit /b %RC%
