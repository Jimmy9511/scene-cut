"""用户设置（持久化到 <数据目录>/settings.json）。

目前只有日志级别，后续设置项直接往里加。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from app.paths import DATA_ROOT

_lock = threading.Lock()
DEFAULTS: dict = {"log_level": "info"}


def path() -> Path:
    return DATA_ROOT / "settings.json"


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        raw = path().read_text(encoding="utf-8")
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            data.update({k: v for k, v in loaded.items() if v is not None})
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return data


def get(key: str, default=None):
    return load().get(key, DEFAULTS.get(key, default))


def update(**fields) -> dict:
    with _lock:
        data = load()
        data.update({k: v for k, v in fields.items() if v is not None})
        target = path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(target)
        except OSError:
            pass
        return data
