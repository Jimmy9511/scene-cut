"""T3 候选窗精修：在 ±window 帧内找真正的切点帧，并识别闪光/渐变。

输入是已解码的小尺寸灰度帧序列（M0 全内存；M2 改分块）。
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Refined:
    frame: int            # 新镜头首帧
    confidence_hint: float
    kind: str             # cut | flash | gradual | unknown
    mse_peak: float
    hist_dist: float
    spike: bool


def _mse(a: np.ndarray, b: np.ndarray) -> float:
    d = a.astype(np.float32) - b.astype(np.float32)
    return float(np.mean(d * d) / (255.0 * 255.0))


def _hist_dist(a: np.ndarray, b: np.ndarray) -> float:
    ha = cv2.calcHist([a], [0], None, [64], [0, 256])
    hb = cv2.calcHist([b], [0], None, [64], [0, 256])
    cv2.normalize(ha, ha)
    cv2.normalize(hb, hb)
    return float(cv2.compareHist(ha, hb, cv2.HISTCMP_BHATTACHARYYA))


def refine(gray: np.ndarray, frame: int, window: int = 3) -> Refined | None:
    """在 [frame-window-1, frame+window] 内找差值最大的相邻帧对。"""
    n = len(gray)
    lo = max(1, frame - window - 1)
    hi = min(n - 1, frame + window)
    if hi <= lo:
        return None

    scores = [(i, _mse(gray[i - 1], gray[i])) for i in range(lo, hi + 1)]
    peak_i, peak = max(scores, key=lambda x: x[1])
    median = float(np.median([s for _, s in scores])) or 1e-9
    spike = peak > 2.5 * median
    hist = _hist_dist(gray[peak_i - 1], gray[peak_i])

    # 闪光/频闪：跨过切点后画面又回到变化前（±2 帧后相似）
    kind = "cut"
    a = peak_i - 1
    b = min(n - 1, peak_i + 2)
    if b > a:
        recovery = _mse(gray[a], gray[b])
        if recovery < 0.35 * peak:
            kind = "flash"
        elif not spike and hist > 0.15:
            kind = "gradual"

    # 字幕/底部水印：变化只集中在画面底部 → 降权并标记
    h = gray.shape[1]
    band = int(h * 0.78)
    before, after = gray[peak_i - 1], gray[peak_i]
    mse_top = _mse(before[:band], after[:band])
    mse_bot = _mse(before[band:], after[band:])
    subtitle_only = mse_bot > 0.4 * peak and mse_top < 0.25 * mse_bot

    hint = min(1.0, 0.6 * min(1.0, peak / 0.06) + 0.4 * min(1.0, hist / 0.5))
    if subtitle_only:
        kind = "subtitle"
        hint *= 0.25
    return Refined(frame=peak_i, confidence_hint=round(hint, 4),
                   kind=kind, mse_peak=peak, hist_dist=hist, spike=spike)
