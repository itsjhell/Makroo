@echo off
cd /d "%~dp0"
rem Avvia Makroo senza finestra della console (pythonw); fallback su python
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw makroo.py
) else (
    start "" python makroo.py
)
