"""scene-cut M0 CLI：跑一条素材的检测，输出 analysis JSON 与摘要。"""

from __future__ import annotations

import argparse
import json
import sys

from app.detect.pipeline import PRESETS, analyze


def _force_utf8_stdio() -> None:
    """把 stdout/stderr 切到 UTF-8：Windows CI/老终端默认 cp1252/cp936，打印中文会崩。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    ap = argparse.ArgumentParser(prog="run.py", description="scene-cut M0 检测")
    ap.add_argument("video", help="输入视频路径")
    ap.add_argument("-o", "--out", default="out", help="analysis 输出目录")
    ap.add_argument("--asset-id", default="a_001")
    ap.add_argument("--preset", default="实拍短片", choices=list(PRESETS))
    ap.add_argument("--anchor", default="mid", choices=["mid", "end"],
                    help="渐变转场切点锚点")
    ap.add_argument("--no-ai", action="store_true", help="只用 T1 统计检测")
    ap.add_argument("--json", action="store_true", help="打印完整 JSON 摘要")
    args = ap.parse_args(argv)

    res = analyze(
        args.video,
        args.out,
        asset_id=args.asset_id,
        preset_name=args.preset,
        use_ai=not args.no_ai,
        anchor=args.anchor,
    )

    print(f"文件      : {res['path']}")
    print(f"帧数/FPS  : {res['frames']} / {res['fps']:.3f}")
    print(f"耗时      : {res['elapsed_ms']} ms "
          f"(T1={res['timings_ms']['t1']} T2={res['timings_ms']['t2']} fuse={res['timings_ms']['fuse']})")
    print(f"镜头数    : {res['shots']}   关键帧: {res['keyframes']}")
    print("切点:")
    for frame, kind, conf in res["cuts"]:
        sec = frame / res["fps"] if res["fps"] else 0
        print(f"  frame={frame:<6d} t={sec:8.3f}s  type={kind:<8s} conf={conf:.2f}")
    print(f"JSON      : {res['analysis_file']}")

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
