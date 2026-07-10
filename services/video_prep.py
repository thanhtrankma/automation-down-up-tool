"""Chuẩn bị file video tương thích YouTube (H.264 + AAC trong MP4)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Callable, Optional

FRAGMENT_PATTERN = re.compile(r"\.f\d+\.[a-z0-9]+$", re.IGNORECASE)
AUDIO_EXTENSIONS = {".m4a", ".mp3", ".opus", ".aac", ".wav", ".flac"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".wmv", ".flv", ".webm"}


def is_fragment_file(filepath: str) -> bool:
    return bool(FRAGMENT_PATTERN.search(filepath))


def is_audio_only_extension(filepath: str) -> bool:
    return os.path.splitext(filepath)[1].lower() in AUDIO_EXTENSIONS


def probe_media(filepath: str) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            filepath,
        ],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def has_video_stream(filepath: str) -> bool:
    data = probe_media(filepath)
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            return True
    return False


def should_process_download(filepath: str) -> bool:
    """Chỉ xử lý file video cuối cùng, bỏ qua fragment/audio trung gian của yt-dlp."""
    if not filepath or not os.path.exists(filepath):
        return False
    if is_audio_only_extension(filepath):
        return False
    if is_fragment_file(filepath):
        return False
    return has_video_stream(filepath)


def is_youtube_compatible(filepath: str) -> bool:
    data = probe_media(filepath)
    if not data:
        return False

    video_ok = False
    audio_ok = False
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            codec = (stream.get("codec_name") or "").lower()
            if codec in ("h264", "avc1"):
                video_ok = True
        elif stream.get("codec_type") == "audio":
            codec = (stream.get("codec_name") or "").lower()
            if codec in ("aac", "mp4a"):
                audio_ok = True

    ext = os.path.splitext(filepath)[1].lower()
    return video_ok and audio_ok and ext == ".mp4"


def prepare_for_youtube(
    input_path: str,
    *,
    log: Optional[Callable[[str], None]] = None,
) -> str:
    """
    Trả về đường dẫn file sẵn sàng upload.
    Nếu cần convert sẽ tạo file mới cạnh file gốc với hậu tố _yt.mp4.
    """
    if is_youtube_compatible(input_path):
        if log:
            log(f"✅ File đã tương thích YouTube: {os.path.basename(input_path)}")
        return input_path

    if log:
        log(f"🔄 Đang chuyển đổi sang H.264+AAC cho YouTube: {os.path.basename(input_path)}")

    base, _ = os.path.splitext(input_path)
    output_path = f"{base}_yt.mp4"

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        input_path,
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-pix_fmt",
        "yuv420p",
        "-loglevel",
        "error",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg convert thất bại: {(result.stderr or result.stdout)[-500:]}"
        )

    if not os.path.exists(output_path) or not has_video_stream(output_path):
        raise RuntimeError("Convert xong nhưng không tạo được file video hợp lệ.")

    if log:
        log(f"✅ Đã convert xong: {os.path.basename(output_path)}")
    return output_path


def list_local_videos(folder: str) -> list[str]:
    """Liệt kê file video hợp lệ trong thư mục (không đệ quy), sắp xếp theo tên."""
    if not os.path.isdir(folder):
        return []

    videos: list[str] = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in VIDEO_EXTENSIONS:
            continue
        if name.endswith("_yt.mp4"):
            continue
        if is_fragment_file(path):
            continue
        if not has_video_stream(path):
            continue
        videos.append(path)
    return videos
