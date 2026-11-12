@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Setting up for the first time...
    py -3 -m venv .venv || goto :error
    .venv\Scripts\python.exe -m pip install --quiet -r requirements.txt || goto :error
)
start "" ".venv\Scripts\pythonw.exe" main.py
exit /b 0

:error
echo Setup failed.
pause
exit /b 1
