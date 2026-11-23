@echo off
chcp 65001 >nul
title yaroPhotoSort server
cd /d "%~dp0"

if not exist "venv" (
    echo Creating venv...
    python -m venv venv
    if errorlevel 1 pause
    call venv\Scripts\activate.bat
    pip install -r requirements.txt
    if errorlevel 1 pause
) else (
    call venv\Scripts\activate.bat
)

echo Starting yaroPhotoSort on http://127.0.0.1:8460
start "" http://127.0.0.1:8460
python server.py

if errorlevel 1 pause
