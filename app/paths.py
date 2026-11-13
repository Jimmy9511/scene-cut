"""路径解析：区分「源码运行」与「PyInstaller 打包运行」。

- BUNDLE_ROOT：只读资源（web/），打包后在 _MEIPASS 内。
- DATA_ROOT：可写数据（工作区、素材池索引、日志、设置、WebView2 缓存）。
  优先级：SCENECUT_DATA 环境变量（含启动器 --data-dir）→ 指针文件 data_root.txt
  → %LOCALAPPDATA%\\SceneCut → 程序目录\\data → %TEMP%\\SceneCut。
  任何一步不可写都会自动降级，不会因为权限问题直接崩溃。

指针文件（默认在 exe 同目录，源码模式在仓库根）只有一行绝对路径，
用于「界面里把数据目录改到别的盘」——它必须留在稳定位置，不能跟着数据搬走。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
WEB_DIR = BUNDLE_ROOT / "web"
EXE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parents[1]

POINTER_NAME = "data_root.txt"


def pointer_path() -> Path:
    """指针文件位置：exe 同目录（源码模式为仓库根）。"""
    return EXE_DIR / POINTER_NAME


def read_pointer() -> Path | None:
    try:
        raw = pointer_path().read_text(encoding="utf-8").strip().strip('"')
    except (OSError, UnicodeDecodeError):
        return None
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else None


def write_pointer(root: Path) -> Path:
    target = pointer_path()
    target.write_text(str(Path(root).resolve()), encoding="utf-8")
    return target


def legacy_root() -> Path | None:
    """系统默认数据目录（用于界面展示"默认位置"）。"""
    if os.getenv("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "SceneCut"
    return None


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _candidates() -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    if os.getenv("SCENECUT_DATA"):
        out.append((Path(os.environ["SCENECUT_DATA"]), "env"))
    pointer = read_pointer()
    if pointer is not None:
        out.append((pointer, "pointer"))
    if os.getenv("LOCALAPPDATA"):
        out.append((Path(os.environ["LOCALAPPDATA"]) / "SceneCut", "default"))
    if not FROZEN:
        out.append((Path(__file__).resolve().parents[1], "default"))
    out.append((EXE_DIR / "data", "fallback"))
    out.append((Path(tempfile.gettempdir()) / "SceneCut", "fallback"))
    return out


def resolve_data_root() -> tuple[Path, str]:
    """返回 (可写数据目录, 来源标签)。"""
    for cand, source in _candidates():
        if _is_writable(cand):
            return cand, source
    raise RuntimeError("找不到可写的数据目录（已尝试 SCENECUT_DATA / 指针 / LOCALAPPDATA / 程序目录 / TEMP）")


DATA_ROOT, DATA_ROOT_SOURCE = resolve_data_root()
