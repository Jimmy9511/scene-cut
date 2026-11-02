"""视频解码工具：为检测与帧级预览提供统一的小尺寸帧序列。

M0 阶段一次读入内存（短素材），后续 M2 改为分块流式解码。
"""

from __future__ import annotations

import cv2
import numpy as np


def decode_rgb(path: str, size: tuple[int, int]) -> np.ndarray:
    """解码为 RGB24 小图序列，返回 (N, h, w, 3) uint8。size 为 (w, h)。"""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {path}")
    frames: list[np.ndarray] = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(cv2.resize(frame, size, interpolation=cv2.INTER_AREA))
    cap.release()
    if not frames:
        raise RuntimeError(f"未读到任何帧: {path}")
    return np.stack(frames).astype(np.uint8)


def decode_gray(path: str, size: tuple[int, int]) -> np.ndarray:
    """解码为灰度小图序列，返回 (N, h, w) uint8。size 为 (w, h)。"""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {path}")
    frames: list[np.ndarray] = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        small = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
        frames.append(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
    cap.release()
    if not frames:
        raise RuntimeError(f"未读到任何帧: {path}")
    return np.stack(frames).astype(np.uint8)
