"""三档导出：copy（无损） / smart（段首补码） / exact（帧级重编码）。

方案 §7.1。M0 只做片段级导出与帧精确性校验，命名模板/清单在 M1 接入。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from app.detect.probe import keyframes


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败 ({proc.returncode}): {proc.stderr[-600:]}")


def duration(path: str | Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    return float(json.loads(proc.stdout)["format"]["duration"])


def frame_count(path: str | Path) -> int:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    try:
        return int(json.loads(proc.stdout)["streams"][0]["nb_read_frames"])
    except (KeyError, IndexError, ValueError):
        return -1


def next_keyframe(path: str | Path, t: float) -> float | None:
    for kf in keyframes(path):
        if kf >= t - 1e-3:
            return kf
    return None


def keyframe_before(path: str | Path, t: float) -> float:
    """请求起点的实际落点：≤t 的最后一个关键帧（copy 模式必然吸附到它）。"""
    before = 0.0
    for kf in keyframes(path):
        if kf <= t + 1e-6:
            before = kf
        else:
            break
    return before


def cut_copy(src: str | Path, start: float, end: float, out: str | Path,
             container_args: list[str] | None = None) -> dict:
    """无损流拷贝：起点吸附到 ≤start 的最近关键帧；返回实际落点与偏移。"""
    actual = keyframe_before(src, start)
    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.6f}", "-i", str(src),
        "-t", f"{max(end - start, 0.001):.6f}",
        "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero",
        *(container_args or []), str(out),
    ]
    _run(args)
    return {"mode": "copy", "out": str(out), "requested_start": start,
            "actual_start": actual, "offset": round(actual - start, 4)}


def cut_exact(src: str | Path, start: float, end: float, out: str | Path,
              encoder: str = "nvenc", crf: int = 18, scale: str | None = None,
              frames: int | None = None) -> dict:
    """帧级精确：重编码整段。

    优先用 `-frames:v N` 限定帧数（-t 会把边界那一帧也算进去，导致 +1 帧）。
    """
    vf = ["-vf", scale] if scale else []
    if encoder == "nvenc":
        codec = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
    else:
        codec = ["-c:v", "libx264", "-crf", str(crf), "-preset", "slow"]
    limit = ["-frames:v", str(frames)] if frames and frames > 0 else ["-t", f"{max(end - start, 1e-3):.6f}"]
    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.6f}", "-i", str(src),
        *limit,
        *codec, *vf, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out),
    ]
    _run(args)
    return {"mode": "exact", "out": str(out), "encoder": encoder,
            "actual_start": _first_pts(out)}


def cut_smart(src: str | Path, start: float, end: float, out: str | Path,
              encoder: str = "nvenc", crf: int = 18, fps: float | None = None) -> dict:
    """段首到下一关键帧重编码，其后无损拷贝，再拼接。"""
    kf = next_keyframe(src, start)
    if kf is None or kf >= end:
        # 整段都在同一个 GOP 内：退化为精确重编码
        info = cut_exact(src, start, end, out, encoder=encoder, crf=crf)
        info["mode"] = "smart(->exact)"
        return info
    if kf <= start + 1e-3:
        # 起点本身就是关键帧：等价于无损拷贝
        info = cut_copy(src, start, end, out)
        info["mode"] = "smart(->copy)"
        return info

    head_frames = None
    if fps:
        head_frames = max(1, round(kf * fps) - round(start * fps))
    with tempfile.TemporaryDirectory() as td:
        head = Path(td) / "head.mp4"
        tail = Path(td) / "tail.mp4"
        cut_exact(src, start, kf, head, encoder=encoder, crf=crf, frames=head_frames)
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
              "-ss", f"{kf:.6f}", "-to", f"{end:.6f}", "-i", str(src),
              "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero", str(tail)])
        lst = Path(td) / "list.txt"
        lst.write_text(f"file '{head.as_posix()}'\nfile '{tail.as_posix()}'\n", encoding="utf-8")
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
              "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])
    return {"mode": "smart", "out": str(out), "head_kf": kf,
            "head_sec": round(kf - start, 4), "actual_start": _first_pts(out)}


def _first_pts(path: str | Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "frame=pts_time", "-read_intervals", "%+#1", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(json.loads(proc.stdout)["frames"][0]["pts_time"])
    except (KeyError, IndexError, ValueError):
        return 0.0
