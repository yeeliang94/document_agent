@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3.12 -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>nul
  if not errorlevel 1 (
    py -3.12 scripts\start.py --bootstrap %*
    exit /b
  )
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>nul
  if not errorlevel 1 (
    py -3 scripts\start.py --bootstrap %*
    exit /b
  )
)
where python >nul 2>nul
if not errorlevel 1 (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>nul
  if not errorlevel 1 (
    python scripts\start.py --bootstrap %*
    exit /b
  )
)
echo Python 3.12 or newer is required. Install it with the Python launcher or add it to PATH.
exit /b 1
