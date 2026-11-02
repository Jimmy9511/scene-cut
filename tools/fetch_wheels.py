"""从阿里云镜像直连下载 wheel（本机 pip 走不通时的备用方案）。

用法：
    python tools/fetch_wheels.py pyinstaller pyinstaller-hooks-contrib altgraph pefile pywin32-ctypes
"""

from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

MIRROR = "https://mirrors.aliyun.com/pypi/simple/"
OUT = Path(__file__).resolve().parents[1] / "vendor" / "wheels"
FILE_RE = re.compile(r'href="([^"]+)"[^>]*>([^<]+)</a>')


def _pick(files: list[tuple[str, str]]) -> str | None:
    """在候选文件里挑当前 Python 可用的最新 wheel。"""
    vi = sys.version_info
    tags = [f"cp{vi.major}{vi.minor}", "py3", "cp3"]
    best = None
    for url, name in files:
        if not name.endswith(".whl"):
            continue
        low = name.lower()
        if "win_amd64" not in low and "any" not in low:
            continue
        if not any(t in low for t in tags):
            continue
        best = url
    return best


def _abs(url: str) -> str:
    if url.startswith("http"):
        return url
    if url.startswith("../../"):
        return "https://mirrors.aliyun.com/pypi/" + url[6:]
    if url.startswith("/"):
        return "https://mirrors.aliyun.com" + url
    return MIRROR + url


def fetch(pkg: str) -> bool:
    try:
        html = urllib.request.urlopen(f"{MIRROR}{pkg}/", timeout=30).read().decode("utf-8", "ignore")
    except Exception as exc:  # noqa: BLE001
        print(f"[{pkg}] 索引失败: {exc}")
        return False
    files = [(m.group(1), m.group(2)) for m in FILE_RE.finditer(html)]
    url = _pick(files)
    if not url:
        print(f"[{pkg}] 未找到可用 wheel（{len(files)} 个文件）")
        return False
    url = _abs(url)
    name = url.rsplit("/", 1)[-1].split("#")[0]
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / name
    if target.exists():
        print(f"[{pkg}] 已存在 {name}")
        return True
    with urllib.request.urlopen(url, timeout=120) as r, target.open("wb") as fh:
        while chunk := r.read(1 << 20):
            fh.write(chunk)
    print(f"[{pkg}] 下载 {name} ({target.stat().st_size / 1e6:.1f} MB)")
    return True


def main(argv: list[str]) -> int:
    ok = True
    for pkg in argv:
        ok = fetch(pkg) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
