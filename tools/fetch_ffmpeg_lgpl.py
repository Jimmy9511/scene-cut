"""下载 LGPL 版 ffmpeg（BtbN），放入 dist/scene-cut/_internal/_bin。

发布合规要求：随包分发的 ffmpeg 必须是 LGPL 构建（不含 x264/x265）。
开发机可以继续用系统里的 GPL 构建，但**不要**把 GPL 的 exe 复制进产物。

用法：python tools/fetch_ffmpeg_lgpl.py
"""

from __future__ import annotations

import io
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "dist" / "scene-cut" / "_internal" / "_bin"
MIRRORS = [
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl.zip",
    "https://mirror.ghproxy.com/https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl.zip",
]


def download() -> bytes:
    last: Exception | None = None
    for url in MIRRORS:
        try:
            print("下载", url)
            with urllib.request.urlopen(url, timeout=180) as r:
                data = r.read()
            print(f"  {len(data)/1e6:.1f} MB")
            return data
        except Exception as exc:  # noqa: BLE001
            print("  失败:", type(exc).__name__)
            last = exc
    raise SystemExit(f"全部下载源失败：{last}")


def main() -> int:
    data = download()
    TARGET.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        picked = 0
        for name in zf.namelist():
            base = Path(name).name
            if base.lower() in ("ffmpeg.exe", "ffprobe.exe"):
                with zf.open(name) as src, (TARGET / base).open("wb") as dst:
                    dst.write(src.read())
                picked += 1
                print("解出", base)
        if picked < 2:
            print("压缩包里没找到 ffmpeg.exe / ffprobe.exe")
            return 1
    print("完成，产物：", TARGET)
    print("提示：这是 LGPL 构建，可随包分发；请同时在 THIRD_PARTY_NOTICES.md 里保留源码链接。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
