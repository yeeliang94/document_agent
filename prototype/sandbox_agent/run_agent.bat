@echo off
rem Runs the agent on your own zip and template:
rem   run_agent.bat path\to\packet.zip path\to\template.xlsx "What the run should produce" [flags]
setlocal
rem SHIFT also moves %0, so remember this folder first.
set "HERE=%~dp0"
set "RC=1"
if "%~3"=="" (
  echo Usage: run_agent.bat path\to\packet.zip path\to\template.xlsx "What the run should produce" [flags]
  goto finish_nopopd
)
rem Resolve the paths before changing folder, so paths relative to where you ran it still work.
set "ZIP=%~f1"
set "TEMPLATE=%~f2"
set "GOAL=%~3"
set "FLAGS="
shift
shift
shift
:collect
if "%~1"=="" goto collected
set FLAGS=%FLAGS% %1
shift
goto collect
:collected
pushd "%HERE%"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  goto finish
)
".venv\Scripts\python.exe" -m sandbox_agent run --zip "%ZIP%" --template "%TEMPLATE%" --goal "%GOAL%" --ask console %FLAGS%
set "RC=%errorlevel%"

:finish
popd
:finish_nopopd
if not defined NO_PAUSE pause
exit /b %RC%
