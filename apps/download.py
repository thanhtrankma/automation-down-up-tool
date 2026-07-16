"""
apps/download.py - Tải video/playlist/kênh hàng loạt (yt-dlp)

Chạy: python3 main.py hoặc python3 -m apps.download
"""

from __future__ import annotations

import os
import queue
import ssl
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Optional
from urllib.parse import urlparse

# Khắc phục lỗi kinh điển trên macOS: bản Python cài từ python.org không dùng chung
# kho chứng chỉ (Keychain) của hệ điều hành, nên các kết nối HTTPS (vd. tới YouTube)
# có thể báo lỗi "CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate".
# Ta chủ động trỏ Python tới bộ chứng chỉ gốc của `certifi` NGAY TRƯỚC KHI import yt_dlp,
# để mọi request HTTPS bên trong yt-dlp đều xác thực SSL thành công mà không cần người
# dùng phải tự chạy "Install Certificates.command" trên từng máy.
try:
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    ssl._create_default_https_context = ssl.create_default_context  # đảm bảo dùng context chuẩn
except ImportError:
    # certifi chưa được cài (thiếu trong requirements) -> vẫn chạy tiếp, dùng kho chứng
    # chỉ mặc định của hệ thống, có thể vẫn lỗi SSL nếu môi trường chưa được cấu hình.
    pass

import yt_dlp
from yt_dlp.cookies import extract_cookies_from_browser
from yt_dlp.utils import DownloadCancelled

from services.douyin_auth import (
    DouyinAutomationError,
    download_douyin_videos,
    fetch_douyin_videos,
    get_playwright_cookies,
    has_saved_session,
    open_login_browser,
)
from services.downloader import COOKIE_BROWSER_CHOICES, extra_headers_for_url

# ---------------------------------------------------------------------------
# Cấu hình mặc định
# ---------------------------------------------------------------------------
DEFAULT_OUTPUT_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "VideoDownloader")
# Mẫu đặt tên thư mục/tệp đầu ra:
#   - Nếu link là Playlist/Kênh -> gom video vào thư mục trùng tên Playlist.
#   - Nếu là video đơn lẻ (không có playlist_title) -> dùng thư mục "Videos".
OUTPUT_TEMPLATE = os.path.join("%(playlist_title|Videos)s", "%(title)s.%(ext)s")

# YouTube (và một số nền tảng khác) thường có bản chất lượng cao nhất dùng codec
# VP9/AV1 (video) + Opus (audio). Các codec này hợp lệ trong container .mp4 về mặt
# kỹ thuật, NHƯNG QuickTime Player của Apple chỉ hỗ trợ H.264/HEVC (video) và AAC
# (audio) -> phát các file VP9/AV1+Opus sẽ báo lỗi "isn't compatible with QuickTime
# Player". Chuỗi format dưới đây ưu tiên tải bản H.264 + AAC để đảm bảo phát được
# trên QuickTime/iPhone/iPad, và chỉ rơi về "best" (có thể không tương thích) nếu
# video đó không có bản H.264 nào.
FORMAT_QUICKTIME_COMPATIBLE = (
    "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a][acodec^=mp4a]"
    "/bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]"
    "/best[ext=mp4][vcodec^=avc1]"
    "/best[ext=mp4]/best"
)
# Chất lượng cao nhất tuyệt đối, không quan tâm codec (có thể là VP9/AV1 + Opus,
# không tương thích QuickTime nhưng phát tốt trên VLC, trình duyệt, hầu hết app khác).
FORMAT_BEST_QUALITY = "bestvideo+bestaudio/best"
COOKIE_PREVIEW_LIMIT = 12


def _mask_cookie_value(value: str, *, keep: int = 6) -> str:
    """Che bớt giá trị cookie trong log để debug mà không lộ nguyên session."""
    if not value:
        return "(rỗng)"
    if len(value) <= keep * 2:
        return value[:keep] + "..."
    return f"{value[:keep]}...{value[-keep:]}"


