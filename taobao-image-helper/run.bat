@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe call setup.bat
if not exist .venv\Scripts\python.exe exit /b 1
.venv\Scripts\python.exe app.py
