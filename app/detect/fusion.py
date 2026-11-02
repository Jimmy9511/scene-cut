"""融合评分：把 T1 候选、T2 概率、T3 精修合成最终切点表（方案 §4.7）。

M0 打分公式（暂定，M3 用真实标注集调参）：
    score = 0.50 * transnet_prob
          + 0.35 * refine_hint          # 由 MSE 峰值与直方图距离归一而来
          + 0.15 * sharpness            # 单帧尖峰 1.0，非尖峰 0.5
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np

from .t2_transnet import peaks
from .t3_refine import refine

CLUSTER_WINDOW = 5
BLACK_LUMA = 18.0        # 灰度均值低于此值视为黑场
FADE_HALF_WINDOW = 20    # 黑场前后各 20 帧视为淡入淡出区
MIN_SHOT_FRAMES = 12     # 最短镜头（0.4s@30fps），更短的相邻切点合并


@dataclass
class Boundary:
    frame: int
    kind: str
    confidence: float
    sources: list[str] = field(default_factory=list)


def _tc(frame: int, fps: float) -> str:
    ms = int(round(frame * 1000.0 / fps))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{milli:03d}"


def _cluster(items: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    items.sort()
    groups: list[list[tuple[int, str]]] = []
    for frame, src in items:
        if groups and frame - groups[-1][0][0] <= CLUSTER_WINDOW:
            groups[-1].append((frame, src))
        else:
            groups.append([(frame, src)])
    return groups


def fuse(
    candidates: Iterable[int],
    fades: Iterable[int],
    probs: np.ndarray | None,
    gray: np.ndarray,
    fps: float,
    *,
    anchor: str = "mid",
) -> list[Boundary]:
    raw: list[tuple[int, str]] = [(int(f), "t1") for f in candidates]
    raw += [(int(f), "t1:fade") for f in fades]

    t2_runs: list[tuple[int, int, float]] = []
    if probs is not None and len(probs):
        t2_runs = peaks(probs, threshold=0.4)

    # 长 run（≥4 帧）视为渐变转场，单独按锚点处理；短 run 当硬切候选并入聚类
    gradual_runs = [(s, e, p) for s, e, p in t2_runs if e - s >= 4]
    for s, e, _p in t2_runs:
        if e - s < 4:
            raw.append((int(e - 1), "t2"))

    # 1) T1 候选聚类 + T3 精修
    refined_all: list[tuple] = []
    for group in _cluster(raw):
        sources = sorted({s for _, s in group})
        rep = int(group[len(group) // 2][0])
        r = refine(gray, rep, window=3)
        if r is not None:
            refined_all.append((r, sources))

    out: list[Boundary] = []
    used: set[int] = set()

    # 2) 渐变转场：区间内所有 T1 精修点合并为一条边界，落点按 anchor
    for s, e, _p in gradual_runs:
        inside = [(i, r, src) for i, (r, src) in enumerate(refined_all)
                  if s - 3 <= r.frame < e + 3]
        for i, _, _ in inside:
            used.add(i)
        f = (s + e) // 2 if anchor == "mid" else max(s, e - 1)
        srcs = {"t2:run"} | {x for _, _, src in inside for x in src}
        tn = float(probs[s:e].max()) if probs is not None and len(probs) else 0.0
        hint = max([r.confidence_hint for _, r, _ in inside], default=0.5)
        kind = "fade" if any("t1:fade" in src for _, _, src in inside) else "dissolve"
        conf = min(1.0, 0.50 * tn + 0.35 * hint + 0.15 * 0.5)
        out.append(Boundary(frame=int(f), kind=kind, confidence=round(conf, 2),
                            sources=sorted(srcs)))

    # 3) 其余 T1/T2 候选：按精修结果打分
    for i, (r, sources) in enumerate(refined_all):
        if i in used:
            continue
        tn = 0.0
        if probs is not None and len(probs):
            lo, hi = max(0, r.frame - 1), min(len(probs), r.frame + 2)
            tn = float(probs[lo:hi].max()) if hi > lo else 0.0

        sharp = 1.0 if r.spike else 0.5
        score = 0.50 * tn + 0.35 * r.confidence_hint + 0.15 * sharp

        kind = r.kind
        if kind == "flash":
            score *= 0.4
        elif kind == "subtitle":
            # 变化只在底部（字幕/水印）：保留但大幅降权，落进复核队列
            score *= 0.35
            kind = "cut"
            sources = sorted(set(sources) | {"subtitle:only"})
        elif kind == "gradual":
            kind = "fade" if "t1:fade" in sources else "dissolve"
        elif "t1:fade" in sources:
            kind = "fade"

        out.append(Boundary(frame=r.frame, kind=kind,
                            confidence=round(min(score, 1.0), 2), sources=sources))

    # 4) 黑场/淡入淡出区：区间内所有边界合并成一个锚点
    out = _merge_black_regions(out, gray)

    # 5) 过短镜头合并：相邻切点间距 < MIN_SHOT_FRAMES 时合成一个（优先取 T2 概率峰）
    out = _merge_short_shots(out, probs, min_shot=MIN_SHOT_FRAMES)

    # 5.5) T2 强峰主导：强峰附近 15 帧内的弱边界被吸收，位置对齐概率峰
    out = _suppress_near_strong(out, probs, gray)

    # 6) 去重（相邻帧只留一个）
    dedup: list[Boundary] = []
    for b in sorted(out, key=lambda x: x.frame):
        if dedup and b.frame - dedup[-1].frame <= 2:
            if b.confidence > dedup[-1].confidence:
                dedup[-1] = b
            continue
        dedup.append(b)
    return dedup


def _black_intervals(gray: np.ndarray) -> list[tuple[int, int]]:
    if len(gray) == 0:
        return []
    means = gray.reshape(len(gray), -1).mean(axis=1)
    black = means < BLACK_LUMA
    intervals: list[tuple[int, int]] = []
    i = 0
    while i < len(black):
        if black[i]:
            j = i
            while j < len(black) and black[j]:
                j += 1
            if j - i >= 3:
                intervals.append((i, j))
            i = j
        else:
            i += 1
    return intervals


def _merge_black_regions(boundaries: list[Boundary], gray: np.ndarray) -> list[Boundary]:
    blacks = _black_intervals(gray)
    if not blacks:
        return boundaries

    items = list(boundaries)
    for s, e in blacks:
        lo, hi = max(0, s - FADE_HALF_WINDOW), e + FADE_HALF_WINDOW
        inside = [b for b in items if lo <= b.frame <= hi]
        if not inside:
            continue
        if len(inside) >= 2:
            keep = max(inside, key=lambda b: b.confidence)
            merged = Boundary(frame=(s + e) // 2, kind="fade",
                              confidence=keep.confidence,
                              sources=sorted({"black"} | {x for b in inside for x in b.sources}))
            items = [b for b in items if not (lo <= b.frame <= hi)] + [merged]
        else:
            # 黑场边缘只有一条边界（如片头黑场→首个镜头）：标成 fade，位置不动
            b = inside[0]
            items = [
                Boundary(frame=x.frame, kind="fade", confidence=x.confidence,
                         sources=sorted(set(x.sources) | {"black"})) if x is b else x
                for x in items
            ]
    return sorted(items, key=lambda b: b.frame)


def _merge_short_shots(boundaries: list[Boundary], probs, min_shot: int) -> list[Boundary]:
    """相邻切点间距过短时合并；位置优先取该区间内 T2 概率最大帧。"""
    items = sorted(boundaries, key=lambda b: b.frame)
    changed = True
    while changed and len(items) >= 2:
        changed = False
        for i in range(len(items) - 1):
            a, b = items[i], items[i + 1]
            if b.frame - a.frame >= min_shot:
                continue
            lo, hi = a.frame, b.frame
            peak = None
            if probs is not None and len(probs):
                seg = probs[lo:hi + 1]
                if len(seg):
                    peak = lo + int(seg.argmax())
            frame = peak if peak is not None else (lo + hi) // 2
            kind = "dissolve" if (a.kind != "cut" or b.kind != "cut") else "cut"
            merged = Boundary(frame=int(frame), kind=kind,
                              confidence=max(a.confidence, b.confidence),
                              sources=sorted(set(a.sources) | set(b.sources)))
            items = [x for x in items if x is not a and x is not b]
            items.append(merged)
            items.sort(key=lambda x: x.frame)
            changed = True
            break
    return items


def _suppress_near_strong(boundaries: list[Boundary], probs, gray: np.ndarray,
                          window: int = 15) -> list[Boundary]:
    """T2 强峰附近的其他边界被吸收；仅对非尖峰（渐变）重新对齐到概率峰。

    硬切（refine 判定为单帧尖峰）不动位置——否则概率峰常落在切点前一帧，
    会造成 1 帧系统误差。
    """
    if probs is None or len(probs) == 0:
        return boundaries

    def tn_at(frame: int) -> float:
        lo, hi = max(0, frame - 3), min(len(probs), frame + 4)
        return float(probs[lo:hi].max()) if hi > lo else 0.0

    adjusted: list[Boundary] = []
    for b in sorted(boundaries, key=lambda x: x.frame):
        if tn_at(b.frame) < 0.45:
            adjusted.append(b)
            continue
        r = refine(gray, b.frame, window=3)
        if r is not None and r.spike:
            adjusted.append(b)
            continue

        lo, hi = max(0, b.frame - 4), min(len(probs), b.frame + 5)
        f = lo + int(probs[lo:hi].argmax())
        r2 = refine(gray, f, window=3)
        hint = r2.confidence_hint if r2 is not None else 0.5
        conf = min(1.0, 0.50 * tn_at(f) + 0.35 * hint + 0.075)
        adjusted.append(Boundary(frame=f, kind="dissolve",
                                 confidence=round(conf, 2), sources=list(b.sources)))

    out: list[Boundary] = []
    for b in adjusted:
        hit = next((x for x in out if abs(x.frame - b.frame) < window), None)
        if hit is None:
            out.append(b)
            continue
        if tn_at(b.frame) > tn_at(hit.frame) + 0.1:
            out.remove(hit)
            out.append(b)
    return sorted(out, key=lambda x: x.frame)


def build_shots(boundaries: list[Boundary], nb_frames: int, fps: float) -> list[dict]:
    """半开区间 [start, end)，shots[i].start 即上一镜头的切点。"""
    shots: list[dict] = []
    prev = 0
    for b in boundaries:
        if b.frame <= prev or b.frame >= nb_frames:
            continue
        shots.append({
            "start": prev,
            "end": b.frame,
            "cut_type": "video_start" if prev == 0 else None,
            "confidence": None,
            "pts": round(prev / fps, 6),
            "tc": _tc(prev, fps),
            "sources": [],
        })
        prev = b.frame

    shots.append({
        "start": prev, "end": nb_frames,
        "cut_type": "video_start" if prev == 0 else None,
        "confidence": None, "pts": round(prev / fps, 6), "tc": _tc(prev, fps), "sources": [],
    })

    # 第 i 段的 cut_type 由第 i-1 个边界描述
    for i, b in enumerate(boundaries):
        if i + 1 < len(shots):
            shots[i + 1]["cut_type"] = b.kind
            shots[i + 1]["confidence"] = b.confidence
            shots[i + 1]["sources"] = b.sources
    for s in shots:
        s["cut_type"] = s["cut_type"] or "unknown"
    return shots
