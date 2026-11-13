"""检测耗时基准：在真实素材上对比解码引擎 / 抽帧开关。

用法：
  python tools/bench_detect.py <video> [--preset 实拍短片] [--fast] [--limit 20000] [--no-ai]
输出：
  每个阶段耗时（probe/t1/decode/t2/fuse）与切点数量、切点帧号。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import log as logmod           # noqa: E402
from app.detect.pipeline import analyze  # noqa: E402


def run(video: Path, preset: str, use_ai: bool, fast: bool, limit: int,
        decoder: str) -> dict:
    os.environ["SCENECUT_DECODER"] = decoder
    out = ROOT / "out" / "bench" / decoder
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    result = analyze(video, out, asset_id=f"bench_{decoder}", preset_name=preset,
                     use_ai=use_ai, ai_fast=fast, t2_sample_limit=limit)
    wall = time.perf_counter() - t0
    data = json.loads(Path(result["analysis_file"]).read_text(encoding="utf-8"))
    timings = data.get("run", {}).get("timings_ms", {})
    return {
        "decoder": data.get("run", {}).get("decode_engine", decoder),
        "t2_step": data.get("run", {}).get("t2_step", 1),
        "probe": timings.get("probe", 0),
        "t1": timings.get("t1", 0),
        "decode": timings.get("decode", 0),
        "t2": timings.get("t2", 0),
        "fuse": timings.get("fuse", 0),
        "wall_s": round(wall, 1),
        "shots": result["shots"],
        "cuts": [c[0] for c in result["cuts"]],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--preset", default="实拍短片")
    ap.add_argument("--fast", action="store_true", help="开启长片抽帧")
    ap.add_argument("--limit", type=int, default=20000)
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--decoders", default="ffmpeg,cv2")
    ap.add_argument("--log-level", default="warning")
    args = ap.parse_args()

    logmod.setup(args.log_level)
    video = Path(args.video).expanduser()
    if not video.exists():
        print("素材不存在:", video)
        return 1

    rows = []
    for decoder in [d.strip() for d in args.decoders.split(",") if d.strip()]:
        print(f"\n=== decoder={decoder} fast={args.fast} ai={not args.no_ai} ===")
        rows.append(run(video, args.preset, not args.no_ai, args.fast, args.limit, decoder))

    print("\n解码引擎 | t2_step | probe |  t1   | decode |   t2   | fuse | 总计 | 镜头 | 切点")
    print("-" * 100)
    for r in rows:
        total = r["probe"] + r["t1"] + r["decode"] + r["t2"] + r["fuse"]
        print(f"{r['decoder']:8s} | {r['t2_step']:>7d} | {r['probe']:5d} | {r['t1']:5d} | "
              f"{r['decode']:6d} | {r['t2']:6d} | {r['fuse']:4d} | {total / 1000:5.1f}s | "
              f"{r['shots']:4d} | {len(r['cuts'])}")

    if len(rows) == 2:
        same = rows[0]["cuts"] == rows[1]["cuts"]
        print(f"\n切点是否完全一致: {same}")
        if not same:
            print("  ", rows[0]["decoder"], rows[0]["cuts"])
            print("  ", rows[1]["decoder"], rows[1]["cuts"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
