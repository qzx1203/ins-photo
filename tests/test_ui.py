from datetime import datetime
from io import BytesIO
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace

import pytest
from PIL import Image
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from ins_saver import ui
from ins_saver.core import AppError, Cancelled, DownloadResult, Photo, PostInfo, Video


@pytest.fixture(scope="module")
def qapp(tmp_path_factory):
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                      str(tmp_path_factory.mktemp("settings")))
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    for filename in ("msyh.ttc", "msyhbd.ttc"):
        path = Path("C:/Windows/Fonts") / filename
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
    return app


def image_bytes():
    output = BytesIO()
    Image.new("RGB", (80, 60), "#d48250").save(output, "PNG")
    return output.getvalue()


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert predicate(), "UI did not reach the expected state"


def click(button):
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)


def wait_idle(window):
    wait_for(lambda: window._worker is None and window._preview_worker is None)


def wait_gate(gate, cancel):
    if gate is None:
        return
    deadline = time.monotonic() + 3
    while not gate.is_set() and time.monotonic() < deadline:
        if cancel.wait(0.01):
            raise Cancelled("已取消")
    if cancel.is_set():
        raise Cancelled("已取消")
    assert gate.is_set(), "Test gate was not released"


@pytest.fixture
def app_window(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "QSettings", lambda org, name: QSettings(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, org, name
    ))
    settings = ui.QSettings("Shiguang", "InsPhotoSaver")
    settings.clear()
    settings.setValue("destination", str(tmp_path / "照片"))
    preview_payload = image_bytes()
    state = SimpleNamespace(
        resolved=[], saved=[], previews=[], failed_resolve=set(), failed_save=set(),
        resolve_gate=None, save_gate=None, preview_gate=None, fail_preview=False,
    )

    class Client:
        def __init__(self, cookies=None):
            self.cookies = cookies

        def resolve(self, shortcode, cancel):
            state.resolved.append(shortcode)
            wait_gate(state.resolve_gate, cancel)
            if shortcode in state.failed_resolve:
                raise AppError("Instagram 要求登录，请导入 Cookie。")
            return PostInfo(shortcode, "author", datetime(2026, 1, 1),
                            [Photo(1, f"https://cdn.test/{shortcode}/photo")], 1,
                            [Video(2, f"https://cdn.test/{shortcode}/video",
                                   f"https://cdn.test/{shortcode}/cover")])

        def close(self):
            pass

    class Service:
        def save_post(self, post, destination, *, include_videos=False, cancel=None, on_progress=None):
            state.saved.append((post.shortcode, include_videos))
            wait_gate(state.save_gate, cancel)
            if post.shortcode in state.failed_save:
                raise AppError("测试网络中断，请重试。")
            directory = destination / f"{post.author}_{post.shortcode}"
            directory.mkdir(parents=True, exist_ok=True)
            paths = [directory / f"{post.shortcode}_01.png"]
            if include_videos:
                paths.append(directory / f"{post.shortcode}_02.mp4")
            saved, skipped = [], []
            for index, path in enumerate(paths, 1):
                if path.exists():
                    skipped.append(path)
                else:
                    payload = preview_payload if path.suffix == ".png" else (Path(__file__).parent / "fixtures/tiny.mp4").read_bytes()
                    path.write_bytes(payload)
                    saved.append(path)
                if on_progress:
                    on_progress(index, len(paths), f"已处理 {path.name}")
            return DownloadResult(directory, saved, skipped)

    def preview(url, cancel=None):
        state.previews.append(url)
        wait_gate(state.preview_gate, cancel)
        if state.fail_preview:
            raise AppError("预览加载失败")
        return preview_payload

    monkeypatch.setattr(ui, "InstagramClient", Client)
    monkeypatch.setattr(ui, "DownloadService", Service)
    monkeypatch.setattr(ui, "fetch_preview", preview)
    window = ui.MainWindow()
    window.show()
    qapp.processEvents()
    yield window, state
    for gate in (state.resolve_gate, state.save_gate, state.preview_gate):
        if gate:
            gate.set()
    for worker in (window._worker, window._preview_worker):
        if worker:
            worker.cancel_event.set()
            worker.wait(3500)
    qapp.processEvents()
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window.close()
    qapp.processEvents()


def parse(window, text="https://www.instagram.com/p/ABC/"):
    window.batch_input.setPlainText(text)
    click(window.parse_button)
    wait_idle(window)


