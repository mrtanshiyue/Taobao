@echo off
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
if not exist models mkdir models
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; $path='models\face_detection_yunet_2023mar.onnx'; $url='https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx'; $hash='8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4'; $download=$true; if (Test-Path $path) { $download=(Get-FileHash $path -Algorithm SHA256).Hash.ToLower() -ne $hash }; if ($download) { $tmp=$path+'.tmp'; Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $tmp; if ((Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower() -ne $hash) { throw 'YuNet model integrity check failed' }; Move-Item -Force $tmp $path }"
if errorlevel 1 goto failed
echo Setup complete. Microsoft Edge is required to run Taobao Image Helper.
if not defined CI pause
exit /b 0
:failed
echo Setup failed. Check Python 3 and your network connection, then try again.
if not defined CI pause
exit /b 1
