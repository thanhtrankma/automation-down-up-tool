"""
apps/pipeline.py - Quy trình tự động: Tải → Generate → Upload YouTube

Chạy: python3 main.py hoặc python3 -m apps.pipeline
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from tkinter import filedialog, messagebox, scrolledtext, ttk

from services.config_store import load_config, save_config
from services.downloader import DownloadCancelledFlag, download_videos
from services.ollama_client import extract_keywords, fetch_models, generate_description, generate_title, sanitize_youtube_title
from services.pending_uploads import add_pending, count_pending, list_pending, remove_pending
from services.video_prep import list_local_videos, prepare_for_youtube
from youtube.uploader import (
    CATEGORIES,
    UPLOAD_LIMIT_HELP,
    UploadLimitExceededError,
    get_active_account_id,
    get_active_account_label,
    get_authenticated_service,
    list_accounts,
    login_new_account,
    remove_account,
    set_active_account,
    sync_accounts_from_disk,
    upload_video,
)

try:
    from yt_dlp.utils import DownloadCancelled
except ImportError:
    DownloadCancelled = Exception  # type: ignore[misc, assignment]

PRIVACY_LABELS = {
    "private": "Riêng tư (Private)",
    "unlisted": "Không công khai (Unlisted)",
    "public": "Công khai (Public)",
}

AUDIENCE_LABELS = {
    False: "Không, video này KHÔNG dành cho trẻ em",
    True: "Có, video này dành cho trẻ em",
}


@dataclass
class PipelineMessage:
    kind: str  # log | progress | status | video_done | finished | error
    text: str = ""
    percent: float = 0.0


class PipelineApp(tk.Toplevel):
    def __init__(self, master, *, show_back: bool = False) -> None:
        super().__init__(master)

        self._standalone_root = isinstance(master, tk.Tk) and not show_back
        self._show_back = show_back

        self.title("Quy trình tự động - Tải & Đăng YouTube")
        self.geometry("900x820")
        self.minsize(800, 700)

        self.config_data = load_config()
        self._msg_queue: queue.Queue[PipelineMessage] = queue.Queue()
        self._cancel_flag = DownloadCancelledFlag()
        self._worker_thread: threading.Thread | None = None
        self._youtube_service = None
        self._scheduled_count = 0
        self._pipeline_running = False
        self._account_label_to_id: dict[str, str] = {}
        self._uploads_this_run = 0
        self._stop_uploads = False
        self._upload_limit_hit = False
        self._blocked_account_ids: set[str] = set()

        self._build_ui()
        self._refresh_accounts_ui()
        self._update_pending_button()
        self._load_ollama_models_async()

        if self._standalone_root:
            self.protocol("WM_DELETE_WINDOW", self._close_standalone)
        else:
            self.transient(master)
            self.focus_set()

        self.after(100, self._poll_queue)

    def _close_standalone(self) -> None:
        self.destroy()
        self.master.quit()

    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 5}

        if self._show_back:
            back_bar = ttk.Frame(self, padding=(10, 8, 10, 0))
            back_bar.pack(fill=tk.X)
            ttk.Button(back_bar, text="← Về menu", command=self.destroy).pack(side=tk.LEFT)

        # Thanh dưới luôn hiển thị: progress, trạng thái, nút bắt đầu
        bottom = ttk.Frame(self, padding=(10, 0, 10, 8))
        bottom.pack(side=tk.BOTTOM, fill=tk.X)

        prog_frame = ttk.Frame(bottom)
        prog_frame.pack(fill=tk.X, pady=(0, 4))
        prog_frame.columnconfigure(0, weight=1)
        self.progress_bar = ttk.Progressbar(prog_frame, mode="determinate", maximum=100)
        self.progress_bar.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.progress_label = ttk.Label(prog_frame, text="0%")
        self.progress_label.grid(row=0, column=1)

        self.status_var = tk.StringVar(value="Sẵn sàng.")
        ttk.Label(bottom, textvariable=self.status_var).pack(anchor="w", pady=(0, 4))

        action = ttk.Frame(bottom)
        action.pack(fill=tk.X)
        self.start_btn = ttk.Button(action, text="▶ Bắt đầu quy trình", command=self._start_pipeline)
        self.start_btn.pack(side=tk.LEFT)
        self.cancel_btn = ttk.Button(action, text="Hủy", command=self._cancel_pipeline, state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.pending_btn = ttk.Button(action, text="📤 Upload hàng chờ (0)", command=self._start_pending_uploads)
        self.pending_btn.pack(side=tk.LEFT, padx=(8, 0))

        root = self._create_scrollable_frame(self)

        # --- Nguồn video ---
        source_frame = ttk.LabelFrame(root, text="Nguồn video")
        source_frame.pack(fill=tk.X, **pad)

        mode_row = ttk.Frame(source_frame)
        mode_row.pack(fill=tk.X, padx=8, pady=(8, 4))
        self.source_mode_var = tk.StringVar(value="url")
        ttk.Radiobutton(
            mode_row, text="Tải từ link", variable=self.source_mode_var, value="url", command=self._toggle_source_mode
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            mode_row,
            text="Lấy từ thư mục máy",
            variable=self.source_mode_var,
            value="folder",
            command=self._toggle_source_mode,
        ).pack(side=tk.LEFT, padx=(16, 0))

        self.url_row = ttk.Frame(source_frame)
        self.url_row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(self.url_row, text="Link video / playlist / kênh:").pack(anchor="w")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(self.url_row, textvariable=self.url_var)
        self.url_entry.pack(fill=tk.X, pady=(4, 0))

        self.folder_row = ttk.Frame(source_frame)
        self.folder_row.pack(fill=tk.X, padx=8, pady=(4, 8))
        self.folder_row.columnconfigure(0, weight=1)
        ttk.Label(self.folder_row, text="Thư mục chứa video sẵn có:").grid(row=0, column=0, columnspan=2, sticky="w")
        self.local_source_var = tk.StringVar(value=self.config_data.get("local_source_dir", ""))
        self.local_source_entry = ttk.Entry(self.folder_row, textvariable=self.local_source_var)
        self.local_source_entry.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(self.folder_row, text="Chọn thư mục...", command=self._browse_local_source).grid(
            row=1, column=1, padx=(6, 0), pady=(4, 0)
        )
        self._toggle_source_mode()

        # --- Tài khoản YouTube ---
        account_frame = ttk.LabelFrame(root, text="Tài khoản YouTube")
        account_frame.pack(fill=tk.X, **pad)
        account_frame.columnconfigure(1, weight=1)

        ttk.Label(account_frame, text="Tài khoản đang dùng:").grid(row=0, column=0, sticky="w", padx=8, pady=8)
        self.account_var = tk.StringVar()
        self.account_combo = ttk.Combobox(
            account_frame, textvariable=self.account_var, state="readonly", width=40
        )
        self.account_combo.grid(row=0, column=1, sticky="ew", padx=8, pady=8)
        self.account_combo.bind("<<ComboboxSelected>>", self._on_account_selected)

        account_btns = ttk.Frame(account_frame)
        account_btns.grid(row=0, column=2, sticky="e", padx=8, pady=8)
        ttk.Button(account_btns, text="➕ Đăng nhập mới", command=self._login_new_account).pack(side=tk.LEFT)
        ttk.Button(account_btns, text="🗑️ Xóa", command=self._remove_current_account).pack(side=tk.LEFT, padx=(6, 0))

        self.current_account_label = ttk.Label(account_frame, text="Chưa đăng nhập")
        self.current_account_label.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))

        # --- Cấu hình ---
        cfg = ttk.LabelFrame(root, text="Cấu hình")
        cfg.pack(fill=tk.X, **pad)
        cfg.columnconfigure(1, weight=1)
        cfg.columnconfigure(3, weight=1)

        row = 0
        ttk.Label(cfg, text="Thư mục lưu video:").grid(row=row, column=0, sticky="w", padx=8, pady=4)
        self.output_dir_var = tk.StringVar(value=self.config_data["output_dir"])
        out_row = ttk.Frame(cfg)
        out_row.grid(row=row, column=1, columnspan=3, sticky="ew", padx=8, pady=4)
        out_row.columnconfigure(0, weight=1)
        ttk.Entry(out_row, textvariable=self.output_dir_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(out_row, text="Chọn...", command=self._browse_output).grid(row=0, column=1, padx=(6, 0))

        row += 1
        ttk.Label(cfg, text="Khoảng cách đăng (giờ):").grid(row=row, column=0, sticky="w", padx=8, pady=4)
        self.interval_hours_var = tk.StringVar(value=str(self.config_data["schedule_interval_hours"]))
        ttk.Entry(cfg, textvariable=self.interval_hours_var, width=8).grid(row=row, column=1, sticky="w", padx=8, pady=4)

        ttk.Label(cfg, text="Video đầu sau (phút):").grid(row=row, column=2, sticky="w", padx=8, pady=4)
        self.first_offset_var = tk.StringVar(value=str(self.config_data["first_publish_offset_minutes"]))
        ttk.Entry(cfg, textvariable=self.first_offset_var, width=8).grid(row=row, column=3, sticky="w", padx=8, pady=4)

        row += 1
        self.generate_title_var = tk.BooleanVar(value=bool(self.config_data.get("generate_title", True)))
        ttk.Checkbutton(
            cfg,
            text="Generate tiêu đề bằng Ollama (bỏ chọn = dùng tên file)",
            variable=self.generate_title_var,
            command=self._toggle_generate_title,
        ).grid(row=row, column=0, columnspan=4, sticky="w", padx=8, pady=4)

        row += 1
        ttk.Label(cfg, text="Hướng dẫn tiêu đề (Ollama):").grid(row=row, column=0, sticky="nw", padx=8, pady=4)
        self.title_instruction_var = tk.StringVar(value=self.config_data["title_instruction"])
        self.title_instruction_entry = ttk.Entry(cfg, textvariable=self.title_instruction_var)
        self.title_instruction_entry.grid(row=row, column=1, columnspan=3, sticky="ew", padx=8, pady=4)
        self._toggle_generate_title()

        row += 1
        ttk.Label(cfg, text="Tags mặc định:").grid(row=row, column=0, sticky="w", padx=8, pady=4)
        self.tags_var = tk.StringVar(value=self.config_data["default_tags"])
        ttk.Entry(cfg, textvariable=self.tags_var).grid(row=row, column=1, columnspan=3, sticky="ew", padx=8, pady=4)

        row += 1
        ttk.Label(cfg, text="Danh mục:").grid(row=row, column=0, sticky="w", padx=8, pady=4)
        self.category_var = tk.StringVar(value=self.config_data["category"])
        ttk.Combobox(cfg, textvariable=self.category_var, values=list(CATEGORIES.keys()), state="readonly").grid(
            row=row, column=1, sticky="ew", padx=8, pady=4
        )

        ttk.Label(cfg, text="Chế độ hiển thị:").grid(row=row, column=2, sticky="w", padx=8, pady=4)
        privacy_key = self.config_data["privacy"]
        self.privacy_var = tk.StringVar(value=PRIVACY_LABELS.get(privacy_key, PRIVACY_LABELS["private"]))
        ttk.Combobox(cfg, textvariable=self.privacy_var, values=list(PRIVACY_LABELS.values()), state="readonly").grid(
            row=row, column=3, sticky="ew", padx=8, pady=4
        )

        row += 1
        ttk.Label(cfg, text="Đối tượng người xem:").grid(row=row, column=0, sticky="w", padx=8, pady=4)
        self.audience_var = tk.StringVar(
            value=AUDIENCE_LABELS[bool(self.config_data["made_for_kids"])]
        )
        ttk.Combobox(cfg, textvariable=self.audience_var, values=list(AUDIENCE_LABELS.values()), state="readonly").grid(
            row=row, column=1, sticky="ew", padx=8, pady=4
        )

        ttk.Label(cfg, text="Model Ollama:").grid(row=row, column=2, sticky="w", padx=8, pady=4)
        self.ollama_model_var = tk.StringVar(value=self.config_data["ollama_model"])
        self.ollama_combo = ttk.Combobox(
            cfg, textvariable=self.ollama_model_var, values=[self.config_data["ollama_model"]], state="readonly"
        )
        self.ollama_combo.grid(row=row, column=3, sticky="ew", padx=8, pady=4)

        row += 1
        ttk.Label(cfg, text="Giới hạn upload/lần chạy:").grid(row=row, column=0, sticky="w", padx=8, pady=4)
        self.max_uploads_var = tk.StringVar(value=str(self.config_data.get("max_uploads_per_run", 0)))
        ttk.Entry(cfg, textvariable=self.max_uploads_var, width=8).grid(row=row, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(cfg, text="(0 = không giới hạn)").grid(row=row, column=2, sticky="w", padx=8, pady=4)

        self.auto_switch_account_var = tk.BooleanVar(
            value=bool(self.config_data.get("auto_switch_account_on_limit", True))
        )
        ttk.Checkbutton(
            cfg,
            text="Tự chuyển tài khoản khi hết hạn mức",
            variable=self.auto_switch_account_var,
        ).grid(row=row, column=3, sticky="w", padx=8, pady=4)

        row += 1
        self.quicktime_var = tk.BooleanVar(value=self.config_data["quicktime_compat"])
        ttk.Checkbutton(cfg, text="Ưu tiên tương thích QuickTime (H.264+AAC)", variable=self.quicktime_var).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=8, pady=4
        )
        ttk.Button(cfg, text="💾 Lưu cấu hình", command=self._save_config).grid(
            row=row, column=3, sticky="e", padx=8, pady=4
        )

        # --- Hàng đợi video đã xử lý ---
        queue_frame = ttk.LabelFrame(root, text="Tiến trình video")
        queue_frame.pack(fill=tk.BOTH, expand=True, **pad)

        cols = ("stt", "title", "schedule", "status")
        self.video_tree = ttk.Treeview(queue_frame, columns=cols, show="headings", height=4)
        self.video_tree.heading("stt", text="#")
        self.video_tree.heading("title", text="Tiêu đề")
        self.video_tree.heading("schedule", text="Lịch đăng (local)")
        self.video_tree.heading("status", text="Trạng thái")
        self.video_tree.column("stt", width=40, anchor="center")
        self.video_tree.column("title", width=320)
        self.video_tree.column("schedule", width=180)
        self.video_tree.column("status", width=200)
        self.video_tree.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self.log_box = scrolledtext.ScrolledText(root, height=6, state="disabled", wrap="word")
        self.log_box.pack(fill=tk.BOTH, expand=False, **pad)

    def _create_scrollable_frame(self, parent: tk.Misc) -> ttk.Frame:
        """Tạo vùng nội dung có thể cuộn; trả về frame bên trong để gắn widget."""
        container = ttk.Frame(parent)
        container.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=canvas.yview)
        scroll_frame = ttk.Frame(canvas, padding=12)
        canvas_window = canvas.create_window((0, 0), window=scroll_frame, anchor="nw")

        def _on_frame_configure(_event: tk.Event) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event: tk.Event) -> None:
            canvas.itemconfigure(canvas_window, width=event.width)

        scroll_frame.bind("<Configure>", _on_frame_configure)
        canvas.bind("<Configure>", _on_canvas_configure)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        def _on_mousewheel(event: tk.Event) -> None:
            if event.delta:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            elif getattr(event, "num", None) == 4:
                canvas.yview_scroll(-1, "units")
            elif getattr(event, "num", None) == 5:
                canvas.yview_scroll(1, "units")

        def _bind_scroll(_event: tk.Event) -> None:
            canvas.bind_all("<MouseWheel>", _on_mousewheel)
            canvas.bind_all("<Button-4>", _on_mousewheel)
            canvas.bind_all("<Button-5>", _on_mousewheel)

        def _unbind_scroll(_event: tk.Event) -> None:
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")

        canvas.bind("<Enter>", _bind_scroll)
        canvas.bind("<Leave>", _unbind_scroll)
        scroll_frame.bind("<Enter>", _bind_scroll)
        scroll_frame.bind("<Leave>", _unbind_scroll)

        return scroll_frame

    def _browse_output(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.output_dir_var.get() or os.getcwd())
        if chosen:
            self.output_dir_var.set(chosen)

    def _browse_local_source(self) -> None:
        chosen = filedialog.askdirectory(
            initialdir=self.local_source_var.get() or self.output_dir_var.get() or os.getcwd()
        )
        if chosen:
            self.local_source_var.set(chosen)

    def _refresh_accounts_ui(self) -> None:
        try:
            sync_accounts_from_disk()
        except Exception as exc:  # noqa: BLE001
            self._log(f"⚠️ Không đồng bộ được tài khoản: {exc}")

        self.config_data = load_config()
        accounts = list_accounts()
        self._account_label_to_id = {}
        labels: list[str] = []
        seen: dict[str, int] = {}

        for acc in accounts:
            label = acc["label"]
            if label in seen:
                seen[label] += 1
                label = f"{label} ({acc['id'][-6:]})"
            else:
                seen[acc["label"]] = 1
            self._account_label_to_id[label] = acc["id"]
            labels.append(label)

        # macOS ttk.Combobox readonly cần tạm mở state để cập nhật values
        self.account_combo.configure(state="normal")
        self.account_combo["values"] = labels

        active_id = self.config_data.get("youtube_account_id") or get_active_account_id()
        active_label = ""
        for display_label, acc_id in self._account_label_to_id.items():
            if acc_id == active_id:
                active_label = display_label
                break

        if not active_label and accounts:
            active_id = accounts[0]["id"]
            set_active_account(active_id)
            self.config_data["youtube_account_id"] = active_id
            save_config(self.config_data)
            for display_label, acc_id in self._account_label_to_id.items():
                if acc_id == active_id:
                    active_label = display_label
                    break

        if active_label:
            self.account_var.set(active_label)
        else:
            self.account_var.set("")

        self.account_combo.configure(state="readonly")
        self.account_combo.update_idletasks()

        count = len(accounts)
        if count:
            self.current_account_label.config(text=f"Đang dùng: {get_active_account_label()} ({count} tài khoản)")
            self._log(f"📋 Đã tải {count} tài khoản YouTube.")
        else:
            self.current_account_label.config(text="Chưa đăng nhập")
        self._youtube_service = None

    def _on_account_selected(self, _event=None) -> None:
        label = self.account_var.get().strip()
        account_id = self._account_label_to_id.get(label)
        if not account_id:
            return
        try:
            set_active_account(account_id)
            self.config_data["youtube_account_id"] = account_id
            save_config(self.config_data)
            self._youtube_service = None
            self.current_account_label.config(text=f"Đang dùng: {label}")
            self._log(f"👤 Đã chuyển sang tài khoản: {label}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Lỗi", str(exc))

    def _login_new_account(self) -> None:
        if self._pipeline_running:
            messagebox.showwarning("Đang chạy", "Vui lòng đợi quy trình hiện tại kết thúc.")
            return
        threading.Thread(target=self._run_login_new_account, daemon=True).start()

    def _run_login_new_account(self) -> None:
        try:
            account = login_new_account(log=self._log)
            self.config_data["youtube_account_id"] = account["id"]
            save_config(self.config_data)

            def update_ui():
                self._refresh_accounts_ui()
                for display_label, acc_id in self._account_label_to_id.items():
                    if acc_id == account["id"]:
                        self.account_var.set(display_label)
                        break
                self.account_combo.update_idletasks()
                messagebox.showinfo("Thành công", f"Đã đăng nhập: {account['label']}")

            self.after(0, update_ui)
        except Exception as exc:  # noqa: BLE001
            err_msg = str(exc)
            self._log(f"❌ Đăng nhập thất bại: {err_msg}")
            self.after(0, lambda msg=err_msg: messagebox.showerror("Lỗi đăng nhập", msg))

    def _remove_current_account(self) -> None:
        label = self.account_var.get().strip()
        account_id = self._account_label_to_id.get(label)
        if not account_id:
            messagebox.showwarning("Thiếu thông tin", "Chưa có tài khoản để xóa.")
            return
        if not messagebox.askyesno("Xác nhận", f"Xóa tài khoản '{label}' khỏi danh sách?"):
            return
        remove_account(account_id, log=self._log)
        self._youtube_service = None
        self.config_data["youtube_account_id"] = ""
        save_config(self.config_data)
        self._refresh_accounts_ui()

    def _toggle_source_mode(self) -> None:
        is_url = self.source_mode_var.get() == "url"
        url_state = "normal" if is_url else "disabled"
        folder_state = "disabled" if is_url else "normal"
        self.url_entry.configure(state=url_state)
        self.local_source_entry.configure(state=folder_state)

    def _toggle_generate_title(self) -> None:
        state = tk.NORMAL if self.generate_title_var.get() else tk.DISABLED
        self.title_instruction_entry.configure(state=state)

    def _save_config(self) -> None:
        try:
            interval = float(self.interval_hours_var.get())
            offset = int(self.first_offset_var.get())
            max_uploads = int(self.max_uploads_var.get().strip() or "0")
        except ValueError:
            messagebox.showwarning("Lỗi", "Khoảng cách đăng, offset và giới hạn upload phải là số hợp lệ.")
            return

        privacy_status = next(
            (k for k, v in PRIVACY_LABELS.items() if v == self.privacy_var.get()), "private"
        )
        made_for_kids = next(
            (k for k, v in AUDIENCE_LABELS.items() if v == self.audience_var.get()), False
        )

        self.config_data = {
            "output_dir": self.output_dir_var.get().strip(),
            "schedule_interval_hours": interval,
            "first_publish_offset_minutes": offset,
            "title_instruction": self.title_instruction_var.get().strip(),
            "generate_title": self.generate_title_var.get(),
            "default_tags": self.tags_var.get().strip(),
            "category": self.category_var.get(),
            "privacy": privacy_status,
            "made_for_kids": made_for_kids,
            "ollama_model": self.ollama_model_var.get().strip(),
            "quicktime_compat": self.quicktime_var.get(),
            "local_source_dir": self.local_source_var.get().strip(),
            "youtube_account_id": self._account_label_to_id.get(self.account_var.get().strip(), ""),
            "max_uploads_per_run": max(0, max_uploads),
            "auto_switch_account_on_limit": self.auto_switch_account_var.get(),
        }
        save_config(self.config_data)
        self._log("💾 Đã lưu cấu hình.")

    def _log(self, text: str) -> None:
        def append():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", text + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        self.after(0, append)

    def _set_status(self, text: str) -> None:
        self.after(0, lambda: self.status_var.set(text))

    def _set_progress(self, percent: float) -> None:
        def update():
            self.progress_bar["value"] = percent
            self.progress_label.config(text=f"{int(percent)}%")

        self.after(0, update)

    def _add_video_row(self, index: int, title: str, schedule_local: str, status: str) -> str:
        item_id = self.video_tree.insert("", "end", values=(index, title, schedule_local, status))
        return item_id

    def _update_video_row(self, item_id: str, title: str = "", schedule_local: str = "", status: str = "") -> None:
        def update():
            values = list(self.video_tree.item(item_id, "values"))
            if title:
                values[1] = title
            if schedule_local:
                values[2] = schedule_local
            if status:
                values[3] = status
            self.video_tree.item(item_id, values=values)

        self.after(0, update)

    def _load_ollama_models_async(self) -> None:
        threading.Thread(target=self._load_ollama_models, daemon=True).start()

    def _load_ollama_models(self) -> None:
        try:
            models = fetch_models()
            if not models:
                return

            def update():
                self.ollama_combo["values"] = models
                if self.ollama_model_var.get() not in models:
                    self.ollama_model_var.set(models[0])
                self._log(f"🤖 Ollama models: {', '.join(models)}")

            self.after(0, update)
        except Exception as exc:  # noqa: BLE001
            self._log(f"⚠️ Không lấy được model Ollama: {exc}")

    def _update_pending_button(self) -> None:
        pending_count = count_pending()
        self.pending_btn.configure(text=f"📤 Upload hàng chờ ({pending_count})")

    def _reset_upload_run_state(self) -> None:
        self._uploads_this_run = 0
        self._stop_uploads = False
        self._upload_limit_hit = False
        self._blocked_account_ids = set()

    def _current_account_id(self) -> str:
        return self.config_data.get("youtube_account_id") or get_active_account_id() or ""

    def _ensure_youtube_service(self) -> None:
        if self._youtube_service is None:
            account_id = self._current_account_id() or None
            self._youtube_service = get_authenticated_service(
                account_id=account_id,
                log=lambda m: self._msg_queue.put(PipelineMessage(kind="log", text=m)),
            )

    def _switch_to_account(self, account_id: str) -> bool:
        try:
            set_active_account(account_id)
            self.config_data["youtube_account_id"] = account_id
            save_config(self.config_data)
            self._youtube_service = None
            for label, acc_id in self._account_label_to_id.items():
                if acc_id == account_id:
                    self.account_var.set(label)
                    break
            self.current_account_label.config(text=f"Đang dùng: {get_active_account_label()}")
            return True
        except Exception as exc:  # noqa: BLE001
            self._msg_queue.put(PipelineMessage(kind="log", text=f"⚠️ Không chuyển được tài khoản: {exc}"))
            return False

    def _try_next_account(self) -> str | None:
        if not self.config_data.get("auto_switch_account_on_limit", True):
            return None
        current = self._current_account_id()
        for acc in list_accounts():
            acc_id = acc["id"]
            if acc_id in self._blocked_account_ids or acc_id == current:
                continue
            if self._switch_to_account(acc_id):
                self._msg_queue.put(
                    PipelineMessage(kind="log", text=f"🔁 Chuyển sang tài khoản: {acc['label']}")
                )
                return acc_id
        return None

    def _save_to_pending(self, payload: dict, reason: str = "") -> None:
        add_pending({**payload, "last_error": reason})
        self.after(0, self._update_pending_button)
        self._msg_queue.put(
            PipelineMessage(kind="log", text=f"📥 Đã lưu hàng chờ: {payload.get('title', '')}")
        )

    def _upload_video_with_limit_handling(
        self,
        *,
        upload_path: str,
        new_title: str,
        description: str,
        tags: list[str],
        category_id: str,
        privacy_status: str,
        made_for_kids: bool,
        publish_at: str | None,
        schedule_local: str,
        original_title: str,
        item_id: str | None,
    ) -> str | None:
        payload = {
            "video_path": upload_path,
            "title": new_title,
            "description": description,
            "tags": tags,
            "category_id": category_id,
            "privacy_status": privacy_status,
            "made_for_kids": made_for_kids,
            "publish_at": publish_at,
            "schedule_local": schedule_local,
            "account_id": self._current_account_id(),
            "original_title": original_title,
        }

        max_per_run = int(self.config_data.get("max_uploads_per_run", 0) or 0)
        if max_per_run > 0 and self._uploads_this_run >= max_per_run:
            self._save_to_pending(payload, "Đạt giới hạn upload/lần chạy")
            if item_id:
                self._update_video_row(item_id, status="⏸ Hàng chờ")
            return None

        def upload_progress(p: float) -> None:
            self._msg_queue.put(PipelineMessage(kind="progress", percent=p * 100))

        while True:
            self._ensure_youtube_service()
            try:
                video_id = upload_video(
                    youtube=self._youtube_service,
                    video_path=upload_path,
                    title=new_title,
                    description=description,
                    tags=tags,
                    category_id=category_id,
                    privacy_status=privacy_status,
                    made_for_kids=made_for_kids,
                    publish_at=publish_at,
                    progress_callback=upload_progress,
                    log=lambda m: self._msg_queue.put(PipelineMessage(kind="log", text=m)),
                    account_label=get_active_account_label(),
                )
                self._uploads_this_run += 1
                return video_id
            except UploadLimitExceededError as exc:
                self._blocked_account_ids.add(self._current_account_id())
                self._save_to_pending(payload, str(exc))
                if item_id:
                    self._update_video_row(item_id, status="⏸ Hết hạn mức")
                if self._try_next_account():
                    self._msg_queue.put(
                        PipelineMessage(kind="log", text=f"🔁 Thử upload lại: {new_title}")
                    )
                    continue
                self._stop_uploads = True
                self._upload_limit_hit = True
                self._cancel_flag.request_cancel()
                self._msg_queue.put(PipelineMessage(kind="upload_limit", text=UPLOAD_LIMIT_HELP))
                raise

    def _start_pending_uploads(self) -> None:
        if self._pipeline_running:
            messagebox.showwarning("Đang chạy", "Vui lòng đợi quy trình hiện tại kết thúc.")
            return
        if not list_pending():
            messagebox.showinfo("Hàng chờ trống", "Không có video nào trong hàng chờ.")
            return

        self._save_config()
        self._reset_upload_run_state()
        self._cancel_flag.reset()
        self._pipeline_running = True
        self.start_btn.configure(state=tk.DISABLED)
        self.pending_btn.configure(state=tk.DISABLED)
        self.cancel_btn.configure(state=tk.NORMAL)
        self._set_progress(0)
        self._set_status("Đang upload hàng chờ...")
        self._worker_thread = threading.Thread(target=self._pending_worker, daemon=True)
        self._worker_thread.start()

    def _pending_worker(self) -> None:
        items = list_pending()
        total = len(items)
        self._msg_queue.put(PipelineMessage(kind="log", text=f"📤 Bắt đầu upload {total} video trong hàng chờ..."))

        for index, item in enumerate(items, start=1):
            if self._cancel_flag.is_cancelled() or self._stop_uploads:
                break

            title = item.get("title", "Video")
            self._msg_queue.put(
                PipelineMessage(
                    kind="progress",
                    text=title,
                    percent=(index - 1) / total * 100,
                )
            )
            self._msg_queue.put(
                PipelineMessage(kind="log", text=f"▶ Hàng chờ ({index}/{total}): {title}")
            )

            filepath = item.get("video_path", "")
            if not filepath or not os.path.exists(filepath):
                remove_pending(item["id"])
                self.after(0, self._update_pending_button)
                self._msg_queue.put(
                    PipelineMessage(kind="error", text=f"❌ File không tồn tại, bỏ khỏi hàng chờ: {title}")
                )
                continue

            account_id = item.get("account_id", "")
            if account_id and account_id != self._current_account_id():
                self._switch_to_account(account_id)

            try:
                video_id = self._upload_video_with_limit_handling(
                    upload_path=filepath,
                    new_title=item.get("title", "Video"),
                    description=item.get("description", ""),
                    tags=item.get("tags", []),
                    category_id=item.get("category_id", "22"),
                    privacy_status=item.get("privacy_status", "private"),
                    made_for_kids=bool(item.get("made_for_kids", False)),
                    publish_at=item.get("publish_at"),
                    schedule_local=item.get("schedule_local", ""),
                    original_title=item.get("original_title", title),
                    item_id=None,
                )
            except UploadLimitExceededError:
                break
            except Exception as exc:  # noqa: BLE001
                self._msg_queue.put(PipelineMessage(kind="error", text=f"❌ Upload hàng chờ thất bại '{title}': {exc}"))
                continue

            if video_id:
                remove_pending(item["id"])
                self.after(0, self._update_pending_button)
                self._msg_queue.put(
                    PipelineMessage(kind="log", text=f"✅ Đã upload hàng chờ: {title} ({video_id})")
                )

        if not self._cancel_flag.is_cancelled() and not self._stop_uploads:
            self._msg_queue.put(PipelineMessage(kind="log", text="🏁 Đã xử lý xong hàng chờ."))
        self._msg_queue.put(PipelineMessage(kind="finished"))

    def _start_pipeline(self) -> None:
        source_mode = self.source_mode_var.get()
        url = self.url_var.get().strip()
        local_folder = self.local_source_var.get().strip()

        if source_mode == "url":
            if not url:
                messagebox.showwarning("Thiếu thông tin", "Vui lòng dán đường dẫn video/playlist/kênh.")
                return
        else:
            if not local_folder:
                messagebox.showwarning("Thiếu thông tin", "Vui lòng chọn thư mục chứa video.")
                return
            if not os.path.isdir(local_folder):
                messagebox.showerror("Lỗi", f"Thư mục không tồn tại: {local_folder}")
                return

        self._save_config()

        for item in self.video_tree.get_children():
            self.video_tree.delete(item)

        self._scheduled_count = 0
        self._cancel_flag.reset()
        self._reset_upload_run_state()
        self._pipeline_running = True
        self.start_btn.configure(state=tk.DISABLED)
        self.cancel_btn.configure(state=tk.NORMAL)
        self._set_progress(0)
        self._set_status("Đang bắt đầu quy trình...")

        if source_mode == "url":
            self._worker_thread = threading.Thread(target=self._pipeline_worker_url, args=(url,), daemon=True)
        else:
            self._worker_thread = threading.Thread(
                target=self._pipeline_worker_folder, args=(local_folder,), daemon=True
            )
        self._worker_thread.start()

    def _cancel_pipeline(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            self._cancel_flag.request_cancel()
            self._set_status("Đang hủy...")
            self.cancel_btn.configure(state=tk.DISABLED)

    def _process_video(self, filepath: str, original_title: str) -> None:
        """Download xong 1 video -> generate -> upload. Chạy trên worker thread."""
        if self._stop_uploads:
            self._msg_queue.put(
                PipelineMessage(kind="log", text=f"⏭ Bỏ qua (đã hết hạn mức upload): {original_title}")
            )
            return

        video_index = self._scheduled_count
        publish_at, schedule_local = self._calc_publish_at_utc_for_index(video_index)
        self._scheduled_count += 1
        display_num = self._scheduled_count

        row_ready = threading.Event()
        row_data: dict[str, str] = {}

        def add_row() -> None:
            row_data["item_id"] = self._add_video_row(
                display_num,
                original_title,
                schedule_local,
                "Đang generate..." if self.config_data.get("generate_title", True) else "Đang chuẩn bị...",
            )
            row_ready.set()

        self.after(0, add_row)
        row_ready.wait(timeout=5)

        model = self.config_data["ollama_model"]
        title_instruction = self.config_data["title_instruction"]
        use_generated_title = bool(self.config_data.get("generate_title", True))

        if use_generated_title:
            self._msg_queue.put(PipelineMessage(kind="log", text=f"✨ Đang generate tiêu đề cho: {original_title}"))
            new_title = generate_title(model, original_title, title_instruction)
            if not new_title.strip():
                new_title = sanitize_youtube_title(os.path.splitext(os.path.basename(filepath))[0], "Video")
            self._msg_queue.put(PipelineMessage(kind="log", text=f"📝 Tiêu đề mới: {new_title}"))
        else:
            new_title = sanitize_youtube_title(
                original_title or os.path.splitext(os.path.basename(filepath))[0],
                "Video",
            )
            self._msg_queue.put(
                PipelineMessage(kind="log", text=f"📝 Dùng tên file làm tiêu đề: {new_title}")
            )

        keywords = extract_keywords(original_title) or extract_keywords(os.path.basename(filepath))
        self._msg_queue.put(PipelineMessage(kind="log", text="✨ Đang generate mô tả..."))
        description = generate_description(model, new_title, keywords)

        item_id = row_data.get("item_id")
        if item_id:
            self._update_video_row(item_id, title=new_title, status="Đang upload...")

        tags = [t.strip() for t in self.config_data["default_tags"].split(",") if t.strip()]
        category_id = CATEGORIES.get(self.config_data["category"], "22")
        privacy_status = self.config_data["privacy"]
        made_for_kids = self.config_data["made_for_kids"]

        upload_path = prepare_for_youtube(
            filepath,
            log=lambda m: self._msg_queue.put(PipelineMessage(kind="log", text=m)),
        )

        video_id = self._upload_video_with_limit_handling(
            upload_path=upload_path,
            new_title=new_title,
            description=description,
            tags=tags,
            category_id=category_id,
            privacy_status=privacy_status,
            made_for_kids=made_for_kids,
            publish_at=publish_at,
            schedule_local=schedule_local,
            original_title=original_title,
            item_id=item_id,
        )

        if not video_id:
            return

        if item_id:
            self._update_video_row(item_id, status=f"✅ Đã đăng ({video_id})")
        self._msg_queue.put(
            PipelineMessage(
                kind="log",
                text=f"🎉 Hoàn tất video #{display_num}: {new_title} | Lịch: {schedule_local}",
            )
        )

    def _calc_publish_at_utc_for_index(self, index: int) -> tuple[str, str]:
        interval_hours = float(self.config_data["schedule_interval_hours"])
        first_offset = int(self.config_data["first_publish_offset_minutes"])
        base = datetime.now().astimezone() + timedelta(minutes=first_offset)
        publish_local = base + timedelta(hours=interval_hours * index)
        local_tz = datetime.now().astimezone().tzinfo
        publish_utc = publish_local.replace(tzinfo=local_tz).astimezone(timezone.utc)
        return publish_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), publish_local.strftime("%Y-%m-%d %H:%M")

    def _pipeline_worker_url(self, url: str) -> None:
        output_dir = self.config_data["output_dir"]
        os.makedirs(output_dir, exist_ok=True)

        process_lock = threading.Lock()

        def on_video_finished(filepath: str, original_title: str) -> None:
            if self._cancel_flag.is_cancelled():
                return
            with process_lock:
                try:
                    self._process_video(filepath, original_title)
                except UploadLimitExceededError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    self._msg_queue.put(PipelineMessage(kind="error", text=f"❌ Lỗi xử lý '{original_title}': {exc}"))

        try:
            self._msg_queue.put(PipelineMessage(kind="log", text="⬇️ Bắt đầu tải video..."))
            download_videos(
                url,
                output_dir,
                on_video_finished=on_video_finished,
                on_log=lambda m: self._msg_queue.put(PipelineMessage(kind="log", text=m)),
                on_progress=lambda title, pct: self._msg_queue.put(
                    PipelineMessage(kind="progress", text=title, percent=pct)
                ),
                cancel_flag=self._cancel_flag,
                quicktime_compat=self.config_data["quicktime_compat"],
            )
            if self._cancel_flag.is_cancelled():
                self._msg_queue.put(PipelineMessage(kind="log", text="Đã hủy quy trình."))
            else:
                self._msg_queue.put(
                    PipelineMessage(
                        kind="log",
                        text=f"🏁 Đã tải và xử lý xong {self._scheduled_count} video. Quy trình hoàn tất!",
                    )
                )
        except DownloadCancelled:
            self._msg_queue.put(PipelineMessage(kind="log", text="Đã hủy quy trình tải."))
        except Exception as exc:  # noqa: BLE001
            self._msg_queue.put(PipelineMessage(kind="error", text=f"Lỗi pipeline: {exc}"))
        finally:
            self._msg_queue.put(PipelineMessage(kind="finished"))

    def _pipeline_worker_folder(self, folder: str) -> None:
        try:
            videos = list_local_videos(folder)
            if not videos:
                self._msg_queue.put(
                    PipelineMessage(kind="error", text=f"❌ Không tìm thấy video hợp lệ trong: {folder}")
                )
                return

            self._msg_queue.put(
                PipelineMessage(kind="log", text=f"📂 Tìm thấy {len(videos)} video trong thư mục.")
            )

            for i, filepath in enumerate(videos, start=1):
                if self._cancel_flag.is_cancelled() or self._stop_uploads:
                    self._msg_queue.put(PipelineMessage(kind="log", text="Đã hủy quy trình."))
                    break

                original_title = os.path.splitext(os.path.basename(filepath))[0]
                self._msg_queue.put(
                    PipelineMessage(
                        kind="progress",
                        text=original_title,
                        percent=(i - 1) / len(videos) * 100,
                    )
                )
                self._msg_queue.put(
                    PipelineMessage(kind="log", text=f"▶ Đang xử lý ({i}/{len(videos)}): {original_title}")
                )
                try:
                    self._process_video(filepath, original_title)
                except UploadLimitExceededError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    self._msg_queue.put(
                        PipelineMessage(kind="error", text=f"❌ Lỗi xử lý '{original_title}': {exc}")
                    )

            if not self._cancel_flag.is_cancelled():
                self._msg_queue.put(
                    PipelineMessage(
                        kind="log",
                        text=f"🏁 Đã xử lý xong {self._scheduled_count} video từ thư mục. Quy trình hoàn tất!",
                    )
                )
                self._msg_queue.put(PipelineMessage(kind="progress", percent=100))
        except Exception as exc:  # noqa: BLE001
            self._msg_queue.put(PipelineMessage(kind="error", text=f"Lỗi pipeline: {exc}"))
        finally:
            self._msg_queue.put(PipelineMessage(kind="finished"))

    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self._msg_queue.get_nowait()
                if msg.kind == "log":
                    self._log(msg.text)
                elif msg.kind == "progress":
                    self._set_progress(msg.percent)
                    if msg.text:
                        self._set_status(f"Đang xử lý: {msg.text} - {msg.percent:.1f}%")
                elif msg.kind == "error":
                    self._log(msg.text)
                elif msg.kind == "upload_limit":
                    self._log(f"⚠️ {msg.text}")
                    messagebox.showwarning("Hết hạn mức upload YouTube", msg.text)
                elif msg.kind == "finished":
                    self._on_pipeline_finished()
        except queue.Empty:
            pass
        finally:
            self.after(100, self._poll_queue)

    def _on_pipeline_finished(self) -> None:
        self._pipeline_running = False
        self.start_btn.configure(state=tk.NORMAL)
        self.pending_btn.configure(state=tk.NORMAL)
        self.cancel_btn.configure(state=tk.DISABLED)
        pending_count = count_pending()
        if self._upload_limit_hit:
            self._set_status(f"Hết hạn mức upload. {pending_count} video trong hàng chờ.")
            messagebox.showwarning(
                "Hết hạn mức upload",
                f"{UPLOAD_LIMIT_HELP}\n\nHiện có {pending_count} video trong hàng chờ.",
            )
        elif not self._cancel_flag.is_cancelled() and self._scheduled_count > 0:
            self._set_status(f"Hoàn tất! Đã xử lý {self._scheduled_count} video.")
            if pending_count:
                messagebox.showinfo(
                    "Hoàn tất",
                    f"Đã xử lý {self._scheduled_count} video.\n"
                    f"Còn {pending_count} video trong hàng chờ — bấm «Upload hàng chờ» để đăng sau.",
                )
            else:
                messagebox.showinfo("Hoàn tất", f"Đã tải và đăng lịch {self._scheduled_count} video lên YouTube!")
        elif self._cancel_flag.is_cancelled():
            self._set_status("Đã hủy.")
        else:
            self._set_status("Hoàn tất (không có video nào được xử lý).")
        self._update_pending_button()


def main() -> None:
    root = tk.Tk()
    root.withdraw()
    PipelineApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