def _summarize_browser_cookies(browser_name: str, url: str, *, douyin_only: bool = False) -> list[str]:
    """Đọc cookie từ trình duyệt và trả về các dòng log mô tả kết quả."""
    cookie_jar = extract_cookies_from_browser(browser_name)
    cookies = list(cookie_jar)
    if not cookies:
        return [f"Cookie {browser_name}: không đọc được cookie nào."]

    target_host = (urlparse(url).netloc or "").lower()
    target_hint = "douyin" if douyin_only else ""
    matched = [
        cookie
        for cookie in cookies
        if (target_hint and target_hint in (cookie.domain or "").lower())
        or (target_host and target_host in (cookie.domain or "").lower())
    ]
    if douyin_only and not matched:
        matched = [cookie for cookie in cookies if "douyin" in (cookie.domain or "").lower()]

    lines = [f"Cookie {browser_name}: đọc được {len(cookies)} cookie từ trình duyệt."]
    relevant = matched or cookies
    lines.append(
        f"Cookie liên quan tới {'Douyin' if douyin_only else 'URL hiện tại'}: {len(matched) if matched else 0}."
    )

    for cookie in relevant[:COOKIE_PREVIEW_LIMIT]:
        domain = cookie.domain or "(không rõ domain)"
        value_preview = _mask_cookie_value(cookie.value or "")
        lines.append(f" - {domain} | {cookie.name}={value_preview}")

    if len(relevant) > COOKIE_PREVIEW_LIMIT:
        lines.append(f" - ... và thêm {len(relevant) - COOKIE_PREVIEW_LIMIT} cookie khác")
    return lines


@dataclass
class ProgressMessage:
    """Gói dữ liệu tiến trình gửi từ luồng tải (worker thread) về luồng giao diện (main thread).

    Không được cập nhật trực tiếp widget tkinter từ thread phụ, vì vậy mọi thông tin
    tiến trình sẽ được đóng gói vào đây rồi đẩy vào hàng đợi (queue) để luồng chính xử lý.
    """

    kind: str  # "progress" | "status" | "log" | "finished" | "error"
    title: Optional[str] = None
    index: Optional[int] = None
    count: Optional[int] = None
    percent: Optional[float] = None
    text: Optional[str] = None


