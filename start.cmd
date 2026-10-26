@echo off
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 goto use_python
py -3 "%~dp0scripts\control.py" start %*
exit /b %errorlevel%
:use_python
python "%~dp0scripts\control.py" start %*
exit /b %errorlevel%
