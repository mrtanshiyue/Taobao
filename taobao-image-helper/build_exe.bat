@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe call setup.bat
if not exist .venv\Scripts\python.exe exit /b 1

set "PLAYWRIGHT_BROWSERS_PATH=%LOCALAPPDATA%\ms-playwright"
.venv\Scripts\python.exe -m pip install pyinstaller
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m playwright install chromium --no-shell
if errorlevel 1 goto failed

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
.venv\Scripts\pyinstaller.exe --noconfirm --clean --windowed --onedir --name "淘宝图片助手" --collect-all cv2 --collect-all playwright app.py
if errorlevel 1 goto failed

if not exist "%LOCALAPPDATA%\ms-playwright" goto failed
xcopy /E /I /Y "%LOCALAPPDATA%\ms-playwright" "dist\淘宝图片助手\ms-playwright\" >nul
if errorlevel 1 goto failed

echo Build complete: dist\淘宝图片助手\淘宝图片助手.exe
if not defined CI pause
exit /b 0
:failed
echo Build failed. Check the messages above and try again.
if not defined CI pause
exit /b 1
