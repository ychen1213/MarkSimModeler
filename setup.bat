@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "scripts\bootstrap_modeler.py"
  goto done
)
py -3.12 -c "import sys" >nul 2>nul
if not errorlevel 1 (
  py -3.12 "scripts\bootstrap_modeler.py"
  goto done
)
py -3.11 -c "import sys" >nul 2>nul
if not errorlevel 1 (
  py -3.11 "scripts\bootstrap_modeler.py"
  goto done
)
python "scripts\bootstrap_modeler.py"
:done
if errorlevel 1 echo Setup did not complete. Read the error above; no administrator access is required.
pause
