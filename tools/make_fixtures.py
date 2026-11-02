"""生成合成测试素材（已知切点），用于回归与 M0 验证。

- hardcuts.mp4：3 段硬切，切点 6s / 12s（30fps → 帧 180 / 360）
- dissolve.mp4：两段之间 1s 叠化，过渡区间约 6.0–7.0s
- fade.mp4：淡出到黑再淡入
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"ffmpeg 失败: {proc.stderr[-800:]}")


def make_hardcuts(out: Path, width: int = 1920, height: int = 1080, fps: int = 30) -> Path:
    target = out / "hardcuts.mp4"
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate={fps}:duration=6",
        "-f", "lavfi", "-i", f"smptebars=size={width}x{height}:rate={fps}:duration=6",
        "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate={fps}:duration=6",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        str(target),
    ])
    return target


def make_dissolve(out: Path, width: int = 1280, height: int = 720, fps: int = 30) -> Path:
    target = out / "dissolve.mp4"
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate={fps}:duration=7",
        "-f", "lavfi", "-i", f"smptebars=size={width}x{height}:rate={fps}:duration=7",
        "-filter_complex", "[0:v][1:v]xfade=transition=dissolve:duration=1:offset=6[v]",
        "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        str(target),
    ])
    return target


def make_fade(out: Path, width: int = 1280, height: int = 720, fps: int = 30) -> Path:
    target = out / "fade.mp4"
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate={fps}:duration=5",
        "-f", "lavfi", "-i", f"color=c=black:size={width}x{height}:rate={fps}:duration=1",
        "-f", "lavfi", "-i", f"smptebars=size={width}x{height}:rate={fps}:duration=5",
        "-filter_complex",
        "[0:v]fade=t=out:st=4:d=1[v0];[2:v]fade=t=in:st=0:d=1[v2];"
        "[v0][1:v][v2]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        str(target),
    ])
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tests/fixtures/synthetic")
    ap.add_argument("--only", choices=["hardcuts", "dissolve", "fade"])
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    makers = {"hardcuts": make_hardcuts, "dissolve": make_dissolve, "fade": make_fade}
    for name, fn in makers.items():
        if args.only and name != args.only:
            continue
        p = fn(out)
        print(f"OK {name}: {p} ({p.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
