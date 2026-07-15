@echo off
rem Rebuild the portable Makroo.exe (requires: pip install pyinstaller pillow)
cd /d "%~dp0"
python -m PyInstaller --onefile --noconsole --name Makroo --icon makroo.ico --add-data "makroo.ico;." --clean --noconfirm makroo.py
if errorlevel 1 (
    echo Build FAILED.
    pause
    exit /b 1
)
move /y dist\Makroo.exe .\Makroo.exe >nul
rmdir /s /q build dist 2>nul
del Makroo.spec 2>nul
echo Done: Makroo.exe updated.
pause
