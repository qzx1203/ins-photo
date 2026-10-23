"""Native batch downloader with in-memory media previews."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event

from PySide6.QtCore import QSettings, QSize, QStandardPaths, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSplitter, QVBoxLayout, QWidget,
)

from .core import (
    AppError, Cancelled, DownloadResult, DownloadService, InstagramClient,
    PostInfo, fetch_preview, load_instagram_cookies, parse_post_url,
)


@dataclass
class BatchEntry:
    url: str
    shortcode: str = ""
    status: str = "待解析"
    error: str = ""
    post: PostInfo | None = None
    result: DownloadResult | None = None
    done: int = 0
    total: int = 0
    saved_destination: Path | None = None
    saved_include_videos: bool = False


def parse_batch(text: str) -> tuple[list[BatchEntry], int]:
    """Keep invalid lines visible and deduplicate valid post shortcodes."""
    entries, seen, duplicates = [], set(), 0
    for line in text.splitlines():
        url = line.strip()
        if not url:
            continue
        try:
            shortcode = parse_post_url(url)
        except AppError as exc:
            entries.append(BatchEntry(url, status="链接无效", error=str(exc)))
            continue
        if shortcode in seen:
            duplicates += 1
            continue
        seen.add(shortcode)
        entries.append(BatchEntry(url, shortcode))
    return entries, duplicates


class ResolveWorker(QThread):
    item_started = Signal(int)
    item_resolved = Signal(int, object)
    item_failed = Signal(int, str)
    progress = Signal(int, int, str)
    cancelled = Signal()

    def __init__(self, jobs, cookies=None, parent=None):
        super().__init__(parent)
        self.jobs = list(jobs)
        self.cookies = dict(cookies) if cookies else None
        self.cancel_event = Event()

    def run(self):
        client = None
        try:
            client = InstagramClient(self.cookies)
            for position, (row, shortcode) in enumerate(self.jobs):
                if self.cancel_event.is_set():
                    raise Cancelled()
                self.item_started.emit(row)
                try:
                    self.item_resolved.emit(row, client.resolve(shortcode, self.cancel_event))
                except Cancelled:
                    raise
                except AppError as exc:
                    self.item_failed.emit(row, str(exc))
                except Exception:
                    self.item_failed.emit(row, "读取贴文失败，请稍后重试。")
                self.progress.emit(position + 1, len(self.jobs), "读取贴文")
        except Cancelled:
            self.cancelled.emit()
        except Exception:
            for row, _ in self.jobs:
                self.item_failed.emit(row, "无法连接 Instagram，请检查网络或 Cookie 后重试。")
        finally:
            if client:
                client.close()
            self.cookies = None


class DownloadWorker(QThread):
    item_started = Signal(int)
    item_resolved = Signal(int, object)
    item_progress = Signal(int, int, int, str)
    item_succeeded = Signal(int, object)
    item_failed = Signal(int, str)
    progress = Signal(int, int, str)
    cancelled = Signal()

    def __init__(self, jobs, destination: Path, cookies=None, include_videos=False, parent=None):
        super().__init__(parent)
        self.jobs = list(jobs)
        self.destination = Path(destination)
        self.cookies = dict(cookies) if cookies else None
        self.include_videos = include_videos
        self.cancel_event = Event()

    def run(self):
        client = None
        try:
            for position, (row, shortcode, post) in enumerate(self.jobs):
                if self.cancel_event.is_set():
                    raise Cancelled()
                self.item_started.emit(row)
                try:
                    if post is None:
                        if client is None:
                            client = InstagramClient(self.cookies)
                        post = client.resolve(shortcode, self.cancel_event)
                        self.item_resolved.emit(row, post)
                    result = DownloadService().save_post(
                        post, self.destination, include_videos=self.include_videos,
                        cancel=self.cancel_event,
                        on_progress=lambda done, total, message, row=row:
                            self.item_progress.emit(row, done, total, message),
                    )
                    self.item_succeeded.emit(row, result)
                except Cancelled:
                    raise
                except AppError as exc:
                    self.item_failed.emit(row, str(exc))
                except Exception:
                    self.item_failed.emit(row, "保存时发生意外错误，请稍后重试。")
                self.progress.emit(position + 1, len(self.jobs), "保存贴文")
        except Cancelled:
            self.cancelled.emit()
        finally:
            if client:
                client.close()
            self.cookies = None


class PreviewWorker(QThread):
    media_ready = Signal(int, int, bytes)
    media_failed = Signal(int, int, str)

    def __init__(self, token: int, post: PostInfo, parent=None):
        super().__init__(parent)
        self.token = token
        self.post = post
        self.cancel_event = Event()

    def run(self):
        media = [(photo.index, photo.url) for photo in self.post.photos]
        media += [(video.index, video.thumbnail_url) for video in self.post.videos]
        for index, url in sorted(media):
            if self.cancel_event.is_set():
                return
            try:
                if not url:
                    raise AppError("暂无预览图")
                data = fetch_preview(url, cancel=self.cancel_event)
                if not self.cancel_event.is_set():
                    self.media_ready.emit(self.token, index, data)
            except Cancelled:
                return
            except Exception:
                if not self.cancel_event.is_set():
                    self.media_failed.emit(self.token, index, "预览加载失败")


class PhotoMark(QWidget):
    def __init__(self, *, large=False, parent=None):
        super().__init__(parent)
        self.large = large
        self.setFixedSize(80, 62) if large else self.setFixedSize(40, 40)

    def paintEvent(self, event):  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.large:
            painter.translate(22, 10)
            painter.setPen(QPen(QColor("#ddd7cc"), 1.5))
            painter.setBrush(QColor("#faf8f3"))
            painter.rotate(-10)
            painter.drawRoundedRect(-7, 0, 42, 45, 5, 5)
            painter.rotate(10)
            painter.setBrush(QColor("#ffffff"))
            painter.drawRoundedRect(0, 0, 42, 45, 5, 5)
            painter.setPen(QPen(QColor("#dda186"), 1.7))
            painter.drawEllipse(26, 8, 6, 6)
            painter.drawLine(7, 31, 17, 20)
            painter.drawLine(17, 20, 28, 32)
            painter.drawLine(28, 32, 35, 24)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#ee7543"))
            painter.drawRoundedRect(0, 0, 40, 40, 12, 12)
            painter.setPen(QPen(QColor("#ffffff"), 1.8))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(10, 11, 20, 19, 4, 4)
            painter.drawEllipse(15, 16, 10, 10)
            painter.drawPoint(26, 15)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("拾光 V2 · Instagram 图片与视频保存")
        self.resize(1200, 850)
        self.setMinimumSize(1000, 740)
        self.settings = QSettings("Shiguang", "InsPhotoSaver")
        self._worker = None
        self._preview_worker = None
        self._pending_preview = None
        self._preview_token = 0
        self._preview_images = {}
        self._preview_buttons = {}
        self._entries = []
        self._active_rows = []
        self._parsed_text = ""
        self._phase = ""
        self._auto_download = False
        self._cancelled = False
        self._cookies = None
        self._busy = False
        self._close_after_cancel = False
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
    def _label(text, name="", *, wrap=False):
        label = QLabel(text)
        label.setObjectName(name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(wrap)
        return label

    @staticmethod
    def _button(text, name):
        button = QPushButton(text)
        button.setObjectName(name)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        content = QVBoxLayout(root)
        content.setContentsMargins(28, 24, 28, 20)
        content.setSpacing(16)
        header = QHBoxLayout()
        header.setSpacing(13)
        header.addWidget(PhotoMark())
        brand = QVBoxLayout()
        brand.setSpacing(1)
        brand.addWidget(self._label("拾光", "brand"))
        brand.addWidget(self._label("把喜欢，留在本地。", "caption"))
        header.addLayout(brand)
        header.addWidget(self._label("V2", "version_badge"), 0, Qt.AlignmentFlag.AlignTop)
        header.addStretch()
        header.addWidget(self._label("●  仅保存在本机", "local_badge"))
        self.help_button = self._button("使用帮助  ↗", "help_button")
        self.help_button.clicked.connect(self._show_help)
        header.addWidget(self.help_button)
        content.addLayout(header)

        form = QFrame()
        form.setObjectName("form_card")
        form_layout = QVBoxLayout(form)
        form_layout.setContentsMargins(20, 16, 20, 14)
        form_layout.setSpacing(9)
        title_row = QHBoxLayout()
        title_row.addWidget(self._label("01   添加贴文链接", "section_title"))
        title_row.addWidget(self._label("每行一个 · 单图 / 轮播 / Reel", "muted"), 1)
        self.paste_button = self._button("粘贴链接", "paste_button")
        self.paste_button.clicked.connect(self._paste_link)
        title_row.addWidget(self.paste_button)
        self.parse_button = self._button("解析链接", "parse_button")
        self.parse_button.clicked.connect(self._parse_links)
        title_row.addWidget(self.parse_button)
        form_layout.addLayout(title_row)
        self.batch_input = QPlainTextEdit()
        self.batch_input.setObjectName("batch_input")
        self.batch_input.setAccessibleName("Instagram 批量贴文链接，每行一个")
        self.batch_input.setPlaceholderText("https://www.instagram.com/p/…/\nhttps://www.instagram.com/reel/…/")
        self.batch_input.setFixedHeight(76)
        self.batch_input.textChanged.connect(self._input_changed)
        form_layout.addWidget(self.batch_input)
        self.link_hint = self._label("先解析查看预览，也可直接开始保存。重复贴文会自动合并。", "link_hint")
        form_layout.addWidget(self.link_hint)
        settings_row = QHBoxLayout()
        settings_row.setSpacing(9)
        settings_row.addWidget(self._label("保存到", "muted"))
        self.destination_input = QLineEdit()
        self.destination_input.setObjectName("destination_input")
        self.destination_input.setAccessibleName("保存文件夹")
        self.destination_input.setReadOnly(True)
        self.destination_input.setMinimumHeight(36)
        settings_row.addWidget(self.destination_input, 1)
        self.browse_button = self._button("选择文件夹", "browse_button")
        self.browse_button.clicked.connect(self._choose_destination)
        settings_row.addWidget(self.browse_button)
        self.video_checkbox = QCheckBox("同时下载视频")
        self.video_checkbox.setObjectName("video_checkbox")
        self.video_checkbox.setToolTip("默认仅保存照片；勾选后也保存贴文中的视频。预览不会下载视频。")
        self.video_checkbox.toggled.connect(self._update_selection_note)
        settings_row.addWidget(self.video_checkbox)
        form_layout.addLayout(settings_row)
        cookies = QHBoxLayout()
        cookies.setSpacing(9)
        cookies.addWidget(self._label("遇到登录限制？", "muted"))
        self.cookie_button = self._button("导入 Cookie", "cookie_button")
        self.cookie_button.clicked.connect(self._import_cookies)
        cookies.addWidget(self.cookie_button)
        self.cookie_status = self._label("", "cookie_status")
        cookies.addWidget(self.cookie_status)
        self.clear_cookie_button = self._button("清除", "clear_cookie_button")
        self.clear_cookie_button.clicked.connect(self._clear_cookies)
        self.clear_cookie_button.hide()
        cookies.addWidget(self.clear_cookie_button)
        cookies.addStretch()
        cookies.addWidget(self._label("请仅保存有权使用的内容。", "small_note"))
        form_layout.addLayout(cookies)
        content.addWidget(form)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(14)
        queue = QFrame()
        queue.setObjectName("queue_card")
        queue_layout = QVBoxLayout(queue)
        queue_layout.setContentsMargins(16, 16, 16, 12)
        queue_layout.setSpacing(10)
        queue_header = QHBoxLayout()
        queue_header.addWidget(self._label("02   贴文队列", "section_title"), 1)
        self.queue_count = self._label("0 条", "muted")
        queue_header.addWidget(self.queue_count)
        queue_layout.addLayout(queue_header)
        self.queue_list = QListWidget()
        self.queue_list.setObjectName("queue_list")
        self.queue_list.setAccessibleName("批量贴文队列")
        self.queue_list.setWordWrap(True)
        self.queue_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.queue_list.setSpacing(4)
        self.queue_list.currentRowChanged.connect(self._selection_changed)
        queue_layout.addWidget(self.queue_list, 1)
        self.queue_empty = self._label("还没有贴文\n\n粘贴链接后，点击「解析链接」。", "queue_empty", wrap=True)
        self.queue_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        queue_layout.addWidget(self.queue_empty, 1)
        self.queue_list.hide()
        queue_layout.addWidget(self._label("一条失败，其他贴文继续保存。", "small_note"))
        splitter.addWidget(queue)

        preview = QFrame()
        preview.setObjectName("preview_card")
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(18, 16, 18, 12)
        preview_layout.setSpacing(8)
        preview_header = QHBoxLayout()
        preview_header.addWidget(self._label("03   内容预览", "section_title"), 1)
        preview_header.addWidget(self._label("点击图片放大", "muted"))
        preview_layout.addLayout(preview_header)
        self.preview_title = self._label("选择一条贴文，查看它的内容。", "preview_title", wrap=True)
        preview_layout.addWidget(self.preview_title)
        self.preview_note = self._label("预览仅加载图片与视频封面。", "muted", wrap=True)
        preview_layout.addWidget(self.preview_note)
        self.preview_area = QScrollArea()
        self.preview_area.setObjectName("preview_area")
        self.preview_area.setWidgetResizable(True)
        self.preview_area.setFrameShape(QFrame.Shape.NoFrame)
        self.preview_content = QWidget()
        self.preview_grid = QGridLayout(self.preview_content)
        self.preview_grid.setContentsMargins(0, 8, 0, 8)
        self.preview_grid.setSpacing(12)
        self.preview_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.preview_area.setWidget(self.preview_content)
        preview_layout.addWidget(self.preview_area, 1)
        self.preview_empty = QWidget()
        empty = QVBoxLayout(self.preview_empty)
        empty.setContentsMargins(12, 0, 12, 12)
        empty.addStretch()
        empty.addWidget(PhotoMark(large=True), 0, Qt.AlignmentFlag.AlignHCenter)
        empty.addWidget(self._label("下一份灵感，等待一个链接", "empty_title"), 0, Qt.AlignmentFlag.AlignHCenter)
        empty.addWidget(self._label("解析完成后，照片会显示在这里。", "muted"), 0, Qt.AlignmentFlag.AlignHCenter)
        empty.addStretch()
        preview_layout.addWidget(self.preview_empty, 1)
        self.preview_area.hide()
        splitter.addWidget(preview)
        splitter.setSizes([350, 740])
        content.addWidget(splitter, 1)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("progress_bar")
        self.progress_bar.setAccessibleName("批量任务进度")
        self.progress_bar.setFixedHeight(5)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        content.addWidget(self.progress_bar)
        bottom = QHBoxLayout()
        bottom.setSpacing(9)
        summary = QVBoxLayout()
        summary.setSpacing(3)
        self.status_label = self._label("准备好，留住喜欢。", "status_label")
        self.result_label = self._label("默认只保存照片，视频由你决定。", "result_label", wrap=True)
        summary.addWidget(self.status_label)
        summary.addWidget(self.result_label)
        bottom.addLayout(summary, 1)
        self.open_folder_button = self._button("打开文件夹  ↗", "open_folder_button")
        self.open_folder_button.clicked.connect(self._open_folder)
        bottom.addWidget(self.open_folder_button)
        self.retry_button = self._button("重试失败项", "retry_button")
        self.retry_button.clicked.connect(self._retry_failed)
        self.retry_button.hide()
        bottom.addWidget(self.retry_button)
        self.cancel_button = self._button("取消任务", "cancel_button")
        self.cancel_button.clicked.connect(self._cancel_download)
        self.cancel_button.hide()
        bottom.addWidget(self.cancel_button)
        self.download_button = self._button("↓  开始保存", "download_button")
        self.download_button.setMinimumSize(144, 42)
        self.download_button.clicked.connect(self._start_download)
        bottom.addWidget(self.download_button)
        content.addLayout(bottom)

    def _apply_style(self):
        self.setStyleSheet("""
            QWidget { color: #333630; font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 12px; }
            QWidget#root, QDialog { background: #f7f5f0; }
            QLabel { background: transparent; }
            QLabel#brand { font-size: 26px; font-weight: 700; letter-spacing: 3px; }
            QLabel#caption { color: #939184; font-size: 10px; }
            QLabel#version_badge { background: #ede7dc; color: #9c826a; border-radius: 5px; padding: 3px 7px; font-size: 10px; }
            QLabel#local_badge { background: #ebece3; border-radius: 12px; padding: 7px 12px; color: #777e68; font-size: 10px; }
            QFrame#form_card, QFrame#queue_card, QFrame#preview_card { background: #fff; border: 1px solid #e9e6df; border-radius: 12px; }
            QLabel#section_title { font-size: 13px; font-weight: 600; }
            QLabel#muted, QLabel#link_hint, QLabel#cookie_status { color: #959589; font-size: 11px; }
            QLabel#small_note { color: #a4a296; font-size: 10px; }
            QLabel#queue_empty, QLabel#empty_title { color: #96998b; }
            QLabel#preview_title { font-size: 13px; font-weight: 600; color: #5d6654; }
            QLabel#status_label { color: #59644e; font-weight: 600; }
            QLabel#result_label { color: #929588; font-size: 10px; }
            QLineEdit, QPlainTextEdit#batch_input { background: #fbfaf7; border: 1px solid #e6e3db; border-radius: 7px; padding: 7px 10px; selection-background-color: #f6c4ad; }
            QLineEdit:focus, QPlainTextEdit#batch_input:focus { border-color: #ee7543; background: #fff; }
            QLineEdit#destination_input { color: #777c72; font-size: 11px; }
            QPushButton { border: 1px solid #e4e1d9; background: #fff; border-radius: 7px; padding: 8px 13px; font-size: 12px; }
            QPushButton:hover { background: #f8f5ee; border-color: #d8d1c5; }
            QPushButton:pressed { background: #eee9df; }
            QPushButton:disabled { color: #adaea5; background: #f2f1ec; border-color: #eceae4; }
            QPushButton#download_button { background: #ee7543; border-color: #ee7543; color: #fff; font-size: 14px; font-weight: 600; }
            QPushButton#download_button:hover { background: #df6636; border-color: #df6636; }
            QPushButton#download_button:disabled { background: #eac1ae; border-color: #eac1ae; }
            QPushButton#parse_button { color: #b96c45; border-color: #e6c4b0; background: #fff9f3; }
            QPushButton#cookie_button, QPushButton#clear_cookie_button, QPushButton#help_button { border: none; background: transparent; color: #b67452; padding: 2px 0; font-size: 11px; }
            QPushButton#cookie_button:disabled, QPushButton#clear_cookie_button:disabled { color: #aaa89f; }
            QCheckBox { spacing: 7px; font-size: 12px; padding-left: 8px; }
            QCheckBox::indicator { width: 15px; height: 15px; }
            QCheckBox::indicator:unchecked { border: 1px solid #d5d0c6; border-radius: 4px; background: #fff; }
            QCheckBox::indicator:checked { border: 1px solid #ee7543; border-radius: 4px; background: #ee7543; image: none; }
            QListWidget#queue_list { border: none; background: transparent; outline: none; }
            QListWidget#queue_list::item { color: #717769; background: #faf9f5; border: 1px solid transparent; border-radius: 7px; padding: 10px; }
            QListWidget#queue_list::item:selected { background: #fff4e9; border-color: #efc6ad; color: #a8623d; }
            QListWidget#queue_list::item:hover:!selected { background: #f2f1ea; }
            QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }
            QPushButton#thumbnail { padding: 0; border-color: #eae5dc; background: #f7f5f0; color: #aaa495; font-size: 11px; }
            QPushButton#thumbnail:hover { border-color: #ee7543; }
            QProgressBar { border: none; background: #eae6dd; border-radius: 2px; }
            QProgressBar::chunk { background: #ee7543; border-radius: 2px; }
            QSplitter::handle { background: transparent; }
            QScrollBar:vertical { background: #f5f3ee; width: 7px; border: none; }
            QScrollBar::handle:vertical { background: #dcd9d0; border-radius: 3px; min-height: 20px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)

    def _paste_link(self):
        self.batch_input.setPlainText(QApplication.clipboard().text().strip())
        self.batch_input.setFocus()

    def _input_changed(self):
        if self._entries and self.batch_input.toPlainText().strip() != self._parsed_text:
            self.link_hint.setText("链接已修改；再次解析或开始保存时，将更新队列。")
        else:
            self.link_hint.setText("先解析查看预览，也可直接开始保存。重复贴文会自动合并。")

    def _choose_destination(self):
        directory = QFileDialog.getExistingDirectory(self, "选择保存文件夹", self.destination_input.text())
        if directory:
            self.destination_input.setText(directory)
            self.settings.setValue("destination", directory)

    def _import_cookies(self):
        filename, _ = QFileDialog.getOpenFileName(self, "导入 Instagram Cookie（Netscape 格式）", "", "Cookie 文件 (*.txt);;所有文件 (*)")
        if not filename:
            return
        try:
            self._cookies = load_instagram_cookies(Path(filename))
        except (AppError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法导入 Cookie", str(exc))
            return
        self.cookie_status.setText("已导入 · 仅本次运行有效")
        self.clear_cookie_button.show()
        self.cookie_button.setText("重新导入")

    def _clear_cookies(self):
        self._cookies = None
        self.cookie_status.clear()
        self.clear_cookie_button.hide()
        self.cookie_button.setText("导入 Cookie")

    def _set_busy(self, busy):
        self._busy = busy
        for control in (self.batch_input, self.paste_button, self.parse_button, self.browse_button,
                        self.cookie_button, self.clear_cookie_button, self.download_button,
                        self.retry_button, self.video_checkbox):
            control.setEnabled(not busy)
        self.cancel_button.setVisible(busy)
        self.cancel_button.setEnabled(busy)
        self.download_button.setText("正在保存…" if busy and self._phase == "download" else "↓  开始保存")

    def _parse_links(self, checked=False, *, then_download=False):
        if self._busy:
            return
        entries, duplicates = parse_batch(self.batch_input.toPlainText())
        if not entries:
            self.link_hint.setText("请先粘贴 Instagram 贴文链接，每行一个。")
            self.batch_input.setFocus()
            return
        self._entries = entries
        self._parsed_text = self.batch_input.toPlainText().strip()
        self._cancel_preview()
        self.queue_list.clear()
        for _ in entries:
            self.queue_list.addItem(QListWidgetItem())
        self.queue_list.show()
        self.queue_empty.hide()
        self.queue_count.setText(f"{len(entries)} 条")
        for row in range(len(entries)):
            self._render_entry(row)
        invalid = sum(not entry.shortcode for entry in entries)
        self.link_hint.setText(f"共 {len(entries)} 条贴文 · 合并 {duplicates} 条重复链接 · {invalid} 条链接无效")
        self.retry_button.hide()
        self.result_label.setText("解析完成后可查看预览；预览失败不影响保存。")
        self.queue_list.setCurrentRow(0)
        jobs = [(row, entry.shortcode) for row, entry in enumerate(entries) if entry.shortcode]
        if not jobs:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.status_label.setText("没有可用的 Instagram 贴文链接")
            return
        self._phase = "resolve"
        self._auto_download = then_download
        worker = ResolveWorker(jobs, self._cookies, self)
        worker.item_started.connect(self._on_resolve_started)
        worker.item_resolved.connect(self._on_resolved)
        worker.item_failed.connect(self._on_item_failed)
        self._launch_worker(worker, [row for row, _ in jobs])

    def _launch_worker(self, worker, rows):
        self._worker = worker
        self._active_rows = rows
        self._cancelled = False
        self._set_busy(True)
        self.progress_bar.setRange(0, len(rows))
        self.progress_bar.setValue(0)
        self.status_label.setText("正在解析贴文…" if self._phase == "resolve" else f"正在保存 {len(rows)} 条贴文…")
        worker.progress.connect(self._on_progress)
        worker.cancelled.connect(self._on_cancelled)
        worker.finished.connect(self._worker_finished)
        worker.start()

    def _start_download(self):
        if self._busy:
            return
        if not self._entries or self.batch_input.toPlainText().strip() != self._parsed_text:
            self._parse_links(then_download=True)
            return
        destination = Path(self.destination_input.text())
        rows = [row for row, entry in enumerate(self._entries)
                if entry.shortcode and (entry.status != "已完成"
                    or entry.saved_destination != destination
                    or (self.video_checkbox.isChecked() and not entry.saved_include_videos))]
        self._download_rows(rows)

    def _retry_failed(self):
        if self._busy:
            return
        rows = [row for row, entry in enumerate(self._entries) if entry.shortcode and entry.status == "失败"]
        for row in rows:
            self._entries[row].post = None
        self._download_rows(rows)

    def _download_rows(self, rows):
        if not rows:
            self.status_label.setText("当前没有待保存的贴文")
            return
        destination = Path(self.destination_input.text())
        self.settings.setValue("destination", str(destination))
        for row in rows:
            entry = self._entries[row]
            entry.status, entry.error, entry.done, entry.total = "排队中", "", 0, 0
            entry.result = None
            self._render_entry(row)
        self._phase = "download"
        self._auto_download = False
        jobs = [(row, self._entries[row].shortcode, self._entries[row].post) for row in rows]
        worker = DownloadWorker(jobs, destination, self._cookies, self.video_checkbox.isChecked(), self)
        worker.item_started.connect(self._on_download_started)
        worker.item_resolved.connect(self._on_resolved)
        worker.item_progress.connect(self._on_item_progress)
        worker.item_succeeded.connect(self._on_item_success)
        worker.item_failed.connect(self._on_item_failed)
        self._launch_worker(worker, rows)

    def _on_resolve_started(self, row):
        self._entries[row].status = "解析中"
        self._render_entry(row)
        self.status_label.setText(f"正在读取贴文 {self._active_rows.index(row) + 1} / {len(self._active_rows)}")

    def _on_download_started(self, row):
        self._entries[row].status = "保存中"
        self._render_entry(row)

    def _on_resolved(self, row, post):
        entry = self._entries[row]
        entry.post = post
        if self._phase == "resolve":
            entry.status, entry.error = "待保存", ""
        self._render_entry(row)
        if self.queue_list.currentRow() == row:
            self._selection_changed(row)

    def _on_item_failed(self, row, message):
        entry = self._entries[row]
        entry.status, entry.error = "失败", message
        self._render_entry(row)
        if self.queue_list.currentRow() == row:
            self._update_selection_note()

    def _on_item_progress(self, row, done, total, message):
        entry = self._entries[row]
        entry.done, entry.total = done, total
        self._render_entry(row)
        self.status_label.setText(f"贴文 {self._active_rows.index(row) + 1} / {len(self._active_rows)} · 媒体 {done} / {total}")
        self.result_label.setText(message)

    def _on_item_success(self, row, result):
        entry = self._entries[row]
        entry.status, entry.result, entry.error = "已完成", result, ""
        entry.saved_destination = self._worker.destination
        entry.saved_include_videos = self._worker.include_videos
        self._render_entry(row)

    def _on_progress(self, done, total, message):
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(done)

    def _on_cancelled(self):
        self._cancelled = True
        self._auto_download = False
        for row in self._active_rows:
            entry = self._entries[row]
            if entry.status in {"待解析", "解析中", "排队中", "保存中"}:
                entry.status = "已取消"
                self._render_entry(row)

    def _worker_finished(self):
        worker = self._worker
        self._worker = None
        self._set_busy(False)
        if worker:
            worker.deleteLater()
        failed = sum(entry.status in {"失败", "链接无效"} for entry in self._entries)
        completed = sum(entry.status == "已完成" for entry in self._entries)
        ready = sum(entry.post is not None for entry in self._entries)
        self.retry_button.setVisible(any(entry.status == "失败" for entry in self._entries))
        if self._cancelled:
            self.status_label.setText("已取消 · 完整文件已保留")
            self.result_label.setText(f"已完成 {completed} 条 · 点击开始保存可继续未完成的贴文。")
        elif self._phase == "resolve":
            self.status_label.setText(f"解析完成 · {ready} 条可预览 · {failed} 条失败")
            self.result_label.setText("选择左侧贴文查看预览；确认后开始保存。")
        else:
            saved = sum(len(entry.result.saved) for entry in self._entries if entry.result)
            skipped = sum(len(entry.result.skipped) for entry in self._entries if entry.result)
            self.status_label.setText("保存完成" if not failed else "本批次处理完成")
            self.result_label.setText(f"已完成 {completed} 条 · 已保存 {saved} 个文件 · 已存在 {skipped} 个 · 失败 {failed} 条")
        if self._close_after_cancel:
            self._finish_close_if_idle()
        elif self._auto_download and not self._cancelled:
            # Failed resolves remain failed for an explicit retry, avoiding a second request immediately.
            self._auto_download = False
            rows = [row for row, entry in enumerate(self._entries) if entry.post is not None]
            if rows:
                self._download_rows(rows)

    def _render_entry(self, row):
        entry = self._entries[row]
        title = f"@{entry.post.author}" if entry.post else (entry.shortcode or "无效链接")
        detail = f"{len(entry.post.photos)} 张照片 · {len(entry.post.videos)} 个视频" if entry.post else entry.url
        status = entry.status
        if entry.status == "保存中" and entry.total:
            status += f"  {entry.done}/{entry.total}"
        if entry.result:
            detail += f" · 保存 {len(entry.result.saved)} / 已有 {len(entry.result.skipped)}"
        if entry.error:
            detail = entry.error
        item = self.queue_list.item(row)
        item.setText(f"{row + 1:02d}   {title}   ·   {status}\n{detail}")
        item.setToolTip(f"{entry.url}\n{entry.error or detail}")
        item.setSizeHint(QSize(0, 76))
        item.setForeground(QColor("#b7674b" if entry.status in {"失败", "链接无效"} else "#717769"))

    def _cancel_download(self):
        self._auto_download = False
        if self._worker:
            self._worker.cancel_event.set()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("正在取消，请等待当前请求结束…")

    def _clear_preview_grid(self):
        self._preview_images.clear()
        self._preview_buttons.clear()
        while self.preview_grid.count():
            item = self.preview_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _cancel_preview(self):
        self._preview_token += 1
        self._pending_preview = None
        if self._preview_worker:
            self._preview_worker.cancel_event.set()
        self._clear_preview_grid()

    def _selection_changed(self, row):
        self._cancel_preview()
        if row < 0 or row >= len(self._entries):
            return
        entry = self._entries[row]
        self._update_selection_note()
        if entry.post is None:
            self.preview_title.setText(entry.shortcode or "链接无效")
            self.preview_empty.show()
            self.preview_area.hide()
            return
        post = entry.post
        self.preview_title.setText(f"@{post.author}  ·  {post.date:%Y.%m.%d}  ·  {post.shortcode}")
        media = [(photo.index, False) for photo in post.photos] + [(video.index, True) for video in post.videos]
        self.preview_empty.setVisible(not media)
        self.preview_area.setVisible(bool(media))
        for position, (index, is_video) in enumerate(sorted(media)):
            card = QWidget()
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(0, 0, 0, 0)
            card_layout.setSpacing(6)
            button = self._button("加载预览中…", "thumbnail")
            button.setFixedSize(156, 126)
            button.setIconSize(QSize(152, 122))
            button.clicked.connect(lambda checked=False, index=index: self._open_preview(index))
            self._preview_buttons[index] = button
            card_layout.addWidget(button)
            label = self._label(f"{'▶ 视频封面' if is_video else '照片'}  {index:02d}", "muted")
            card_layout.addWidget(label)
            self.preview_grid.addWidget(card, position // 3, position % 3)
        self._pending_preview = (self._preview_token, post)
        if self._preview_worker is None:
            self._start_pending_preview()

    def _update_selection_note(self):
        row = self.queue_list.currentRow()
        if row < 0 or row >= len(self._entries):
            return
        entry = self._entries[row]
        if entry.error:
            self.preview_note.setText(entry.error)
        elif entry.post:
            post = entry.post
            videos = "视频也会保存" if self.video_checkbox.isChecked() else "视频将跳过"
            self.preview_note.setText(f"{len(post.photos)} 张照片 · {len(post.videos)} 个视频 · {videos}")
        else:
            self.preview_note.setText("等待解析；解析失败时可调整 Cookie 后重试。")

    def _start_pending_preview(self):
        if self._pending_preview is None or self._close_after_cancel:
            return
        token, post = self._pending_preview
        self._pending_preview = None
        worker = PreviewWorker(token, post, self)
        self._preview_worker = worker
        worker.media_ready.connect(self._on_preview_ready)
        worker.media_failed.connect(self._on_preview_failed)
        worker.finished.connect(self._preview_finished)
        worker.start()

    def _on_preview_ready(self, token, index, data):
        if token != self._preview_token:
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self._on_preview_failed(token, index, "预览加载失败")
            return
        self._preview_images[index] = data
        button = self._preview_buttons.get(index)
        if button:
            button.setText("")
            button.setIcon(QIcon(pixmap.scaled(152, 122, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)))
            button.setToolTip("点击放大预览")

    def _on_preview_failed(self, token, index, message):
        if token == self._preview_token and index in self._preview_buttons:
            self._preview_buttons[index].setText(f"{message}\n仍可尝试保存")

    def _preview_finished(self):
        worker = self._preview_worker
        self._preview_worker = None
        if worker:
            worker.deleteLater()
        if self._close_after_cancel:
            self._finish_close_if_idle()
        else:
            self._start_pending_preview()

    def _open_preview(self, index):
        data = self._preview_images.get(index)
        if not data:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"拾光 · 媒体 {index:02d} 预览")
        layout = QVBoxLayout(dialog)
        image = QLabel()
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = QPixmap()
        pixmap.loadFromData(data)
        image.setPixmap(pixmap.scaled(900, 680, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(image)
        close = self._button("关闭预览", "preview_close_button")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        dialog.exec()

    def _open_folder(self):
        row = self.queue_list.currentRow()
        result = self._entries[row].result if 0 <= row < len(self._entries) else None
        directory = result.directory if result else Path(self.destination_input.text())
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
            QMessageBox.warning(self, "无法打开文件夹", f"请在文件资源管理器中打开：\n{directory}")

    def _show_help(self):
        QMessageBox.information(self, "使用拾光 V2", (
            "1. 每行粘贴一个公开 Instagram 贴文链接，点击“解析链接”。\n"
            "2. 在左侧选择贴文，右侧查看照片或视频封面，点击图片可放大。\n"
            "3. 选择保存位置；需要视频时勾选“同时下载视频”，再开始保存。\n\n"
            "重复链接会合并，已存在的完整文件会跳过。单条失败不会中断其他贴文；"
            "“重试失败项”只处理失败的有效链接。取消后再次开始可继续未完成的贴文。\n\n"
            "遇到登录限制，可导入 Netscape 格式 Cookie。Cookie 只在本次运行的内存中保存，"
            "不会记录账号、密码或 Cookie 文件位置。请妥善保管导出的 Cookie 文件。\n\n"
            "仅保存你有权访问和使用的公开内容。不支持私密账号、快拍或绕过访问限制。"
            "网站变更、验证与限流可能导致解析或保存失败。"
        ))

    def _finish_close_if_idle(self):
        if self._worker is None and self._preview_worker is None:
            self.close()

    def closeEvent(self, event):  # noqa: N802 - Qt API
        if self._worker is not None or self._preview_worker is not None:
            event.ignore()
            if self._close_after_cancel:
                return
            answer = QMessageBox.StandardButton.Yes
            if self._busy:
                answer = QMessageBox.question(
                    self, "正在处理贴文", "是否取消当前任务并退出？\n已保存的完整文件会保留。",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
            if answer == QMessageBox.StandardButton.Yes:
                self._close_after_cancel = True
                self._cancel_download()
                self._cancel_preview()
                if self._worker is None and self._preview_worker is None:
                    self._cookies = None
                    event.accept()
            return
        self._cookies = None
        self._preview_images.clear()
        event.accept()
