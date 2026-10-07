@echo off
rem ============================================================
rem  SlideCapture build script (Windows)
rem    build.bat           -> dist\SlideCapture\SlideCapture.exe  (folder, fast start)
rem    build.bat onefile   -> dist\SlideCapture.exe              (single exe)
rem ============================================================
setlocal
cd /d "%~dp0"

set "PY=py -3"
where py >nul 2>nul || set "PY=python"

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment .venv ...
    %PY% -m venv .venv || goto :error
)
call ".venv\Scripts\activate.bat" || goto :error

echo [2/3] Installing packages ...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt pyinstaller || goto :error

if /i "%~1"=="onefile" (set "ONEFILE=1") else (set "ONEFILE=0")

echo [3/3] Building (ONEFILE=%ONEFILE%) ...
pyinstaller --noconfirm --clean slide_capture.spec || goto :error

echo.
if "%ONEFILE%"=="1" (
    echo Done: dist\SlideCapture.exe
) else (
    echo Done: dist\SlideCapture\SlideCapture.exe
    echo       Copy the whole dist\SlideCapture folder when you move it.
)
echo.
pause
exit /b 0

:error
echo.
echo Build failed. See the messages above.
pause
exit /b 1
