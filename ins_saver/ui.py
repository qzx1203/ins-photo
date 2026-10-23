"""Native desktop interface for saving Instagram post photos."""

from __future__ import annotations

from pathlib import Path
from threading import Event

from PySide6.QtCore import QSettings, QStandardPaths, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .core import (
    AppError,
    Cancelled,
    DownloadResult,
    DownloadService,
    load_instagram_cookies,
    parse_post_url,
)


class PhotoMark(QWidget):
    """A small, resolution-independent photo emblem."""

    def __init__(self, *, large: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.large = large
        self.setFixedSize(88, 58) if large else self.setFixedSize(40, 40)

    def paintEvent(self, event):  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.large:
            painter.setPen(QPen(QColor("#dad4c9"), 1.5))
            painter.setBrush(QColor("#faf8f4"))
            painter.translate(42, 28)
            painter.rotate(-11)
            painter.drawRoundedRect(-24, -22, 44, 46, 5, 5)
            painter.rotate(11)
            painter.setBrush(QColor("#ffffff"))
            painter.drawRoundedRect(-17, -23, 44, 46, 5, 5)
            painter.setPen(QPen(QColor("#dba084"), 1.7))
            painter.drawEllipse(8, -14, 6, 6)
            painter.drawLine(-10, 9, 0, -2)
            painter.drawLine(0, -2, 10, 9)
            painter.drawLine(10, 9, 16, 2)
            painter.drawLine(16, 2, 21, 9)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#ee7543"))
            painter.drawRoundedRect(0, 0, 40, 40, 12, 12)
            painter.setPen(QPen(QColor("#ffffff"), 1.8))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(10, 11, 20, 19, 4, 4)
            painter.drawEllipse(15, 16, 10, 10)
            painter.drawPoint(26, 15)


class DownloadWorker(QThread):
    progress = Signal(int, int, str)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, url: str, destination: Path, cookies: dict | None, parent=None):
        super().__init__(parent)
        self.url = url
        self.destination = destination
        self.cookies = dict(cookies) if cookies else None
        self.cancel_event = Event()

    def run(self):
        try:
            result = DownloadService().run(
                self.url,
                self.destination,
                cookies=self.cookies,
                cancel=self.cancel_event,
                on_progress=self.progress.emit,
            )
            self.succeeded.emit(result)
        except Cancelled:
            self.cancelled.emit()
        except AppError as exc:
            self.failed.emit(str(exc))
        except Exception:
            self.failed.emit("保存时发生意外错误，请稍后重试。")
        finally:
            self.cookies = None


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("拾光 · Instagram 图片保存")
        self.resize(1080, 800)
        self.setMinimumSize(900, 680)
        self.settings = QSettings("Shiguang", "InsPhotoSaver")
        self._worker: DownloadWorker | None = None
        self._cookies: dict | None = None
        self._busy = False
        self._close_after_cancel = False
        self._last_message = ""
        self._last_directory: Path | None = None
        self._build_ui()
        self._apply_style()
        pictures = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
        fallback = str(Path(pictures or Path.home()) / "Instagram")
        try:
            destination = self.settings.value("destination", fallback, type=str) or fallback
        except (TypeError, ValueError):
            destination = fallback
        self.destination_input.setText(destination)

    @staticmethod
    def _label(text: str, name: str = "", *, wrap: bool = False) -> QLabel:
        label = QLabel(text)
        if name:
            label.setObjectName(name)
        label.setWordWrap(wrap)
        return label

    @staticmethod
    def _button(text: str, name: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(name)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(192)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(24, 32, 22, 26)
        side.setSpacing(0)
        side.addWidget(PhotoMark())
        side.addSpacing(13)
        side.addWidget(self._label("拾光", "brand"))
        side.addSpacing(3)
        side.addWidget(self._label("INS PHOTO SAVER", "brand_caption"))
        side.addSpacing(43)
        current = self._label("▧   链接下载", "current_nav")
        current.setMinimumHeight(42)
        side.addWidget(current)
        side.addSpacing(7)
        self.help_button = self._button("?    使用帮助", "help_button")
        self.help_button.clicked.connect(self._show_help)
        self.help_button.setMinimumHeight(40)
        side.addWidget(self.help_button)
        side.addStretch()
        side.addWidget(self._label("把喜欢，\n留在本地。", "side_quote"))
        side.addSpacing(12)
        side.addWidget(self._label("收集每一张\n值得回看的照片。", "side_note"))
        side.addSpacing(28)
        side.addWidget(self._label("LOCAL FIRST  /  拾光", "side_footer"))
        root_layout.addWidget(sidebar)

        main = QWidget()
        main.setObjectName("main_panel")
        content = QVBoxLayout(main)
        content.setContentsMargins(32, 30, 32, 20)
        content.setSpacing(18)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(6)
        titles.addWidget(self._label("A LITTLE SPACE FOR INSPIRATION", "eyebrow"))
        titles.addWidget(self._label("保存此刻的灵感", "page_title"))
        titles.addWidget(self._label("粘贴贴文链接，让喜欢的照片有处安放。", "page_subtitle"))
        header.addLayout(titles, 1)
        local = self._label("●  仅保存在本机", "local_badge")
        local.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(local, 0, Qt.AlignmentFlag.AlignTop)
        content.addLayout(header)

        form = QFrame()
        form.setObjectName("form_card")
        form_layout = QVBoxLayout(form)
        form_layout.setContentsMargins(24, 20, 24, 20)
        form_layout.setSpacing(10)
        form_layout.addWidget(self._label("01   贴文链接", "section_title"))
        input_row = QHBoxLayout()
        input_row.setSpacing(9)
        self.link_input = QLineEdit()
        self.link_input.setObjectName("link_input")
        self.link_input.setAccessibleName("Instagram 贴文链接")
        self.link_input.setPlaceholderText("https://www.instagram.com/p/…/")
        self.link_input.setClearButtonEnabled(True)
        self.link_input.setMinimumHeight(46)
        self.link_input.returnPressed.connect(self._start_download)
        self.link_input.textChanged.connect(self._reset_link_error)
        input_row.addWidget(self.link_input, 1)
        self.paste_button = self._button("粘贴链接", "paste_button")
        self.paste_button.setMinimumSize(96, 46)
        self.paste_button.clicked.connect(self._paste_link)
        input_row.addWidget(self.paste_button)
        form_layout.addLayout(input_row)
        self.link_hint = self._label("支持公开单图与轮播照片 · 视频自动跳过", "link_hint", wrap=True)
        form_layout.addWidget(self.link_hint)
        form_layout.addSpacing(8)
        form_layout.addWidget(self._label("02   保存位置", "section_title"))
        destination_row = QHBoxLayout()
        destination_row.setSpacing(9)
        self.destination_input = QLineEdit()
        self.destination_input.setObjectName("destination_input")
        self.destination_input.setAccessibleName("保存文件夹")
        self.destination_input.setReadOnly(True)
        self.destination_input.setMinimumHeight(42)
        destination_row.addWidget(self.destination_input, 1)
        self.browse_button = self._button("选择文件夹", "browse_button")
        self.browse_button.setMinimumSize(96, 42)
        self.browse_button.clicked.connect(self._choose_destination)
        destination_row.addWidget(self.browse_button)
        form_layout.addLayout(destination_row)

        cookie_row = QHBoxLayout()
        cookie_row.setSpacing(8)
        cookie_row.addWidget(self._label("遇到登录限制？", "muted"))
        self.cookie_button = self._button("导入 Cookie", "cookie_button")
        self.cookie_button.clicked.connect(self._import_cookies)
        self.cookie_button.setToolTip("从本机导入 Netscape 格式的 Instagram Cookie 文件")
        cookie_row.addWidget(self.cookie_button)
        self.cookie_status = self._label("", "cookie_status")
        cookie_row.addWidget(self.cookie_status)
        self.clear_cookie_button = self._button("清除 Cookie", "clear_cookie_button")
        self.clear_cookie_button.clicked.connect(self._clear_cookies)
        self.clear_cookie_button.hide()
        cookie_row.addWidget(self.clear_cookie_button)
        cookie_row.addStretch()
        form_layout.addLayout(cookie_row)
        line = QFrame()
        line.setObjectName("divider")
        line.setFixedHeight(1)
        form_layout.addWidget(line)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 4, 0, 0)
        actions.addWidget(self._label("请仅保存有权使用的图片。", "small_note"), 1)
        self.cancel_button = self._button("取消", "cancel_button")
        self.cancel_button.setMinimumHeight(44)
        self.cancel_button.clicked.connect(self._cancel_download)
        self.cancel_button.hide()
        actions.addWidget(self.cancel_button)
        self.download_button = self._button("↓  开始保存", "download_button")
        self.download_button.setMinimumSize(152, 44)
        self.download_button.clicked.connect(self._start_download)
        actions.addWidget(self.download_button)
        form_layout.addLayout(actions)
        content.addWidget(form)

        activity = QFrame()
        activity.setObjectName("activity_card")
        activity_layout = QVBoxLayout(activity)
        activity_layout.setContentsMargins(24, 18, 24, 16)
        activity_layout.setSpacing(10)
        activity_header = QHBoxLayout()
        activity_header.addWidget(self._label("下载动态", "section_title"), 1)
        self.status_label = self._label("等待新链接", "status_label")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        activity_header.addWidget(self.status_label)
        activity_layout.addLayout(activity_header)

        self.empty_state = QWidget()
        self.empty_state.setObjectName("empty_state")
        empty = QVBoxLayout(self.empty_state)
        empty.setContentsMargins(0, 0, 0, 0)
        empty.setSpacing(5)
        empty.addStretch()
        empty.addWidget(PhotoMark(large=True), 0, Qt.AlignmentFlag.AlignHCenter)
        empty.addWidget(self._label("你的下一份灵感，从一个链接开始", "empty_title"), 0, Qt.AlignmentFlag.AlignHCenter)
        empty.addWidget(self._label("照片会保存在指定文件夹，方便随时回看。", "empty_note"), 0, Qt.AlignmentFlag.AlignHCenter)
        empty.addStretch()
        activity_layout.addWidget(self.empty_state, 1)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("progress_bar")
        self.progress_bar.setAccessibleName("下载进度")
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.hide()
        activity_layout.addWidget(self.progress_bar)
        self.activity_log = QPlainTextEdit()
        self.activity_log.setObjectName("activity_log")
        self.activity_log.setAccessibleName("下载动态记录")
        self.activity_log.setReadOnly(True)
        self.activity_log.setMaximumBlockCount(300)
        self.activity_log.setMinimumHeight(72)
        self.activity_log.setFrameShape(QFrame.Shape.NoFrame)
        self.activity_log.hide()
        activity_layout.addWidget(self.activity_log, 1)
        result_row = QHBoxLayout()
        self.result_label = self._label("", "result_label", wrap=True)
        result_row.addWidget(self.result_label, 1)
        self.retry_button = self._button("重试", "retry_button")
        self.retry_button.clicked.connect(self._start_download)
        self.retry_button.hide()
        result_row.addWidget(self.retry_button)
        self.open_folder_button = self._button("打开文件夹  ↗", "open_folder_button")
        self.open_folder_button.clicked.connect(self._open_folder)
        self.open_folder_button.hide()
        result_row.addWidget(self.open_folder_button)
        activity_layout.addLayout(result_row)
        content.addWidget(activity, 1)

        footer = QHBoxLayout()
        footer.addWidget(self._label("一条链接，一份珍藏。", "footer_note"))
        footer.addStretch()
        footer.addWidget(self._label("公开贴文  /  按贴文归档", "footer_note"))
        content.addLayout(footer)
        root_layout.addWidget(main, 1)

    def _apply_style(self):
        self.setStyleSheet("""
            QWidget { color: #333630; font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 13px; }
            QWidget#root, QWidget#main_panel { background: #f7f5f0; }
            QFrame#sidebar { background: #eeece5; border-right: 1px solid #e4e1d9; }
            QLabel { background: transparent; }
            QLabel#brand { font-size: 27px; font-weight: 700; letter-spacing: 3px; }
            QLabel#brand_caption { color: #8e8e83; font-size: 9px; letter-spacing: 1px; }
            QLabel#current_nav { background: #fffaf4; color: #b75b36; border-radius: 7px; padding-left: 13px; font-weight: 600; }
            QLabel#side_quote { font-size: 22px; font-weight: 600; color: #777b6c; }
            QLabel#side_note { font-size: 12px; color: #929588; }
            QLabel#side_footer { font-size: 9px; color: #999b90; letter-spacing: 1px; }
            QLabel#eyebrow { font-size: 9px; color: #a29889; letter-spacing: 1.5px; }
            QLabel#page_title { font-size: 28px; font-weight: 700; letter-spacing: 1px; }
            QLabel#page_subtitle { font-size: 12px; color: #929184; }
            QLabel#local_badge { background: #ebece3; border-radius: 12px; padding: 6px 11px; color: #777e68; font-size: 10px; }
            QFrame#form_card, QFrame#activity_card { background: #ffffff; border: 1px solid #e9e6df; border-radius: 13px; }
            QLabel#section_title { font-size: 13px; font-weight: 600; }
            QLabel#link_hint, QLabel#muted, QLabel#cookie_status { color: #959589; font-size: 11px; }
            QLabel#small_note { font-size: 10px; color: #a2a296; }
            QLineEdit { background: #fbfaf7; border: 1px solid #e6e3db; border-radius: 7px; padding: 0 12px; selection-background-color: #f6c4ad; }
            QLineEdit:focus { border: 1px solid #ee7543; background: #ffffff; }
            QLineEdit:disabled { color: #a4a59e; background: #f4f3ef; }
            QLineEdit#destination_input { color: #777c72; font-size: 12px; }
            QPushButton { border: 1px solid #e4e1d9; background: #ffffff; border-radius: 7px; padding: 7px 13px; font-size: 12px; }
            QPushButton:hover { background: #f8f5ee; border-color: #d8d1c5; }
            QPushButton:pressed { background: #eee9df; }
            QPushButton:disabled { color: #adaea5; background: #f2f1ec; border-color: #eceae4; }
            QPushButton#download_button { background: #ee7543; border-color: #ee7543; color: #ffffff; font-size: 14px; font-weight: 600; }
            QPushButton#download_button:hover { background: #df6636; border-color: #df6636; }
            QPushButton#download_button:pressed { background: #ce5b2e; }
            QPushButton#download_button:disabled { background: #eac1ae; border-color: #eac1ae; color: #fff8f4; }
            QPushButton#help_button { border: none; background: transparent; color: #858a7d; text-align: left; padding-left: 13px; }
            QPushButton#help_button:hover { background: #e6e4dc; }
            QPushButton#cookie_button, QPushButton#clear_cookie_button { border: none; background: transparent; color: #b67452; padding: 4px 0; font-size: 11px; }
            QPushButton#cookie_button:hover, QPushButton#clear_cookie_button:hover { color: #df6636; }
            QPushButton#cookie_button:disabled, QPushButton#clear_cookie_button:disabled { color: #aaa89f; }
            QFrame#divider { background: #eeece6; border: none; }
            QLabel#status_label { color: #98998e; font-size: 11px; }
            QLabel#empty_title { color: #8c8f80; font-size: 12px; }
            QLabel#empty_note { color: #b1b0a5; font-size: 10px; }
            QLabel#result_label { color: #777d6c; font-size: 11px; }
            QLabel#footer_note { color: #aaa99e; font-size: 10px; }
            QPlainTextEdit#activity_log { background: transparent; color: #878b7f; font-size: 11px; border: none; }
            QProgressBar { border: none; background: #f1eee7; border-radius: 3px; }
            QProgressBar::chunk { background: #ee7543; border-radius: 3px; }
            QDialog { background: #f7f5f0; }
            QScrollBar:vertical { background: #f5f3ee; width: 7px; border: none; }
            QScrollBar::handle:vertical { background: #dcd9d0; border-radius: 3px; min-height: 20px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)

    def _paste_link(self):
        self.link_input.setText(QApplication.clipboard().text().strip())
        self.link_input.setFocus()

    def _reset_link_error(self):
        self.link_input.setStyleSheet("")
        self.link_hint.setText("支持公开单图与轮播照片 · 视频自动跳过")
        self.link_hint.setStyleSheet("")

    def _choose_destination(self):
        directory = QFileDialog.getExistingDirectory(self, "选择保存文件夹", self.destination_input.text())
        if directory:
            self.destination_input.setText(directory)
            self.settings.setValue("destination", directory)

    def _import_cookies(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "导入 Instagram Cookie（Netscape 格式）", "", "Cookie 文件 (*.txt);;所有文件 (*)"
        )
        if not filename:
            return
        try:
            cookies = load_instagram_cookies(Path(filename))
        except (AppError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法导入 Cookie", str(exc))
            return
        self._cookies = cookies
        self.cookie_status.setText("已导入 · 仅本次运行有效")
        self.clear_cookie_button.show()
        self.cookie_button.setText("重新导入")

    def _clear_cookies(self):
        self._cookies = None
        self.cookie_status.clear()
        self.clear_cookie_button.hide()
        self.cookie_button.setText("导入 Cookie")

    def _set_busy(self, busy: bool):
        self._busy = busy
        for control in (
            self.link_input,
            self.paste_button,
            self.browse_button,
            self.cookie_button,
            self.clear_cookie_button,
            self.download_button,
            self.retry_button,
        ):
            control.setEnabled(not busy)
        self.cancel_button.setVisible(busy)
        self.cancel_button.setEnabled(busy)
        self.download_button.setText("正在保存…" if busy else "↓  开始保存")

    def _start_download(self):
        if self._busy:
            return
        url = self.link_input.text().strip()
        try:
            parse_post_url(url)
        except AppError as exc:
            self.link_input.setStyleSheet("border: 1px solid #cf684d;")
            self.link_hint.setText(str(exc))
            self.link_hint.setStyleSheet("color: #bd6148;")
            self.link_input.setFocus()
            return
        self._reset_link_error()
        destination = Path(self.destination_input.text())
        self.settings.setValue("destination", str(destination))
        self._last_message = ""
        self._last_directory = None
        self.empty_state.hide()
        self.activity_log.clear()
        self.activity_log.show()
        self.result_label.setText("")
        self.retry_button.hide()
        self.open_folder_button.hide()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()
        self.status_label.setText("正在读取贴文…")
        self.status_label.setStyleSheet("color: #c9784e;")
        self._set_busy(True)
        self._worker = DownloadWorker(url, destination, self._cookies, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.succeeded.connect(self._on_success)
        self._worker.failed.connect(self._on_failure)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.finished.connect(self._worker_finished)
        self._worker.start()

    def _append_message(self, message: str):
        if message and message != self._last_message:
            self.activity_log.appendPlainText(message)
            self._last_message = message

    def _on_progress(self, done: int, total: int, message: str):
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)
            if not (self._worker and self._worker.cancel_event.is_set()):
                self.status_label.setText(f"正在保存  {done} / {total}")
        self._append_message(message)

    def _on_success(self, result: DownloadResult):
        self._last_directory = result.directory
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self.status_label.setText("保存完成")
        self.status_label.setStyleSheet("color: #798965;")
        summary = f"已保存 {len(result.saved)} 张 · 已存在 {len(result.skipped)} 张"
        self.result_label.setText(summary)
        self._append_message(summary)
        self.open_folder_button.show()

    def _on_failure(self, message: str):
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.status_label.setText("保存失败")
        self.status_label.setStyleSheet("color: #bd6148;")
        self._append_message(message)
        self.result_label.setText("可调整链接或 Cookie 后重试。")
        self.retry_button.show()

    def _on_cancelled(self):
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.status_label.setText("已取消")
        self.status_label.setStyleSheet("color: #98998e;")
        self._append_message("任务已取消，已保存的完整图片会保留。")
        self.result_label.setText("再次保存时会跳过已存在的图片。")
        self.retry_button.show()

    def _cancel_download(self):
        if self._worker:
            self._worker.cancel_event.set()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("正在取消…")
            self._append_message("正在取消，请等待当前网络请求结束…")

    def _worker_finished(self):
        worker = self._worker
        self._worker = None
        self._set_busy(False)
        if worker:
            worker.deleteLater()
        if self._close_after_cancel:
            self.close()

    def _open_folder(self):
        directory = self._last_directory or Path(self.destination_input.text())
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
            QMessageBox.warning(self, "无法打开文件夹", f"请在文件资源管理器中打开：\n{directory}")

    def _show_help(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("使用拾光")
        dialog.setMinimumWidth(490)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(15)
        title = self._label("把照片，留给未来的自己。")
        title.setStyleSheet("font-size: 19px; font-weight: 600;")
        layout.addWidget(title)
        for heading, body in (
            ("保存照片", "复制公开 Instagram 贴文的完整链接，选择文件夹，再点击“开始保存”。支持 /p/、/reel/、/reels/ 和 /tv/ 链接；只保存其中的照片，视频会跳过。重复下载会跳过已存在的图片。"),
            ("登录限制与 Cookie", "Instagram 可能要求登录或限制请求。可从已登录的浏览器导出 Netscape 格式 Cookie 文本文件，再点击“导入 Cookie”。只导入 instagram.com 的 Cookie；Cookie 仅保留在本次应用的内存中，退出即清除。应用不会记录 Cookie 文件位置，也不要求输入账号密码。Cookie 文件本身包含登录凭据，请妥善保管。"),
            ("支持范围", "仅用于保存你有权访问和使用的公开贴文照片。不支持私密账号、快拍或绕过访问限制。网站变更、链接失效、登录验证或限流可能导致保存失败；可稍后重试或更新 Cookie。"),
            ("文件与取消", "照片按贴文分文件夹保存，保持服务器提供的图片质量。取消会保留已经下载完成的照片；关闭时会先等待当前网络请求结束。"),
        ):
            label = self._label(heading)
            label.setStyleSheet("font-weight: 600;")
            layout.addWidget(label)
            detail = self._label(body, wrap=True)
            detail.setStyleSheet("font-size: 12px; color: #7d8274;")
            layout.addWidget(detail)
        close_button = self._button("知道了", "help_close_button")
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button, 0, Qt.AlignmentFlag.AlignRight)
        dialog.exec()

    def closeEvent(self, event):  # noqa: N802 - Qt API
        if self._busy:
            event.ignore()
            if self._close_after_cancel:
                return
            answer = QMessageBox.question(
                self,
                "正在保存照片",
                "是否取消当前任务并退出？\n已保存的完整图片会保留。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                if self._busy:
                    self._close_after_cancel = True
                    self._cancel_download()
                else:
                    self._cookies = None
                    event.accept()
            return
        self._cookies = None
        event.accept()
