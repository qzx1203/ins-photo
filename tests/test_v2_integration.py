"""Verify V2 media and previews through a real local HTTP server."""

from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from random import Random
from threading import Event, Thread
from types import SimpleNamespace

from PIL import Image
import pytest
import requests

from ins_saver.core import (
    AppError, Cancelled, DownloadService, Photo, PostInfo, Video, fetch_preview,
)


SHORTCODE = "V2Example_123"
POST_URL = f"https://www.instagram.com/p/{SHORTCODE}/"


@pytest.fixture(scope="module")
def media_payloads():
    def png(size, *, noisy=False):
        picture = (
            Image.frombytes("RGB", size, Random(7).randbytes(size[0] * size[1] * 3))
            if noisy else Image.new("RGB", size, (30, 120, 210))
        )
        buffer = BytesIO()
        picture.save(buffer, format="PNG")
        return buffer.getvalue()

    # A real 0.6-second H.264 clip, generated once with ffmpeg. Tests and the
    # application require no ffmpeg installation to use it.
    video = (Path(__file__).parent / "fixtures" / "tiny.mp4").read_bytes()
    # ISO-BMFF permits padding in a free box; keep the fixture small on disk.
    large_video = video + (512 * 1024 + 8).to_bytes(4, "big") + b"free" + bytes(512 * 1024)
    return SimpleNamespace(
        photo=png((13, 9)),
        large_photo=png((512, 512), noisy=True),
        preview=png((2400, 1600)),
        video=video,
        large_video=large_video,
    )