def test_clipboard_multiline_and_video_default_off(app_window):
    window, _ = app_window
    QApplication.clipboard().setText("https://www.instagram.com/p/ABC/\nhttps://instagram.com/p/DEF/")
    click(window.paste_button)
    assert "ABC" in window.batch_input.toPlainText()
    assert "DEF" in window.batch_input.toPlainText()
    assert not window.video_checkbox.isChecked()


def test_batch_parse_deduplicates_and_keeps_invalid_line_visible(app_window):
    window, state = app_window
    parse(window, "\nhttps://instagram.com/p/ABC/?igsh=1\nhttps://www.instagram.com/reel/ABC/\nhttps://example.com/no\nhttps://instagram.com/p/DEF/\n")
    assert state.resolved == ["ABC", "DEF"]
    assert window.queue_list.count() == 3
    assert any(entry.status == "链接无效" for entry in window._entries)
    assert sum(entry.post is not None for entry in window._entries) == 2


def test_parse_fetches_previews_and_download_reuses_metadata(app_window):
    window, state = app_window
    parse(window)
    assert state.resolved == ["ABC"]
    assert state.previews
    assert all("/video" not in url for url in state.previews)
    assert not state.saved
    click(window.download_button)
    wait_idle(window)
    assert state.saved == [("ABC", False)]
    assert state.resolved == ["ABC"]
    assert window._entries[0].status == "已完成"
    assert window.open_folder_button.isVisible()


def test_video_checkbox_is_forwarded_and_can_add_video_after_photos(app_window):
    window, state = app_window
    parse(window)
    click(window.download_button)
    wait_idle(window)
    click(window.video_checkbox)
    click(window.download_button)
    wait_idle(window)
    assert state.saved == [("ABC", False), ("ABC", True)]
    directory = Path(window.destination_input.text()) / "author_ABC"
    assert sorted(path.suffix for path in directory.iterdir()) == [".mp4", ".png"]


def test_batch_failure_continues_and_retry_targets_only_failed(app_window):
    window, state = app_window
    state.failed_save.add("ABC")
    parse(window, "https://instagram.com/p/ABC/\nhttps://instagram.com/p/DEF/")
    click(window.download_button)
    wait_idle(window)
    assert state.saved == [("ABC", False), ("DEF", False)]
    assert [entry.status for entry in window._entries] == ["失败", "已完成"]
    assert window.retry_button.isEnabled()
    state.failed_save.clear()
    click(window.retry_button)
    wait_idle(window)
    assert [call[0] for call in state.saved] == ["ABC", "DEF", "ABC"]
    assert state.resolved == ["ABC", "DEF", "ABC"]
    assert all(entry.status == "已完成" for entry in window._entries)


def test_resolve_failure_continues_then_retry_refreshes_metadata(app_window):
    window, state = app_window
    state.failed_resolve.add("ABC")
    parse(window, "https://instagram.com/p/ABC/\nhttps://instagram.com/p/DEF/")
    assert state.resolved == ["ABC", "DEF"]
    assert window._entries[0].status == "失败"
    assert window._entries[1].post is not None
    state.failed_resolve.clear()
    click(window.retry_button)
    wait_idle(window)
    assert state.resolved.count("ABC") == 2
    assert window._entries[0].status == "已完成"


def test_cancel_releases_controls_and_preserves_retry(app_window):
    window, state = app_window
    parse(window)
    state.save_gate = Event()
    click(window.download_button)
    assert not window.video_checkbox.isEnabled()
    click(window.cancel_button)
    wait_idle(window)
    assert window._entries[0].status == "已取消"
    assert window.download_button.isEnabled()
    assert window.video_checkbox.isEnabled()
    assert window.retry_button.isEnabled()


def test_cookie_import_and_clear_never_persist_credentials(app_window, tmp_path, monkeypatch):
    window, _ = app_window
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tfake-test-session\n", encoding="utf-8")
    monkeypatch.setattr(ui.QFileDialog, "getOpenFileName", lambda *args: (str(cookies), ""))
    click(window.cookie_button)
    assert window._cookies == {"sessionid": "fake-test-session"}
    assert set(window.settings.allKeys()) == {"destination"}
    click(window.clear_cookie_button)
    assert window._cookies is None


def test_open_folder_uses_completed_result(app_window, monkeypatch):
    window, _ = app_window
    opened = []
    monkeypatch.setattr(ui.QDesktopServices, "openUrl", lambda url: opened.append(url) or True)
    parse(window)
    click(window.download_button)
    wait_idle(window)
    click(window.open_folder_button)
    assert opened
    assert Path(opened[0].toLocalFile()).exists()


