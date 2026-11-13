"""视频解码工具：为检测与帧级预览提供统一的小尺寸帧序列。

M0 阶段一次读入内存（短素材），后续 M2 改为分块流式解码。
`decode_frames()` 是检测路径用的单遍解码：一次 cv2 循环同时产出
T2 需要的 RGB 小图与 T3 需要的灰度序列，避免整片解码两遍。
"""

from __future__ import annotations

from collections.abc import Callable

import cv2
import numpy as np

from app.errors import Cancelled


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


def decode_frames(
    path: str,
    rgb_size: tuple[int, int],
    gray_size: tuple[int, int],
    *,
    progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    check_every: int = 30,
) -> tuple[np.ndarray, np.ndarray]:
    """单遍解码：同时返回 (N,h,w,3) RGB 与 (N,h,w) 灰度。

    progress(done, total) 每 ``check_every`` 帧回调一次；total 可能为 0（容器未给帧数）。
    should_cancel() 为真时抛 :class:`app.errors.Cancelled`。
    """
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {path}")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

    rgb_frames: list[np.ndarray] = []
    gray_frames: list[np.ndarray] = []
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            rgb_frames.append(cv2.resize(frame, rgb_size, interpolation=cv2.INTER_AREA))
            small = cv2.resize(frame, gray_size, interpolation=cv2.INTER_AREA)
            gray_frames.append(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
            n = len(rgb_frames)
            if n % check_every == 0:
                if should_cancel is not None and should_cancel():
                    raise Cancelled("用户取消")
                if progress is not None:
                    progress(n, total)
    finally:
        cap.release()

    if not rgb_frames:
        raise RuntimeError(f"未读到任何帧: {path}")
    if progress is not None:
        progress(len(rgb_frames), total or len(rgb_frames))
    return (np.stack(rgb_frames).astype(np.uint8),
            np.stack(gray_frames).astype(np.uint8))
