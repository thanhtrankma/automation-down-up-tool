# Video Tool

Bộ công cụ desktop (Python + Tkinter) hỗ trợ **tải video**, **upload YouTube**, và **quy trình tự động** (tải → generate tiêu đề/mô tả bằng Ollama → upload & đặt lịch).

## Yêu cầu hệ thống

| Thành phần | Ghi chú |
|------------|---------|
| Python 3.10+ | Khuyến nghị 3.11+ |
| ffmpeg / ffprobe | `brew install ffmpeg` (macOS) |
| Ollama | Chỉ cần cho generate tiêu đề/mô tả ([ollama.com](https://ollama.com)) |
| Tài khoản Google Cloud | OAuth cho YouTube Data API v3 |

## Cài đặt

```bash
cd download-tool
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Cấu hình YouTube OAuth

1. Tạo project trên [Google Cloud Console](https://console.cloud.google.com/)
2. Bật **YouTube Data API v3**
3. Tạo **OAuth Client ID** (loại Desktop)
4. Tải file JSON, đổi tên thành `client_secret.json` và đặt vào:

```
credentials/client_secret.json
```

## Chạy ứng dụng

```bash
python3 main.py
```

Menu chính gồm 4 mục:

| Tính năng | Mô tả |
|-----------|--------|
| **Tải videos** | Tải video/playlist/kênh bằng yt-dlp |
| **Upload Video** | Upload thủ công, nhiều tab, generate mô tả Ollama |
| **Quy trình tự động** | Tải hoặc chọn thư mục → generate → upload & lịch |
| **Tự động cắt video** | Chuyển ngang → dọc (16:9 → 9:16), crop / blur / pad |

### Chạy từng module (tuỳ chọn)

```bash
python3 -m apps.download    # Chỉ tải video
python3 -m apps.upload      # Chỉ upload
python3 -m apps.pipeline    # Chỉ quy trình tự động
python3 -m apps.cut_video   # Chỉ chuyển tỉ lệ ngang → dọc
```

File `app.py` và `video_downloader.py` ở thư mục gốc vẫn chạy được (tương thích ngược).

## Cấu trúc thư mục

```
download-tool/
├── main.py                 # Điểm vào chính — menu chọn tính năng
├── requirements.txt
├── README.md
│
├── apps/                   # Giao diện từng tính năng
│   ├── download.py         # Tải videos (yt-dlp)
│   ├── upload.py           # Upload YouTube
│   ├── pipeline.py         # Quy trình tự động
│   └── cut_video.py        # Chuyển tỉ lệ ngang → dọc
│
├── services/               # Logic dùng chung
│   ├── paths.py            # Đường dẫn chuẩn & migrate layout cũ
│   ├── config_store.py     # Đọc/ghi cấu hình
│   ├── downloader.py       # Wrapper yt-dlp
│   ├── ollama_client.py    # Generate tiêu đề/mô tả
│   ├── video_prep.py       # Chuẩn hóa video (H.264+AAC)
│   ├── aspect_convert.py   # Chuyển tỉ lệ khung hình (FFmpeg)
│   └── pending_uploads.py  # Hàng chờ khi hết hạn mức upload
│
├── youtube/
│   └── uploader.py         # OAuth & upload YouTube API
│
├── config/
│   └── config.example.json # Mẫu cấu hình
│
├── data/                   # Dữ liệu runtime (không commit)
│   ├── config.json           # Cấu hình người dùng
│   └── pending_uploads.json  # Video chờ upload
│
└── credentials/            # OAuth & token (không commit)
    ├── client_secret.json
    ├── accounts.json
    └── tokens/
```

## Quy trình tự động (pipeline)

1. Chọn nguồn: **link** hoặc **thư mục video sẵn có**
2. (Tuỳ chọn) Generate tiêu đề bằng Ollama hoặc dùng tên file
3. Generate mô tả bằng Ollama
4. Convert video sang H.264+AAC nếu cần (YouTube/QuickTime)
5. Upload lên YouTube với lịch đăng (mặc định: video đầu sau 10 phút, mỗi video cách nhau 3 giờ)

Cấu hình lưu tại `data/config.json`. Có thể sao chép từ `config/config.example.json` lần đầu.

## Hết hạn mức upload YouTube

YouTube giới hạn số video upload / 24h (kênh mới thường ~6–15 video). Khi gặp lỗi `uploadLimitExceeded`:

- Video được lưu vào **hàng chờ** (`data/pending_uploads.json`)
- Bấm **«Upload hàng chờ»** sau khi hạn mức reset (~24h)
- Bật **«Tự chuyển tài khoản»** nếu có nhiều kênh YouTube
- Đặt **«Giới hạn upload/lần chạy»** để tránh vượt hạn mức một lúc
- Xác minh số điện thoại tại YouTube Studio để tăng hạn mức

## Chuyển ngang → dọc (Tự động cắt video)

Menu **Tự động cắt video** dùng FFmpeg để đổi tỉ lệ, ví dụ **16:9 → 9:16** (1080×1920) cho YouTube Shorts / Reels / TikTok.

| Chế độ | Mô tả |
|--------|--------|
| **Phóng + nền mờ (blur)** | Giữ toàn bộ nội dung, nền blur (mặc định, đẹp cho Shorts) |
| **Cắt giữa (crop)** | Cắt vùng giữa / trái / phải theo khung dọc |
| **Viền đen (pad)** | Giữ toàn bộ, thêm viền đen |

Có thể chọn nhiều file hoặc cả thư mục. File đầu ra: `tên_1080x1920.mp4` (cạnh file gốc hoặc thư mục bạn chọn).

## Ollama

```bash
ollama serve
ollama pull llama3
```

Trong app, chọn model Ollama trong phần Cấu hình.

## Ghi chú bảo mật

- **Không commit** `credentials/` và `data/config.json` lên git
- File `client_secret.json` và token OAuth là thông tin nhạy cảm
- Thư mục `credentials/` và `data/` đã được thêm vào `.gitignore`

## Migrate từ cấu trúc cũ

Nếu bạn đã dùng phiên bản trước (file `config.json` ở gốc, thư mục `upload-tool/`), app sẽ **tự chuyển** sang layout mới khi khởi động lần đầu. Bạn cũng có thể di chuyển thủ công:

- `config.json` → `data/config.json`
- `upload-tool/client_secret.json` → `credentials/client_secret.json`
- `upload-tool/accounts.json` → `credentials/accounts.json`
- `upload-tool/tokens/` → `credentials/tokens/`

## Khắc phục sự cố

| Vấn đề | Gợi ý |
|--------|--------|
| Lỗi SSL khi tải | Đã dùng `certifi`; chạy `pip install certifi` |
| Không tìm thấy video trong thư mục | Cần `ffprobe`; file phải có stream video |
| Ollama không kết nối | Mở Ollama app hoặc `ollama serve` |
| `client_secret.json` not found | Đặt file vào `credentials/` |
| QuickTime không phát được | Bật «Ưu tiên tương thích QuickTime» khi tải |

## License

Dự án cá nhân / nội bộ. Sử dụng tuân thủ điều khoản YouTube và nền tảng nguồn khi tải video.
