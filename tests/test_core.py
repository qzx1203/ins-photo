from datetime import datetime
from io import BytesIO
from threading import Event
from types import SimpleNamespace

import pytest
from PIL import Image
import requests

from ins_saver import core


@pytest.mark.parametrize("url", [
    "https://www.instagram.com/p/Ab_c-123/",
    "http://instagram.com/p/Ab_c-123?igsh=abc#photo",
    "instagram.com/reel/Ab_c-123/",
    "https://m.instagram.com/reels/Ab_c-123/",
    "  https://WWW.INSTAGRAM.COM/tv/Ab_c-123/  ",
])
def test_parse_post_url_accepts_only_instagram_posts(url):
    assert core.parse_post_url(url) == "Ab_c-123"


@pytest.mark.parametrize("url", [
    "", "Ab_c-123", "https://instagram.com/username/", "https://instagram.com/",
    "https://instagram.com/p/", "https://instagram.com/p/a/more/",
    "https://instagram.com/p/a%2Fb/", "https://instagram.com/p/a b/",
    "https://instagram.com.evil.test/p/abc/", "https://evil.test/instagram.com/p/abc/",
    "https://user@instagram.com/p/abc/", "https://instagram.com@evil.test/p/abc/",
    "https://instagram.com:443/p/abc/", "file://instagram.com/p/abc/",
    "https://[instagram.com/p/abc/", "https://instagram.com/stories/user/123/",
    "https://instagram.com/share/p/abc/",
])
def test_parse_post_url_rejects_unsafe_or_non_post_urls(url):
    with pytest.raises(core.AppError, match="帖文链接"):
        core.parse_post_url(url)


def cookie_line(domain, name, value, expiry="0"):
    return f"{domain}\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}\n"


def test_load_netscape_cookies_limits_domains_and_handles_httponly(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(
        "\ufeff# Netscape HTTP Cookie File\n"
        + cookie_line("#HttpOnly_.instagram.com", "sessionid", "private-session")
        + cookie_line("www.instagram.com", "csrftoken", "csrf")
        + cookie_line(".instagram.com.evil.test", "sessionid", "evil")
        + cookie_line(".evilinstagram.com", "evil", "no")
        + cookie_line(".instagram.com", "expired", "old", "1"), encoding="utf-8",
    )
    assert core.load_instagram_cookies(path) == {"sessionid": "private-session", "csrftoken": "csrf"}


@pytest.mark.parametrize("body,message", [
    ("not a Netscape file", "Netscape"),
    ("# Netscape HTTP Cookie File\n" + cookie_line(".instagram.com", "csrftoken", "x"), "sessionid"),
    ("# Netscape HTTP Cookie File\n" + cookie_line(".instagram.com", "sessionid", "x", "1"), "已过期"),
    ("# Netscape HTTP Cookie File\n.instagram.com\tbroken", "第 2 行"),
    ("# Netscape HTTP Cookie File\n" + cookie_line(".instagram.com", "sessionid", "x", "bad"), "有效期"),
])
def test_bad_cookie_files_give_actionable_errors(tmp_path, body, message):
    path = tmp_path / "cookies.txt"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(core.AppError, match=message):
        core.load_instagram_cookies(path)


def fake_loader(monkeypatch, post, username=None, error=None):
    class Loader:
        def __init__(self, **kwargs):
            self.context = SimpleNamespace(update_cookies=lambda cookies: None, username=None)
            self.closed = False

        def test_login(self):
            return username

        def close(self):
            self.closed = True

    def resolve(context, shortcode):
        if error is not None:
            raise error
        return post

    monkeypatch.setattr(core.instaloader, "Instaloader", Loader)
    monkeypatch.setattr(core.instaloader.Post, "from_shortcode", resolve)


def test_resolve_carousel_preserves_order_and_skips_video_thumbnails(monkeypatch):
    post = SimpleNamespace(
        typename="GraphSidecar", owner_username="artist", date_utc=datetime(2026, 1, 1),
        get_sidecar_nodes=lambda: iter([
            SimpleNamespace(is_video=False, display_url="https://cdn.test/first"),
            SimpleNamespace(is_video=True, display_url="https://cdn.test/video-thumbnail"),
            SimpleNamespace(is_video=False, display_url="https://cdn.test/third"),
        ]),
    )
    fake_loader(monkeypatch, post)
    client = core.InstagramClient()
    result = client.resolve("test123", Event())
    assert result.photos == [core.Photo(1, "https://cdn.test/first"), core.Photo(3, "https://cdn.test/third")]
    assert result.skipped_videos == 1
    assert result.author == "artist"
    client.close()
    assert client.loader.closed


@pytest.mark.parametrize("is_video,expected_photos,skipped", [
    (False, [core.Photo(1, "https://cdn.test/picture")], 0),
    (True, [], 1),
])
def test_resolve_single_media(monkeypatch, is_video, expected_photos, skipped):
    post = SimpleNamespace(
        typename="GraphVideo" if is_video else "GraphImage", is_video=is_video,
        url="https://cdn.test/picture", owner_username="artist", date_utc=datetime(2026, 1, 1),
    )
    fake_loader(monkeypatch, post)
    result = core.InstagramClient().resolve("test123", Event())
    assert result.photos == expected_photos
    assert result.skipped_videos == skipped


def test_cookie_login_must_be_verified(monkeypatch):
    fake_loader(monkeypatch, None, username=None)
    with pytest.raises(core.AppError, match="Cookie 登录验证失败"):
        core.InstagramClient({"sessionid": "do-not-log"}).resolve("test123", Event())


def test_cookie_login_sets_account_for_instaloader(monkeypatch):
    post = SimpleNamespace(
        typename="GraphImage", is_video=False, url="https://cdn.test/picture",
        owner_username="artist", date_utc=datetime(2026, 1, 1),
    )
    fake_loader(monkeypatch, post, username="my-account")
    client = core.InstagramClient({"sessionid": "do-not-log"})
    client.resolve("test123", Event())
    assert client.loader.context.username == "my-account"


@pytest.mark.parametrize("error,message", [
    (core.instagram_errors.ConnectionException("429 Too Many Requests"), "429"),
    (core.instagram_errors.LoginRequiredException("login_required"), "要求登录"),
    (core.instagram_errors.QueryReturnedNotFoundException("404"), "找不到"),
    (core.instagram_errors.ConnectionException("network error"), "检查网络"),
    (requests.exceptions.Timeout("timed out"), "20 秒"),
])
def test_instagram_errors_are_translated(monkeypatch, error, message):
    fake_loader(monkeypatch, None, error=error)
    with pytest.raises(core.AppError, match=message):
        core.InstagramClient().resolve("test123", Event())


def png_bytes():
    output = BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, format="PNG")
    return output.getvalue()


