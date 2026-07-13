"""
main.py - Menu chính Video Tool

Chạy: python3 main.py
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from apps.cut_video import CutVideoApp
from apps.download import VideoDownloaderApp
from apps.pipeline import PipelineApp
from apps.upload import YoutubeUploaderApp


class HubApp(tk.Tk):
    """Trang chọn tính năng."""

    def __init__(self) -> None:
        super().__init__()
        self.title("Video Tool")
        self.geometry("520x620")
        self.minsize(440, 560)
        self.configure(bg="#f0f4f8")
        self._build_menu()

    def _build_menu(self) -> None:
        container = ttk.Frame(self, padding=24)
        container.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            container,
            text="Video Tool",
            font=("Segoe UI", 22, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            container,
            text="Chọn tính năng bạn muốn sử dụng",
            font=("Segoe UI", 11),
            foreground="#4b5563",
        ).pack(anchor="w", pady=(4, 20))

        menu_items = [
            ("⬇️", "Tải videos", "Tải video/playlist/kênh bằng yt-dlp", self._open_download, True),
            ("⬆️", "Upload Video", "Upload video lên YouTube (nhiều tab)", self._open_upload, True),
            (
                "🔄",
                "Quy trình tự động",
                "Tải → Generate tiêu đề/mô tả → Upload & đặt lịch",
                self._open_pipeline,
                True,
            ),
            (
                "✂️",
                "Tự động cắt video",
                "Chuyển ngang → dọc (16:9 → 9:16) cho Shorts/Reels",
                self._open_cut_video,
                True,
            ),
        ]

        for icon, title, subtitle, command, enabled in menu_items:
            self._add_menu_card(container, icon, title, subtitle, command, enabled)

    def _add_menu_card(
        self,
        parent: ttk.Frame,
        icon: str,
        title: str,
        subtitle: str,
        command,
        enabled: bool,
    ) -> None:
        card = ttk.Frame(parent, padding=12)
        card.pack(fill=tk.X, pady=6)

        inner = tk.Frame(
            card,
            bg="#ffffff",
            highlightbackground="#d1d5db",
            highlightthickness=1,
            cursor="hand2" if enabled else "arrow",
        )
        inner.pack(fill=tk.X)

        row = tk.Frame(inner, bg="#ffffff", padx=14, pady=12)
        row.pack(fill=tk.X)

        tk.Label(row, text=icon, font=("Segoe UI", 24), bg="#ffffff").pack(side=tk.LEFT, padx=(0, 12))
        text_col = tk.Frame(row, bg="#ffffff")
        text_col.pack(side=tk.LEFT, fill=tk.X, expand=True)

        title_color = "#111827" if enabled else "#9ca3af"
        tk.Label(text_col, text=title, font=("Segoe UI", 13, "bold"), fg=title_color, bg="#ffffff", anchor="w").pack(
            fill=tk.X
        )
        tk.Label(text_col, text=subtitle, font=("Segoe UI", 10), fg="#6b7280", bg="#ffffff", anchor="w").pack(
            fill=tk.X, pady=(2, 0)
        )

        if not enabled:
            badge = tk.Label(
                row,
                text="Đang phát triển",
                font=("Segoe UI", 9, "bold"),
                fg="#92400e",
                bg="#fef3c7",
                padx=8,
                pady=4,
            )
            badge.pack(side=tk.RIGHT)
            return

        def on_click(_event=None) -> None:
            command()

        def on_enter(_event=None) -> None:
            inner.configure(highlightbackground="#2f6fed", bg="#f8fbff")
            row.configure(bg="#f8fbff")
            for widget in row.winfo_children():
                if isinstance(widget, tk.Label):
                    widget.configure(bg="#f8fbff")
                elif isinstance(widget, tk.Frame):
                    widget.configure(bg="#f8fbff")
                    for child in widget.winfo_children():
                        if isinstance(child, tk.Label):
                            child.configure(bg="#f8fbff")

        def on_leave(_event=None) -> None:
            inner.configure(highlightbackground="#d1d5db", bg="#ffffff")
            row.configure(bg="#ffffff")
            for widget in row.winfo_children():
                if isinstance(widget, tk.Label):
                    widget.configure(bg="#ffffff")
                elif isinstance(widget, tk.Frame):
                    widget.configure(bg="#ffffff")
                    for child in widget.winfo_children():
                        if isinstance(child, tk.Label):
                            child.configure(bg="#ffffff")

        for widget in (inner, row, text_col):
            widget.bind("<Button-1>", on_click)
        for widget in row.winfo_children():
            widget.bind("<Button-1>", on_click)
        for widget in text_col.winfo_children():
            widget.bind("<Button-1>", on_click)

        inner.bind("<Enter>", on_enter)
        inner.bind("<Leave>", on_leave)

    def _open_download(self) -> None:
        VideoDownloaderApp(self, show_back=True)

    def _open_upload(self) -> None:
        win = tk.Toplevel(self)
        win.title("Upload Video")
        win.geometry("860x760")
        win.minsize(760, 660)

        bar = ttk.Frame(win, padding=(8, 8, 8, 0))
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="← Về menu", command=win.destroy).pack(side=tk.LEFT)

        YoutubeUploaderApp(win)

    def _open_pipeline(self) -> None:
        PipelineApp(self, show_back=True)

    def _open_cut_video(self) -> None:
        CutVideoApp(self, show_back=True)


def main() -> None:
    app = HubApp()
    app.mainloop()


if __name__ == "__main__":
    main()
