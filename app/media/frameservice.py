"""帧级预览服务：批量解码 + LRU 缓存（替代单帧冷启动）。

原理：一次 ffmpeg 从目标帧起连续解出 N 帧（burst），后续步进直接命中缓存，
把 123 ms/帧 的冷启动摊到 ~2 ms/帧。随机跳转时按目标位置重新 burst。

帧号 ↔ 时间：t = (frame + 0.5) / fps；用 -ss 输入定位（帧精确），
输出帧序列的第 i 帧即 frame + i（同一 PTS 网格内）。
"""

from __future__ import annotations

import subprocess
import threading
from collections import OrderedDict

BURST = 48               # 每次 burst 解码帧数
CACHE_FRAMES = 480       # LRU 上限（JPEG，约 19 KB/帧 @960 → ~9 MB）


class FrameService:
    def __init__(self, path: str, fps: float, total_frames: int, width: int = 960):
        self.path = path
        self.fps = fps or 30.0
        self.total = total_frames
        self.width = width
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._lock = threading.Lock()
        self._inflight: dict[int, threading.Event] = {}

    # ---------- 对外 ----------
    def frame(self, idx: int) -> bytes:
        idx = max(0, min(self.total - 1, idx))
        cached = self._get(idx)
        if cached is not None:
            return cached

        with self._lock:
            wait = self._inflight.get(idx)
            if wait is None:
                wait = threading.Event()
                self._inflight[idx] = wait
                starter = True
            else:
                starter = False
        if not starter:
            wait.wait(timeout=5)
            cached = self._get(idx)
            if cached is not None:
                return cached

        try:
            self._burst(idx)
        finally:
            with self._lock:
                ev = self._inflight.pop(idx, None)
        if ev:
            ev.set()

        cached = self._get(idx)
        if cached is None:
            raise RuntimeError(f"取帧失败: frame={idx}")
        return cached

    def prefetch(self, idx: int, ahead: bool = True) -> None:
        """后台预取相邻 burst（不阻塞请求）。"""
        start = idx if ahead else max(0, idx - BURST + 1)
        threading.Thread(target=self._safe_burst, args=(start,), daemon=True).start()

    # ---------- 内部 ----------
    def _safe_burst(self, start: int) -> None:
        try:
            self._burst(start)
        except RuntimeError:
            pass

    def _get(self, idx: int) -> bytes | None:
        with self._lock:
            data = self._cache.get(idx)
            if data is not None:
                self._cache.move_to_end(idx)
            return data

    def _burst(self, start: int) -> None:
        start = max(0, min(self.total - 1, start))
        # 已被别人填过就直接返回
        if self._get(start) is not None:
            return
        t = (start + 0.5) / self.fps
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{t:.6f}", "-i", self.path,
            "-frames:v", str(BURST),
            "-vf", f"scale={self.width}:-2",
            "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4",
            "pipe:1",
        ]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0 or not proc.stdout:
            raise RuntimeError(f"burst 解码失败: {proc.stderr[-300:]!r}")

        for i, jpg in enumerate(_split_jpeg(proc.stdout)):
            idx = start + i
            if idx >= self.total:
                break
            with self._lock:
                self._cache[idx] = jpg
                self._cache.move_to_end(idx)
                while len(self._cache) > CACHE_FRAMES:
                    self._cache.popitem(last=False)


def _split_jpeg(buf: bytes) -> list[bytes]:
    """按 JPEG 的 SOI/EOI 标记切分连续流。"""
    out: list[bytes] = []
    i = 0
    n = len(buf)
    while i < n:
        start = buf.find(b"\xff\xd8", i)
        if start < 0:
            break
        end = buf.find(b"\xff\xd9", start + 2)
        if end < 0:
            break
        out.append(buf[start:end + 2])
        i = end + 2
    return out


_SERVICES: dict[str, FrameService] = {}
_SERVICES_LOCK = threading.Lock()


def get_service(key: str, path: str, fps: float, total: int, width: int = 960) -> FrameService:
    with _SERVICES_LOCK:
        svc = _SERVICES.get(key)
        if svc is None or svc.width != width:
            svc = FrameService(path, fps, total, width=width)
            _SERVICES[key] = svc
        return svc
