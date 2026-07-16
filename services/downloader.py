"""Tải video bằng yt-dlp, hỗ trợ callback sau mỗi video hoàn tất."""

from __future__ import annotations

import os
import ssl
from typing import Callable, Optional
from urllib.parse import urlparse

from services.video_prep import should_process_download

try:
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    ssl._create_default_https_context = ssl.create_default_context
except ImportError:
    pass

import yt_dlp
from yt_dlp.utils import DownloadCancelled

OUTPUT_TEMPLATE = os.path.join("%(playlist_title|Videos)s", "%(title)s.%(ext)s")

FORMAT_QUICKTIME_COMPATIBLE = (
    "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a][acodec^=mp4a]"
    "/bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]"
    "/best[ext=mp4][vcodec^=avc1]"
    "/best[ext=mp4]/best"
)
FORMAT_BEST_QUALITY = "bestvideo+bestaudio/best"

# yt-dlp đã hỗ trợ sẵn Douyin và Bilibili (extractor có trong thư viện).
# Một số video trên 2 nền tảng này yêu cầu header Referer đúng domain,
# nếu không sẽ bị chặn (403) dù URL hợp lệ.
_REFERER_BY_HOST = {
    "douyin.com": "https://www.douyin.com/",
    "iesdouyin.com": "https://www.douyin.com/",
    "bilibili.com": "https://www.bilibili.com/",
    "b23.tv": "https://www.bilibili.com/",
}

# Cookie trình duyệt chỉ được áp dụng khi người dùng CHỌN RÕ trong giao diện.
# Không set mặc định để tránh ảnh hưởng tới các tải bình thường (vd. YouTube)
# hoặc gây lỗi trên máy chưa cấp quyền đọc cookie trình duyệt.
COOKIE_BROWSER_CHOICES = {"Không dùng": "", "Safari": "safari", "Chrome": "chrome"}


def extra_headers_for_url(url: str) -> dict:
    """Trả về header bổ sung (nếu cần) cho một số nền tảng cụ thể (Douyin/Bilibili)."""
    host = (urlparse(url).netloc or "").lower()
    for domain, referer in _REFERER_BY_HOST.items():
        if domain in host:
            return {"Referer": referer}
    return {}


class DownloadCancelledFlag:
    def __init__(self) -> None:
        import threading

        self._event = threading.Event()

    def request_cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def reset(self) -> None:
        self._event.clear()


def download_videos(
    url: str,
    output_dir: str,
    *,
    on_video_finished: Optional[Callable[[str, str], None]] = None,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[str, float], None]] = None,
    cancel_flag: Optional[DownloadCancelledFlag] = None,
    quicktime_compat: bool = True,
    cookies_browser: str = "",
) -> None:
    """Tải video/playlist. Gọi on_video_finished(filepath, original_title) sau mỗi video."""
    os.makedirs(output_dir, exist_ok=True)
    outtmpl = os.path.join(output_dir, OUTPUT_TEMPLATE)
    cancel_flag = cancel_flag or DownloadCancelledFlag()

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    ydl_opts = {
        "outtmpl": outtmpl,
        "ignoreerrors": True,
        "noplaylist": False,
        "progress_hooks": [],
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": False,
    }

    chosen_format = FORMAT_QUICKTIME_COMPATIBLE if quicktime_compat else FORMAT_BEST_QUALITY
    ydl_opts.update({"format": chosen_format, "merge_output_format": "mp4"})
    if quicktime_compat:
        ydl_opts["format_sort"] = ["vcodec:h264", "acodec:aac", "ext:mp4:m4a", "res"]

    headers = extra_headers_for_url(url)
    if headers:
        ydl_opts["http_headers"] = headers
    if cookies_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_browser,)

    def hook(d: dict) -> None:
        if cancel_flag.is_cancelled():
            raise DownloadCancelled("Người dùng đã hủy tác vụ tải.")

        status = d.get("status")
        info = d.get("info_dict", {}) or {}
        title = info.get("title") or os.path.basename(d.get("filename", ""))

        if status == "downloading" and on_progress:
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes") or 0
            percent = (downloaded / total * 100) if total else 0.0
            on_progress(title, percent)
        elif status == "finished":
            filepath = d.get("filename") or ""
            if not should_process_download(filepath):
                return
            log(f"✅ Đã tải xong: {title}")
            if on_video_finished:
                on_video_finished(filepath, title)

    ydl_opts["progress_hooks"] = [hook]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
