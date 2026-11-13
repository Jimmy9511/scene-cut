"""分级日志：控制台 + 文件双写。

级别（数值越大越严重），默认 ``info``：

======  ====  ======================================
级别    数值  用途
======  ====  ======================================
debug   10    HTTP 请求、帧/切片请求、内部细节
detail  15    阶段内部细节（解码进度、候选数、参数）
info    20    用户行为 + 任务进度（默认）
warning 30    可恢复异常（torch 缺失、降级、重试）
error   40    失败（检测/导出失败、未捕获异常）
======  ====  ======================================

配置优先级：``setup(level)`` 显式参数 > ``SCENECUT_LOG_LEVEL`` 环境变量 >
``<数据目录>/settings.json`` 的 ``log_level`` > 默认 info。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

LEVELS: dict[str, int] = {
    "debug": 10,
    "detail": 15,
    "info": 20,
    "warning": 30,
    "error": 40,
}
LEVEL_NAMES: dict[int, str] = {v: k for k, v in LEVELS.items()}
DEFAULT_LEVEL = "info"

_ALIASES = {
    "trace": "debug", "verbose": "detail", "warning": "warning", "warn": "warning",
    "err": "error", "critical": "error", "fatal": "error", "information": "info",
}

MAX_BYTES = 2 * 1024 * 1024
KEEP_FILES = 3

_lock = threading.Lock()
_level = LEVELS[DEFAULT_LEVEL]
_verbosity_name = DEFAULT_LEVEL
_log_file: Path | None = None
_console_ok = True
_throttle: dict[str, float] = {}
_configured = False


def normalize(name: str | None) -> str | None:
    """把用户输入规范成合法级别名；无法识别返回 None。"""
    if not name:
        return None
    key = str(name).strip().lower()
    key = _ALIASES.get(key, key)
    return key if key in LEVELS else None


def _env_level() -> str | None:
    return normalize(os.getenv("SCENECUT_LOG_LEVEL"))


def _settings_level() -> str | None:
    try:
        from app.settings import get as settings_get

        return normalize(settings_get("log_level"))
    except Exception:  # noqa: BLE001 - 日志不能因设置读取失败而崩
        return None


def setup(level: str | None = None, log_dir: Path | None = None) -> str:
    """初始化日志：决定级别、准备文件、把控制台切到 UTF-8。返回生效级别名。"""
    global _level, _verbosity_name, _log_file, _console_ok, _configured

    chosen = normalize(level) or _env_level() or _settings_level() or DEFAULT_LEVEL
    _level = LEVELS[chosen]
    _verbosity_name = chosen
    _configured = True

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass

    if log_dir is not None:
        _log_file = Path(log_dir) / "logs" / "app.log"
        try:
            _log_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            _log_file = None
    return chosen


def ensure() -> None:
    """未显式 setup 时按默认规则初始化一次。"""
    if not _configured:
        setup()


def get_level() -> str:
    return _verbosity_name


def is_enabled(level: str) -> bool:
    return LEVELS.get(level, 999) >= _level


def set_level(name: str) -> str:
    """运行时切换级别（同时写回 settings.json）。返回生效级别名。"""
    global _level, _verbosity_name
    clean = normalize(name)
    if clean is None:
        raise ValueError(f"未知日志级别: {name}")
    _level = LEVELS[clean]
    _verbosity_name = clean
    try:
        from app.settings import update as settings_update

        settings_update(log_level=clean)
    except Exception:  # noqa: BLE001
        pass
    return clean


def _rotate_if_needed() -> None:
    if _log_file is None:
        return
    try:
        if not _log_file.exists() or _log_file.stat().st_size < MAX_BYTES:
            return
        for i in range(KEEP_FILES - 1, 0, -1):
            old = _log_file.with_name(f"{_log_file.name}.{i}")
            new = _log_file.with_name(f"{_log_file.name}.{i + 1}")
            if old.exists():
                old.replace(new)
        _log_file.replace(_log_file.with_name(f"{_log_file.name}.1"))
    except OSError:
        pass


def _write(line: str) -> None:
    global _console_ok
    if _console_ok:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except (OSError, ValueError):
            _console_ok = False
    if _log_file is not None:
        try:
            _rotate_if_needed()
            with _log_file.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def log(level: str, tag: str, message: str, *, throttle: float = 0.0) -> None:
    clean = normalize(level) or DEFAULT_LEVEL
    if not is_enabled(clean):
        return
    if throttle > 0:
        key = f"{tag}|{message[:60]}"
        now = time.time()
        with _lock:
            last = _throttle.get(key, 0.0)
            if now - last < throttle:
                return
            _throttle[key] = now
            if len(_throttle) > 512:            # 防止长跑泄漏
                for k in [k for k, t in _throttle.items() if now - t > 60]:
                    _throttle.pop(k, None)
    line = f"{time.strftime('%H:%M:%S')} {clean.upper():<7} [{tag}] {message}"
    with _lock:
        _write(line)


def debug(tag: str, message: str, **kw) -> None:
    log("debug", tag, message, **kw)


def detail(tag: str, message: str, **kw) -> None:
    log("detail", tag, message, **kw)


def info(tag: str, message: str, **kw) -> None:
    log("info", tag, message, **kw)


def warn(tag: str, message: str, **kw) -> None:
    log("warning", tag, message, **kw)


def error(tag: str, message: str, **kw) -> None:
    log("error", tag, message, **kw)


def progress(tag: str, phase: str, done: int, total: int, note: str = "") -> None:
    """阶段进度：默认写 detail；每 10% 额外写一行 info（由调用方决定是否调用）。"""
    pct = int(done / total * 100) if total else 0
    extra = f" {note}" if note else ""
    detail(tag, f"{phase} {pct}% ({done}/{total}){extra}")
