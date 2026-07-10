"""Lưu video đã chuẩn bị nhưng chưa upload được (ví dụ hết hạn mức YouTube)."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from services.paths import PENDING_FILE, ensure_project_dirs, migrate_legacy_layout


def _load_raw() -> list[dict[str, Any]]:
    migrate_legacy_layout()
    ensure_project_dirs()
    if not os.path.exists(PENDING_FILE):
        return []
    try:
        with open(PENDING_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_raw(items: list[dict[str, Any]]) -> None:
    ensure_project_dirs()
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def list_pending() -> list[dict[str, Any]]:
    return _load_raw()


def count_pending() -> int:
    return len(_load_raw())


def add_pending(item: dict[str, Any]) -> dict[str, Any]:
    items = _load_raw()
    entry = {
        "id": item.get("id") or str(uuid.uuid4()),
        "created_at": item.get("created_at") or datetime.now(timezone.utc).isoformat(),
        **item,
    }
    items.append(entry)
    _save_raw(items)
    return entry


def remove_pending(item_id: str) -> bool:
    items = _load_raw()
    new_items = [item for item in items if item.get("id") != item_id]
    if len(new_items) == len(items):
        return False
    _save_raw(new_items)
    return True
