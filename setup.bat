@echo off
cd /d "%~dp0"
if exist .venv\setup.complete del /q .venv\setup.complete
py -3 -m venv .venv
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
if not exist models mkdir models
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; $models=@(@{path='models\face_detection_yunet_2023mar.onnx';url='https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx';hash='8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4'},@{path='models\object_detection_nanodet_2022nov.onnx';url='https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/object_detection_nanodet/object_detection_nanodet_2022nov.onnx';hash='4b82da9944b88577175ee23a459dce2e26e6e4be573def65b1055dc2d9720186'}); foreach ($model in $models) { $download=$true; if (Test-Path $model.path) { $download=(Get-FileHash $model.path -Algorithm SHA256).Hash.ToLower() -ne $model.hash }; if ($download) { $tmp=$model.path+'.tmp'; Invoke-WebRequest -UseBasicParsing -Uri $model.url -OutFile $tmp; if ((Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower() -ne $model.hash) { throw ('Model integrity check failed: '+$model.path) }; Move-Item -Force $tmp $model.path } }"
if errorlevel 1 goto failed
> .venv\setup.complete echo setup complete
if errorlevel 1 goto failed
echo Setup complete. Microsoft Edge is required to run Taobao Image Helper.
if not defined CI pause
exit /b 0
:failed
echo Setup failed. Check Python 3 and your network connection, then try again.
if not defined CI pause
exit /b 1
