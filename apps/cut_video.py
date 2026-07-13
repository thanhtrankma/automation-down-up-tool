"""
apps/cut_video.py - Chuyển đổi tỉ lệ video (vd. 16:9 → 9:16)

Chạy: python3 main.py hoặc python3 -m apps.cut_video
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, scrolledtext, ttk

from services.aspect_convert import (
    ASPECT_PRESETS,
    CROP_POSITIONS,
    MODE_BLUR,
    MODE_CROP,
    MODE_LABELS,
    MODE_PAD,
    convert_aspect,
    default_output_path,
    get_video_size,
    list_videos_for_convert,
)


@dataclass
class ConvertMessage:
    kind: str  # log | progress | finished | error
    text: str = ""
    percent: float = 0.0


class CutVideoApp(tk.Toplevel):
    def __init__(self, master, *, show_back: bool = False) -> None:
        super().__init__(master)
        self._standalone_root = isinstance(master, tk.Tk) and not show_back
        self._show_back = show_back

        self.title("Chuyển đổi tỉ lệ video — ngang → dọc")
        self.geometry("720x640")
        self.minsize(640, 560)

        self._msg_queue: queue.Queue[ConvertMessage] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._cancel = False
        self._running = False
        self._file_list: list[str] = []

        self._build_ui()
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

        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            root,
            text="Chuyển video ngang (16:9) thành dọc (9:16) cho Shorts / Reels / TikTok",
            font=("Segoe UI", 11),
        ).pack(anchor="w", pady=(0, 8))

        # --- Nguồn ---
        src = ttk.LabelFrame(root, text="Nguồn video")
        src.pack(fill=tk.X, **pad)
        src.columnconfigure(1, weight=1)

        mode_row = ttk.Frame(src)
        mode_row.grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 4))
        self.source_mode = tk.StringVar(value="files")
        ttk.Radiobutton(
            mode_row, text="Chọn file", variable=self.source_mode, value="files", command=self._toggle_source
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            mode_row, text="Cả thư mục", variable=self.source_mode, value="folder", command=self._toggle_source
        ).pack(side=tk.LEFT, padx=(16, 0))

        self.files_row = ttk.Frame(src)
        self.files_row.grid(row=1, column=0, columnspan=3, sticky="ew", padx=8, pady=4)
        self.files_row.columnconfigure(0, weight=1)
        self.files_var = tk.StringVar(value="Chưa chọn file")
        ttk.Label(self.files_row, textvariable=self.files_var).grid(row=0, column=0, sticky="w")
        ttk.Button(self.files_row, text="Chọn video...", command=self._browse_files).grid(
            row=0, column=1, padx=(6, 0)
        )

        self.folder_row = ttk.Frame(src)
        self.folder_row.grid(row=2, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 8))
        self.folder_row.columnconfigure(0, weight=1)
        self.folder_var = tk.StringVar()
        ttk.Entry(self.folder_row, textvariable=self.folder_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(self.folder_row, text="Chọn thư mục...", command=self._browse_folder).grid(
            row=0, column=1, padx=(6, 0)
        )
        self._toggle_source()

        # --- Tuỳ chọn ---
        opts = ttk.LabelFrame(root, text="Tuỳ chọn chuyển đổi")
        opts.pack(fill=tk.X, **pad)
        opts.columnconfigure(1, weight=1)

        ttk.Label(opts, text="Tỉ lệ đích:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self.preset_var = tk.StringVar(value=list(ASPECT_PRESETS.keys())[0])
        ttk.Combobox(
            opts, textvariable=self.preset_var, values=list(ASPECT_PRESETS.keys()), state="readonly", width=42
        ).grid(row=0, column=1, sticky="ew", padx=8, pady=6)

        ttk.Label(opts, text="Chế độ:").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        self.mode_var = tk.StringVar(value=MODE_LABELS[MODE_BLUR])
        ttk.Combobox(
            opts, textvariable=self.mode_var, values=list(MODE_LABELS.values()), state="readonly", width=42
        ).grid(row=1, column=1, sticky="ew", padx=8, pady=6)

        ttk.Label(opts, text="Vùng cắt (khi crop):").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        self.crop_pos_var = tk.StringVar(value=CROP_POSITIONS["center"])
        ttk.Combobox(
            opts,
            textvariable=self.crop_pos_var,
            values=list(CROP_POSITIONS.values()),
            state="readonly",
            width=20,
        ).grid(row=2, column=1, sticky="w", padx=8, pady=6)

        ttk.Label(opts, text="Thư mục lưu:").grid(row=3, column=0, sticky="w", padx=8, pady=6)
        out_row = ttk.Frame(opts)
        out_row.grid(row=3, column=1, sticky="ew", padx=8, pady=6)
        out_row.columnconfigure(0, weight=1)
        self.output_dir_var = tk.StringVar()
        ttk.Entry(out_row, textvariable=self.output_dir_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(out_row, text="Chọn...", command=self._browse_output).grid(row=0, column=1, padx=(6, 0))
        ttk.Label(opts, text="(Để trống = lưu cạnh file gốc)").grid(
            row=4, column=1, sticky="w", padx=8, pady=(0, 6)
        )

        # --- Progress ---
        prog = ttk.Frame(root)
        prog.pack(fill=tk.X, **pad)
        prog.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.progress_label = ttk.Label(prog, text="0%")
        self.progress_label.grid(row=0, column=1)

        self.status_var = tk.StringVar(value="Sẵn sàng.")
        ttk.Label(root, textvariable=self.status_var).pack(anchor="w", padx=10)

        self.log_box = scrolledtext.ScrolledText(root, height=10, state="disabled", wrap="word")
        self.log_box.pack(fill=tk.BOTH, expand=True, **pad)

        action = ttk.Frame(root)
        action.pack(fill=tk.X, pady=(4, 0))
        self.start_btn = ttk.Button(action, text="▶ Bắt đầu chuyển đổi", command=self._start)
        self.start_btn.pack(side=tk.LEFT)
        self.cancel_btn = ttk.Button(action, text="Hủy", command=self._cancel_run, state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT, padx=(8, 0))

    def _toggle_source(self) -> None:
        is_files = self.source_mode.get() == "files"
        for child in self.files_row.winfo_children():
            try:
                child.configure(state="normal" if is_files else "disabled")
            except tk.TclError:
                pass
        for child in self.folder_row.winfo_children():
            try:
                child.configure(state="disabled" if is_files else "normal")
            except tk.TclError:
                pass

    def _browse_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Chọn video",
            filetypes=[
                ("Video", "*.mp4 *.mov *.mkv *.avi *.webm *.flv *.wmv"),
                ("Tất cả", "*.*"),
            ],
        )
        if paths:
            self._file_list = list(paths)
            if len(paths) == 1:
                w, h = get_video_size(paths[0])
                size_info = f" ({w}×{h})" if w else ""
                self.files_var.set(f"{os.path.basename(paths[0])}{size_info}")
            else:
                self.files_var.set(f"Đã chọn {len(paths)} file")

    def _browse_folder(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.folder_var.get() or os.path.expanduser("~"))
        if chosen:
            self.folder_var.set(chosen)

    def _browse_output(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.output_dir_var.get() or os.path.expanduser("~"))
        if chosen:
            self.output_dir_var.set(chosen)

    def _resolve_mode(self) -> str:
        label = self.mode_var.get()
        for key, value in MODE_LABELS.items():
            if value == label:
                return key
        return MODE_BLUR

    def _resolve_crop_pos(self) -> str:
        label = self.crop_pos_var.get()
        for key, value in CROP_POSITIONS.items():
            if value == label:
                return key
        return "center"

    def _collect_inputs(self) -> list[str]:
        if self.source_mode.get() == "files":
            return list(self._file_list)
        folder = self.folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            return []
        return list_videos_for_convert(folder)

    def _start(self) -> None:
        if self._running:
            return
        inputs = self._collect_inputs()
        if not inputs:
            messagebox.showwarning("Thiếu video", "Vui lòng chọn file hoặc thư mục chứa video.")
            return

        preset = self.preset_var.get()
        if preset not in ASPECT_PRESETS:
            messagebox.showwarning("Lỗi", "Vui lòng chọn tỉ lệ đích hợp lệ.")
            return

        out_w, out_h = ASPECT_PRESETS[preset]
        mode = self._resolve_mode()
        crop_pos = self._resolve_crop_pos()
        output_dir = self.output_dir_var.get().strip()

        self._cancel = False
        self._running = True
        self.start_btn.configure(state=tk.DISABLED)
        self.cancel_btn.configure(state=tk.NORMAL)
        self.progress["value"] = 0
        self.progress_label.config(text="0%")
        self.status_var.set(f"Đang chuyển đổi 0/{len(inputs)}...")

        self._worker = threading.Thread(
            target=self._worker_run,
            args=(inputs, out_w, out_h, mode, crop_pos, output_dir),
            daemon=True,
        )
        self._worker.start()

    def _cancel_run(self) -> None:
        self._cancel = True
        self.status_var.set("Đang hủy...")
        self.cancel_btn.configure(state=tk.DISABLED)

    def _worker_run(
        self,
        inputs: list[str],
        out_w: int,
        out_h: int,
        mode: str,
        crop_pos: str,
        output_dir: str,
    ) -> None:
        total = len(inputs)
        ok = 0
        for i, path in enumerate(inputs, start=1):
            if self._cancel:
                self._msg_queue.put(ConvertMessage(kind="log", text="⏹ Đã hủy."))
                break

            name = os.path.basename(path)
            self._msg_queue.put(
                ConvertMessage(kind="progress", text=name, percent=(i - 1) / total * 100)
            )
            self._msg_queue.put(ConvertMessage(kind="log", text=f"▶ ({i}/{total}) {name}"))

            out_path = default_output_path(path, out_w, out_h, output_dir)
            try:
                convert_aspect(
                    path,
                    out_path,
                    out_w=out_w,
                    out_h=out_h,
                    mode=mode,
                    crop_position=crop_pos if mode == MODE_CROP else "center",
                    log=lambda m: self._msg_queue.put(ConvertMessage(kind="log", text=m)),
                )
                ok += 1
            except Exception as exc:  # noqa: BLE001
                self._msg_queue.put(ConvertMessage(kind="error", text=f"❌ {name}: {exc}"))

        self._msg_queue.put(
            ConvertMessage(kind="progress", percent=100 if not self._cancel else (ok / total * 100))
        )
        self._msg_queue.put(
            ConvertMessage(kind="finished", text=f"Hoàn tất {ok}/{total} video.")
        )

    def _log(self, text: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self._msg_queue.get_nowait()
                if msg.kind == "log":
                    self._log(msg.text)
                elif msg.kind == "progress":
                    self.progress["value"] = msg.percent
                    self.progress_label.config(text=f"{int(msg.percent)}%")
                    if msg.text:
                        self.status_var.set(f"Đang xử lý: {msg.text}")
                elif msg.kind == "error":
                    self._log(msg.text)
                elif msg.kind == "finished":
                    self._running = False
                    self.start_btn.configure(state=tk.NORMAL)
                    self.cancel_btn.configure(state=tk.DISABLED)
                    self.status_var.set(msg.text)
                    self._log(f"🏁 {msg.text}")
                    if not self._cancel:
                        messagebox.showinfo("Hoàn tất", msg.text)
        except queue.Empty:
            pass
        finally:
            self.after(100, self._poll_queue)


def main() -> None:
    root = tk.Tk()
    root.withdraw()
    CutVideoApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
