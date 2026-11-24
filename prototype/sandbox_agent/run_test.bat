@echo off
rem Builds the synthetic test packets once, runs the agent on one of them through the proxy and scores the result.
rem   run_test.bat                         the payroll consolidation packet
rem   run_test.bat payment_journal         another packet (see runs\packets)
rem   run_test.bat payroll_noise --shape A extra flags pass through
setlocal
pushd "%~dp0"
set PYTHONUTF8=1
set "RC=1"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  goto finish
)
set "PACKET=payroll_consolidation"
set "FIRST=%~1"
if defined FIRST if not "%FIRST:~0,2%"=="--" (
  set "PACKET=%FIRST%"
  shift
)
set "FLAGS="
:collect
if "%~1"=="" goto collected
set FLAGS=%FLAGS% %1
shift
goto collect
:collected
if not exist "runs\packets\%PACKET%\job.json" (
  ".venv\Scripts\python.exe" -m sandbox_agent packets runs\packets
  if errorlevel 1 goto finish
)
".venv\Scripts\python.exe" -m sandbox_agent run --job "runs\packets\%PACKET%" %FLAGS%
set "RC=%errorlevel%"

:finish
popd
if not defined NO_PAUSE pause
exit /b %RC%
