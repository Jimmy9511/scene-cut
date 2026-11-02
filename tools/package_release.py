"""打包 Windows 发布物：zip + SHA256 + 发布说明。

用法：python tools/package_release.py [--version v0.9.0]
产物：release/scene-cut-<version>-win64.zip
      release/SHA256SUMS.txt
      release/RELEASE_NOTES.md
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "scene-cut"
OUT = ROOT / "release"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="v0.9.0")
    args = ap.parse_args()

    if not (DIST / "scene-cut.exe").exists():
        print("缺少 dist/scene-cut，先运行 python tools/build_all.py")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    zip_path = OUT / f"scene-cut-{args.version}-win64.zip"
    if zip_path.exists():
        zip_path.unlink()

    print("打包", zip_path, "...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(DIST.rglob("*")):
            if path.is_file():
                zf.write(path, Path("scene-cut") / path.relative_to(DIST))

    digest = sha256(zip_path)
    (OUT / "SHA256SUMS.txt").write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8")

    notes = f"""# scene-cut {args.version} (Windows x64)

## 下载

- `{zip_path.name}`（{zip_path.stat().st_size / 1e6:.0f} MB）
- 校验：`{digest}`

## 使用

1. 解压到任意目录（不要放进 `Program Files`）
2. 双击 `scene-cut.exe`：启动本地服务并打开独立窗口（最小 1200×780）
3. 需要系统 `PATH` 中有 `ffmpeg` / `ffprobe`，或放到 `_internal\\_bin\\`

## 说明

- 数据默认写入 `%LOCALAPPDATA%\\SceneCut`（可用 `SCENECUT_DATA` 覆盖）
- 窗口依赖系统 WebView2 运行时（Win10/11 默认附带）
- 本包**不包含 ffmpeg**；如需随包分发，请使用 LGPL 构建并附源码链接
- exe 未签名，首次运行会出现 SmartScreen 提示，选择“仍要运行”
"""
    (OUT / "RELEASE_NOTES.md").write_text(notes, encoding="utf-8")
    print(f"完成：{zip_path.name}  {zip_path.stat().st_size / 1e6:.0f} MB")
    print("SHA256:", digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
