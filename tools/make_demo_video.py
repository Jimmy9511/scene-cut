"""生成演示素材（渐变 + 文字，硬切 + 叠化），用于 README 截图与本地演示。

用法：python tools/make_demo_video.py [输出路径]
输出：tests/fixtures/demo/demo.mp4（1280x720，18s，切点 6s / 12s）
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "tests" / "fixtures" / "demo" / "demo.mp4"
W, H = 1280, 720

SCENES = [
    ("01", (18, 26, 44), (32, 84, 128), "CITY / DAWN"),
    ("02", (30, 16, 40), (150, 60, 90), "FOREST / NOON"),
    ("03", (10, 30, 30), (40, 130, 110), "SEA / DUSK"),
]


def font(size: int) -> ImageFont.FreeTypeFont:
    for name in ("segoeuib.ttf", "arialbd.ttf", "msyhbd.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def gradient(c0: tuple[int, int, int], c1: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):
        for x in range(0, W, 4):
            t = (x / W) * 0.6 + (y / H) * 0.4
            col = tuple(int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3))
            for dx in range(4):
                if x + dx < W:
                    px[x + dx, y] = col
    return img


def make_scene(index: str, c0, c1, title: str) -> Image.Image:
    img = gradient(c0, c1)
    d = ImageDraw.Draw(img, "RGBA")
    for i in range(6):
        x = 120 + i * 170
        d.ellipse([x, 420, x + 260, 680], outline=(255, 255, 255, 26), width=2)
    d.rectangle([80, 80, 80 + 520, 80 + 220], outline=(255, 255, 255, 40), width=2)
    d.text((110, 110), index, font=font(120), fill=(255, 255, 255))
    d.text((110, 240), title, font=font(34), fill=(220, 230, 245))
    d.text((82, 620), "scene-cut demo", font=font(22), fill=(200, 210, 225))
    return img


def main(argv: list[str]) -> int:
    out = Path(argv[0]) if argv else DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="sc-demo-"))
    paths = []
    for index, c0, c1, title in SCENES:
        p = tmp / f"scene{index}.png"
        make_scene(index, c0, c1, title).save(p)
        paths.append(p)

    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for p in paths:
        cmd += ["-loop", "1", "-t", "6", "-i", str(p)]
    filter_complex = (
        "[0:v]scale=1280:720,setsar=1,fps=30[v0];"
        "[1:v]scale=1280:720,setsar=1,fps=30[v1];"
        "[2:v]scale=1280:720,setsar=1,fps=30[v2];"
        "[v1][v2]xfade=transition=fade:duration=1:offset=5[x1];"
        "[v0][x1]concat=n=2:v=1:a=0[out]"
    )
    cmd += ["-filter_complex", filter_complex, "-map", "[out]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", str(out)]
    subprocess.run(cmd, check=True)
    print("saved", out, f"{out.stat().st_size/1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
