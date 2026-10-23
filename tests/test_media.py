from datetime import datetime
from io import BytesIO
from threading import Event
from types import SimpleNamespace

import pytest
from PIL import Image

from ins_saver import core
from test_core import Response, Session, png_bytes


def box(kind, payload=b"", *, extended=False, until_eof=False):
    if until_eof:
        return b"\0\0\0\0" + kind + payload
    if extended:
        return b"\0\0\0\1" + kind + (len(payload) + 16).to_bytes(8, "big") + payload
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


def container_bytes():
    # Container-only fixture: validation checks box structure, not codec playback.
    return box(b"ftyp", b"isom\0\0\0\0isom") + box(b"moov", b"metadata") + box(b"mdat", b"media bytes")


def post_info():
    return core.PostInfo(
        "test123", "artist", datetime(2026, 1, 1),
        [core.Photo(3, "https://cdn.test/third"), core.Photo(1, "https://cdn.test/first")],
        1, [core.Video(2, "https://cdn.test/video", "https://cdn.test/cover")],
    )


def download_service(responses):
    service = core.DownloadService(SimpleNamespace(resolve=lambda *args: pytest.fail("Post was resolved twice")))
    service.session.close()
    service.session = Session(responses)
    return service


def test_save_resolved_post_opt_in_video_preserves_order_bytes_and_duplicates(tmp_path):
    video = container_bytes()
    service = download_service([Response(), Response(video, content_type="video/mp4"), Response()])
    result = service.save_post(post_info(), tmp_path, include_videos=True)
    assert [path.name for path in result.saved] == ["test123_01.png", "test123_02.mp4", "test123_03.png"]
    assert result.saved[1].read_bytes() == video
    assert [url for url, _ in service.session.calls] == [
        "https://cdn.test/first", "https://cdn.test/video", "https://cdn.test/third",
    ]
    assert service.session.closed
    duplicate = download_service([])
    second = duplicate.save_post(post_info(), tmp_path, include_videos=True)
    assert second.saved == []
    assert second.skipped == result.saved
    assert duplicate.session.calls == []


def test_save_resolved_post_defaults_to_photos_only(tmp_path):
    service = download_service([Response(), Response()])
    result = service.save_post(post_info(), tmp_path)
    assert [path.suffix for path in result.saved] == [".png", ".png"]
    assert all("video" not in url and "cover" not in url for url, _ in service.session.calls)


def test_pure_video_post_requires_opt_in_and_never_saves_cover(tmp_path):
    post = post_info()
    post.photos = []
    disabled = download_service([])
    with pytest.raises(core.AppError, match="同时下载视频"):
        disabled.save_post(post, tmp_path)
    assert list(tmp_path.iterdir()) == []
    assert disabled.session.closed
    enabled = download_service([Response(container_bytes(), content_type="application/octet-stream")])
    result = enabled.save_post(post, tmp_path, include_videos=True)
    assert [path.name for path in result.saved] == ["test123_02.mp4"]


@pytest.mark.parametrize("data", [
    b"<html>not a video</html>",
    box(b"ftyp", b"isom\0\0\0\0"),
    box(b"ftyp", b"isom\0\0\0\0") + box(b"mdat", b"data"),
    box(b"ftyp", b"isom\0\0\0\0") + box(b"moov", b"metadata") + box(b"mdat"),
    container_bytes()[:-3],
    container_bytes() + b"trailing bytes",
    box(b"ftyp", b"isom\0\0\0\0") + b"\0\0\0\1moov\0\0",
    box(b"ftyp", b"isom\0\0\0\0") + b"\0\0\0\4mdat",
])
def test_invalid_mp4_is_rejected_and_no_partial_file_is_published(tmp_path, data):
    post = post_info()
    post.photos = []
    service = download_service([Response(data, content_type="video/mp4")])
    with pytest.raises(core.AppError, match="不是完整、有效的视频"):
        service.save_post(post, tmp_path, include_videos=True)
    assert list(tmp_path.rglob("*.part")) == []
    assert list(tmp_path.rglob("*.mp4")) == []
    assert service.session.closed


