"""Chuyển đổi tỉ lệ khung hình video (vd. 16:9 → 9:16) bằng FFmpeg."""

from __future__ import annotations

import os
import subprocess
from typing import Callable, Optional

from services.video_prep import VIDEO_EXTENSIONS, has_video_stream, list_local_videos, probe_media

# Tỉ lệ phổ biến: (label, width, height)
ASPECT_PRESETS: dict[str, tuple[int, int]] = {
    "9:16 (1080×1920) — Shorts/Reels/TikTok": (1080, 1920),
    "9:16 (720×1280)": (720, 1280),
    "16:9 (1920×1080) — ngang": (1920, 1080),
    "1:1 (1080×1080) — vuông": (1080, 1080),
    "4:5 (1080×1350) — Instagram": (1080, 1350),
}

# Chế độ lấp đầy khung hình
MODE_CROP = "crop"
MODE_BLUR = "blur"
MODE_PAD = "pad"

MODE_LABELS = {
    MODE_CROP: "Cắt giữa (crop) — giữ vùng giữa, bỏ hai bên",
    MODE_BLUR: "Phóng + nền mờ (blur) — giữ toàn bộ, nền blur",
    MODE_PAD: "Viền đen (pad) — giữ toàn bộ, thêm viền",
}

CROP_POSITIONS = {
    "center": "Giữa",
    "left": "Trái / trên",
    "right": "Phải / dưới",
}


def get_video_size(filepath: str) -> tuple[int, int]:
    """Trả về (width, height) của stream video đầu tiên."""
    data = probe_media(filepath)
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            w = int(stream.get("width") or 0)
            h = int(stream.get("height") or 0)
            if w > 0 and h > 0:
                return w, h
    return 0, 0


def _build_filter(
    mode: str,
    out_w: int,
    out_h: int,
    crop_position: str = "center",
) -> str:
    """Tạo chuỗi -vf / -filter_complex cho ffmpeg."""
    if mode == MODE_CROP:
        # Scale để phủ kín khung rồi crop
        if crop_position == "left":
            x, y = "0", "0"
        elif crop_position == "right":
            x, y = f"iw-{out_w}", f"ih-{out_h}"
        else:
            x, y = f"(iw-{out_w})/2", f"(ih-{out_h})/2"
        return (
            f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h}:{x}:{y}"
        )

    if mode == MODE_PAD:
        return (
            f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black"
        )

    # MODE_BLUR: nền blur + video gốc căn giữa
    # [0:v] split → blur scale full + foreground fit → overlay
    return (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
        f"crop={out_w}:{out_h},gblur=sigma=20[bg];"
        f"[fg]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )


def convert_aspect(
    input_path: str,
    output_path: str,
    *,
    out_w: int = 1080,
    out_h: int = 1920,
    mode: str = MODE_BLUR,
    crop_position: str = "center",
    log: Optional[Callable[[str], None]] = None,
) -> str:
    """
    Chuyển video sang tỉ lệ out_w×out_h.
    Trả về đường dẫn file đầu ra.
    """
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Không tìm thấy file: {input_path}")
    if not has_video_stream(input_path):
        raise ValueError(f"File không có stream video: {input_path}")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)

    filter_str = _build_filter(mode, out_w, out_h, crop_position)
    use_filter_complex = mode == MODE_BLUR

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        input_path,
    ]
    if use_filter_complex:
        cmd += ["-filter_complex", filter_str]
    else:
        cmd += ["-vf", filter_str]

    cmd += [
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

    if log:
        src_w, src_h = get_video_size(input_path)
        log(
            f"🔄 {os.path.basename(input_path)} ({src_w}×{src_h}) → "
            f"{out_w}×{out_h} [{MODE_LABELS.get(mode, mode)}]"
        )

    result = subprocess.run(cmd, capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg thất bại: {(result.stderr or result.stdout)[-500:]}")

    if not os.path.exists(output_path) or not has_video_stream(output_path):
        raise RuntimeError("Convert xong nhưng không tạo được file video hợp lệ.")

    if log:
        log(f"✅ Đã lưu: {output_path}")
    return output_path


def default_output_path(input_path: str, out_w: int, out_h: int, output_dir: str = "") -> str:
    base = os.path.splitext(os.path.basename(input_path))[0]
    folder = output_dir.strip() or os.path.dirname(os.path.abspath(input_path))
    return os.path.join(folder, f"{base}_{out_w}x{out_h}.mp4")


def list_videos_for_convert(folder: str) -> list[str]:
    """Liệt kê video trong thư mục, bỏ qua file đã convert (*_WxH.mp4)."""
    videos = list_local_videos(folder)
    result: list[str] = []
    for path in videos:
        name = os.path.basename(path)
        # Bỏ file đầu ra kiểu name_1080x1920.mp4
        stem, _ = os.path.splitext(name)
        if "_" in stem and stem.rsplit("_", 1)[-1].count("x") == 1:
            suffix = stem.rsplit("_", 1)[-1]
            parts = suffix.split("x")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                continue
        result.append(path)
    return result


__all__ = [
    "ASPECT_PRESETS",
    "MODE_BLUR",
    "MODE_CROP",
    "MODE_PAD",
    "MODE_LABELS",
    "CROP_POSITIONS",
    "VIDEO_EXTENSIONS",
    "convert_aspect",
    "default_output_path",
    "get_video_size",
    "list_videos_for_convert",
]
