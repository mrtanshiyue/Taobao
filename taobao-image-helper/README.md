# 淘宝图片助手（Windows）

## 安装与运行

1. 安装 Python 3.10 或更新版本，并在安装时启用 Python Launcher（`py` 命令）。
2. 双击 `setup.bat`，等待依赖和 Chromium 安装完成。
3. 想用网页界面，双击 `run_web.bat`；程序会启动本地服务并自动打开浏览器。想用桌面窗口则双击 `run.bat`。

网页模式只监听本机地址，其他设备无法访问；关闭启动窗口即可停止服务。淘宝登录、下载和图片处理都在本机完成。

如果想生成免安装 Python 的 Windows 程序，在 Windows 上双击 `build_exe.bat`。完成后运行 `dist/淘宝图片助手/淘宝图片助手.exe`；整个 `dist/淘宝图片助手` 文件夹需要一起保留和分发（包含浏览器文件）。

也可以在 GitHub 仓库的 Actions 页面下载自动构建的 Windows 压缩包：打开最新成功的“Build Windows EXE”运行，在 Artifacts 中下载并解压，双击 `淘宝图片助手.exe`。

## 使用

1. 粘贴淘宝商品链接，点“打开商品页”。
2. 在弹出的 Chromium 窗口中按需登录淘宝，确认打开的是目标商品页。
3. 回到助手点“扫描图片”，检查列表后点“下载并处理”。
4. 原图保存在所选目录的“下载原图”文件夹；镜像和马赛克结果保存在“处理后”。

程序会记住本机 Chromium 登录状态。镜像是左右翻转。人脸识别使用 OpenCV 内置分类器，侧脸、遮挡或很小的人脸可能漏检，建议检查处理结果。

## 文件

- `app.py`：桌面程序
- `requirements.txt`：运行依赖
- `setup.bat`：首次安装
- `run.bat`：启动程序
- `run_web.bat`：启动本机网页界面
- `web_app.py`、`index.html`：本机网页和服务
- `build_exe.bat`：在 Windows 上生成 exe 发布目录
