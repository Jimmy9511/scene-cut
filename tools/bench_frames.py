"""M0 验证：帧级预览取帧延迟（冷启动单帧）。"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.detect.probe import probe
from app.media.frames import frame_at_frame_number, frame_jpeg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--samples", type=int, default=12)
    ap.add_argument("--width", type=int, default=960)
    args = ap.parse_args(argv)

    meta = probe(args.source)
    fps = meta["fps"] or 30.0
    total = meta["nb_frames"] or int(meta["duration"] * fps)
    step = max(1, total // (args.samples + 1))

    times: list[float] = []
    sizes: list[int] = []
    for i in range(args.samples):
        frame = min(total - 1, step * (i + 1))
        t0 = time.perf_counter()
        data = frame_at_frame_number(args.source, frame, fps, width=args.width)
        times.append((time.perf_counter() - t0) * 1000)
        sizes.append(len(data))

    # 连续步进（相邻帧，冷启动每次都重启进程）
    seq: list[float] = []
    base = total // 2
    for k in range(5):
        t0 = time.perf_counter()
        frame_jpeg(args.source, (base + k + 0.5) / fps, width=args.width)
        seq.append((time.perf_counter() - t0) * 1000)

    print(f"源: {args.source}  frames={total}  fps={fps:.3f}")
    print(f"随机取帧 {len(times)} 次: 中位 {statistics.median(times):.0f} ms  "
          f"均值 {statistics.mean(times):.0f} ms  P90 {sorted(times)[int(len(times)*0.9)-1]:.0f} ms")
    print(f"连续步进 5 帧: 中位 {statistics.median(seq):.0f} ms")
    print(f"单帧 JPEG 平均 {statistics.mean(sizes)/1024:.0f} KB @ width={args.width}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
