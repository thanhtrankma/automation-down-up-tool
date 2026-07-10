"""Tải video bằng yt-dlp, hỗ trợ callback sau mỗi video hoàn tất."""

from __future__ import annotations

import os
import ssl
from typing import Callable, Optional

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