def test_mp4_extended_boxes_and_final_mdat_without_size(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(
        box(b"ftyp", b"isom\0\0\0\0", extended=True)
        + box(b"moov", b"metadata", extended=True)
        + box(b"mdat", b"media", until_eof=True)
    )
    assert core._video_extension(path) == ".mp4"


def test_damaged_video_is_preserved_instead_of_overwritten(tmp_path):
    folder = tmp_path / "artist_test123"
    folder.mkdir()
    target = folder / "test123_02.mp4"
    target.write_bytes(b"original damaged file")
    post = post_info()
    post.photos = []
    service = download_service([Response(container_bytes(), content_type="video/mp4")])
    with pytest.raises(core.AppError, match="已存在"):
        service.save_post(post, tmp_path, include_videos=True)
    assert target.read_bytes() == b"original damaged file"
    assert list(tmp_path.rglob("*.part")) == []


def test_cancelling_video_cleans_partial_and_keeps_finished_photo(tmp_path):
    cancel = Event()

    class CancellingResponse(Response):
        def iter_content(self, chunk_size):
            yield self.data[:10]
            cancel.set()
            yield self.data[10:]

    response = CancellingResponse(container_bytes(), content_type="video/mp4")
    service = download_service([Response(), response])
    with pytest.raises(core.Cancelled):
        service.save_post(post_info(), tmp_path, include_videos=True, cancel=cancel)
    assert [path.name for path in tmp_path.rglob("*.png")] == ["test123_01.png"]
    assert list(tmp_path.rglob("*.mp4")) == []
    assert list(tmp_path.rglob("*.part")) == []
    assert service.session.closed and response.closed


def preview_session(monkeypatch, response):
    session = Session([response])
    monkeypatch.setattr(core.requests, "Session", lambda: session)
    return session


def test_preview_resizes_to_1280_and_returns_valid_png_without_writing_files(monkeypatch, tmp_path):
    data = BytesIO()
    Image.new("RGB", (2400, 1600), "blue").save(data, format="JPEG")
    response = Response(data.getvalue(), content_type="image/jpeg")
    session = preview_session(monkeypatch, response)
    result = core.fetch_preview("https://cdn.test/preview")
    with Image.open(BytesIO(result)) as image:
        image.load()
        assert image.format == "PNG"
        assert image.width == 1280
        assert image.height <= 1280
    assert list(tmp_path.iterdir()) == []
    assert session.calls[0][1]["timeout"] == 20
    assert session.closed and response.closed


@pytest.mark.parametrize("response,message", [
    (Response(b"<html>login</html>", content_type="text/html"), "不是图片"),
    (Response(b"broken image"), "有效的图片"),
    (Response(png_bytes()[:25]), "有效的图片"),
    (Response(length=99_999_999), "20 MB"),
    (Response(length=12345), "不完整"),
])
def test_bad_preview_closes_response_and_session(monkeypatch, response, message):
    session = preview_session(monkeypatch, response)
    with pytest.raises(core.AppError, match=message):
        core.fetch_preview("https://cdn.test/preview")
    assert session.closed and response.closed


def test_preview_stream_limit_applies_without_content_length(monkeypatch):
    response = Response(chunks=[b"a" * 6, b"b" * 6])
    response.headers.pop("Content-Length")
    session = preview_session(monkeypatch, response)
    monkeypatch.setattr(core, "_PREVIEW_LIMIT", 10)
    with pytest.raises(core.AppError, match="20 MB"):
        core.fetch_preview("https://cdn.test/preview")
    assert session.closed and response.closed


def test_preview_cancel_during_stream_closes_everything(monkeypatch):
    cancel = Event()

    class CancellingResponse(Response):
        def iter_content(self, chunk_size):
            yield self.data[:10]
            cancel.set()
            yield self.data[10:]

    response = CancellingResponse()
    session = preview_session(monkeypatch, response)
    with pytest.raises(core.Cancelled):
        core.fetch_preview("https://cdn.test/preview", cancel)
    assert session.closed and response.closed


def test_cancelled_save_does_not_create_directory_and_closes_session(tmp_path):
    cancel = Event()
    cancel.set()
    service = download_service([])
    with pytest.raises(core.Cancelled):
        service.save_post(post_info(), tmp_path, include_videos=True, cancel=cancel)
    assert list(tmp_path.iterdir()) == []
    assert service.session.closed
