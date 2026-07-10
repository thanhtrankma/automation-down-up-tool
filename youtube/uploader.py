import hashlib
import json
import os
import pickle
import re
import time

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

# Quyền upload là bắt buộc; openid + email để nhận diện tài khoản
UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
SCOPES = [
    UPLOAD_SCOPE,
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

from services.paths import (
    ACCOUNTS_FILE,
    CLIENT_SECRETS_FILE,
    TOKEN_FILE,
    TOKENS_DIR,
    ensure_project_dirs,
    migrate_legacy_layout,
)

CATEGORIES = {
    "Phim & Hoạt hình": "1",
    "Ô tô & Xe cộ": "2",
    "Âm nhạc": "10",
    "Thú cưng & Động vật": "15",
    "Thể thao": "17",
    "Du lịch & Sự kiện": "19",
    "Trò chơi (Gaming)": "20",
    "Người & Blog": "22",
    "Hài kịch": "23",
    "Giải trí": "24",
    "Tin tức & Chính trị": "25",
    "Hướng dẫn & Phong cách": "26",
    "Giáo dục": "27",
    "Khoa học & Công nghệ": "28",
}

PRIVACY_OPTIONS = ["private", "unlisted", "public"]

UPLOAD_LIMIT_REASON = "uploadLimitExceeded"

UPLOAD_LIMIT_HELP = (
    "YouTube đã chặn upload vì vượt hạn mức trong 24 giờ.\n\n"
    "Cách xử lý:\n"
    "• Đợi ~24 giờ rồi bấm «Upload hàng chờ»\n"
    "• Xác minh số điện thoại tại YouTube Studio (tăng hạn mức)\n"
    "• Đăng nhập thêm tài khoản YouTube khác và bật «Tự chuyển tài khoản»\n"
    "• Giảm «Giới hạn upload/lần chạy» để tránh vượt hạn mức"
)


class UploadLimitExceededError(Exception):
    """Tài khoản YouTube đã hết hạn mức upload trong ngày."""

    def __init__(self, account_label: str = "", details: str = "") -> None:
        self.account_label = account_label
        self.details = details
        label = f" ({account_label})" if account_label else ""
        super().__init__(f"YouTube hết hạn mức upload{label}. {UPLOAD_LIMIT_HELP}")


def is_upload_limit_error(exc: BaseException) -> bool:
    if isinstance(exc, UploadLimitExceededError):
        return True
    if isinstance(exc, HttpError):
        return _http_error_reason(exc) == UPLOAD_LIMIT_REASON
    return False


def _http_error_reason(exc: HttpError) -> str:
    try:
        payload = json.loads(exc.content.decode("utf-8"))
        errors = payload.get("error", {}).get("errors", [])
        if errors:
            return str(errors[0].get("reason", ""))
    except (AttributeError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    return ""


def raise_for_upload_error(exc: HttpError, *, account_label: str = "") -> None:
    if _http_error_reason(exc) == UPLOAD_LIMIT_REASON:
        raise UploadLimitExceededError(account_label=account_label, details=str(exc)) from exc
    raise exc


def _ensure_tokens_dir() -> None:
    migrate_legacy_layout()
    ensure_project_dirs()
    os.makedirs(TOKENS_DIR, exist_ok=True)


def _load_accounts_data() -> dict:
    if not os.path.exists(ACCOUNTS_FILE):
        return {"active_account": "", "accounts": {}}
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("active_account", "")
        data.setdefault("accounts", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"active_account": "", "accounts": {}}


def _save_accounts_data(data: dict) -> None:
    _ensure_tokens_dir()
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _token_path(account_id: str) -> str:
    safe_id = re.sub(r"[^\w\-]", "_", account_id)
    return os.path.join(TOKENS_DIR, f"{safe_id}.pickle")


def _load_credentials(account_id: str):
    path = _token_path(account_id)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


def _save_credentials(account_id: str, credentials) -> None:
    _ensure_tokens_dir()
    with open(_token_path(account_id), "wb") as f:
        pickle.dump(credentials, f)


def _account_id_from_credentials(credentials) -> str:
    token = getattr(credentials, "refresh_token", None) or getattr(credentials, "token", None) or ""
    digest = hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:12]
    return f"acc_{digest}"


def _granted_scopes(credentials) -> set[str]:
    return set(getattr(credentials, "scopes", None) or [])


def _credentials_need_reauth(credentials) -> bool:
    """True nếu token thiếu quyền upload hoặc scope không khớp cấu hình hiện tại."""
    if not credentials:
        return True
    granted = _granted_scopes(credentials)
    if not granted:
        return not getattr(credentials, "valid", False)
    if UPLOAD_SCOPE not in granted:
        return True
    return not set(SCOPES).issubset(granted)


def _credentials_have_all_scopes(credentials) -> bool:
    return not _credentials_need_reauth(credentials)


def _run_oauth_flow(log=print, *, force_consent: bool = False):
    log("🌐 Đang mở trình duyệt để đăng nhập Google...")
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
    if force_consent:
        credentials = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    else:
        credentials = flow.run_local_server(port=0, access_type="offline")

    granted = _granted_scopes(credentials)
    if granted and UPLOAD_SCOPE not in granted:
        raise ValueError(
            "Google chưa cấp quyền upload YouTube. "
            "Vui lòng đăng nhập lại và chọn đủ quyền upload video lên YouTube."
        )
    return credentials


def _refresh_or_reauth(credentials, log=print, *, force_consent: bool = False):
    if _credentials_need_reauth(credentials):
        return _run_oauth_flow(log, force_consent=True)

    if credentials.expired and credentials.refresh_token:
        try:
            log("🔄 Đang làm mới token đăng nhập...")
            credentials.refresh(Request())
            if _credentials_need_reauth(credentials):
                log("⚠️ Token sau refresh thiếu quyền, đăng nhập lại...")
                return _run_oauth_flow(log, force_consent=True)
            return credentials
        except Exception as exc:  # noqa: BLE001
            if "scope" in str(exc).lower():
                log("⚠️ Scope token đã thay đổi, đăng nhập lại...")
                return _run_oauth_flow(log, force_consent=True)
            raise

    if not credentials.valid:
        return _run_oauth_flow(log, force_consent=force_consent)
    return credentials


def _get_account_identity(credentials) -> tuple[str, str]:
    """Lấy nhãn hiển thị và account_id từ email hoặc hash token."""
    try:
        oauth2 = build("oauth2", "v2", credentials=credentials)
        info = oauth2.userinfo().get().execute()
        email = (info.get("email") or "").strip()
        if email:
            digest = hashlib.sha256(email.encode("utf-8")).hexdigest()[:12]
            return email, f"email_{digest}"
    except HttpError as exc:
        if exc.resp.status not in (401, 403):
            raise
    except Exception:
        pass

    account_id = _account_id_from_credentials(credentials)
    return f"Tài khoản ({account_id[-6:]})", account_id


def _get_channel_info_safe(youtube, credentials) -> tuple[str, str]:
    """Giữ tương thích tên hàm cũ, ưu tiên email thay vì gọi YouTube channels API."""
    _ = youtube
    return _get_account_identity(credentials)


def sync_accounts_from_disk(log=print) -> None:
    """Đồng bộ accounts.json từ các file token đã lưu."""
    _ensure_tokens_dir()
    data = _load_accounts_data()
    changed = False

    if not os.path.isdir(TOKENS_DIR):
        return

    for filename in os.listdir(TOKENS_DIR):
        if not filename.endswith(".pickle"):
            continue
        path = os.path.join(TOKENS_DIR, filename)
        try:
            with open(path, "rb") as f:
                credentials = pickle.load(f)
            label, account_id = _get_account_identity(credentials)
            existing = data.get("accounts", {}).get(account_id)
            if not existing:
                data.setdefault("accounts", {})[account_id] = {
                    "label": label,
                    "channel_id": account_id,
                }
                changed = True
            elif existing.get("label") != label:
                existing["label"] = label
                changed = True
            if not data.get("active_account"):
                data["active_account"] = account_id
                changed = True
        except Exception as exc:  # noqa: BLE001
            log(f"⚠️ Bỏ qua token {filename}: {exc}")

    if changed:
        _save_accounts_data(data)


def list_accounts() -> list[dict]:
    data = _load_accounts_data()
    accounts = []
    for account_id, info in data.get("accounts", {}).items():
        accounts.append(
            {
                "id": account_id,
                "label": info.get("label") or account_id,
                "channel_id": info.get("channel_id") or account_id,
            }
        )
    accounts.sort(key=lambda x: x["label"].lower())
    return accounts


def get_active_account_id() -> str:
    return _load_accounts_data().get("active_account", "")


def get_active_account_label() -> str:
    data = _load_accounts_data()
    account_id = data.get("active_account", "")
    if not account_id:
        return "Chưa đăng nhập"
    return data.get("accounts", {}).get(account_id, {}).get("label", account_id)


def set_active_account(account_id: str) -> None:
    data = _load_accounts_data()
    if account_id and account_id not in data.get("accounts", {}):
        raise ValueError(f"Không tìm thấy tài khoản: {account_id}")
    data["active_account"] = account_id
    _save_accounts_data(data)


def remove_account(account_id: str, log=print) -> None:
    data = _load_accounts_data()
    if account_id not in data.get("accounts", {}):
        return
    label = data["accounts"][account_id].get("label", account_id)
    del data["accounts"][account_id]
    if data.get("active_account") == account_id:
        remaining = next(iter(data["accounts"]), "")
        data["active_account"] = remaining
    _save_accounts_data(data)

    token_file = _token_path(account_id)
    if os.path.exists(token_file):
        os.remove(token_file)
    log(f"🗑️ Đã xóa tài khoản: {label}")


def _migrate_legacy_token(log=print) -> None:
    if not os.path.exists(TOKEN_FILE):
        return
    data = _load_accounts_data()
    if data.get("accounts"):
        return

    log("🔄 Đang chuyển token cũ sang hệ thống đa tài khoản...")
    with open(TOKEN_FILE, "rb") as f:
        credentials = pickle.load(f)

    label, account_id = _get_account_identity(credentials)

    _save_credentials(account_id, credentials)
    data.setdefault("accounts", {})[account_id] = {"label": label, "channel_id": account_id}
    data["active_account"] = account_id
    _save_accounts_data(data)
    log(f"✅ Đã nhập tài khoản cũ: {label}")


def _authenticate_credentials(account_id: str | None, force_login: bool, log=print):
    if not os.path.exists(CLIENT_SECRETS_FILE):
        raise FileNotFoundError(
            f"❌ Không tìm thấy file '{CLIENT_SECRETS_FILE}'. "
            "Hãy đảm bảo file client_secret.json nằm cùng thư mục với script này."
        )

    if force_login:
        return _run_oauth_flow(log, force_consent=True)

    credentials = _load_credentials(account_id) if account_id else None
    if credentials:
        return _refresh_or_reauth(credentials, log)

    return _run_oauth_flow(log, force_consent=True)


def login_new_account(log=print) -> dict:
    """Đăng nhập tài khoản Google/YouTube mới và lưu vào danh sách."""
    try:
        _migrate_legacy_token(log=log)
    except Exception as exc:  # noqa: BLE001
        log(f"⚠️ Không migrate được token cũ: {exc}")

    sync_accounts_from_disk(log=log)

    credentials = _authenticate_credentials(account_id=None, force_login=True, log=log)

    label, account_id = _get_account_identity(credentials)

    _save_credentials(account_id, credentials)
    data = _load_accounts_data()
    data.setdefault("accounts", {})[account_id] = {"label": label, "channel_id": account_id}
    data["active_account"] = account_id
    _save_accounts_data(data)

    log(f"✅ Đã đăng nhập tài khoản: {label}")
    return {"id": account_id, "label": label, "channel_id": account_id}


def get_authenticated_service(account_id: str | None = None, log=print):
    """Đăng nhập và trả về YouTube API service cho tài khoản đã chọn."""
    try:
        _migrate_legacy_token(log=log)
    except Exception as exc:  # noqa: BLE001
        log(f"⚠️ Không migrate được token cũ: {exc}")
    sync_accounts_from_disk(log=log)

    data = _load_accounts_data()
    account_id = account_id or data.get("active_account")
    if not account_id:
        log("ℹ️ Chưa có tài khoản, mở trình duyệt để đăng nhập...")
        account = login_new_account(log=log)
        account_id = account["id"]
    elif account_id not in data.get("accounts", {}):
        raise ValueError(f"Tài khoản không tồn tại: {account_id}")

    credentials = _authenticate_credentials(account_id, force_login=False, log=log)
    _save_credentials(account_id, credentials)

    if account_id != data.get("active_account"):
        data["active_account"] = account_id
        _save_accounts_data(data)

    label = data.get("accounts", {}).get(account_id, {}).get("label", account_id)
    log(f"👤 Đang dùng tài khoản: {label}")
    return build("youtube", "v3", credentials=credentials)


def upload_video(
    youtube,
    video_path,
    title,
    description,
    tags,
    category_id="22",
    privacy_status="private",
    made_for_kids=False,
    publish_at=None,
    progress_callback=None,
    log=print,
    account_label="",
):
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Không tìm thấy file video tại đường dẫn: {video_path}")

    title = (title or "").strip()
    if not title:
        raise ValueError("Tiêu đề video không được để trống.")
    if len(title) > 100:
        title = title[:100].rstrip()

    description = (description or "").strip()

    status_block = {"privacyStatus": privacy_status}
    status_block["selfDeclaredMadeForKids"] = bool(made_for_kids)
    if publish_at:
        status_block["privacyStatus"] = "private"
        status_block["publishAt"] = publish_at

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": status_block,
    }

    media = MediaFileUpload(
        video_path,
        chunksize=1024 * 1024,
        resumable=True,
        mimetype="video/mp4",
    )

    log(f"🚀 Đang chuẩn bị tải lên: {title}...")
    request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=media,
    )

    response = None
    retries = 3
    while response is None:
        try:
            status, response = request.next_chunk()
        except (BrokenPipeError, OSError, HttpError) as exc:
            is_pipe = isinstance(exc, BrokenPipeError) or (
                isinstance(exc, OSError) and getattr(exc, "errno", None) == 32
            )
            is_retriable_http = isinstance(exc, HttpError) and exc.resp.status in (500, 502, 503, 504)
            if isinstance(exc, HttpError) and is_upload_limit_error(exc):
                raise_for_upload_error(exc, account_label=account_label)
            if (is_pipe or is_retriable_http) and retries > 0:
                retries -= 1
                log(f"⚠️ Upload bị ngắt, thử lại... (còn {retries} lần)")
                time.sleep(2)
                continue
            raise
        if status and progress_callback:
            progress_callback(status.progress())

    if progress_callback:
        progress_callback(1.0)

    log(f"✅ Thành công! Video đã được tải lên. ID của video là: {response['id']}")
    log(f"🔗 Link video của bạn: https://youtu.be/{response['id']}")

    return response["id"]


if __name__ == "__main__":
    from apps.upload import main as run_gui

    run_gui()
