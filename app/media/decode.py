"""视频解码工具：为检测与帧级预览提供统一的小尺寸帧序列。

M0 阶段一次读入内存（短素材），后续 M2 改为分块流式解码。
`decode_frames()` 是检测路径用的单遍解码：一次 cv2 循环同时产出
T2 需要的 RGB 小图与 T3 需要的灰度序列，避免整片解码两遍。
"""

from __future__ import annotations

import subprocess
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
    """单遍解码（cv2 回退路径）：同时返回 (N,h,w,3) RGB 与 (N,h,w) 灰度。

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


def decode_frames_ffmpeg(
    path: str,
    rgb_size: tuple[int, int],
    gray_size: tuple[int, int],
    *,
    total: int = 0,
    progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    check_every: int = 30,
) -> tuple[np.ndarray, np.ndarray]:
    """用 ffmpeg 一次解码到 gray_size（BGR），再降采样出 T2 的 rgb_size。

    比 cv2 逐帧两次 resize 快得多；失败时由调用方回退到 :func:`decode_frames`。
    """
    gw, gh = gray_size
    frame_bytes = gw * gh * 3
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(path), "-an", "-sn",
        "-vf", f"scale={gw}:{gh}:flags=area",
        "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=frame_bytes * 4)
    rgb_frames: list[np.ndarray] = []
    gray_frames: list[np.ndarray] = []
    try:
        assert proc.stdout is not None
        while True:
            buf = proc.stdout.read(frame_bytes)
            if not buf or len(buf) < frame_bytes:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(gh, gw, 3)
            gray_frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
            rgb_frames.append(cv2.resize(frame, rgb_size, interpolation=cv2.INTER_AREA))
            n = len(gray_frames)
            if n % check_every == 0:
                if should_cancel is not None and should_cancel():
                    raise Cancelled("用户取消")
                if progress is not None:
                    progress(n, total)
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
        finally:
            proc.wait(timeout=10)
            if proc.stderr is not None:
                proc.stderr.close()
            if proc.stdout is not None:
                proc.stdout.close()

    if not rgb_frames:
        err = ""
        raise RuntimeError(f"ffmpeg 未解出任何帧: {path} {err}")
    if progress is not None:
        progress(len(rgb_frames), total or len(rgb_frames))
    return (np.stack(rgb_frames).astype(np.uint8),
            np.stack(gray_frames).astype(np.uint8))
