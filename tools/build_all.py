"""一键构建：Python 后端（PyInstaller）+ WebView2 原生窗口宿主（csc）。

用法：python tools/build_all.py
产物：dist/scene-cut/{scene-cut.exe, scene-cut-window.exe, WebView2*.dll, _internal/}

解释器：优先用当前运行本脚本的 Python（CI 上没有 .venv，本地通常就是 .venv 里的）；
若 `.venv` 存在且与当前解释器不同，仍以当前解释器为准，避免路径写死。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = Path(sys.executable)


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=ROOT, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def main() -> int:
    print("python:", PY)
    run([str(PY), "-m", "PyInstaller", "--clean", "--noconfirm",
         "--distpath", "dist", "--workpath", "build", "scene-cut.spec"])
    run([str(PY), str(ROOT / "tools" / "build_window.py")])
    print("完成：dist/scene-cut")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
