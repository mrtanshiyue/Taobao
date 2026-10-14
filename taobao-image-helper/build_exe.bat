@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe call setup.bat
if not exist .venv\Scripts\python.exe exit /b 1

.venv\Scripts\python.exe -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); p.chromium.launch(channel='msedge', headless=True).close(); p.stop()"
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip install pyinstaller
if errorlevel 1 goto failed
.venv\Scripts\python.exe -c "import cv2; names=('haarcascade_frontalface_default.xml','haarcascade_profileface.xml'); assert hasattr(cv2, 'CascadeClassifier') and all(not cv2.CascadeClassifier(cv2.data.haarcascades + name).empty() for name in names), 'OpenCV face cascades are unavailable'"
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m unittest discover -s tests -v
if errorlevel 1 goto failed

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
.venv\Scripts\pyinstaller.exe --noconfirm --clean --windowed --onefile --name "淘宝图片助手" --collect-all cv2 --collect-all playwright app.py
if errorlevel 1 goto failed

echo Build complete: dist\淘宝图片助手.exe
if not defined CI pause
exit /b 0
:failed
echo Build failed. Check the messages above and try again.
if not defined CI pause
exit /b 1
