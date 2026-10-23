# 拾光 V2 · Instagram 图片与视频保存

Windows 桌面应用：批量粘贴 Instagram 贴文链接，预览照片，选择是否同时下载视频，保存到指定文件夹。

## 直接使用

分享给普通使用者的文件是 **`releases\Shiguang-v2.0.0-windows-x64.zip`**。解压整个 ZIP 后，打开 `Shiguang` 文件夹，双击 **`Shiguang.exe`**，无需安装 Python。移动应用时必须保留同级的 `_internal` 目录。

在开发目录中，也可以双击 **`start.cmd`**，它会优先启动 `dist\Shiguang\Shiguang.exe`。

1. 粘贴一条或多条贴文链接，**一行一个链接**。空行会忽略，同一贴文的不同分享参数会去重。
2. 点击「解析链接」，在任务队列中选择一篇贴文，查看右侧缩略图；点击照片可以放大。视频显示封面与类型标识。
3. 选择保存位置。默认只下载照片；需要视频时，勾选「同时下载视频」。
4. 点击「开始保存」。队列逐条处理，一条失败不影响其他贴文。可取消，也可只重试失败的任务。
5. 完成后打开保存文件夹。已经存在且有效的文件会跳过。

支持 `instagram.com`、`www.instagram.com`、`m.instagram.com` 下的 `/p/`、`/reel/`、`/reels/`、`/tv/` 完整贴文地址，忽略分享查询参数。主页、快拍及 `/share/` 短链接不属于当前支持范围，请先在浏览器打开并复制标准贴文地址。

## 保存方式

```text
选定的保存位置/
  作者_贴文短代码/
    贴文短代码_01.jpg
    贴文短代码_02.mp4
    贴文短代码_03.jpg
```

编号保留贴文中的原始位置。关闭视频选项时会跳过视频，因此照片编号中可能有空缺。只包含视频的贴文，需启用视频选项才能保存。

照片扩展名依据真实格式决定，视频保存为 MP4。下载保持服务器返回的原字节，不再次压缩，不承诺获得拍摄设备的原始文件。预览使用缩小的图片，不影响实际下载质量；视频目前只预览封面，不在应用中播放。

应用会检查图片能否完整解码，检查 MP4 容器的基本结构及传输长度；这不等同于对视频所有帧进行解码验证。有效的已有文件会跳过，同名的损坏或冲突文件不会被覆盖，请按提示先移走后重试。

取消或失败后，已完成的文件保留，未完成的 `.part` 临时文件清理。重试会补齐缺少的内容。图片预览失败时会显示提示，不会将预览失败误当成下载失败。

## 登录与 Cookie

Instagram 可能限制匿名访问公开贴文。应用通过 [Instaloader 官方 Python 接口](https://instaloader.github.io/as-module.html) 读取媒体，不保证所有贴文始终支持匿名解析。

1. 在自己的浏览器中登录 Instagram，并确认目标贴文可以正常打开。
2. 使用你信任的本地 Cookie 导出工具，将 **instagram.com** 的 Cookie 导出为 **Netscape cookies.txt** 格式，文件应包含 `sessionid`。JSON 文件不能直接导入。
3. 点击应用中的「导入 Cookie」，选择 `.txt` 文件，然后重试失败任务。
4. 可点击「清除 Cookie」，或退出应用。登录信息只在本次进程内存中使用，应用不保存 Cookie、文件副本、Cookie 文件路径或密码。浏览器导出的原文件仍由你管理。

Cookie 文件相当于登录凭证，不要把自己的 Cookie 放入仓库或分享压缩包。应用只读取 Instagram 域名下的 Cookie，不能绕过账号权限、验证码、验证页面或限流。

## 网络与常见问题

- **连接失败 / 超时**：电脑必须能访问 Instagram 和其媒体 CDN。应用不提供代理服务；网络请求支持进程环境中的 `HTTPS_PROXY` / `HTTP_PROXY` 设置。
- **登录失效 / 要求验证**：在浏览器完成验证，重新导出 Cookie 后重试。
- **部分任务失败**：查看对应队列项的错误，处理后点击重试；其他成功文件已经保留。
- **请求过于频繁**：稍后再试。批量下载按顺序执行，不会为了批量而并发请求 Instagram 元数据。
- **取消需要等待**：取消会在当前网络请求返回或超时后完成。关闭窗口时也会等待后台任务安全结束。
- **目录无法写入**：选择有权限且空间充足的位置。

请保存你有权访问和使用的内容，尊重作者的使用约定。

## GitHub 源码包与成品包

**Code → Download ZIP 下载的是源码，不是上述可直接运行的成品包。** `.gitignore` 忽略 `.venv/`、`dist/`、`build/`、`releases/` 是正常做法。

源码使用者需要安装 Python 3.12，再运行 `setup.cmd` 安装依赖；普通使用者应下载完整的 `Shiguang-v2.0.0-windows-x64.zip`。

关于 V1 存档、V2 分支和如何分享，见 [版本与分享说明](docs/version-and-sharing.md)。本次保留了 V1 的代码提交 `a7c0851` 与成品 ZIP；V2 在本地 `codex/v2-batch-preview` 分支开发。

## 从源码运行

当前在 Windows 11 64 位验证，建议使用 Python 3.12。首次运行双击 `setup.cmd`，完成后双击 `start.cmd`。如果希望运行修改后的源码，可执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

界面使用 PySide6 Widgets，解析、预览及下载放在后台线程中执行。用户设置仅记录保存目录，任务队列和预览只在本次会话中保留。

## 测试、打包与分享

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1
```

打包脚本会运行自动测试、构建桌面程序、从另一个工作目录检查启动和正常退出，再生成完整的成品 ZIP。构建期间临时隔离 PATH，避免其他软件的同名动态库被误收集。

自动测试使用模拟 Instagram 元数据和本地 HTTP 服务，覆盖批量、图片、视频、预览、重复跳过、失败与取消，不读取真实浏览器账号。在线可用性取决于使用者的网络、账号权限和 Instagram 接口状态。

2026-10-23 验证：116 项自动测试通过；公开贴文实测解析出 5 张照片和 4 个视频，界面成功展示全部 9 个照片及封面预览，另实际保存了照片和一段约 27 秒的 MP4。测试验证关闭视频时只保存照片、开启视频后补保存 MP4 并跳过已有照片。真实账号 Cookie 登录未纳入在线验证。

运行依赖：[PySide6 / Qt](https://doc.qt.io/qtforpython-6/)、[Instaloader](https://github.com/instaloader/instaloader)、[Requests](https://requests.readthedocs.io/)、[Pillow](https://pillow.readthedocs.io/)。相关第三方许可随运行库保留。界面及行为设计见 [V2 设计说明](docs/v2-design.md)。
