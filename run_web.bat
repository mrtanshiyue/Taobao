@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\setup.complete (
    call setup.bat
    if errorlevel 1 exit /b 1
)
if not exist .venv\Scripts\python.exe exit /b 1
.venv\Scripts\python.exe web_app.py
