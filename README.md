# 拾光 · Instagram 图片保存

Windows 桌面应用：粘贴 Instagram 贴文链接，选择本地文件夹，保存贴文中的照片。

## 直接使用

双击 **`dist\Shiguang\Shiguang.exe`**，或项目根目录的 **`start.cmd`**。打包版无需另外安装 Python。移动到其他位置时，请复制整个 `Shiguang` 文件夹，保留其中的 `_internal` 目录。

1. 从 Instagram 复制贴文地址，粘贴到应用。
2. 点击「选择文件夹」，设置保存位置。下次启动会记住此位置。
3. 点击「开始保存」。下载结束后可直接打开保存文件夹。

支持 `https://www.instagram.com/p/短代码/` 形式的贴文链接，链接中附带的分享参数会被忽略。轮播里的照片按原位置编号，视频会被跳过；纯视频贴文会提示没有可保存的照片。`reel` / `tv` 链接也会解析，但不会把视频封面当照片保存。

文件组织方式：

```text
选定的保存位置/
  作者_贴文短代码/
    贴文短代码_01.jpg
    贴文短代码_02.jpg
```

扩展名依据实际图片格式决定。应用保留 Instagram 返回的图片字节，不再次压缩，也不承诺取得拍摄设备的原始文件。已有的有效照片会跳过。失败或取消后，可再次点击保存，继续补齐缺少的照片；正在传输的临时文件不会被当作完成文件。

如果保存目录中有同名的损坏图片或冲突文件，应用会提示你先移走该文件，再重试，不会直接覆盖它。

## 遇到登录提示时

Instagram 有时会限制匿名访问公开贴文。应用使用 [Instaloader 的官方 Python 接口](https://instaloader.github.io/as-module.html) 解析贴文，不能保证所有贴文始终允许匿名获取。

1. 在自己的浏览器中登录 Instagram，确认目标贴文可以正常打开。
2. 用你信任的本地 Cookie 导出工具，将 **instagram.com** 的 Cookie 导出为 **Netscape cookies.txt** 格式。此文件应包含 `sessionid`；JSON 文件不能直接导入。
3. 在应用中点击「导入 Cookie」，选择这个 `.txt` 文件，然后重试。
4. 本次使用结束后，可点击「清除 Cookie」，或退出应用。导入的登录信息只在进程内存中使用，应用不保存 Cookie 文件副本、路径或密码。浏览器导出的原文件仍由你管理。

Cookie 文件相当于登录凭证，请不要发给别人或提交到代码仓库。应用只读取 Instagram 域名下的 Cookie。它不能绕过私密账号权限、验证码、账号验证或 Instagram 的限流。出现限流提示时，请稍后重试。

## 网络与常见问题

- **网络超时 / 连接失败**：本机必须能够访问 Instagram 及其图片 CDN。应用不会提供网络代理服务；下载请求会使用进程环境中的标准 `HTTPS_PROXY` / `HTTP_PROXY` 设置。
- **需要登录 / Cookie 失效**：在浏览器重新确认登录状态并导出 Cookie。应用不收集账号密码。
- **贴文不存在或不可见**：确认链接未删除、当前账号有权查看。
- **下载中断**：已完成的照片保留；重试会跳过有效文件，重新下载未完成的照片。
- **取消需要等待**：取消会在当前网络请求返回或超时后完成，期间界面保持响应。关闭正在下载的窗口也会先取消任务。
- **磁盘不可写 / 空间不足**：选择有写入权限且空间充足的目录。

请保存你有权使用的内容，尊重作者的使用约定。

## 从源码运行

建议使用 Windows 10 / 11 64 位和 Python 3.12。首次使用双击 `setup.cmd` 安装依赖，然后双击 `start.cmd`。

也可以在 PowerShell 中执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

应用界面为 PySide6 Widgets，下载工作在独立线程中执行。设置通过 Qt 的用户设置保存，仅记录保存目录。

## 测试与打包

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1
```

测试使用模拟 Instagram 元数据和本地 HTTP 图片服务，检查链接验证、轮播选择、图片完整性、重复跳过、取消和失败清理。它们不会读取浏览器账号或访问私人贴文。实际 Instagram 登录与贴文可用性仍取决于网络、账号权限和平台接口状态。

打包脚本还会从另一个工作目录启动生成的程序，检查窗口创建及正常退出。

2026-10-23 验证：62 项自动测试通过；另使用 Instagram 官方账号的一篇公开轮播贴文完成在线解析与实际保存，共下载 5 张照片、跳过 4 段视频，重复保存时跳过全部 5 张已有照片。这个样本的成功不代表所有贴文都能匿名下载；Cookie 登录流程已做模拟测试，未使用真实账号验证。

运行依赖： [PySide6 / Qt](https://doc.qt.io/qtforpython-6/)、[Instaloader](https://github.com/instaloader/instaloader)、[Requests](https://requests.readthedocs.io/)、[Pillow](https://pillow.readthedocs.io/)。打包采用动态链接目录布局，相关第三方许可随运行库保留。
