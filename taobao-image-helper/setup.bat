@echo off
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Setup complete. Microsoft Edge is required to run Taobao Image Helper.
if not defined CI pause
exit /b 0
:failed
echo Setup failed. Check Python 3 and your network connection, then try again.
if not defined CI pause
exit /b 1
