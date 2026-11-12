@echo off
rem Builds the synthetic test packet once, runs the agent on it through the proxy and scores the result.
rem Extra flags pass through, for example: run_test.bat --max-scouts 0
setlocal
pushd "%~dp0"
set PYTHONUTF8=1
set "RC=1"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  goto finish
)
if not exist "runs\fixture\packet.zip" (
  if exist "runs\fixture" rd /s /q "runs\fixture"
  ".venv\Scripts\python.exe" -m sandbox_agent fixture runs\fixture
  if errorlevel 1 goto finish
)
".venv\Scripts\python.exe" -m sandbox_agent run --zip runs\fixture\packet.zip --template runs\fixture\output_template.xlsx --answer-key runs\fixture\answer_key.json %*
set "RC=%errorlevel%"

:finish
popd
if not defined NO_PAUSE pause
exit /b %RC%
