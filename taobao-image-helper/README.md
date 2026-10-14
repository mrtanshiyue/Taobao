# 淘宝图片助手（Windows）

## 安装与运行

1. 安装 Python 3.10 或更新版本，并在安装时启用 Python Launcher（`py` 命令）。
2. 双击 `setup.bat` 安装依赖；运行时需要 Microsoft Edge。
3. 想用网页界面，双击 `run_web.bat`；程序会启动本地服务并自动打开浏览器。想用桌面窗口则双击 `run.bat`。

网页模式只监听本机地址，其他设备无法访问；关闭启动窗口即可停止服务。淘宝登录、下载和图片处理都在本机完成。

如果想生成免安装 Python 的 Windows 单文件程序，在 Windows 上双击 `build_exe.bat`。完成后运行 `dist/淘宝图片助手.exe`。程序使用电脑上已安装的 Microsoft Edge，不再附带 Chromium。

也可以在 GitHub 仓库的 Actions 页面下载自动构建的 Windows 压缩包：打开最新成功的“Build Windows EXE”运行，在 Artifacts 中下载并解压，双击里面的 `淘宝图片助手.exe`。

## 使用

1. 粘贴淘宝商品链接，点“打开商品页”。
2. 在弹出的 Microsoft Edge 窗口中按需登录淘宝，确认打开的是目标商品页。
3. 回到助手点“扫描图片”，可在列表中把图片类型改为主图或详情图，再点“下载并处理”。
4. 每次下载会在所选目录下创建独立的时间戳文件夹；原图保存在其中的“下载原图”，处理结果保存在“处理后”。

程序会在单独的 Edge 配置中记住登录状态。镜像是左右翻转。扫描会逐段滚动并等待图片加载，超长页面会提示达到扫描上限；图片类型可以手动修正。人脸识别使用 OpenCV 内置分类器，较小的人脸更容易被检出，但侧脸或遮挡仍可能漏检，建议检查处理结果。

## 文件

- `app.py`：桌面程序
- `requirements.txt`：运行依赖
- `setup.bat`：首次安装
- `run.bat`：启动程序
- `run_web.bat`：启动本机网页界面
- `web_app.py`、`index.html`：本机网页和服务
- `build_exe.bat`：在 Windows 上生成 exe 发布目录
