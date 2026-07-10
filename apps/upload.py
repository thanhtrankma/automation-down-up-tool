"""Giao diện upload video lên YouTube.

Chạy: python3 main.py hoặc python3 -m apps.upload
"""

import json
import os
import re
import threading
import tkinter as tk
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from tkinter import filedialog, messagebox, scrolledtext, ttk

from youtube.uploader import CATEGORIES, get_authenticated_service, upload_video

PRIVACY_LABELS = {
    "private": "Riêng tư (Private)",
    "unlisted": "Không công khai (Unlisted)",
    "public": "Công khai (Public)",
}

AUDIENCE_LABELS = {
    False: "Không, video này KHÔNG dành cho trẻ em",
    True: "Có, video này dành cho trẻ em",
}

OLLAMA_BASE_URL = "http://localhost:11434"


class YoutubeUploaderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("YouTube Video Uploader")
        self.root.geometry("860x760")
        self.root.minsize(760, 660)

        self.youtube_service = None
        self.is_uploading = False
        self.is_generating_description = False

        self.tab_counter = 0
        self.tab_forms = {}  # tab_id -> form widgets/state

        self.ollama_models = ["llama3:latest"]
        self.ollama_model_var = tk.StringVar(value="llama3:latest")

        self._build_ui()
        self._load_ollama_models_async()

    def _to_title_case(self, text):
        words = re.split(r"\s+", text.strip())
        normalized = []
        for word in words:
            if not word:
                continue
            normalized.append(word[:1].upper() + word[1:].lower())
        return " ".join(normalized)

    def _build_ui(self):
        pad = {"padx": 12, "pady": 6}

        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True, padx=8, pady=8)
        main.columnconfigure(0, weight=1)

        # --- Toolbar: chọn nhiều video / model Ollama ---
        toolbar = ttk.LabelFrame(main, text="Video")
        toolbar.pack(fill="x", **pad)
        toolbar.columnconfigure(1, weight=1)

        ttk.Button(toolbar, text="Chọn 1 video...", command=self._choose_video).grid(
            row=0, column=0, padx=(8, 4), pady=8, sticky="w"
        )
        ttk.Button(toolbar, text="Chọn nhiều video...", command=self._choose_multiple_videos).grid(
            row=0, column=1, padx=4, pady=8, sticky="w"
        )
        ttk.Button(toolbar, text="Đóng tab hiện tại", command=self._close_current_tab).grid(
            row=0, column=2, padx=4, pady=8, sticky="w"
        )

        model_frame = ttk.Frame(toolbar)
        model_frame.grid(row=1, column=0, columnspan=3, sticky="ew", padx=8, pady=(0, 8))
        model_frame.columnconfigure(1, weight=1)

        ttk.Label(model_frame, text="Model Ollama:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.ollama_model_combo = ttk.Combobox(
            model_frame,
            textvariable=self.ollama_model_var,
            values=self.ollama_models,
            state="readonly",
            width=28,
        )
        self.ollama_model_combo.grid(row=0, column=1, sticky="w")

        # --- Tabs video ---
        self.notebook = ttk.Notebook(main)
        self.notebook.pack(fill="both", expand=True, **pad)

        # --- Tiến độ & log chung ---
        progress_frame = ttk.Frame(main)
        progress_frame.pack(fill="x", **pad)
        progress_frame.columnconfigure(0, weight=1)

        self.progress_bar = ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
        self.progress_bar.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.progress_label = ttk.Label(progress_frame, text="0%")
        self.progress_label.grid(row=0, column=1)

        self.log_box = scrolledtext.ScrolledText(main, height=8, state="disabled", wrap="word")
        self.log_box.pack(fill="both", expand=False, **pad)

        # --- Nút thao tác ---
        action_frame = ttk.Frame(main)
        action_frame.pack(fill="x", padx=12, pady=(0, 12))
        action_frame.columnconfigure(0, weight=1)
        action_frame.columnconfigure(1, weight=1)

        self.generate_desc_button = ttk.Button(
            action_frame, text="✨ Generate mô tả (tab hiện tại)", command=self._start_generate_description
        )
        self.generate_desc_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.upload_button = ttk.Button(
            action_frame, text="🚀 Đăng video tab hiện tại", command=self._start_upload
        )
        self.upload_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))

    def _create_video_tab(self, video_path):
        self.tab_counter += 1
        raw_title_hint = os.path.splitext(os.path.basename(video_path))[0] or f"Video {self.tab_counter}"
        title_hint = self._to_title_case(raw_title_hint)
        tab_title = title_hint[:24] + ("..." if len(title_hint) > 24 else "")

        frame = ttk.Frame(self.notebook)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Đường dẫn video:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        video_path_var = tk.StringVar(value=video_path)
        ttk.Entry(frame, textvariable=video_path_var, state="readonly").grid(
            row=0, column=1, sticky="ew", padx=8, pady=6
        )

        ttk.Label(frame, text="Tiêu đề:").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        title_var = tk.StringVar(value=title_hint)
        ttk.Entry(frame, textvariable=title_var).grid(row=1, column=1, sticky="ew", padx=8, pady=6)

        ttk.Label(frame, text="Mô tả:").grid(row=2, column=0, sticky="nw", padx=8, pady=6)
        description_text = tk.Text(frame, height=7, wrap="word")
        description_text.grid(row=2, column=1, sticky="ew", padx=8, pady=6)

        ttk.Label(frame, text="Tags (cách nhau bởi dấu phẩy):").grid(
            row=3, column=0, sticky="w", padx=8, pady=6
        )
        tags_var = tk.StringVar()
        ttk.Entry(frame, textvariable=tags_var).grid(row=3, column=1, sticky="ew", padx=8, pady=6)

        ttk.Label(frame, text="Danh mục:").grid(row=4, column=0, sticky="w", padx=8, pady=6)
        category_var = tk.StringVar(value="Người & Blog")
        ttk.Combobox(
            frame,
            textvariable=category_var,
            values=list(CATEGORIES.keys()),
            state="readonly",
        ).grid(row=4, column=1, sticky="ew", padx=8, pady=6)

        ttk.Label(frame, text="Chế độ hiển thị:").grid(row=5, column=0, sticky="w", padx=8, pady=6)
        privacy_var = tk.StringVar(value=PRIVACY_LABELS["public"])
        ttk.Combobox(
            frame,
            textvariable=privacy_var,
            values=list(PRIVACY_LABELS.values()),
            state="readonly",
        ).grid(row=5, column=1, sticky="ew", padx=8, pady=6)

        ttk.Label(frame, text="Đối tượng người xem:").grid(row=6, column=0, sticky="w", padx=8, pady=6)
        audience_var = tk.StringVar(value=AUDIENCE_LABELS[False])
        ttk.Combobox(
            frame,
            textvariable=audience_var,
            values=list(AUDIENCE_LABELS.values()),
            state="readonly",
        ).grid(row=6, column=1, sticky="ew", padx=8, pady=6)

        schedule_frame = ttk.LabelFrame(frame, text="Đặt lịch đăng (tuỳ chọn)")
        schedule_frame.grid(row=7, column=0, columnspan=2, sticky="ew", padx=8, pady=8)
        schedule_frame.columnconfigure(1, weight=1)

        schedule_enabled_var = tk.BooleanVar(value=False)
        schedule_entry_var = tk.StringVar()

        schedule_entry = ttk.Entry(schedule_frame, textvariable=schedule_entry_var, state="disabled")

        def toggle_schedule():
            if schedule_enabled_var.get():
                schedule_entry.configure(state="normal")
                if not schedule_entry_var.get().strip():
                    dt_local = datetime.now().astimezone() + timedelta(minutes=10)
                    schedule_entry_var.set(dt_local.strftime("%Y-%m-%dT%H:%M:%S"))
            else:
                schedule_entry.configure(state="disabled")

        ttk.Checkbutton(
            schedule_frame,
            text="Đặt lịch đăng video vào thời điểm cụ thể",
            variable=schedule_enabled_var,
            command=toggle_schedule,
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 0))

        ttk.Label(schedule_frame, text="Thời gian local máy bạn (YYYY-MM-DDTHH:MM:SS):").grid(
            row=1, column=0, sticky="w", padx=8, pady=6
        )
        schedule_entry.grid(row=1, column=1, sticky="ew", padx=8, pady=6)

        self.notebook.add(frame, text=tab_title)
        self.notebook.select(frame)

        tab_id = str(frame)
        self.tab_forms[tab_id] = {
            "frame": frame,
            "video_path_var": video_path_var,
            "title_var": title_var,
            "description_text": description_text,
            "tags_var": tags_var,
            "category_var": category_var,
            "privacy_var": privacy_var,
            "audience_var": audience_var,
            "schedule_enabled_var": schedule_enabled_var,
            "schedule_entry_var": schedule_entry_var,
        }

    def _current_form(self):
        current_tab = self.notebook.select()
        if not current_tab:
            return None
        return self.tab_forms.get(current_tab)

    def _choose_video(self):
        path = filedialog.askopenfilename(
            title="Chọn file video",
            filetypes=[
                ("Video files", "*.mp4 *.mov *.avi *.mkv *.wmv *.flv *.webm"),
                ("Tất cả file", "*.*"),
            ],
        )
        if path:
            self._create_video_tab(path)

    def _choose_multiple_videos(self):
        paths = filedialog.askopenfilenames(
            title="Chọn nhiều file video",
            filetypes=[
                ("Video files", "*.mp4 *.mov *.avi *.mkv *.wmv *.flv *.webm"),
                ("Tất cả file", "*.*"),
            ],
        )
        if not paths:
            return
        for path in paths:
            self._create_video_tab(path)
        self._log(f"📂 Đã thêm {len(paths)} video vào {len(paths)} tab mới.")

    def _close_current_tab(self):
        current_tab = self.notebook.select()
        if not current_tab:
            return
        self.notebook.forget(current_tab)
        self.tab_forms.pop(current_tab, None)

    def _log(self, message):
        def append():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", message + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        self.root.after(0, append)

    def _set_progress(self, fraction):
        percent = int(fraction * 100)

        def update():
            self.progress_bar["value"] = percent
            self.progress_label.config(text=f"{percent}%")

        self.root.after(0, update)

    def _set_uploading_state(self, is_uploading):
        self.is_uploading = is_uploading
        disable_actions = is_uploading or self.is_generating_description
        state = "disabled" if disable_actions else "normal"

        self.upload_button.configure(state=state, text="⏳ Đang tải lên..." if is_uploading else "🚀 Đăng video tab hiện tại")
        self.generate_desc_button.configure(
            state=state,
            text="⏳ Đang tạo mô tả..." if self.is_generating_description else "✨ Generate mô tả (tab hiện tại)",
        )

    def _set_generating_description_state(self, is_generating):
        self.is_generating_description = is_generating
        self._set_uploading_state(self.is_uploading)

    def _load_ollama_models_async(self):
        threading.Thread(target=self._load_ollama_models, daemon=True).start()

    def _load_ollama_models(self):
        try:
            models = self._fetch_ollama_models()
            if not models:
                return

            def update_ui():
                self.ollama_models = models
                self.ollama_model_combo["values"] = models
                if self.ollama_model_var.get() not in models:
                    self.ollama_model_var.set(models[0])
                self._log(f"🤖 Đã kết nối Ollama. Models: {', '.join(models)}")

            self.root.after(0, update_ui)
        except Exception as exc:  # noqa: BLE001
            self._log(f"⚠️ Không lấy được danh sách model Ollama: {exc}")

    def _fetch_ollama_models(self):
        req = urllib.request.Request(f"{OLLAMA_BASE_URL}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]

    def _extract_keywords_from_filename(self, file_path):
        base = os.path.splitext(os.path.basename(file_path))[0]
        cleaned = base.replace("⧸", " ").replace("｜", " ").replace("|", " ")
        cleaned = re.sub(r"[_/\-]+", " ", cleaned)
        cleaned = re.sub(r"[^\w\sÀ-ỹ]", " ", cleaned, flags=re.UNICODE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            return ""
        words = [w for w in cleaned.split(" ") if len(w) > 1]
        return ", ".join(words[:12])

    def _clean_generated_description(self, text):
        content = text.strip().strip('"').strip()
        if not content:
            return content

        content = re.sub(
            r"^(here is|dưới đây là|mô tả gợi ý|gợi ý mô tả|đây là)\b.*?:\s*",
            "",
            content,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()

        content = re.sub(
            r"\n*(let me know if you need.*|nếu bạn cần.*|bạn muốn tôi.*)$",
            "",
            content,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()

        if (content.startswith('"') and content.endswith('"')) or (
            content.startswith("'") and content.endswith("'")
        ):
            content = content[1:-1].strip()
        return content

    def _start_generate_description(self):
        if self.is_uploading or self.is_generating_description:
            return

        form = self._current_form()
        if not form:
            messagebox.showwarning("Thiếu thông tin", "Vui lòng thêm video (tab) trước khi tạo mô tả.")
            return

        video_path = form["video_path_var"].get().strip()
        if not video_path:
            messagebox.showwarning("Thiếu thông tin", "Tab hiện tại chưa có đường dẫn video.")
            return

        self._set_generating_description_state(True)
        threading.Thread(target=self._run_generate_description, args=(str(form["frame"]),), daemon=True).start()

    def _run_generate_description(self, tab_id):
        try:
            form = self.tab_forms.get(tab_id)
            if not form:
                raise ValueError("Không tìm thấy tab video để generate mô tả.")

            video_path = form["video_path_var"].get().strip()
            title = form["title_var"].get().strip()
            model = self.ollama_model_var.get().strip() or "llama3:latest"
            keywords = self._extract_keywords_from_filename(video_path)
            if not keywords:
                raise ValueError("Không trích xuất được từ khoá từ tên file video.")

            prompt = (
                "Bạn là chuyên gia viết mô tả YouTube bằng tiếng Việt. "
                "YÊU CẦU BẮT BUỘC: Chỉ trả về đúng phần mô tả cuối cùng bằng tiếng Việt, không lời dẫn, "
                "không mở đầu kiểu 'Here is...', không ghi chú, không hỏi thêm, không đặt trong dấu ngoặc kép. "
                "Viết 4-6 câu, giọng tự nhiên, giàu hình ảnh, có 1 câu kêu gọi hành động cuối đoạn. "
                "Không bịa thông tin không có trong tiêu đề/từ khoá. "
                f"Tiêu đề video: {title or '(chưa có)'}\n"
                f"Từ khoá từ tên file: {keywords}\n"
                "Đầu ra chỉ là đoạn mô tả hoàn chỉnh."
            )

            description = self._generate_with_ollama(model=model, prompt=prompt)
            if not description:
                raise ValueError("Ollama không trả về nội dung mô tả.")
            description = self._clean_generated_description(description)

            def update_desc():
                current = self.tab_forms.get(tab_id)
                if not current:
                    return
                current["description_text"].delete("1.0", "end")
                current["description_text"].insert("1.0", description.strip())
                self._log("✅ Đã generate mô tả bằng Ollama cho tab hiện tại.")

            self.root.after(0, update_desc)
        except Exception as exc:  # noqa: BLE001
            self._log(f"❌ Generate mô tả thất bại: {exc}")
            self.root.after(0, lambda: messagebox.showerror("Lỗi Ollama", str(exc)))
        finally:
            self.root.after(0, lambda: self._set_generating_description_state(False))

    def _generate_with_ollama(self, model, prompt):
        payload = json.dumps(
            {
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.7},
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            f"{OLLAMA_BASE_URL}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise ConnectionError(
                "Không kết nối được Ollama tại http://localhost:11434. Hãy mở Ollama trước rồi thử lại."
            ) from exc

        return data.get("response", "").strip()

    def _validate_inputs(self, form):
        video_path = form["video_path_var"].get().strip()
        title = form["title_var"].get().strip()

        if not video_path:
            messagebox.showwarning("Thiếu thông tin", "Tab hiện tại chưa có file video.")
            return None
        if not os.path.exists(video_path):
            messagebox.showerror("Lỗi", f"Không tìm thấy file: {video_path}")
            return None
        if not title:
            messagebox.showwarning("Thiếu thông tin", "Vui lòng nhập tiêu đề video.")
            return None

        publish_at = None
        if form["schedule_enabled_var"].get():
            raw = form["schedule_entry_var"].get().strip()
            if not raw:
                messagebox.showwarning(
                    "Thiếu thông tin", "Vui lòng nhập thời gian đặt lịch hoặc bỏ chọn ô đặt lịch."
                )
                return None
            try:
                local_dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                messagebox.showwarning(
                    "Sai định dạng",
                    "Thời gian phải theo định dạng YYYY-MM-DDTHH:MM:SS (ví dụ 2026-07-09T16:07:00).",
                )
                return None

            local_tz = datetime.now().astimezone().tzinfo
            publish_at = (
                local_dt.replace(tzinfo=local_tz)
                .astimezone(timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")
            )

        tags = [t.strip() for t in form["tags_var"].get().split(",") if t.strip()]
        category_id = CATEGORIES.get(form["category_var"].get(), "22")
        privacy_status = next(
            (k for k, v in PRIVACY_LABELS.items() if v == form["privacy_var"].get()), "private"
        )
        made_for_kids = next(
            (k for k, v in AUDIENCE_LABELS.items() if v == form["audience_var"].get()), False
        )

        return {
            "video_path": video_path,
            "title": title,
            "description": form["description_text"].get("1.0", "end").strip(),
            "tags": tags,
            "category_id": category_id,
            "privacy_status": privacy_status,
            "made_for_kids": made_for_kids,
            "publish_at": publish_at,
        }

    def _start_upload(self):
        if self.is_uploading:
            return

        form = self._current_form()
        if not form:
            messagebox.showwarning("Thiếu thông tin", "Vui lòng thêm video (tab) trước khi upload.")
            return

        data = self._validate_inputs(form)
        if not data:
            return

        self._set_uploading_state(True)
        self.progress_bar["value"] = 0
        self.progress_label.config(text="0%")

        thread = threading.Thread(target=self._run_upload, args=(data,), daemon=True)
        thread.start()

    def _run_upload(self, data):
        try:
            if self.youtube_service is None:
                self.youtube_service = get_authenticated_service(log=self._log)

            upload_video(
                youtube=self.youtube_service,
                video_path=data["video_path"],
                title=data["title"],
                description=data["description"],
                tags=data["tags"],
                category_id=data["category_id"],
                privacy_status=data["privacy_status"],
                made_for_kids=data["made_for_kids"],
                publish_at=data["publish_at"],
                progress_callback=self._set_progress,
                log=self._log,
            )
            self.root.after(0, lambda: messagebox.showinfo("Thành công", "Video tab hiện tại đã được tải lên YouTube!"))
        except Exception as exc:  # noqa: BLE001
            self._log(f"❌ Lỗi: {exc}")
            self.root.after(0, lambda: messagebox.showerror("Lỗi", str(exc)))
        finally:
            self.root.after(0, lambda: self._set_uploading_state(False))


def main():
    root = tk.Tk()
    YoutubeUploaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
