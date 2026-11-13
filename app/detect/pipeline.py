"""检测流水线：probe → T1 → T2 → T3 → 融合 → 写 analysis JSON。"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from app import __version__
from app import log as logmod
from app.detect import t1_scenedetect, t2_transnet
from app.detect.fusion import build_shots, fuse
from app.detect.probe import keyframes, probe
from app.errors import Cancelled
from app.media.decode import decode_frames, decode_frames_ffmpeg

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

# 进度回调：(阶段, 0~1 进度, 备注)
ProgressFn = Callable[[str, float, str], None]

# 各阶段在总进度中的区间
PHASE_SPAN = {
    "probe": (0.00, 0.05),
    "t1": (0.05, 0.35),
    "decode": (0.35, 0.60),
    "t2": (0.60, 0.95),
    "fuse": (0.95, 0.98),
    "write": (0.98, 1.00),
}


def _gray_size(n_frames: int) -> tuple[int, int]:
    for w, h in GRAY_CANDIDATES:
        if n_frames * w * h <= GRAY_BUDGET_BYTES:
            return w, h
    return GRAY_CANDIDATES[-1]


def _params_hash(preset: dict) -> str:
    payload = json.dumps(preset, sort_keys=True, ensure_ascii=False) + __version__
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


class _Heartbeat:
    """长阶段心跳：每 5 秒打一行 detail，便于判断"卡在哪一步"。"""

    def __init__(self, tag: str, label: str, every: float = 5.0) -> None:
        self.tag = tag
        self.label = label
        self.every = every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._t0 = time.perf_counter()

    def _run(self) -> None:
        while not self._stop.wait(self.every):
            elapsed = time.perf_counter() - self._t0
            logmod.detail(self.tag, f"{self.label} 进行中 {elapsed:.0f}s")

    def __enter__(self) -> _Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()


def analyze(
    path: str | Path,
    out_dir: str | Path,
    *,
    asset_id: str = "a_001",
    preset_name: str = "实拍短片",
    use_ai: bool = True,
    anchor: str = "mid",
    progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
    include_keyframes: bool = False,
    probe_timeout: float = 60.0,
    ai_fast: bool = False,
    t2_sample_limit: int = 20000,
) -> dict:
    tag = "detect"
    path = Path(path).resolve()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    preset = PRESETS.get(preset_name, PRESETS["实拍短片"])

    def emit(phase: str, fraction: float, note: str = "") -> None:
        if progress is not None:
            try:
                progress(phase, max(0.0, min(1.0, fraction)), note)
            except Exception:  # noqa: BLE001 - 进度回调不能影响检测
                pass

    def check_cancel() -> None:
        if should_cancel is not None and should_cancel():
            raise Cancelled("用户取消")

    def span(name: str, ratio: float) -> float:
        lo, hi = PHASE_SPAN[name]
        return lo + (hi - lo) * max(0.0, min(1.0, ratio))

    t_start = time.perf_counter()
    check_cancel()

    # ---- T0：探测 ----
    emit("probe", 0.0, "读取流信息")
    t0 = time.perf_counter()
    info = probe(path, timeout=probe_timeout)
    probe_ms = int((time.perf_counter() - t0) * 1000)
    fps = info["fps"] or 30.0
    n_meta = info.get("nb_frames") or 0
    logmod.info(tag, f"开始 素材={path.name} 帧={n_meta or '?'} fps={fps:g} "
                     f"{info.get('width')}x{info.get('height')} 预设={preset_name} "
                     f"ai={'on' if use_ai else 'off'}")
    logmod.detail(tag, f"probe 完成 {probe_ms}ms codec={info.get('codec')} "
                       f"pix={info.get('pix_fmt')} vfr={info.get('vfr')} "
                       f"rotation={info.get('rotation')} hdr={info.get('hdr')}")
    emit("probe", 0.05, f"{info.get('width')}x{info.get('height')}")
    check_cancel()

    # ---- T1：统计检测 ----
    emit("t1", 0.05, "统计检测")
    t0 = time.perf_counter()
    t1 = t1_scenedetect.detect_t1(str(path), preset)
    t1_ms = int((time.perf_counter() - t0) * 1000)
    logmod.info(tag, f"t1 完成 {t1_ms}ms 候选={len(t1.candidates)}")
    emit("t1", 0.35, f"候选 {len(t1.candidates)}")
    check_cancel()

    # ---- 单遍解码（T2 与 T3 共用）----
    emit("decode", 0.35, "解码")
    t0 = time.perf_counter()
    sizes: dict = {}

    def on_decode(done: int, total: int) -> None:
        if total:
            emit("decode", span("decode", done / total), f"{done}/{total}")
        elif done % 300 == 0:
            emit("decode", span("decode", 0.5), f"{done} 帧")

    rgb = np.empty((0, T2_H, T2_W, 3), dtype=np.uint8)
    gray = np.empty((0, 54, 96), dtype=np.uint8)
    frame_total = n_meta

    # 灰度尺寸：按帧数控制在 ~150MB 预算内
    gw, gh = _gray_size(frame_total or 5000)
    decoder = os.getenv("SCENECUT_DECODER", "ffmpeg").strip().lower()
    decode_engine = "cv2"
    if decoder != "cv2":
        try:
            rgb, gray = decode_frames_ffmpeg(
                str(path), (T2_W, T2_H), (gw, gh), total=frame_total,
                progress=on_decode, should_cancel=should_cancel,
            )
            decode_engine = "ffmpeg"
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - ffmpeg 异常时回退 cv2
            logmod.warn(tag, f"ffmpeg 解码失败，回退 cv2：{exc}")
    if decode_engine == "cv2":
        rgb, gray = decode_frames(
            str(path), (T2_W, T2_H), (gw, gh),
            progress=on_decode, should_cancel=should_cancel,
        )
    decode_ms = int((time.perf_counter() - t0) * 1000)
    n = min(len(rgb), len(gray))
    rgb, gray = rgb[:n], gray[:n]
    sizes = {"w": gw, "h": gh}
    logmod.info(tag, f"解码 完成 {decode_ms}ms 帧={n} 灰度={gw}x{gh} 引擎={decode_engine}")
    emit("decode", 0.60, f"{n} 帧")
    check_cancel()

    # ---- T2：神经网络 ----
    probs = None
    t2_ms = 0
    t2_step = 1
    if use_ai:
        emit("t2", 0.60, f"{n} 帧")
        t2_input = rgb
        if ai_fast and n > t2_sample_limit:
            t2_step = max(1, -(-n // max(1, t2_sample_limit)))   # ceil
            t2_input = rgb[::t2_step]
            logmod.warn(tag, f"长片快速模式：每 {t2_step} 帧取 1（{len(t2_input)}/{n} 帧），"
                             f"渐变转场定位误差 ≤ {t2_step} 帧")
        eta = len(t2_input) / 90.0  # CPU 实测约 90 帧/秒
        logmod.info(tag, f"t2 开始 {len(t2_input)} 帧（预计 ≈{eta:.0f}s，CPU 约 90 帧/秒）")
        t0 = time.perf_counter()
        try:
            with _Heartbeat(tag, "t2"):
                probs = t2_transnet.predict_probs(t2_input, device="auto")
            if t2_step > 1:
                probs = np.repeat(probs, t2_step)[:n]
        except ImportError:
            logmod.warn(tag, "未安装 torch，跳过 TransNetV2 渐变检测（本次仅 T1 统计检测）")
            probs = None
        except Cancelled:
            raise
        t2_ms = int((time.perf_counter() - t0) * 1000)
        dev = t2_transnet.device() if probs is not None else "n/a"
        logmod.info(tag, f"t2 完成 {t2_ms}ms device={dev}")
        emit("t2", 0.95, dev)
        check_cancel()

    # ---- 融合 + 写盘 ----
    emit("fuse", 0.95, "融合边界")
    t0 = time.perf_counter()
    boundaries = fuse(t1.candidates, t1.fades, probs, gray, fps, anchor=anchor)
    shots = build_shots(boundaries, n, fps)
    fuse_ms = int((time.perf_counter() - t0) * 1000)
    logmod.detail(tag, f"融合 完成 {fuse_ms}ms 边界={len(boundaries)} 镜头={len(shots)}")

    emit("write", 0.98, "写 analysis.json")
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
            "timings_ms": {"t1": t1_ms, "t2": t2_ms, "fuse": fuse_ms,
                           "probe": probe_ms, "decode": decode_ms},
            "device": t2_transnet.device() if use_ai else "n/a",
            "gray_size": sizes,
            "decode_engine": decode_engine,
            "t2_step": t2_step,
            "ai_fast": bool(ai_fast),
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
    emit("done", 1.0, f"{len(shots)} 镜头")

    kf = len(keyframes(path, timeout=300.0)) if include_keyframes else 0
    logmod.info(tag, f"完成 {len(shots)} 镜头 用时 {elapsed}ms "
                     f"(probe={probe_ms} t1={t1_ms} decode={decode_ms} t2={t2_ms} fuse={fuse_ms})")

    return {
        "path": str(path),
        "frames": n,
        "fps": fps,
        "elapsed_ms": elapsed,
        "timings_ms": {"t1": t1_ms, "t2": t2_ms, "fuse": fuse_ms},
        "shots": len(shots),
        "cuts": [(b.frame, b.kind, b.confidence) for b in boundaries],
        "analysis_file": str(target),
        "keyframes": kf,
    }