def test_close_waits_for_preview_and_ignores_late_results(app_window, monkeypatch):
    window, state = app_window
    state.preview_gate = Event()
    window.batch_input.setPlainText("https://instagram.com/p/ABC/")
    click(window.parse_button)
    wait_for(lambda: window._preview_worker is not None)
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window.close()
    wait_for(lambda: not window.isVisible())
    assert window._worker is None and window._preview_worker is None


def test_exit_if_download_finishes_during_close_confirmation(app_window, monkeypatch):
    window, state = app_window
    parse(window)
    state.save_gate = Event()

    def answer_after_completion(*args):
        state.save_gate.set()
        wait_idle(window)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(ui.QMessageBox, "question", answer_after_completion)
    click(window.download_button)
    window.close()
    assert not window.isVisible()


def test_preview_switch_rejects_old_post_results(app_window):
    window, state = app_window
    parse(window, "https://instagram.com/p/ABC/\nhttps://instagram.com/p/DEF/")
    old_token = window._preview_token
    state.preview_gate = Event()
    window.queue_list.setCurrentRow(1)
    assert window._preview_token != old_token
    assert window._preview_images == {}
    window._on_preview_ready(old_token, 1, image_bytes())
    assert window._preview_images == {}
    state.preview_gate.set()
    wait_idle(window)
    assert "DEF" in window.preview_title.text()
    assert window._preview_images
    assert any("/DEF/" in url for url in state.previews)


def test_thumbnail_click_opens_large_preview(app_window, monkeypatch):
    window, _ = app_window
    parse(window)
    dialogs = []
    monkeypatch.setattr(ui.QDialog, "exec", lambda dialog: dialogs.append(dialog.windowTitle()) or 0)
    click(window._preview_buttons[1])
    assert len(dialogs) == 1 and "预览" in dialogs[0]


def test_direct_start_parses_batch_and_does_not_retry_failed_parse_immediately(app_window):
    window, state = app_window
    state.failed_resolve.add("ABC")
    window.batch_input.setPlainText("https://instagram.com/p/ABC/\nhttps://instagram.com/p/DEF/")
    click(window.download_button)
    wait_idle(window)
    assert state.resolved == ["ABC", "DEF"]
    assert state.saved == [("DEF", False)]
    assert [entry.status for entry in window._entries] == ["失败", "已完成"]


def test_changed_input_replaces_old_queue_before_download(app_window):
    window, state = app_window
    parse(window)
    window.batch_input.setPlainText("https://instagram.com/p/DEF/")
    click(window.download_button)
    wait_idle(window)
    assert state.resolved == ["ABC", "DEF"]
    assert state.saved == [("DEF", False)]
    assert [entry.shortcode for entry in window._entries] == ["DEF"]


def test_preview_failure_does_not_block_saving(app_window):
    window, state = app_window
    state.fail_preview = True
    parse(window)
    assert window._entries[0].status == "待保存"
    assert not window._preview_images
    assert "预览加载失败" in window._preview_buttons[1].text()
    click(window.download_button)
    wait_idle(window)
    assert window._entries[0].status == "已完成"


def test_failed_save_in_new_destination_does_not_keep_previous_result(app_window):
    window, state = app_window
    parse(window)
    click(window.download_button)
    wait_idle(window)
    assert window._entries[0].result is not None
    window.destination_input.setText(str(Path(window.destination_input.text()) / "新目录"))
    state.failed_save.add("ABC")
    click(window.download_button)
    wait_idle(window)
    assert window._entries[0].status == "失败"
    assert window._entries[0].result is None


def test_direct_start_all_failed_keeps_failure_summary(app_window):
    window, state = app_window
    state.failed_resolve.update(("ABC", "DEF"))
    window.batch_input.setPlainText("https://instagram.com/p/ABC/\nhttps://instagram.com/p/DEF/")
    click(window.download_button)
    wait_idle(window)
    assert state.resolved == ["ABC", "DEF"] and not state.saved
    assert all(entry.status == "失败" for entry in window._entries)
    assert "失败" in window.status_label.text()


def test_all_invalid_links_make_no_network_request(app_window):
    window, state = app_window
    parse(window, "not-an-instagram-link\nhttps://example.com/p/ABC/")
    assert not state.resolved and not state.saved and not state.previews
    assert all(entry.status == "链接无效" for entry in window._entries)
    assert "没有可用" in window.status_label.text()
