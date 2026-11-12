@echo off
rem Builds InstagramDownloader.exe next to this file.
rem Run again after updating instaloader, because the .exe carries its own copy.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv || goto :error
)
.venv\Scripts\python.exe -m pip install --quiet --upgrade -r requirements.txt pyinstaller || goto :error
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name InstagramDownloader ^
    --icon "%~dp0assets\icon.ico" ^
    --add-data "%~dp0assets\icon.ico;assets" ^
    --collect-submodules browser_cookie3 ^
    --exclude-module tkinter --exclude-module pytest ^
    --distpath "%~dp0." --workpath "%~dp0build" --specpath "%~dp0build" ^
    main.py || goto :error
echo.
echo Done: %~dp0InstagramDownloader.exe
if "%~1"=="" pause
exit /b 0

:error
echo Build failed.
if "%~1"=="" pause
exit /b 1
