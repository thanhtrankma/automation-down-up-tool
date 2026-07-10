"""Đường dẫn chuẩn của dự án và migrate cấu trúc cũ."""

from __future__ import annotations

import os
import shutil

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = os.path.join(PROJECT_ROOT, "data")
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
CREDENTIALS_DIR = os.path.join(PROJECT_ROOT, "credentials")

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
CONFIG_EXAMPLE_FILE = os.path.join(CONFIG_DIR, "config.example.json")
PENDING_FILE = os.path.join(DATA_DIR, "pending_uploads.json")

CLIENT_SECRETS_FILE = os.path.join(CREDENTIALS_DIR, "client_secret.json")
ACCOUNTS_FILE = os.path.join(CREDENTIALS_DIR, "accounts.json")
TOKEN_FILE = os.path.join(CREDENTIALS_DIR, "token.pickle")
TOKENS_DIR = os.path.join(CREDENTIALS_DIR, "tokens")

# Vị trí cũ (trước khi tổ chức lại thư mục)
_LEGACY_CONFIG = os.path.join(PROJECT_ROOT, "config.json")
_LEGACY_PENDING = os.path.join(PROJECT_ROOT, "pending_uploads.json")
_LEGACY_UPLOAD_TOOL = os.path.join(PROJECT_ROOT, "upload-tool")


def ensure_project_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(CREDENTIALS_DIR, exist_ok=True)
    os.makedirs(TOKENS_DIR, exist_ok=True)


def _move_if_needed(src: str, dst: str) -> None:
    if os.path.exists(src) and not os.path.exists(dst):
        shutil.move(src, dst)


def migrate_legacy_layout() -> None:
    """Chuyển file từ layout cũ sang layout mới (chạy một lần, an toàn)."""
    ensure_project_dirs()

    _move_if_needed(_LEGACY_CONFIG, CONFIG_FILE)
    _move_if_needed(_LEGACY_PENDING, PENDING_FILE)

    legacy_client = os.path.join(_LEGACY_UPLOAD_TOOL, "client_secret.json")
    legacy_accounts = os.path.join(_LEGACY_UPLOAD_TOOL, "accounts.json")
    legacy_token = os.path.join(_LEGACY_UPLOAD_TOOL, "token.pickle")
    legacy_tokens = os.path.join(_LEGACY_UPLOAD_TOOL, "tokens")

    _move_if_needed(legacy_client, CLIENT_SECRETS_FILE)
    _move_if_needed(legacy_accounts, ACCOUNTS_FILE)
    _move_if_needed(legacy_token, TOKEN_FILE)

    if os.path.isdir(legacy_tokens) and not os.listdir(TOKENS_DIR):
        for name in os.listdir(legacy_tokens):
            _move_if_needed(os.path.join(legacy_tokens, name), os.path.join(TOKENS_DIR, name))
