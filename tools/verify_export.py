"""M0 验证：三档导出的帧精确性与偏移报告。

用法：
    python tools/verify_export.py tests/fixtures/synthetic/hardcuts.mp4 --starts 6.0 7.0 --dur 2.0
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.export.cut import cut_copy, cut_exact, cut_smart, duration, frame_count


def _frame_at(path: str | Path, t: float, size=(160, 90)) -> np.ndarray | None:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-ss", f"{t:.6f}", "-i", str(path), "-frames:v", "1",
         "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    img = cv2.imdecode(np.frombuffer(proc.stdout, np.uint8), cv2.IMREAD_GRAYSCALE)
    return cv2.resize(img, size, interpolation=cv2.INTER_AREA) if img is not None else None


def _mse(a: np.ndarray, b: np.ndarray) -> float:
    d = a.astype(np.float32) - b.astype(np.float32)
    return float(np.mean(d * d) / (255.0 * 255.0))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--starts", nargs="+", type=float, default=[6.0, 7.0])
    ap.add_argument("--dur", type=float, default=2.0)
    args = ap.parse_args(argv)

    src = Path(args.source)
    from app.detect.probe import probe as probe_media
    meta = probe_media(str(src))
    fps = meta["fps"]
    print(f"源: {src}  时长={duration(src):.3f}s  fps={fps:.3f}")
    print(f"{'start':>6} {'mode':<20} {'帧数':>5} {'期望':>5} {'实际起点':>9} {'偏移':>7} "
          f"{'首帧MSE':>8} {'帧精确':>6}")

    with tempfile.TemporaryDirectory() as td:
        for start in args.starts:
            end = start + args.dur
            runs = [
                ("copy", lambda o: cut_copy(src, start, end, o)),
                ("exact(nvenc)", lambda o: cut_exact(src, start, end, o, encoder="nvenc")),
                ("exact(x264)", lambda o: cut_exact(src, start, end, o, encoder="x264")),
                ("smart", lambda o: cut_smart(src, start, end, o)),
            ]
            for name, fn in runs:
                out = Path(td) / f"{name.replace('(', '_').replace(')', '')}_{start}.mp4"
                try:
                    info = fn(str(out))
                except RuntimeError as exc:
                    print(f"{start:6.2f} {name:<20} 失败: {str(exc)[:70]}")
                    continue
                mode = str(info.get("mode", name))
                ref_t = info.get("actual_start", start) if "copy" in mode else start
                ref = _frame_at(src, ref_t)
                first = _frame_at(out, 0.0)
                mse = _mse(ref, first) if (ref is not None and first is not None) else float("nan")
                frames = frame_count(out)
                expected = int(round((end - ref_t) * fps))
                print(f"{start:6.2f} {mode:<20} {frames:>5} {expected:>5} "
                      f"{info.get('actual_start', 0):>9.3f} "
                      f"{info.get('offset', 0):>7.3f} {mse:>8.4f} "
                      f"{'EXACT' if mse < 0.002 else 'OFF':>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
