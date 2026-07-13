"""Đọc/ghi cấu hình pipeline."""

from __future__ import annotations

import json
import os

from services.paths import CONFIG_FILE, ensure_project_dirs, migrate_legacy_layout

DEFAULT_CONFIG = {
    "output_dir": os.path.join(os.path.expanduser("~"), "Downloads", "VideoDownloader"),
    "schedule_interval_hours": 3,
    "first_publish_offset_minutes": 10,
    "title_instruction": "Giữ tone hoài niệm, tự nhiên, dễ click.",
    "generate_title": True,
    "default_tags": "",
    "category": "Người & Blog",
    "privacy": "private",
    "made_for_kids": False,
    "ollama_model": "llama3:latest",
    "quicktime_compat": True,
    "local_source_dir": "",
    "youtube_account_id": "",
    "max_uploads_per_run": 0,
    "auto_switch_account_on_limit": True,
    "delete_source_after_upload": False,
}


def load_config() -> dict:
    migrate_legacy_layout()
    ensure_project_dirs()
    if not os.path.exists(CONFIG_FILE):
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        merged = DEFAULT_CONFIG.copy()
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError):
        return DEFAULT_CONFIG.copy()


def save_config(config: dict) -> None:
    ensure_project_dirs()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