@pytest.fixture
def media_server(media_payloads):
    payloads = media_payloads
    hits = Counter()
    order = []
    routes = {
        "/photo.png": ("image/png", payloads.photo, len(payloads.photo)),
        "/large.png": ("image/png", payloads.large_photo, len(payloads.large_photo)),
        "/preview.png": ("image/png", payloads.preview, len(payloads.preview)),
        "/video.mp4": ("video/mp4", payloads.video, len(payloads.video)),
        "/large.mp4": ("video/mp4", payloads.large_video, len(payloads.large_video)),
        "/video-octets": ("application/octet-stream", payloads.video, len(payloads.video)),
        "/video-html": ("text/html", b"<html>login</html>", 18),
        "/video-fake": ("video/mp4", b"<html>login</html>", 18),
        "/video-cut": ("video/mp4", payloads.video[:-19], len(payloads.video) - 19),
        "/video-short-transfer": ("video/mp4", payloads.video, len(payloads.video) + 100),
        "/preview-html": ("image/png", b"<html>login</html>", 18),
        "/preview-large-header": ("image/png", b"invalid", 20 * 1024 * 1024 + 1),
        "/preview-large-stream": ("image/png", bytes(20 * 1024 * 1024 + 1), None),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits[self.path] += 1
            order.append(self.path)
            content_type, payload, declared_length = routes[self.path]
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            if declared_length is not None:
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
            url=f"http://127.0.0.1:{server.server_port}", hits=hits, order=order,
            payloads=payloads,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def post_info(server, *, only_video=False, video_route="/video.mp4"):
    return PostInfo(
        SHORTCODE, "travel.photo", datetime(2025, 6, 2),
        [] if only_video else [Photo(3, server.url + "/photo.png"), Photo(1, server.url + "/photo.png")],
        1, [Video(2, server.url + video_route, server.url + "/preview.png")],
    )


def test_default_photo_only_mode_never_requests_video_or_thumbnail(tmp_path, media_server):
    result = DownloadService().save_post(post_info(media_server), tmp_path)

    assert [path.name for path in result.saved] == [f"{SHORTCODE}_01.png", f"{SHORTCODE}_03.png"]
    assert [path.read_bytes() for path in result.saved] == [media_server.payloads.photo] * 2
    assert media_server.hits == {"/photo.png": 2}
    assert not list(tmp_path.rglob("*.part"))


def test_opt_in_saves_original_order_bytes_and_skips_all_existing_media(tmp_path, media_server):
    post = post_info(media_server)
    progress = []

    result = DownloadService().save_post(
        post, tmp_path, include_videos=True, on_progress=lambda *event: progress.append(event)
    )

    assert [path.name for path in result.saved] == [
        f"{SHORTCODE}_01.png", f"{SHORTCODE}_02.mp4", f"{SHORTCODE}_03.png",
    ]
    assert [path.read_bytes() for path in result.saved] == [
        media_server.payloads.photo, media_server.payloads.video, media_server.payloads.photo,
    ]
    assert media_server.order == ["/photo.png", "/video.mp4", "/photo.png"]
    assert progress[-1][:2] == (3, 3)
    first_hits = media_server.hits.copy()

    repeated = DownloadService().save_post(post, tmp_path, include_videos=True)

    assert repeated.saved == []
    assert repeated.skipped == result.saved
    assert media_server.hits == first_hits
    assert not list(tmp_path.rglob("*.part"))


def test_run_forwards_video_option_to_save_resolved_post(tmp_path, media_server):
    class Resolver:
        def resolve(self, shortcode, cancel):
            assert shortcode == SHORTCODE
            assert not cancel.is_set()
            return post_info(media_server, only_video=True)

    result = DownloadService(Resolver()).run(POST_URL, tmp_path, include_videos=True)

    assert [path.suffix for path in result.saved] == [".mp4"]
    assert result.saved[0].read_bytes() == media_server.payloads.video
    assert media_server.hits == {"/video.mp4": 1}


def test_video_only_post_with_default_option_makes_no_media_request(tmp_path, media_server):
    with pytest.raises(AppError):
        DownloadService().save_post(post_info(media_server, only_video=True), tmp_path)

    assert media_server.hits == {}
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def test_mp4_with_octet_stream_content_type_is_saved(tmp_path, media_server):
    result = DownloadService().save_post(
        post_info(media_server, only_video=True, video_route="/video-octets"),
        tmp_path, include_videos=True,
    )

    assert result.saved[0].read_bytes() == media_server.payloads.video


@pytest.mark.parametrize("route", [
    "/video-html", "/video-fake", "/video-cut", "/video-short-transfer",
])
def test_invalid_video_cleans_up_all_partial_and_final_files(tmp_path, media_server, route):
    with pytest.raises(AppError):
        DownloadService().save_post(
            post_info(media_server, only_video=True, video_route=route), tmp_path,
            include_videos=True,
        )

    assert media_server.hits == {route: 1}
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def test_invalid_existing_mp4_is_preserved_without_partial_files(tmp_path, media_server):
    directory = tmp_path / f"travel.photo_{SHORTCODE}"
    directory.mkdir()
    existing = directory / f"{SHORTCODE}_02.mp4"
    original = b"existing damaged video must not be overwritten"
    existing.write_bytes(original)

    with pytest.raises(AppError, match="已存在"):
        DownloadService().save_post(
            post_info(media_server, only_video=True), tmp_path, include_videos=True
        )

    assert existing.read_bytes() == original
    assert list(directory.iterdir()) == [existing]
    assert media_server.hits == {"/video.mp4": 1}


def test_cancelling_video_transfer_keeps_completed_photo_and_resumes(tmp_path, media_server, monkeypatch):
    post = post_info(media_server, video_route="/large.mp4")
    service = DownloadService()
    cancel = Event()
    real_get = service.session.get
    transferred = []

    def get_with_cancel(url, **kwargs):
        response = real_get(url, **kwargs)
        if url.endswith("/large.mp4"):
            original_iter = response.iter_content

            def stop_after_first_chunk(*args, **chunk_kwargs):
                for chunk in original_iter(*args, **chunk_kwargs):
                    transferred.append(len(chunk))
                    yield chunk
                    cancel.set()

            response.iter_content = stop_after_first_chunk
        return response

    monkeypatch.setattr(service.session, "get", get_with_cancel)

    with pytest.raises(Cancelled):
        service.save_post(post, tmp_path, include_videos=True, cancel=cancel)

    directory = tmp_path / f"travel.photo_{SHORTCODE}"
    completed = directory / f"{SHORTCODE}_01.png"
    assert list(directory.iterdir()) == [completed]
    assert completed.read_bytes() == media_server.payloads.photo
    assert 0 < sum(transferred) < len(media_server.payloads.large_video)

    resumed = DownloadService().save_post(post, tmp_path, include_videos=True)

    assert resumed.skipped == [completed]
    assert [path.name for path in resumed.saved] == [f"{SHORTCODE}_02.mp4", f"{SHORTCODE}_03.png"]
    assert resumed.saved[0].read_bytes() == media_server.payloads.large_video
    assert not list(directory.glob("*.part"))


def test_preview_is_resized_valid_image_and_does_not_create_files(tmp_path, media_server, monkeypatch):
    monkeypatch.chdir(tmp_path)

    preview = fetch_preview(media_server.url + "/preview.png")

    assert isinstance(preview, bytes)
    with Image.open(BytesIO(preview)) as picture:
        picture.load()
        assert picture.size == (1280, 853)
    assert media_server.hits == {"/preview.png": 1}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("route", [
    "/preview-html", "/video-cut", "/preview-large-header", "/preview-large-stream",
])
def test_invalid_or_oversized_preview_is_rejected_without_files(tmp_path, media_server, monkeypatch, route):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(AppError):
        fetch_preview(media_server.url + route)

    assert media_server.hits == {route: 1}
    assert list(tmp_path.iterdir()) == []


def test_precancelled_preview_makes_no_http_request(media_server):
    cancel = Event()
    cancel.set()

    with pytest.raises(Cancelled):
        fetch_preview(media_server.url + "/preview.png", cancel=cancel)

    assert media_server.hits == {}


def test_preview_midstream_cancellation_stops_before_complete_response(tmp_path, media_server, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cancel = Event()
    real_request = requests.Session.request
    transferred = []

    def request_with_cancel(session, *args, **kwargs):
        response = real_request(session, *args, **kwargs)
        original_iter = response.iter_content

        def stop_after_first_chunk(*chunk_args, **chunk_kwargs):
            for chunk in original_iter(*chunk_args, **chunk_kwargs):
                transferred.append(len(chunk))
                yield chunk
                cancel.set()

        response.iter_content = stop_after_first_chunk
        return response

    monkeypatch.setattr(requests.Session, "request", request_with_cancel)

    with pytest.raises(Cancelled):
        fetch_preview(media_server.url + "/large.png", cancel=cancel)

    assert 0 < sum(transferred) < len(media_server.payloads.large_photo)
    assert media_server.hits == {"/large.png": 1}
    assert list(tmp_path.iterdir()) == []
