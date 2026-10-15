@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\setup.complete (
    call setup.bat
    if errorlevel 1 exit /b 1
)
if not exist .venv\Scripts\python.exe goto failed
.venv\Scripts\python.exe -c "import hashlib; from pathlib import Path; p=Path('models/face_detection_yunet_2023mar.onnx'); assert p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() == '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4', 'Run setup.bat to download the valid YuNet model'"
if errorlevel 1 goto failed
.venv\Scripts\python.exe -c "import hashlib; from pathlib import Path; p=Path('models/object_detection_nanodet_2022nov.onnx'); assert p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() == '4b82da9944b88577175ee23a459dce2e26e6e4be573def65b1055dc2d9720186', 'Run setup.bat to download the valid NanoDet model'"
if errorlevel 1 goto failed
.venv\Scripts\python.exe -c "import hashlib; from pathlib import Path; p=Path('models/u2netp.onnx'); assert p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() == '309c8469258dda742793dce0ebea8e6dd393174f89934733ecc8b14c76f4ddd8', 'Run setup.bat to download the valid U2NetP model'"
if errorlevel 1 goto failed

.venv\Scripts\python.exe -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); p.chromium.launch(channel='msedge', headless=True).close(); p.stop()"
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip install pyinstaller
if errorlevel 1 goto failed
.venv\Scripts\python.exe -c "import cv2; d=cv2.FaceDetectorYN.create('models/face_detection_yunet_2023mar.onnx','',(320,320)); assert d is not None, 'YuNet model could not be loaded'"
if errorlevel 1 goto failed
.venv\Scripts\python.exe -c "import cv2; n=cv2.dnn.readNet('models/object_detection_nanodet_2022nov.onnx'); assert not n.empty(), 'NanoDet model could not be loaded'"
if errorlevel 1 goto failed
.venv\Scripts\python.exe -c "import cv2; n=cv2.dnn.readNet('models/u2netp.onnx'); assert not n.empty(), 'U2NetP model could not be loaded'"
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m unittest discover -s tests -v
if errorlevel 1 goto failed

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
.venv\Scripts\pyinstaller.exe --noconfirm --clean --windowed --onefile --name "淘宝图片助手" --add-data "index.html;." --add-data "models\face_detection_yunet_2023mar.onnx;models" --add-data "models\object_detection_nanodet_2022nov.onnx;models" --add-data "models\u2netp.onnx;models" --add-data "models\LICENSE;models" --add-data "models\NANODET_LICENSE;models" --add-data "models\U2NETP_LICENSE;models" --collect-all cv2 --collect-all playwright web_app.py
if errorlevel 1 goto failed
copy /y "models\LICENSE" "dist\第三方模型许可证.txt" >nul
if errorlevel 1 goto failed
copy /y "models\NANODET_LICENSE" "dist\NanoDet模型许可证.txt" >nul
if errorlevel 1 goto failed
copy /y "models\U2NETP_LICENSE" "dist\U2NetP模型许可证.txt" >nul
if errorlevel 1 goto failed

echo Build complete: dist\淘宝图片助手.exe
if not defined CI pause
exit /b 0
:failed
echo Build failed. Check the messages above and try again.
if not defined CI pause
exit /b 1
