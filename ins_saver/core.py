"""Instagram post resolution, previews and lossless media downloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
import os
from pathlib import Path
import re
import tempfile
from threading import Event
import time
from typing import Callable
from urllib.parse import urlsplit

import instaloader
from instaloader import exceptions as instagram_errors
from PIL import Image, UnidentifiedImageError
import requests


class AppError(Exception):
    """A short, user-facing error."""


class Cancelled(AppError):
    pass


@dataclass
class Photo:
    index: int
    url: str


@dataclass
class Video:
    index: int
    url: str
    thumbnail_url: str


@dataclass
class PostInfo:
    shortcode: str
    author: str
    date: datetime
    photos: list[Photo]
    skipped_videos: int = 0
    videos: list[Video] = field(default_factory=list)


@dataclass
class DownloadResult:
    directory: Path
    saved: list[Path]
    skipped: list[Path]


_POST_PATH = re.compile(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]{1,64})/?\Z")
_IMAGE_EXTENSIONS = {
    "JPEG": ".jpg", "MPO": ".jpg", "PNG": ".png", "WEBP": ".webp",
    "AVIF": ".avif", "GIF": ".gif", "TIFF": ".tiff", "HEIF": ".heic",
}
_IMAGE_ERRORS = (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError)
_PREVIEW_LIMIT = 20 * 1024 * 1024


def parse_post_url(text: str) -> str:
    """Return a shortcode from a complete Instagram post URL."""
    value = text.strip()
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        valid_host = parsed.hostname in {"instagram.com", "www.instagram.com", "m.instagram.com"}
        valid_address = (
            parsed.scheme in {"http", "https"} and valid_host
            and parsed.username is None and parsed.password is None and parsed.port is None
        )
        match = _POST_PATH.fullmatch(parsed.path)
    except ValueError:
        valid_address, match = False, None
    if not valid_address or match is None:
        raise AppError("请输入 Instagram 帖文链接，例如 https://www.instagram.com/p/帖文编号/。")
    return match.group(1)


def load_instagram_cookies(path: Path) -> dict[str, str]:
    """Read an exported Netscape cookie file; never persist the imported session."""
    path = Path(path)
    if path.suffix.lower() != ".txt":
        raise AppError("请选择 Netscape 格式的 Cookie 文本文件（.txt）。")
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AppError("无法读取 Cookie 文件，请检查文件路径和 UTF-8 编码。") from exc
    if not any(line.startswith("#") and "HTTP Cookie File" in line for line in lines[:10]):
        raise AppError("Cookie 文件不是 Netscape 格式，请从浏览器重新导出 cookies.txt。")
    cookies: dict[str, str] = {}
    expired_session = False
    for line_number, line in enumerate(lines, 1):
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        domain = fields[0].lstrip(".").lower()
        if domain != "instagram.com" and not domain.endswith(".instagram.com"):
            continue
        if len(fields) != 7:
            raise AppError(f"Cookie 文件第 {line_number} 行格式不完整，请重新导出。")
        _, _, _, _, expiry, name, value = fields
        try:
            is_expired = bool(expiry) and float(expiry) != 0 and float(expiry) <= time.time()
        except ValueError as exc:
            raise AppError(f"Cookie 文件第 {line_number} 行的有效期格式不正确。") from exc
        if is_expired:
            expired_session = expired_session or name == "sessionid"
            continue
        if name and value:
            cookies[name] = value
    if not cookies.get("sessionid"):
        if expired_session:
            raise AppError("Cookie 中的登录会话已过期，请在浏览器登录 Instagram 后重新导出。")
        raise AppError("Cookie 文件缺少 Instagram 的 sessionid，请登录 Instagram 后重新导出。")
    return cookies


def _check_cancel(cancel: Event) -> None:
    if cancel.is_set():
        raise Cancelled("已取消下载。")


def _friendly_error(exc: Exception) -> AppError:
    if isinstance(exc, AppError):
        return exc
    message = str(exc).lower()
    if isinstance(exc, instagram_errors.TooManyRequestsException) or "429" in message or "too many requests" in message:
        return AppError("Instagram 请求过于频繁（429），请稍后手动重试。")
    if isinstance(exc, instagram_errors.PrivateProfileNotFollowedException):
        return AppError("当前账号无权查看该私密帖文，请确认你已关注并获准访问。")
    if isinstance(exc, instagram_errors.QueryReturnedNotFoundException):
        return AppError("找不到该帖文，可能已删除、链接有误，或当前账号无权访问。")
    if isinstance(exc, (instagram_errors.LoginRequiredException, instagram_errors.LoginException)) or any(
        word in message for word in ("login", "logged out", "401", "403", "checkpoint", "challenge", "feedback_required")
    ):
        return AppError("Instagram 要求登录或验证，请在浏览器完成验证并导入新的 Cookie 文件后重试。")
    if isinstance(exc, requests.exceptions.Timeout) or "timed out" in message:
        return AppError("网络请求超时（20 秒），请检查网络后手动重试。")
    if isinstance(exc, (requests.exceptions.RequestException, instagram_errors.ConnectionException)):
        return AppError("无法从 Instagram 获取数据，请检查网络或代理；匿名访问受限时可导入 Cookie 后重试。")
    if isinstance(exc, instagram_errors.InstaloaderException):
        return AppError("Instagram 暂时无法提供帖文数据，请稍后重试或导入新的 Cookie。")
    return AppError("无法读取帖文数据，Instagram 的接口可能已变化，请检查链接或稍后重试。")


class _NoWaitRateController(instaloader.RateController):
    def sleep(self, secs: float) -> None:
        if secs > 0:
            raise AppError("请求较频繁，请稍后手动重试。")

    def handle_429(self, query_type: str) -> None:
        raise AppError("Instagram 请求过于频繁（429），请稍后手动重试。")


class InstagramClient:
    def __init__(self, cookies: dict[str, str] | None = None):
        self.loader = instaloader.Instaloader(
            quiet=True, sleep=False, max_connection_attempts=1,
            request_timeout=20, rate_controller=_NoWaitRateController,
            download_videos=False, download_video_thumbnails=False,
            download_comments=False, save_metadata=False,
        )
        self.cookies = dict(cookies) if cookies else None
        self._login_checked = False
        if self.cookies:
            self.loader.context.update_cookies(self.cookies)

    def resolve(self, shortcode: str, cancel: Event) -> PostInfo:
        try:
            _check_cancel(cancel)
            if self.cookies and not self._login_checked:
                username = self.loader.test_login()
                _check_cancel(cancel)
                if not username:
                    raise AppError("Cookie 登录验证失败，可能已过期或网络受限；请在浏览器登录后重新导出并检查网络。")
                self.loader.context.username = username
                self._login_checked = True
            post = instaloader.Post.from_shortcode(self.loader.context, shortcode)
            _check_cancel(cancel)
            photos: list[Photo] = []
            videos: list[Video] = []
            if post.typename == "GraphSidecar":
                for index, node in enumerate(post.get_sidecar_nodes(), 1):
                    _check_cancel(cancel)
                    if node.is_video:
                        videos.append(Video(index, node.video_url, node.display_url))
                    else:
                        photos.append(Photo(index, node.display_url))
            elif post.is_video:
                videos.append(Video(1, post.video_url, post.url))
            else:
                photos.append(Photo(1, post.url))
            info = PostInfo(shortcode, post.owner_username, post.date_utc, photos, len(videos), videos)
            _check_cancel(cancel)
            return info
        except Exception as exc:
            _check_cancel(cancel)
            raise _friendly_error(exc) from exc

    def close(self) -> None:
        self.loader.close()
        if self.cookies:
            self.cookies.clear()


def _image_extension(path: Path) -> str:
    with Image.open(path) as picture:
        extension = _IMAGE_EXTENSIONS.get(picture.format)
        picture.verify()
    # JPEG.verify() does not decode the image, so also detect truncated pixel data.
    with Image.open(path) as picture:
        picture.load()
    if extension is None:
        raise ValueError("Unsupported image format")
    return extension


def _video_extension(path: Path) -> str:
    """Reject incomplete ISO-BMFF containers without changing their bytes."""
    size = path.stat().st_size
    found: set[bytes] = set()
    with path.open("rb") as source:
        offset = 0
        while offset < size:
            source.seek(offset)
            header = source.read(8)
            if len(header) != 8:
                raise ValueError("Incomplete MP4 box header")
            box_size, kind = int.from_bytes(header[:4], "big"), header[4:]
            header_size = 8
            if box_size == 1:
                extended = source.read(8)
                if len(extended) != 8:
                    raise ValueError("Incomplete extended MP4 box header")
                box_size, header_size = int.from_bytes(extended, "big"), 16
            elif box_size == 0:
                box_size = size - offset
            if box_size < header_size or offset + box_size > size:
                raise ValueError("Incomplete MP4 box")
            if offset == 0 and kind != b"ftyp":
                raise ValueError("Missing MP4 file type")
            payload_size = box_size - header_size
            if kind == b"ftyp" and (payload_size < 8 or payload_size % 4):
                raise ValueError("Invalid MP4 file type")
            if kind in {b"moov", b"mdat"} and payload_size == 0:
                raise ValueError("Empty MP4 media box")
            found.add(kind)
            offset += box_size
    if not {b"ftyp", b"moov", b"mdat"} <= found:
        raise ValueError("Missing MP4 media boxes")
    return ".mp4"


def fetch_preview(url: str, cancel: Event | None = None) -> bytes:
    """Fetch and resize one photo or video cover in memory; never save a file."""
    cancel = cancel if cancel is not None else Event()
    session = requests.Session()
    try:
        _check_cancel(cancel)
        content = BytesIO()
        with session.get(url, stream=True, timeout=20, headers={"Accept-Encoding": "identity"}) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type and not (content_type.startswith("image/") or content_type == "application/octet-stream"):
                raise AppError("预览内容不是图片，请重新读取帖文后重试。")
            content_length = response.headers.get("Content-Length")
            if content_length and not response.headers.get("Content-Encoding"):
                try:
                    expected_bytes = int(content_length)
                except ValueError as exc:
                    raise AppError("服务器返回了无效的预览图片长度，请稍后重试。") from exc
                if expected_bytes > _PREVIEW_LIMIT:
                    raise AppError("预览图片超过 20 MB，无法显示。")
            for chunk in response.iter_content(chunk_size=64 * 1024):
                _check_cancel(cancel)
                if content.tell() + len(chunk) > _PREVIEW_LIMIT:
                    raise AppError("预览图片超过 20 MB，无法显示。")
                content.write(chunk)
            if content_length and not response.headers.get("Content-Encoding") and content.tell() != expected_bytes:
                raise AppError("预览图片下载不完整，请重新读取帖文后重试。")
        _check_cancel(cancel)
        try:
            with Image.open(content) as picture:
                picture.verify()
            content.seek(0)
            with Image.open(content) as picture:
                picture.draft("RGB", (1280, 1280))
                picture.thumbnail((1280, 1280))
                picture.load()
                _check_cancel(cancel)
                preview = picture.convert("RGBA" if "A" in picture.getbands() else "RGB")
                output = BytesIO()
                preview.save(output, format="PNG")
        except _IMAGE_ERRORS as exc:
            raise AppError("无法显示预览，服务器未返回完整、有效的图片。") from exc
        _check_cancel(cancel)
        return output.getvalue()
    except requests.exceptions.RequestException as exc:
        _check_cancel(cancel)
        raise _friendly_error(exc) from exc
    finally:
        session.close()


class DownloadService:
    def __init__(self, client: InstagramClient | None = None):
        self.client = client
        self.session = requests.Session()

    def run(
        self, url: str, destination: Path, cookies: dict[str, str] | None = None,
        cancel: Event | None = None,
        on_progress: Callable[[int, int, str], None] | None = None,
        include_videos: bool = False,
    ) -> DownloadResult:
        cancel = cancel if cancel is not None else Event()
        progress = on_progress or (lambda done, total, message: None)
        client = self.client
        owns_client = False
        try:
            shortcode = parse_post_url(url)
            _check_cancel(cancel)
            if client is None:
                client = InstagramClient(cookies)
                owns_client = True
            progress(0, 0, "正在读取帖文…")
            post = client.resolve(shortcode, cancel)
            return self._save_post(post, destination, include_videos, cancel, progress)
        except OSError as exc:
            raise AppError("无法写入下载文件夹，请检查路径、磁盘空间和写入权限。") from exc
        finally:
            if owns_client and client is not None:
                client.close()
            self.session.close()

    def save_post(
        self, post: PostInfo, destination: Path, *, include_videos: bool = False,
        cancel: Event | None = None,
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> DownloadResult:
        """Download a previously resolved post without querying Instagram again."""
        cancel = cancel if cancel is not None else Event()
        progress = on_progress or (lambda done, total, message: None)
        try:
            return self._save_post(post, destination, include_videos, cancel, progress)
        except OSError as exc:
            raise AppError("无法写入下载文件夹，请检查路径、磁盘空间和写入权限。") from exc
        finally:
            self.session.close()

    def _save_post(
        self, post: PostInfo, destination: Path, include_videos: bool,
        cancel: Event, progress: Callable[[int, int, str], None],
    ) -> DownloadResult:
        _check_cancel(cancel)
        media: list[Photo | Video] = list(post.photos)
        if include_videos:
            media.extend(post.videos)
        media.sort(key=lambda item: item.index)
        if not media:
            raise AppError("该帖文没有可下载的照片；如需保存视频，请勾选“同时下载视频”后重试。")
        shortcode = parse_post_url(f"https://www.instagram.com/p/{post.shortcode}/")
        author = re.sub(r"[^A-Za-z0-9_.-]", "_", post.author).strip(". ") or "instagram"
        directory = Path(destination).expanduser().resolve() / f"{author}_{shortcode}"
        directory.mkdir(parents=True, exist_ok=True)
        result = DownloadResult(directory, [], [])
        total = len(media)
        message = f"找到 {len(post.photos)} 张照片，"
        message += f"{len(post.videos)} 个视频。" if include_videos else f"跳过 {post.skipped_videos} 个视频。"
        progress(0, total, message)
        for position, item in enumerate(media):
            _check_cancel(cancel)
            video = isinstance(item, Video)
            stem = f"{shortcode}_{item.index:02d}"
            existing = self._find_existing(directory, stem, video)
            if existing is not None:
                result.skipped.append(existing)
                progress(position + 1, total, f"已跳过已有{'视频' if video else '图片'}：{existing.name}")
                continue
            progress(position, total, f"正在下载第 {position + 1} / {total} 个文件…")
            saved = self._download(item.url, directory, stem, cancel, video)
            result.saved.append(saved)
            progress(position + 1, total, f"已保存：{saved.name}")
        return result

    @staticmethod
    def _find_existing(directory: Path, stem: str, video: bool = False) -> Path | None:
        for candidate in sorted(directory.glob(stem + ".*")):
            extensions = {".mp4"} if video else {*_IMAGE_EXTENSIONS.values(), ".jpeg"}
            if candidate.suffix.lower() not in extensions or not candidate.is_file():
                continue
            try:
                (_video_extension if video else _image_extension)(candidate)
                return candidate
            except _IMAGE_ERRORS:
                continue
        return None

    def _download(self, url: str, directory: Path, stem: str, cancel: Event, video: bool = False) -> Path:
        temporary: Path | None = None
        label = "视频" if video else "图片"
        try:
            _check_cancel(cancel)
            with self.session.get(url, stream=True, timeout=20, headers={"Accept-Encoding": "identity"}) as response:
                if response.status_code == 429:
                    raise AppError(f"{label}下载请求过于频繁（429），请稍后手动重试。")
                if response.status_code in {401, 403}:
                    raise AppError(f"{label}链接已过期或当前账号无权访问，请重新读取帖文后重试。")
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                valid_type = content_type in {"video/mp4", "application/mp4", "application/octet-stream"} if video else (
                    content_type.startswith("image/") or content_type == "application/octet-stream"
                )
                if content_type and not valid_type:
                    raise AppError(f"服务器返回的内容不是{label}，请重新读取帖文后重试。")
                byte_count = 0
                with tempfile.NamedTemporaryFile(dir=directory, prefix=f".{stem}_", suffix=".part", delete=False) as output:
                    temporary = Path(output.name)
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        _check_cancel(cancel)
                        if chunk:
                            output.write(chunk)
                            byte_count += len(chunk)
                _check_cancel(cancel)
                content_length = response.headers.get("Content-Length")
                if content_length and not response.headers.get("Content-Encoding"):
                    try:
                        expected_bytes = int(content_length)
                    except ValueError as exc:
                        raise AppError(f"服务器返回了无效的{label}长度，请稍后重试。") from exc
                    if byte_count != expected_bytes:
                        raise AppError(f"{label}下载不完整，请检查网络后手动重试。")
            try:
                extension = (_video_extension if video else _image_extension)(temporary)
            except _IMAGE_ERRORS as exc:
                raise AppError(f"下载内容不是完整、有效的{label}，请重新读取帖文后重试。") from exc
            _check_cancel(cancel)
            target = directory / (stem + extension)
            # Windows rename is atomic, refuses overwrite, and works on removable FAT/exFAT disks.
            try:
                if os.name == "nt":
                    os.rename(temporary, target)
                else:
                    os.link(temporary, target)
            except FileExistsError as exc:
                raise AppError(f"目标文件 {target.name} 已存在，请移走损坏或冲突的文件后重试。") from exc
            return target
        except requests.exceptions.RequestException as exc:
            _check_cancel(cancel)
            raise _friendly_error(exc) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