class DownloadCancelledFlag:
    """Cờ báo hiệu người dùng đã bấm Hủy, dùng chung giữa main thread và worker thread."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def request_cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def reset(self) -> None:
        self._event.clear()


class QueueLogger:
    """Chuyển log của yt-dlp về hàng đợi giao diện để dễ debug lỗi tải."""

    def __init__(self, emit) -> None:
        self._emit = emit

    def debug(self, msg: str) -> None:
        if msg and not msg.startswith("[debug] "):
            self._emit(f"yt-dlp: {msg}")

    def info(self, msg: str) -> None:
        if msg:
            self._emit(f"yt-dlp: {msg}")

    def warning(self, msg: str) -> None:
        if msg:
            self._emit(f"yt-dlp warning: {msg}")

    def error(self, msg: str) -> None:
        if msg:
            self._emit(f"yt-dlp error: {msg}")


class VideoDownloaderApp(tk.Toplevel):
    """Cửa sổ ứng dụng tải video hàng loạt."""

    def __init__(self, master, *, show_back: bool = False, douyin_login_mode: bool = False) -> None:
        super().__init__(master)

        self._douyin_login_mode = douyin_login_mode
        self._standalone_root = isinstance(master, tk.Tk) and not show_back

        self.title("Tải videos - yt-dlp" if not self._douyin_login_mode else "Tải Douyin - đăng nhập")
        self.geometry("600x380")
        self.minsize(600, 350)
        self.resizable(True, True)

        # Hàng đợi để worker thread gửi tiến trình về giao diện một cách an toàn.
        self._msg_queue: "queue.Queue[ProgressMessage]" = queue.Queue()
        self._cancel_flag = DownloadCancelledFlag()
        self._worker_thread: Optional[threading.Thread] = None

        self._setup_style()
        self._build_widgets(show_back=show_back)

        if self._standalone_root:
            self.protocol("WM_DELETE_WINDOW", self._close_standalone)
        else:
            self.transient(master)
            self.focus_set()

        # Vòng lặp định kỳ đọc hàng đợi để cập nhật giao diện (chạy trên main thread).
        self.after(100, self._poll_queue)

    def _close_standalone(self) -> None:
        self.destroy()
        self.master.quit()

    # ------------------------------------------------------------------
    # Xây dựng giao diện
    # ------------------------------------------------------------------
    def _setup_style(self) -> None:
        style = ttk.Style(self)
        # "clam" cho giao diện phẳng, hiện đại hơn theme mặc định trên hầu hết hệ điều hành.
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        bg_color = "#f4f6f8"
        accent_color = "#2f6fed"

        self.configure(bg=bg_color)
        style.configure("TFrame", background=bg_color)
        style.configure("TLabel", background=bg_color, foreground="#1f2933", font=("Segoe UI", 10))
        style.configure("Header.TLabel", font=("Segoe UI", 13, "bold"), foreground="#111827")
        style.configure("Status.TLabel", foreground="#4b5563", font=("Segoe UI", 9))
        style.configure("TButton", font=("Segoe UI", 10), padding=6)
        style.configure(
            "Accent.TButton",
            font=("Segoe UI", 10, "bold"),
            background=accent_color,
            foreground="white",
        )
        style.map("Accent.TButton", background=[("active", "#255bc7")])
        style.configure("TEntry", padding=4)
        style.configure(
            "Modern.Horizontal.TProgressbar",
            troughcolor="#e5e7eb",
            background=accent_color,
            thickness=16,
        )

    def _build_widgets(self, *, show_back: bool = False) -> None:
        if show_back:
            back_bar = ttk.Frame(self, padding=(12, 8, 12, 0))
            back_bar.pack(fill=tk.X)
            ttk.Button(back_bar, text="← Về menu", command=self.destroy).pack(side=tk.LEFT)

        root = ttk.Frame(self, padding=16)
        root.pack(fill=tk.BOTH, expand=True)

        # --- Tiêu đề ---
        header = ttk.Label(root, text="Tải Video / Playlist / Kênh hàng loạt", style="Header.TLabel")
        header.pack(anchor="w", pady=(0, 4))
        ttk.Label(
            root,
            text="Hỗ trợ YouTube, TikTok, Facebook, Douyin, Bilibili...",
            style="Status.TLabel",
        ).pack(anchor="w", pady=(0, 10))

        if self._douyin_login_mode:
            ttk.Label(
                root,
                text="Douyin sẽ mở Chromium riêng để đăng nhập và lấy list video từ profile.",
                style="Status.TLabel",
            ).pack(anchor="w", pady=(0, 10))

        # --- Hàng nhập URL ---
        url_frame = ttk.Frame(root)
        url_frame.pack(fill=tk.X, pady=4)
        ttk.Label(url_frame, text="Đường dẫn (Video / Playlist / Kênh):").pack(anchor="w")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(url_frame, textvariable=self.url_var)
        self.url_entry.pack(fill=tk.X, pady=(4, 0))

        # --- Hàng chọn thư mục lưu ---
        folder_frame = ttk.Frame(root)
        folder_frame.pack(fill=tk.X, pady=8)
        ttk.Label(folder_frame, text="Thư mục lưu video:").pack(anchor="w")

        folder_input_row = ttk.Frame(folder_frame)
        folder_input_row.pack(fill=tk.X, pady=(4, 0))
        self.output_dir_var = tk.StringVar(value=DEFAULT_OUTPUT_DIR)
        self.output_entry = ttk.Entry(folder_input_row, textvariable=self.output_dir_var)
        self.output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(folder_input_row, text="Chọn...", command=self._browse_folder).pack(
            side=tk.LEFT, padx=(6, 0)
        )

        # --- Tùy chọn ---
        options_frame = ttk.Frame(root)
        options_frame.pack(fill=tk.X, pady=4)
        self.audio_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            options_frame, text="Chỉ tải âm thanh (MP3)", variable=self.audio_only_var
        ).pack(side=tk.LEFT)

        # Mặc định bật để tránh lỗi "isn't compatible with QuickTime Player" khi phát
        # trên macOS/iPhone/iPad. Người dùng có thể tắt để lấy chất lượng cao nhất tuyệt đối.
        self.quicktime_compat_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            options_frame,
            text="Ưu tiên tương thích QuickTime/Apple (H.264 + AAC)",
            variable=self.quicktime_compat_var,
        ).pack(side=tk.LEFT, padx=(12, 0))

        if self._douyin_login_mode:
            douyin_login_hint = ttk.Label(
                root,
                text="Bước nhanh: (1) Đăng nhập Douyin bằng Chromium. (2) Dán link trang user/video. (3) Bắt đầu tải.",
                style="Status.TLabel",
                foreground="#92400e",
            )
            douyin_login_hint.pack(anchor="w", pady=(6, 0))

            self.douyin_login_button = ttk.Button(
                root,
                text="Đăng nhập Douyin (Playwright)",
                command=self._start_douyin_login,
            )
            self.douyin_login_button.pack(anchor="w", pady=(6, 0))
            self.douyin_session_var = tk.StringVar(
                value="Session: đã có" if has_saved_session() else "Session: chưa đăng nhập"
            )
            ttk.Label(root, textvariable=self.douyin_session_var, style="Status.TLabel").pack(anchor="w", pady=(4, 0))
        else:
            # --- Cookie trình duyệt (chỉ cần khi Douyin/Bilibili/video riêng tư báo lỗi 403/login) ---
            cookie_frame = ttk.Frame(root)
            cookie_frame.pack(fill=tk.X, pady=(4, 0))
            ttk.Label(cookie_frame, text="Cookie trình duyệt (nếu bị chặn/yêu cầu đăng nhập):").pack(side=tk.LEFT)
            self.cookies_browser_var = tk.StringVar(value="Không dùng")
            ttk.Combobox(
                cookie_frame,
                textvariable=self.cookies_browser_var,
                values=list(COOKIE_BROWSER_CHOICES.keys()),
                state="readonly",
                width=12,
            ).pack(side=tk.LEFT, padx=(8, 0))

        # --- Nút hành động ---
        action_frame = ttk.Frame(root)
        action_frame.pack(fill=tk.X, pady=8)
        self.start_button = ttk.Button(
            action_frame, text="Bắt đầu tải", style="Accent.TButton", command=self._start_download
        )
        self.start_button.pack(side=tk.LEFT)
        self.cancel_button = ttk.Button(
            action_frame, text="Hủy", command=self._cancel_download, state=tk.DISABLED
        )
        self.cancel_button.pack(side=tk.LEFT, padx=(8, 0))

        # --- Thanh tiến độ ---
        progress_frame = ttk.Frame(root)
        progress_frame.pack(fill=tk.X, pady=(8, 4))
        self.progress_bar = ttk.Progressbar(
            progress_frame,
            style="Modern.Horizontal.TProgressbar",
            orient="horizontal",
            mode="determinate",
            maximum=100,
        )
        self.progress_bar.pack(fill=tk.X)

        # --- Nhãn trạng thái (tên video, số thứ tự, %) ---
        self.status_var = tk.StringVar(value="Sẵn sàng.")
        ttk.Label(root, textvariable=self.status_var, style="Status.TLabel").pack(
            anchor="w", pady=(2, 6)
        )

        # --- Khung log ---
        log_frame = ttk.Frame(root)
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=6, state=tk.DISABLED, font=("Consolas", 9), wrap=tk.WORD
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

    # ------------------------------------------------------------------
    # Xử lý sự kiện giao diện
    # ------------------------------------------------------------------
    def _browse_folder(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.output_dir_var.get() or os.getcwd())
        if chosen:
            self.output_dir_var.set(chosen)

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _queue_log(self, text: str) -> None:
        self._msg_queue.put(ProgressMessage(kind="log", text=text))

    def _start_douyin_login(self) -> None:
        if not self._douyin_login_mode:
            return
        self.douyin_login_button.configure(state=tk.DISABLED)
        self._queue_log("Đang mở Chromium để đăng nhập Douyin...")
        threading.Thread(target=self._douyin_login_worker, daemon=True).start()

    def _douyin_login_worker(self) -> None:
        try:
            open_login_browser(on_log=self._queue_log)
            self._msg_queue.put(ProgressMessage(kind="status", text="Session Douyin đã được lưu."))
        except Exception as exc:  # noqa: BLE001
            self._msg_queue.put(ProgressMessage(kind="error", text=f"Lỗi đăng nhập Douyin: {exc}"))
        finally:
            self._msg_queue.put(ProgressMessage(kind="status", text="__douyin_login_done__"))

    def _start_download(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Thiếu thông tin", "Vui lòng nhập đường dẫn video/playlist/kênh.")
            return

        output_dir = self.output_dir_var.get().strip() or DEFAULT_OUTPUT_DIR
        os.makedirs(output_dir, exist_ok=True)

        # Khóa các control liên quan trong lúc tải để tránh người dùng bấm chồng lệnh.
        self.start_button.configure(state=tk.DISABLED)
        self.cancel_button.configure(state=tk.NORMAL)
        self.url_entry.configure(state=tk.DISABLED)
        self.output_entry.configure(state=tk.DISABLED)
        self.progress_bar["value"] = 0
        self.status_var.set("Đang chuẩn bị tải...")
        self._cancel_flag.reset()

        audio_only = self.audio_only_var.get()
        quicktime_compat = self.quicktime_compat_var.get()
        cookies_browser = ""
        if not self._douyin_login_mode:
            cookies_browser = COOKIE_BROWSER_CHOICES.get(self.cookies_browser_var.get(), "")

        if self._douyin_login_mode and not has_saved_session():
            messagebox.showwarning(
                "Cần Session Douyin",
                "Hãy bấm 'Đăng nhập Douyin (Playwright)' và đăng nhập xong rồi mới tải.",
            )
            self._on_download_finished()
            return

        # Quan trọng: chạy tác vụ tải trong một thread riêng (daemon=True) để không làm
        # đơ giao diện chính (main thread) của tkinter trong lúc yt-dlp tải dữ liệu.
        self._worker_thread = threading.Thread(
            target=self._download_worker,
            args=(url, output_dir, audio_only, quicktime_compat, cookies_browser),
            daemon=True,
        )
        self._worker_thread.start()

    def _cancel_download(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            self._cancel_flag.request_cancel()
            self.status_var.set("Đang hủy... vui lòng chờ tác vụ hiện tại kết thúc.")
            self.cancel_button.configure(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # Luồng tải (chạy trong background thread, KHÔNG được đụng trực tiếp vào widget)
    # ------------------------------------------------------------------
    def _download_worker(
        self,
        url: str,
        output_dir: str,
        audio_only: bool,
        quicktime_compat: bool,
        cookies_browser: str = "",
    ) -> None:
        if self._douyin_login_mode:
            self._douyin_download_worker(url, output_dir, audio_only)
            return

        outtmpl = os.path.join(output_dir, OUTPUT_TEMPLATE)
        download_targets = [url]
        file_count_before = 0

        def emit_log(text: str) -> None:
            self._msg_queue.put(ProgressMessage(kind="log", text=text))

        for _, _, files in os.walk(output_dir):
            file_count_before += len(files)

        ydl_opts = {
            "outtmpl": outtmpl,
            # Bỏ qua lỗi từng video (private, dính bản quyền, bị gỡ...) để không dừng
            # đột ngột toàn bộ playlist/kênh đang tải.
            "ignoreerrors": True,
            "noplaylist": False,  # cho phép tải playlist/kênh đầy đủ nếu link là playlist/kênh
            "progress_hooks": [self._make_progress_hook()],
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": False,
            "logger": QueueLogger(emit_log),
        }

        # Douyin/Bilibili đôi khi chặn request thiếu Referer đúng domain (403).
        # Không ảnh hưởng tới các site khác vì chỉ set khi domain khớp.
        headers = extra_headers_for_url(url)
        if headers:
            ydl_opts["http_headers"] = headers

        # Chỉ áp dụng cookie trình duyệt khi người dùng chọn rõ trong giao diện
        # (vd. video riêng tư/yêu cầu đăng nhập trên Douyin/Bilibili).
        if cookies_browser:
            ydl_opts["cookiesfrombrowser"] = (cookies_browser,)
            try:
                for line in _summarize_browser_cookies(
                    cookies_browser,
                    url,
                    douyin_only=self._douyin_login_mode,
                ):
                    self._msg_queue.put(ProgressMessage(kind="log", text=line))
            except Exception as exc:  # noqa: BLE001 - cần biết vì sao không lấy được cookie
                self._msg_queue.put(
                    ProgressMessage(
                        kind="log",
                        text=f"Không thể đọc cookie từ {cookies_browser}: {exc}",
                    )
                )

        if audio_only:
            ydl_opts.update(
                {
                    "format": "bestaudio/best",
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "192",
                        }
                    ],
                }
            )
        else:
            # Chọn chuỗi format phù hợp: ưu tiên H.264+AAC (phát được trên QuickTime/Apple)
            # hoặc chất lượng cao nhất tuyệt đối (có thể là VP9/AV1+Opus), tùy tùy chọn người dùng.
            chosen_format = FORMAT_QUICKTIME_COMPATIBLE if quicktime_compat else FORMAT_BEST_QUALITY
            ydl_opts.update({"format": chosen_format, "merge_output_format": "mp4"})
            if quicktime_compat:
                # Sắp xếp lại ưu tiên codec ngay cả khi format selector phải rơi vào "best"
                # ở các nền tảng không expose vcodec rõ ràng (TikTok, Facebook...).
                ydl_opts["format_sort"] = ["vcodec:h264", "acodec:aac", "ext:mp4:m4a", "res"]

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                result_code = ydl.download(download_targets)
            file_count_after = 0
            for _, _, files in os.walk(output_dir):
                file_count_after += len(files)
            created_files = max(0, file_count_after - file_count_before)
            emit_log(f"yt-dlp result code: {result_code}")
            emit_log(f"Số file mới trong thư mục đích: {created_files}")
            if self._cancel_flag.is_cancelled():
                self._msg_queue.put(ProgressMessage(kind="log", text="Đã hủy tải theo yêu cầu."))
            else:
                if created_files == 0:
                    emit_log("Không phát hiện file mới. Nhiều khả năng tất cả URL đã lỗi hoặc bị bỏ qua; hãy xem các dòng `yt-dlp error` phía trên.")
                self._msg_queue.put(ProgressMessage(kind="log", text="Hoàn tất toàn bộ tác vụ tải."))
        except DownloadCancelled:
            self._msg_queue.put(ProgressMessage(kind="log", text="Đã hủy tải theo yêu cầu."))
        except Exception as exc:  # noqa: BLE001 - cần bắt mọi lỗi để không crash worker thread
            err_text = str(exc)
            if not cookies_browser and any(k in err_text.lower() for k in ("403", "login", "cookies")):
                err_text += "\n💡 Gợi ý: thử chọn Cookie trình duyệt (Safari/Chrome) rồi tải lại."
            self._msg_queue.put(ProgressMessage(kind="error", text=f"Lỗi: {err_text}"))
        finally:
            self._msg_queue.put(ProgressMessage(kind="finished"))

    def _douyin_download_worker(self, url: str, output_dir: str, audio_only: bool) -> None:
        def emit_log(text: str) -> None:
            self._msg_queue.put(ProgressMessage(kind="log", text=text))

        if audio_only:
            emit_log("Chế độ Douyin hiện chỉ hỗ trợ tải video MP4 trực tiếp từ API.")

        try:
            cookies = get_playwright_cookies()
            emit_log(f"Đã đọc {len(cookies)} cookie từ session Playwright.")
            items = fetch_douyin_videos(url, on_log=emit_log)
            emit_log(f"Chuẩn bị tải {len(items)} video bằng link trực tiếp từ API Douyin.")
            for index, item in enumerate(items[:5], start=1):
                emit_log(f"Mẫu {index}: {item.title} -> {item.download_urls[0][:80]}...")
            if len(items) > 5:
                emit_log(f"... và còn {len(items) - 5} video khác")

            downloaded_count = download_douyin_videos(
                items,
                output_dir,
                cookies,
                on_log=emit_log,
                on_progress=lambda title, percent: self._msg_queue.put(
                    ProgressMessage(kind="progress", title=title, percent=percent)
                ),
                is_cancelled=self._cancel_flag.is_cancelled,
            )
            emit_log(f"Đã tải thành công {downloaded_count}/{len(items)} video.")
            if downloaded_count == 0:
                emit_log("Không có video nào được tải. Hãy kiểm tra session đăng nhập Douyin.")
        except DownloadCancelled:
            emit_log("Đã hủy tải theo yêu cầu.")
        except DouyinAutomationError as exc:
            self._msg_queue.put(ProgressMessage(kind="error", text=f"Lỗi Douyin automation: {exc}"))
        except Exception as exc:  # noqa: BLE001
            self._msg_queue.put(ProgressMessage(kind="error", text=f"Lỗi: {exc}"))
        finally:
            self._msg_queue.put(ProgressMessage(kind="finished"))

    def _make_progress_hook(self):
        """Tạo hàm progress_hook cho yt-dlp.

        Hàm này được yt-dlp gọi liên tục trong lúc tải (trên worker thread) với một
        dict chứa trạng thái hiện tại. Ta chỉ đóng gói dữ liệu cần thiết và đẩy vào
        hàng đợi, việc cập nhật widget thật sự sẽ do main thread (`_poll_queue`) đảm nhiệm.
        """

        def hook(d: dict) -> None:
            # Nếu người dùng đã bấm Hủy, ném DownloadCancelled để yt-dlp dừng ngay lập tức.
            if self._cancel_flag.is_cancelled():
                raise DownloadCancelled("Người dùng đã hủy tác vụ tải.")

            status = d.get("status")
            info = d.get("info_dict", {}) or {}
            title = info.get("title") or os.path.basename(d.get("filename", ""))
            playlist_index = info.get("playlist_index")
            playlist_count = info.get("n_entries") or info.get("playlist_count")

            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes") or 0
                percent = (downloaded / total * 100) if total else 0.0
                self._msg_queue.put(
                    ProgressMessage(
                        kind="progress",
                        title=title,
                        index=playlist_index,
                        count=playlist_count,
                        percent=percent,
                    )
                )
            elif status == "finished":
                self._msg_queue.put(
                    ProgressMessage(
                        kind="log",
                        text=f"Đã tải xong: {title}",
                    )
                )
            elif status == "error":
                self._msg_queue.put(ProgressMessage(kind="log", text=f"Lỗi khi tải: {title}"))

        return hook

    # ------------------------------------------------------------------
    # Đọc hàng đợi & cập nhật giao diện (chạy định kỳ trên main thread)
    # ------------------------------------------------------------------
    def _poll_queue(self) -> None:
        try:
            while True:
                message = self._msg_queue.get_nowait()
                self._handle_message(message)
        except queue.Empty:
            pass
        finally:
            # Lên lịch gọi lại chính nó sau 100ms, tạo thành vòng lặp cập nhật giao diện.
            self.after(100, self._poll_queue)

    def _handle_message(self, message: ProgressMessage) -> None:
        if message.kind == "progress":
            self.progress_bar["value"] = message.percent or 0

            index_part = ""
            if message.index and message.count:
                index_part = f"[{message.index}/{message.count}] "

            self.status_var.set(
                f"{index_part}Đang tải: {message.title} - {message.percent:.1f}%"
            )
        elif message.kind == "log":
            self._append_log(message.text or "")
        elif message.kind == "status":
            text = message.text or ""
            if text == "__douyin_login_done__":
                if self._douyin_login_mode:
                    self.douyin_login_button.configure(state=tk.NORMAL)
                    self.douyin_session_var.set("Session: đã có" if has_saved_session() else "Session: chưa đăng nhập")
            else:
                self.status_var.set(text)
                self._append_log(text)
        elif message.kind == "error":
            self._append_log(message.text or "")
            messagebox.showerror("Lỗi", message.text or "Đã xảy ra lỗi không xác định.")
        elif message.kind == "finished":
            self._on_download_finished()

    def _on_download_finished(self) -> None:
        self.start_button.configure(state=tk.NORMAL)
        self.cancel_button.configure(state=tk.DISABLED)
        self.url_entry.configure(state=tk.NORMAL)
        self.output_entry.configure(state=tk.NORMAL)
        if self._douyin_login_mode:
            self.douyin_login_button.configure(state=tk.NORMAL)
        if not self._cancel_flag.is_cancelled():
            self.status_var.set("Hoàn tất.")
            self.progress_bar["value"] = 100
        else:
            self.status_var.set("Đã hủy.")


def main() -> None:
    root = tk.Tk()
    root.withdraw()
    VideoDownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
