@echo off
setlocal
cd /d "%~dp0"
rem Read local_runtime.json BEFORE requiring any Python on PATH.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch_windows.ps1" %*
set "MARKSIM_START_EXIT=%ERRORLEVEL%"
if not "%MARKSIM_START_EXIT%"=="0" (
  echo.
  echo MarkSimModeler could not start. Read the error above.
  echo Details are kept in the logs folder next to this launcher.
  if not "%MARKSIM_LAUNCHER_NO_PAUSE%"=="1" pause
)
exit /b %MARKSIM_START_EXIT%
