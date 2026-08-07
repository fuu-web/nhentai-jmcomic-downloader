@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.10 or newer first.
    pause
    exit /b 1
)

python -c "import tkinter, requests, bs4" >nul 2>nul
if errorlevel 1 (
    echo Required dependencies are missing.
    echo Run: python -m pip install -r requirements.txt
    pause
    exit /b 1
)

python entrypoints\start_gui.py
if errorlevel 1 pause
endlocal
