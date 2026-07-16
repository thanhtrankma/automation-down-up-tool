"""Hỗ trợ đăng nhập Douyin bằng Playwright và tải video trực tiếp từ API."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from services.paths import DATA_DIR, ensure_project_dirs

DOUYIN_LOGIN_URL = "https://www.douyin.com/"
DOUYIN_PROFILE_DIR = os.path.join(DATA_DIR, "douyin_browser_profile")
DOUYIN_STATE_HINT_FILE = os.path.join(DOUYIN_PROFILE_DIR, ".session")
DOUYIN_VIDEO_PATH_MARKER = "/video/"
DOUYIN_USER_PATH_MARKER = "/user/"
MAX_SCROLL_ROUNDS = 24
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class DouyinAutomationError(RuntimeError):
    """Lỗi khi điều khiển browser hoặc tải video Douyin."""


@dataclass
class DouyinVideoItem:
    aweme_id: str
    title: str
    page_url: str
    download_urls: list[str]


def _log(on_log: Optional[Callable[[str], None]], text: str) -> None:
    if on_log:
        on_log(text)


def _ensure_profile_dir() -> str:
    ensure_project_dirs()
    os.makedirs(DOUYIN_PROFILE_DIR, exist_ok=True)
    return DOUYIN_PROFILE_DIR


def _touch_session_hint() -> None:
    os.makedirs(DOUYIN_PROFILE_DIR, exist_ok=True)
    with open(DOUYIN_STATE_HINT_FILE, "w", encoding="utf-8") as f:
        f.write(str(int(time.time())))


def has_saved_session() -> bool:
    return os.path.isdir(DOUYIN_PROFILE_DIR) and any(os.scandir(DOUYIN_PROFILE_DIR))


def is_douyin_profile_url(url: str) -> bool:
    parsed = urlparse(url)
    return DOUYIN_USER_PATH_MARKER in (parsed.path or "") and DOUYIN_VIDEO_PATH_MARKER not in (parsed.path or "")


def is_douyin_video_url(url: str) -> bool:
    return DOUYIN_VIDEO_PATH_MARKER in (urlparse(url).path or "")


def _import_playwright():
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - phụ thuộc môi trường cài đặt
        raise DouyinAutomationError(
            "Thiếu Playwright. Hãy chạy `pip install -r requirements.txt` rồi `python3 -m playwright install chromium`."
        ) from exc
    return sync_playwright, PlaywrightTimeoutError


def _normalize_video_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = urljoin(DOUYIN_LOGIN_URL, url)
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _extract_aweme_id(url: str) -> str:
    match = re.search(r"/video/(\d+)", url)
    return match.group(1) if match else ""


def _sanitize_filename(title: str, aweme_id: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", (title or "").strip()) or aweme_id
    return safe[:120]


def _extract_download_urls(item: dict) -> list[str]:
    urls: list[str] = []
    video = item.get("video") or {}

    for key in ("download_addr", "play_addr"):
        addr = video.get(key) or {}
        urls.extend(addr.get("url_list") or [])

    for bit_rate in video.get("bit_rate") or []:
        play_addr = bit_rate.get("play_addr") or {}
        urls.extend(play_addr.get("url_list") or [])

    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(url.replace("http://", "https://"))
    return deduped


def _parse_aweme_item(item: dict) -> Optional[DouyinVideoItem]:
    aweme_id = str(item.get("aweme_id") or "")
    if not aweme_id:
        return None
    title = (item.get("desc") or f"douyin_{aweme_id}").strip()
    download_urls = _extract_download_urls(item)
    if not download_urls:
        return None
    return DouyinVideoItem(
        aweme_id=aweme_id,
        title=title,
        page_url=f"https://www.douyin.com/video/{aweme_id}",
        download_urls=download_urls,
    )


def _cookies_header(cookies: list[dict]) -> str:
    pairs = []
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if name and value is not None:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def get_playwright_cookies() -> list[dict]:
    sync_playwright, _ = _import_playwright()
    profile_dir = _ensure_profile_dir()
    if not has_saved_session():
        raise DouyinAutomationError(
            "Chưa có session Douyin. Hãy bấm 'Đăng nhập Douyin (Playwright)' trước."
        )

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            profile_dir,
            headless=True,
            accept_downloads=False,
        )
        try:
            return context.cookies()
        finally:
            context.close()


def open_login_browser(*, on_log: Optional[Callable[[str], None]] = None) -> None:
    """Mở Chromium profile cố định để người dùng đăng nhập Douyin thủ công."""
    sync_playwright, _ = _import_playwright()
    profile_dir = _ensure_profile_dir()
    _log(on_log, "Đang mở Chromium profile riêng cho Douyin...")
    _log(on_log, "Sau khi đăng nhập xong, hãy đóng cửa sổ Chromium để lưu session.")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            profile_dir,
            headless=False,
            accept_downloads=False,
            viewport={"width": 1440, "height": 960},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(DOUYIN_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            page.bring_to_front()
            page.wait_for_event("close", timeout=0)
        finally:
            context.close()
            _touch_session_hint()
            _log(on_log, "Đã đóng Chromium. Session Douyin đã được lưu cục bộ.")


def _collect_aweme_items_from_response(data: dict, items_by_id: dict[str, DouyinVideoItem]) -> int:
    added = 0
    if data.get("aweme_detail"):
        parsed = _parse_aweme_item(data["aweme_detail"])
        if parsed and parsed.aweme_id not in items_by_id:
            items_by_id[parsed.aweme_id] = parsed
            added += 1
    for raw in data.get("aweme_list") or []:
        parsed = _parse_aweme_item(raw)
        if parsed and parsed.aweme_id not in items_by_id:
            items_by_id[parsed.aweme_id] = parsed
            added += 1
    return added


def _is_aweme_api_response(url: str) -> bool:
    markers = (
        "/aweme/v1/web/aweme/post/",
        "/web/aweme/post/",
        "/aweme/v1/web/aweme/detail/",
        "/aweme/detail/",
    )
    return any(marker in url for marker in markers)


def fetch_douyin_videos(
    source_url: str,
    *,
    on_log: Optional[Callable[[str], None]] = None,
    max_items: int = 0,
) -> list[DouyinVideoItem]:
    """Lấy metadata + link tải trực tiếp từ API Douyin trong session Playwright."""
    sync_playwright, PlaywrightTimeoutError = _import_playwright()
    profile_dir = _ensure_profile_dir()
    if not has_saved_session():
        raise DouyinAutomationError(
            "Chưa có session Douyin. Hãy bấm 'Đăng nhập Douyin (Playwright)' trước."
        )

    items_by_id: dict[str, DouyinVideoItem] = {}
    target_url = source_url.strip()
    _log(on_log, "Đang dùng session Douyin đã lưu để lấy link tải trực tiếp...")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            profile_dir,
            headless=False,
            accept_downloads=False,
            viewport={"width": 1440, "height": 960},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()

            def on_response(response) -> None:
                if not _is_aweme_api_response(response.url):
                    return
                try:
                    data = response.json()
                except Exception:
                    return
                added = _collect_aweme_items_from_response(data, items_by_id)
                if added:
                    _log(on_log, f"Đã bắt thêm {added} video từ API Douyin (tổng {len(items_by_id)}).")

            page.on("response", on_response)
            page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass

            if is_douyin_profile_url(target_url):
                stagnant_rounds = 0
                last_count = 0
                for round_index in range(MAX_SCROLL_ROUNDS):
                    page.mouse.wheel(0, 2200)
                    page.wait_for_timeout(1500)
                    current_count = len(items_by_id)
                    _log(on_log, f"Quét Douyin vòng {round_index + 1}: đã thấy {current_count} video có link tải...")

                    if max_items and current_count >= max_items:
                        break
                    if current_count == last_count:
                        stagnant_rounds += 1
                    else:
                        stagnant_rounds = 0
                    last_count = current_count
                    if stagnant_rounds >= 3:
                        break
            else:
                page.wait_for_timeout(2500)

        finally:
            context.close()

    results = list(items_by_id.values())
    if max_items > 0:
        results = results[:max_items]

    if not results and is_douyin_video_url(target_url):
        aweme_id = _extract_aweme_id(target_url)
        if aweme_id:
            raise DouyinAutomationError(
                f"Không lấy được link tải trực tiếp cho video {aweme_id}. Hãy thử lại sau khi đăng nhập."
            )

    if not results:
        raise DouyinAutomationError(
            "Không lấy được danh sách video từ Douyin. Hãy kiểm tra lại việc đăng nhập và URL."
        )

    _log(on_log, f"Đã lấy được {len(results)} video có link tải trực tiếp từ Douyin.")
    return results


def fetch_profile_video_urls(
    profile_url: str,
    *,
    on_log: Optional[Callable[[str], None]] = None,
    max_items: int = 0,
) -> list[str]:
    """Giữ API cũ: trả về danh sách URL trang video."""
    items = fetch_douyin_videos(profile_url, on_log=on_log, max_items=max_items)
    return [item.page_url for item in items]


def download_douyin_videos(
    items: list[DouyinVideoItem],
    output_dir: str,
    cookies: list[dict],
    *,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[str, float], None]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> int:
    """Tải video Douyin trực tiếp bằng link từ API, không qua yt-dlp."""
    os.makedirs(output_dir, exist_ok=True)
    videos_dir = os.path.join(output_dir, "Videos")
    os.makedirs(videos_dir, exist_ok=True)

    cookie_header = _cookies_header(cookies)
    if not cookie_header:
        raise DouyinAutomationError("Không có cookie hợp lệ để tải video Douyin.")

    downloaded_count = 0
    for index, item in enumerate(items, start=1):
        if is_cancelled and is_cancelled():
            break

        filename = f"{_sanitize_filename(item.title, item.aweme_id)}.mp4"
        filepath = os.path.join(videos_dir, filename)
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            _log(on_log, f"[{index}/{len(items)}] Bỏ qua (đã có): {item.title}")
            downloaded_count += 1
            continue

        success = False
        for mirror_index, download_url in enumerate(item.download_urls, start=1):
            try:
                request = Request(
                    download_url,
                    headers={
                        "Cookie": cookie_header,
                        "Referer": item.page_url,
                        "User-Agent": DEFAULT_USER_AGENT,
                    },
                )
                with urlopen(request, timeout=90) as response:
                    total = int(response.headers.get("Content-Length") or 0)
                    received = 0
                    with open(filepath, "wb") as file_obj:
                        while True:
                            if is_cancelled and is_cancelled():
                                break
                            chunk = response.read(1024 * 256)
                            if not chunk:
                                break
                            file_obj.write(chunk)
                            received += len(chunk)
                            if on_progress and total > 0:
                                on_progress(item.title, received / total * 100)
                if os.path.getsize(filepath) > 0:
                    success = True
                    downloaded_count += 1
                    _log(on_log, f"[{index}/{len(items)}] Đã tải xong: {item.title}")
                    break
                os.remove(filepath)
            except Exception as exc:  # noqa: BLE001
                if os.path.exists(filepath):
                    os.remove(filepath)
                _log(
                    on_log,
                    f"[{index}/{len(items)}] Mirror {mirror_index} lỗi ({item.title}): {exc}",
                )

        if not success:
            _log(on_log, f"[{index}/{len(items)}] Không tải được: {item.title}")

    return downloaded_count