class Response:
    def __init__(self, data=None, *, status=200, chunks=None, content_type="image/png", length=None):
        self.data = png_bytes() if data is None else data
        self.status_code = status
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(self.data) if length is None else length)}
        self.chunks = chunks
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} HTTP error")

    def iter_content(self, chunk_size):
        return iter(self.chunks) if self.chunks is not None else iter([self.data])


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)

    def close(self):
        self.closed = True


def service_with_photos(responses, indices=(1,)):
    post = core.PostInfo("test123", "artist", datetime(2026, 1, 1), [core.Photo(i, f"https://cdn.test/{i}") for i in indices])
    service = core.DownloadService(SimpleNamespace(resolve=lambda code, cancel: post))
    service.session.close()
    service.session = Session(responses)
    return service


def test_download_preserves_bytes_order_and_skips_valid_duplicates(tmp_path):
    image = png_bytes()
    service = service_with_photos([Response(image), Response(image)], indices=(1, 3))
    result = service.run("https://instagram.com/p/test123/", tmp_path)
    assert [path.name for path in result.saved] == ["test123_01.png", "test123_03.png"]
    assert all(path.read_bytes() == image for path in result.saved)
    assert service.session.closed
    assert all(kwargs["timeout"] == 20 for _, kwargs in service.session.calls)
    duplicate = service_with_photos([], indices=(1, 3))
    second = duplicate.run("https://instagram.com/p/test123/", tmp_path)
    assert second.saved == []
    assert second.skipped == result.saved
    assert duplicate.session.calls == []


@pytest.mark.parametrize("response,message", [
    (Response(b"<html>login</html>", content_type="text/html"), "不是图片"),
    (Response(b"not an image"), "不是完整"),
    (Response(length=999), "不完整"),
    (Response(status=429), "429"),
    (Response(status=403), "无权访问"),
])
def test_download_failures_leave_no_partial_files(tmp_path, response, message):
    service = service_with_photos([response])
    with pytest.raises(core.AppError, match=message):
        service.run("https://instagram.com/p/test123/", tmp_path)
    assert list(tmp_path.rglob("*.part")) == []
    assert list(tmp_path.rglob("*.png")) == []
    assert service.session.closed
    assert response.closed


def test_damaged_existing_target_is_preserved(tmp_path):
    folder = tmp_path / "artist_test123"
    folder.mkdir()
    target = folder / "test123_01.png"
    target.write_bytes(b"existing unrelated or damaged content")
    service = service_with_photos([Response()])
    with pytest.raises(core.AppError, match="已存在"):
        service.run("https://instagram.com/p/test123/", tmp_path)
    assert target.read_bytes() == b"existing unrelated or damaged content"
    assert list(tmp_path.rglob("*.part")) == []


def test_cancellation_cleans_current_partial_and_keeps_completed_files(tmp_path):
    cancel = Event()

    class CancellingResponse(Response):
        def iter_content(self, chunk_size):
            yield self.data[:10]
            cancel.set()
            yield self.data[10:]

    service = service_with_photos([Response(), CancellingResponse()], indices=(1, 2))
    with pytest.raises(core.Cancelled):
        service.run("https://instagram.com/p/test123/", tmp_path, cancel=cancel)
    assert [p.name for p in tmp_path.rglob("*.png")] == ["test123_01.png"]
    assert list(tmp_path.rglob("*.part")) == []
    assert service.session.closed


def test_stream_network_failure_cleans_partial(tmp_path):
    class BrokenResponse(Response):
        def iter_content(self, chunk_size):
            yield self.data[:10]
            raise requests.exceptions.ChunkedEncodingError("connection ended")

    service = service_with_photos([BrokenResponse()])
    with pytest.raises(core.AppError, match="检查网络"):
        service.run("https://instagram.com/p/test123/", tmp_path)
    assert list(tmp_path.rglob("*.part")) == []


def test_video_only_post_creates_no_download_folder(tmp_path):
    service = service_with_photos([], indices=())
    with pytest.raises(core.AppError, match="没有可下载的照片"):
        service.run("https://instagram.com/p/test123/", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_cancel_before_resolve_closes_session(tmp_path):
    cancel = Event()
    cancel.set()
    service = service_with_photos([])
    with pytest.raises(core.Cancelled):
        service.run("https://instagram.com/p/test123/", tmp_path, cancel=cancel)
    assert service.session.calls == []
    assert service.session.closed
