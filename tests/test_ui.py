from pathlib import Path
import time
from threading import Event

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from ins_saver import ui
from ins_saver.core import AppError, Cancelled, DownloadResult


@pytest.fixture(scope="module")
def qapp(tmp_path_factory):
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                      str(tmp_path_factory.mktemp("settings")))
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    return app


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "QSettings", lambda org, name: QSettings(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, org, name
    ))
    settings = ui.QSettings("Shiguang", "InsPhotoSaver")
    assert settings.format() == QSettings.Format.IniFormat
    settings.clear()
    settings.setValue("destination", str(tmp_path / "照片"))
    widget = ui.MainWindow()
    widget.show()
    qapp.processEvents()
    yield widget
    if widget._worker:
        widget._worker.cancel_event.set()
        widget._worker.wait(3000)
        qapp.processEvents()
    widget.close()
    qapp.processEvents()


def click(button):
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert predicate(), "UI did not reach the expected state"


def test_clipboard_and_invalid_link_stay_out_of_worker(window, monkeypatch):
    def forbidden_service():
        pytest.fail("An invalid URL must not start a download")

    monkeypatch.setattr(ui, "DownloadService", forbidden_service)
    QApplication.clipboard().setText("  https://example.com/p/ABC/  ")
    click(window.paste_button)
    assert window.link_input.text() == "https://example.com/p/ABC/"
    click(window.download_button)
    assert not window._busy
    assert "Instagram" in window.link_hint.text()
    assert not window.cancel_button.isVisible()


def test_download_worker_success_and_open_folder(window, monkeypatch):
    output = Path(window.destination_input.text()) / "author_ABC"
    calls = []

    class Service:
        def run(self, url, destination, **options):
            calls.append((url, destination, options["cookies"]))
            options["on_progress"](0, 2, "找到 2 张照片")
            options["on_progress"](2, 2, "已保存")
            return DownloadResult(output, [output / "ABC_01.jpg"], [output / "ABC_03.jpg"])

    monkeypatch.setattr(ui, "DownloadService", Service)
    opened = []
    monkeypatch.setattr(ui.QDesktopServices, "openUrl", lambda url: opened.append(url) or True)
    window.link_input.setText("https://www.instagram.com/p/ABC/")
    click(window.download_button)
    wait_for(lambda: not window._busy)
    assert window.status_label.text() == "保存完成"
    assert "已保存 1 张" in window.result_label.text()
    assert "已存在 1 张" in window.result_label.text()
    assert window.progress_bar.value() == 1
    assert window.download_button.isEnabled()
    assert window.open_folder_button.isVisible()
    click(window.open_folder_button)
    assert Path(opened[0].toLocalFile()) == output
    assert len(calls) == 1


def test_failure_reenables_retry(window, monkeypatch):
    class Service:
        def run(self, *args, **kwargs):
            raise AppError("Instagram 要求登录，请导入 Cookie。")

    monkeypatch.setattr(ui, "DownloadService", Service)
    window.link_input.setText("https://www.instagram.com/p/ABC/")
    click(window.download_button)
    wait_for(lambda: not window._busy)
    assert window.status_label.text() == "保存失败"
    assert "请导入 Cookie" in window.activity_log.toPlainText()
    assert window.retry_button.isVisible() and window.retry_button.isEnabled()
    assert window.cookie_button.isEnabled()


def test_cancel_and_close_wait_for_worker(window, monkeypatch):
    class Service:
        def run(self, *args, **kwargs):
            kwargs["cancel"].wait(2)
            raise Cancelled("已取消")

    monkeypatch.setattr(ui, "DownloadService", Service)
    window.link_input.setText("https://www.instagram.com/p/ABC/")
    click(window.download_button)
    assert window._busy and not window.download_button.isEnabled()
    click(window.cancel_button)
    wait_for(lambda: not window._busy)
    assert window.status_label.text() == "已取消"
    assert window.retry_button.isEnabled()

    click(window.retry_button)
    assert window._busy
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window.close()
    wait_for(lambda: not window.isVisible())
    assert not window._busy and window._worker is None


def test_cookie_import_and_clear_do_not_persist_session(window, tmp_path, monkeypatch):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tfake-test-session\n", encoding="utf-8")
    monkeypatch.setattr(ui.QFileDialog, "getOpenFileName", lambda *args: (str(cookies), ""))
    click(window.cookie_button)
    assert window._cookies == {"sessionid": "fake-test-session"}
    assert window.clear_cookie_button.isVisible()
    assert set(window.settings.allKeys()) == {"destination"}
    click(window.clear_cookie_button)
    assert window._cookies is None
    assert not window.clear_cookie_button.isVisible()


def test_exit_when_download_finishes_during_close_question(window, monkeypatch):
    release = Event()

    class Service:
        def run(self, url, destination, **kwargs):
            release.wait(2)
            return DownloadResult(destination, [], [])

    def answer_after_completion(*args):
        release.set()
        wait_for(lambda: not window._busy)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(ui, "DownloadService", Service)
    monkeypatch.setattr(ui.QMessageBox, "question", answer_after_completion)
    window.link_input.setText("https://www.instagram.com/p/ABC/")
    click(window.download_button)
    assert window._busy
    window.close()
    assert not window.isVisible()
