"""检测流水线：probe → T1 → T2 → T3 → 融合 → 写 analysis JSON。"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import numpy as np

from app import __version__
from app.detect import t1_scenedetect, t2_transnet
from app.detect.fusion import build_shots, fuse
from app.detect.probe import keyframes, probe
from app.media.decode import decode_gray, decode_rgb

PRESETS: dict[str, dict] = {
    "实拍短片": {
        "content_threshold": 27.0, "min_scene_len": 15,
        "adaptive_threshold": 3.0, "min_content_val": 15.0,
        "fade_threshold": 12, "auto_accept": 0.75, "review_floor": 0.45,
    },
    "动画": {
        "content_threshold": 32.0, "min_scene_len": 12,
        "adaptive_threshold": 3.5, "min_content_val": 18.0,
        "fade_threshold": 12, "auto_accept": 0.75, "review_floor": 0.45,
    },
    "录屏": {
        "content_threshold": 22.0, "min_scene_len": 30,
        "adaptive_threshold": 2.5, "min_content_val": 12.0,
        "fade_threshold": 10, "auto_accept": 0.72, "review_floor": 0.40,
    },
}

# T2 输入尺寸（TransNetV2 固定 27x48）
T2_W, T2_H = 48, 27
# T3 精修用灰度小图（按帧数自适应，预算 ~150 MB）
GRAY_BUDGET_BYTES = 150 * 1024 * 1024
GRAY_CANDIDATES = [(320, 180), (192, 108), (128, 72), (96, 54)]


def _gray_size(n_frames: int) -> tuple[int, int]:
    for w, h in GRAY_CANDIDATES:
        if n_frames * w * h <= GRAY_BUDGET_BYTES:
            return w, h
    return GRAY_CANDIDATES[-1]


def _params_hash(preset: dict) -> str:
    payload = json.dumps(preset, sort_keys=True, ensure_ascii=False) + __version__
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def analyze(
    path: str | Path,
    out_dir: str | Path,
    *,
    asset_id: str = "a_001",
    preset_name: str = "实拍短片",
    use_ai: bool = True,
    anchor: str = "mid",
) -> dict:
    path = Path(path).resolve()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    preset = PRESETS.get(preset_name, PRESETS["实拍短片"])

    t_start = time.perf_counter()
    info = probe(path)
    fps = info["fps"] or 30.0

    t0 = time.perf_counter()
    t1 = t1_scenedetect.detect_t1(str(path), preset)
    t1_ms = int((time.perf_counter() - t0) * 1000)

    # 一次解码供 T2 / T3 共用（M0 全内存；长素材按帧数自适应降采样）
    rgb = decode_rgb(str(path), (T2_W, T2_H))
    gw, gh = _gray_size(len(rgb))
    gray = decode_gray(str(path), (gw, gh))
    n = min(len(rgb), len(gray))
    rgb, gray = rgb[:n], gray[:n]

    probs = None
    t2_ms = 0
    if use_ai:
        t0 = time.perf_counter()
        probs = t2_transnet.predict_probs(rgb, device="auto")
        t2_ms = int((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    boundaries = fuse(t1.candidates, t1.fades, probs, gray, fps, anchor=anchor)
    shots = build_shots(boundaries, n, fps)
    fuse_ms = int((time.perf_counter() - t0) * 1000)

    elapsed = int((time.perf_counter() - t_start) * 1000)
    analysis = {
        "schema": "scene-cut/1",
        "asset_id": asset_id,
        "source": {
            "path": str(path),
            "size": path.stat().st_size,
            "mtime": int(path.stat().st_mtime),
        },
        "probe": info,
        "run": {
            "engine": "t1+t2" if use_ai else "t1",
            "preset": preset_name,
            "params_hash": _params_hash(preset),
            "app_version": __version__,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "elapsed_ms": elapsed,
            "timings_ms": {"t1": t1_ms, "t2": t2_ms, "fuse": fuse_ms},
            "device": t2_transnet.device() if use_ai else "n/a",
        },
        "shots": shots,
        "clips": [
            {"id": f"k_{i + 1:03d}", "start": s["start"], "end": s["end"],
             "state": "auto", "label": "", "from_shots": [i]}
            for i, s in enumerate(shots)
        ],
    }

    target = out_dir / f"{asset_id}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)

    return {
        "path": str(path),
        "frames": n,
        "fps": fps,
        "elapsed_ms": elapsed,
        "timings_ms": {"t1": t1_ms, "t2": t2_ms, "fuse": fuse_ms},
        "shots": len(shots),
        "cuts": [(b.frame, b.kind, b.confidence) for b in boundaries],
        "analysis_file": str(target),
        "keyframes": len(keyframes(path)),
    }
