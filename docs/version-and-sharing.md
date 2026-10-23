# V1、V2 和分享压缩包的区别

## Git 提交与分支

一次 Git 提交可以理解成一份有编号的代码存档。你的 V1 提交是 `a7c0851`，说明这一版已经有可找回的代码记录。

分支是一条开发路线。本次从这个 V1 提交建立了本地分支 `codex/v2-batch-preview`，在这条分支上开发 V2；原有 V1 提交仍保留在历史里。创建分支不会自动上传代码到 GitHub，修改文件也不会自动产生新提交。

需要查看旧版代码时，先提交或保存当前修改，再从旧提交建立查看分支，例如：

```powershell
git switch -c inspect-v1 a7c0851
```

Git 保存的是已经纳入版本管理的文件。它不会保存被忽略的虚拟环境、编译产物或 Cookie。所以“恢复 V1 代码”与“恢复 V1 的可运行程序包”是两件事。本次也单独保留了 `releases/Shiguang-v1.0.0-windows-x64.zip`，用于直接运行旧版。

## 哪个压缩包能直接给别人用

| 文件来源 | 内容 | 别人如何使用 |
| --- | --- | --- |
| GitHub 的 Code → Download ZIP | 该分支或提交的源码，没有被忽略的 `dist/` | 先安装 Python 3.12，运行 `setup.cmd` 安装依赖，再运行 `start.cmd` |
| 本项目 `releases/Shiguang-v2.0.0-windows-x64.zip` | 完整 Windows 程序，包括 `Shiguang.exe`、`_internal` 和说明 | 解压整个文件夹，双击 `Shiguang.exe`；无需安装 Python |
| 手动压缩本地目录 | 取决于你实际放进压缩包的文件 | `.gitignore` 不会替你过滤手动压缩的文件；务必检查内容 |

`_internal` 是程序需要的运行库，分享时不能只发一个 `.exe`。使用者仍需有能够访问 Instagram 的网络；某些贴文需要使用者自己导入有效的 Instagram Cookie。

## 为什么这些文件应当忽略

- `.venv/`：本机 Python 环境，可以按 `requirements.txt` 重建，不适合直接复制到别人的电脑。
- `build/`、`dist/`、`releases/`：自动生成的中间文件与程序包。源码放 Git，成品包适合通过 GitHub Releases 附件或其他文件分享方式分发。
- `__pycache__/`、`.pytest_cache/`、`*.part`：缓存或未完成的临时文件。
- `cookies*.txt`、`.env`：可能包含登录凭据或其他个人配置，不应放进公开仓库或分享包。

因此，忽略这些内容是正常做法。关键是区分“给开发者的源码包”和“给使用者的完整程序包”，不需要为了分享应用把整个 `.venv` 强行提交到 GitHub。

官方参考：[Git 的忽略文件说明](https://git-scm.com/docs/gitignore)、[GitHub 源码压缩包说明](https://docs.github.com/en/repositories/working-with-files/using-files/downloading-source-code-archives)。
