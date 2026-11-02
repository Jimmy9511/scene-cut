"""T0 探测：ffprobe 拉流信息 + 关键帧表。

设计要点（见方案 §4.2）：
- 全量帧表只在候选窗内解析；这里只取关键帧表（-skip_frame nokey），长片也快。
- 旋转、HDR、VFR 都要记录，否则缩略图/预览/阈值都会错。
"""

from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path


def _run(args: list[str]) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe 失败 ({proc.returncode}): {proc.stderr[-400:]}")
    return proc.stdout


def _rate(value: str | None) -> float:
    if not value or value in ("0/0", "N/A"):
        return 0.0
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path: str | Path) -> dict:
    """返回统一结构的探测信息。"""
    raw = json.loads(
        _run([
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_streams", "-show_format", str(path),
        ])
    )
    vstream = next((s for s in raw.get("streams", []) if s.get("codec_type") == "video"), None)
    if vstream is None:
        raise RuntimeError(f"没有视频流: {path}")

    fps = _rate(vstream.get("avg_frame_rate")) or _rate(vstream.get("r_frame_rate"))
    nominal = _rate(vstream.get("r_frame_rate"))
    rotation = 0
    for sd in vstream.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(float(sd["rotation"]))
            break

    duration = float(raw.get("format", {}).get("duration") or vstream.get("duration") or 0.0)
    nb_frames = int(vstream.get("nb_frames") or 0)
    if nb_frames <= 0 and duration > 0 and fps > 0:
        nb_frames = int(round(duration * fps))

    return {
        "duration": round(duration, 6),
        "width": int(vstream.get("width") or 0),
        "height": int(vstream.get("height") or 0),
        "fps": round(fps, 6),
        "nominal_fps": round(nominal, 6),
        "vfr": abs(fps - nominal) > 0.01 * max(fps, 1.0),
        "rotation": rotation,
        "hdr": (vstream.get("color_transfer") or "").lower() in ("smpte2084", "arib-std-b67"),
        "codec": vstream.get("codec_name") or "",
        "pix_fmt": vstream.get("pix_fmt") or "",
        "nb_frames": nb_frames,
        "start_time": float(vstream.get("start_time") or 0.0),
        "bit_rate": int(vstream.get("bit_rate") or 0),
    }


def keyframes(path: str | Path) -> list[float]:
    """关键帧 pts 列表（秒）。"""
    out = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-skip_frame", "nokey", "-show_entries", "frame=pts_time",
        "-print_format", "json", str(path),
    ])
    pts = []
    for frame in json.loads(out).get("frames", []):
        try:
            pts.append(float(frame["pts_time"]))
        except (KeyError, ValueError):
            continue
    return pts


def frame_pts_window(path: str | Path, start: float, end: float) -> list[float]:
    """候选窗内的精确帧时间戳（只在窗口内调用，避免长片全局解析）。"""
    out = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-read_intervals", f"{max(start, 0):.3f}%{(end + 0.2):.3f}",
        "-show_entries", "frame=pts_time", "-print_format", "json", str(path),
    ])
    pts = []
    for frame in json.loads(out).get("frames", []):
        try:
            pts.append(float(frame["pts_time"]))
        except (KeyError, ValueError):
            continue
    return pts
