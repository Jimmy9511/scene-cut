"""帧级预览后端（M0 最小实现）。

- 冷启动单帧：ffmpeg -ss 精确 seek + 单帧输出，实测 ~120–200 ms/次（含进程启动）。
- 后续 M1 改为常驻解码进程 + LRU 预取（方案 §6.3），目标 <40 ms。
"""

from __future__ import annotations

import subprocess


def frame_jpeg(path: str, t: float, width: int = 960, quality: int = 4) -> bytes:
    """取 t 秒处的单帧（帧精确，-ss 在 -i 前，重编码为 JPEG）。"""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(t, 0.0):.6f}", "-i", str(path),
        "-frames:v", "1",
        "-vf", f"scale={width}:-2",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", str(quality),
        "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"取帧失败: {proc.stderr[-300:]!r}")
    return proc.stdout


def frame_at_frame_number(path: str, frame: int, fps: float, width: int = 960) -> bytes:
    """按帧号取帧（帧号 → 秒，半帧偏移避免浮点边界）。"""
    return frame_jpeg(path, (frame + 0.5) / max(fps, 1e-6), width=width)
