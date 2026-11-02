"""路径解析：区分「源码运行」与「PyInstaller 打包运行」。

- BUNDLE_ROOT：只读资源（web/），打包后在 _MEIPASS 内。
- DATA_ROOT：可写数据（工作区、素材池索引、缓存）。
  优先级：SCENECUT_DATA 环境变量 → %LOCALAPPDATA%\\SceneCut → 程序目录\\data → %TEMP%\\SceneCut。
  任何一步不可写都会自动降级，不会因为权限问题直接崩溃。
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


def _candidates() -> list[Path]:
    out: list[Path] = []
    if os.getenv("SCENECUT_DATA"):
        out.append(Path(os.environ["SCENECUT_DATA"]))
    if FROZEN and os.getenv("LOCALAPPDATA"):
        out.append(Path(os.environ["LOCALAPPDATA"]) / "SceneCut")
    if not FROZEN:
        out.append(Path(__file__).resolve().parents[1])
    out.append(EXE_DIR / "data")
    out.append(Path(tempfile.gettempdir()) / "SceneCut")
    return out


def _pick_root() -> Path:
    for cand in _candidates():
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return cand
        except OSError:
            continue
    raise RuntimeError("找不到可写的数据目录（已尝试 SCENECUT_DATA / LOCALAPPDATA / 程序目录 / TEMP）")


DATA_ROOT = _pick_root()
