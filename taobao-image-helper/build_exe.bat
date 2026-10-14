@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe call setup.bat
if not exist .venv\Scripts\python.exe exit /b 1

.venv\Scripts\python.exe -m pip install pyinstaller
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
