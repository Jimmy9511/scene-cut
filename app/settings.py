"""用户设置（持久化到 <数据目录>/settings.json）。

字段：
- ``log_level``        日志级别（debug/detail/info/warning/error）
- ``workspace``        工作区目录；null 表示默认 <数据目录>/workspace，否则为绝对路径
- ``ai_fast``          长片 AI 加速（隔帧推理），默认关闭
- ``t2_sample_limit``  启用 ai_fast 的帧数阈值，默认 20000
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from app.paths import DATA_ROOT

_lock = threading.Lock()
DEFAULTS: dict = {
    "log_level": "info",
    "workspace": None,
    "ai_fast": False,
    "t2_sample_limit": 20000,
}


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


def workspace_root(default: Path) -> Path:
    """解析工作区目录：settings.workspace 优先，否则默认 <数据目录>/workspace。"""
    custom = load().get("workspace")
    if custom:
        candidate = Path(str(custom))
        if candidate.is_absolute():
            return candidate
    return Path(default)
