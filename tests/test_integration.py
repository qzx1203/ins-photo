"""Exercise the real HTTP, image validation, and filesystem download pipeline."""

from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from random import Random
from threading import Event, Thread
from types import SimpleNamespace

from PIL import Image
import pytest

from ins_saver.core import AppError, Cancelled, DownloadService, Photo, PostInfo


SHORTCODE = "Example_123"
POST_URL = f"https://www.instagram.com/p/{SHORTCODE}/?igsh=example"


def image_bytes(format_name, *, noisy=False):
    buffer = BytesIO()
    if noisy:
        pixels = Random(7).randbytes(512 * 512 * 3)
        picture = Image.frombytes("RGB", (512, 512), pixels)
    else:
        picture = Image.new("RGB", (13, 9), (30, 120, 210))
    picture.save(buffer, format=format_name)
    return buffer.getvalue()


@pytest.fixture
def image_server():
    png = image_bytes("PNG")
    jpeg = image_bytes("JPEG")
    large_png = image_bytes("PNG", noisy=True)
    hits = Counter()
    routes = {
        "/first.png": (200, "image/png", png, len(png)),
        "/third.jpeg": (200, "image/jpeg", jpeg, len(jpeg)),
        "/large.png": (200, "image/png", large_png, len(large_png)),
        "/unavailable": (503, "text/plain", b"unavailable", 11),
        "/not-image": (200, "text/html", b"<html>login</html>", 18),
        "/corrupt-image": (200, "image/png", png[:30], 30),
        "/truncated-transfer": (200, "image/jpeg", jpeg, len(jpeg) + 50),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits[self.path] += 1
            status, content_type, payload, declared_length = routes[self.path]
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(declared_length))
            self.end_headers()
            try:
                self.wfile.write(payload)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{server.server_port}",
            hits=hits,
            png=png,
            jpeg=jpeg,
            large_png=large_png,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class LocalPostResolver:
    """Replace Instagram metadata only; download transport remains real."""

    def __init__(self, photos):
        self.photos = photos
        self.calls = 0

    def resolve(self, shortcode, cancel):
        assert shortcode == SHORTCODE
        assert not cancel.is_set()
        self.calls += 1
        return PostInfo(shortcode, "travel.photo", datetime(2025, 6, 2), self.photos, 1)


def test_carousel_saves_exact_bytes_in_chinese_path_and_skips_repeat(tmp_path, image_server):
    destination = tmp_path / "图片保存" / "旅行"
    resolver = LocalPostResolver([
        Photo(1, image_server.url + "/first.png"),
        Photo(3, image_server.url + "/third.jpeg"),
    ])
    progress = []

    result = DownloadService(resolver).run(
        POST_URL, destination, on_progress=lambda *event: progress.append(event)
    )

    assert result.directory == destination / f"travel.photo_{SHORTCODE}"
    assert [path.name for path in result.saved] == [f"{SHORTCODE}_01.png", f"{SHORTCODE}_03.jpg"]
    assert [path.read_bytes() for path in result.saved] == [image_server.png, image_server.jpeg]
    assert result.skipped == []
    assert progress[-1][:2] == (2, 2)
    assert any("1 个视频" in event[2] for event in progress)
    assert not list(destination.rglob("*.part"))
    requests_after_first_run = image_server.hits.copy()

    repeated = DownloadService(resolver).run(POST_URL, destination)

    assert repeated.saved == []
    assert repeated.skipped == result.saved
    assert resolver.calls == 2
    assert image_server.hits == requests_after_first_run
    assert image_server.hits == {"/first.png": 1, "/third.jpeg": 1}


@pytest.mark.parametrize("route", [
    "/unavailable", "/not-image", "/corrupt-image", "/truncated-transfer",
])
def test_failed_response_leaves_no_final_or_partial_file(tmp_path, image_server, route):
    resolver = LocalPostResolver([Photo(1, image_server.url + route)])

    with pytest.raises(AppError):
        DownloadService(resolver).run(POST_URL, tmp_path)

    assert image_server.hits[route] == 1
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def test_corrupt_existing_target_is_preserved_and_temporary_file_removed(tmp_path, image_server):
    directory = tmp_path / f"travel.photo_{SHORTCODE}"
    directory.mkdir()
    existing = directory / f"{SHORTCODE}_01.png"
    original_bytes = b"existing damaged image must not be overwritten"
    existing.write_bytes(original_bytes)
    resolver = LocalPostResolver([Photo(1, image_server.url + "/first.png")])

    with pytest.raises(AppError, match="已存在"):
        DownloadService(resolver).run(POST_URL, tmp_path)

    assert existing.read_bytes() == original_bytes
    assert list(directory.iterdir()) == [existing]
    assert image_server.hits == {"/first.png": 1}


def test_mid_transfer_cancel_keeps_finished_photo_and_can_resume(tmp_path, image_server, monkeypatch):
    cancel = Event()
    resolver = LocalPostResolver([
        Photo(1, image_server.url + "/first.png"),
        Photo(3, image_server.url + "/large.png"),
    ])
    service = DownloadService(resolver)
    real_get = service.session.get
    transferred_chunks = []

    def get_with_cancel(url, **kwargs):
        response = real_get(url, **kwargs)
        if url.endswith("/large.png"):
            real_iter_content = response.iter_content

            def cancel_after_first_chunk(*args, **chunk_kwargs):
                for chunk in real_iter_content(*args, **chunk_kwargs):
                    transferred_chunks.append(len(chunk))
                    yield chunk
                    cancel.set()

            response.iter_content = cancel_after_first_chunk
        return response

    monkeypatch.setattr(service.session, "get", get_with_cancel)

    with pytest.raises(Cancelled):
        service.run(POST_URL, tmp_path, cancel=cancel)

    directory = tmp_path / f"travel.photo_{SHORTCODE}"
    first = directory / f"{SHORTCODE}_01.png"
    assert first.read_bytes() == image_server.png
    assert list(directory.iterdir()) == [first]
    assert cancel.is_set()
    assert transferred_chunks[0] == 64 * 1024
    assert sum(transferred_chunks) < len(image_server.large_png)

    resumed = DownloadService(resolver).run(POST_URL, tmp_path)

    assert resumed.skipped == [first]
    assert [path.name for path in resumed.saved] == [f"{SHORTCODE}_03.png"]
    assert resumed.saved[0].read_bytes() == image_server.large_png
    assert not list(directory.glob("*.part"))
    assert image_server.hits == {"/first.png": 1, "/large.png": 2}
