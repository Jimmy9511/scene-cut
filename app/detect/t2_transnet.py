"""T2 AI 精检（TransNetV2，transnetv2-pytorch 1.0.5）。

注意（本机实测踩坑，见方案附录 A.4）：
- 不要用 `predict_video()`：它经 ffmpeg-python 拉子进程，在本机会挂起。
- 自行解码为 (N, 27, 48, 3) 后调 `predict_frames()`；输入张量必须与模型同设备
  （GPU 时忘了 `.cuda()` 会报 `aten::slow_conv3d_forward` 的 NotImplementedError）。
"""

from __future__ import annotations

import numpy as np

_MODEL = None
_DEVICE = "cpu"

# TransNetV2 固定输入尺寸 (h, w)
INPUT_SIZE = (27, 48)


def _load(device: str = "auto"):
    global _MODEL, _DEVICE
    if _MODEL is None:
        from transnetv2_pytorch import TransNetV2

        _MODEL = TransNetV2(device=device)
        _MODEL.eval()
        _DEVICE = str(_MODEL.device)
    return _MODEL


def device() -> str:
    return _DEVICE


def predict_probs(rgb_frames: np.ndarray, device: str = "auto") -> np.ndarray:
    """rgb_frames: (N, 27, 48, 3) uint8 → 每帧切点概率 (N,) float32。"""
    import torch

    model = _load(device)
    tensor = torch.from_numpy(np.ascontiguousarray(rgb_frames)).to(model.device)
    with torch.no_grad():
        single, _many = model.predict_frames(tensor, quiet=True)
    probs = single.detach().cpu().numpy().reshape(-1)
    return probs.astype(np.float32)


def peaks(probs: np.ndarray, threshold: float = 0.5, min_len: int = 1) -> list[tuple[int, int, float]]:
    """把概率序列压成区间：返回 [(start, end_exclusive, max_prob)]。

    - run 长度 ≤2 → 硬切
    - run 更长 → 渐变转场区间
    同时保证两端至少留 min_len 帧，避免切出 0 长度镜头。
    """
    out: list[list] = []
    n = len(probs)
    i = 0
    while i < n:
        if probs[i] > threshold:
            j = i
            while j < n and probs[j] > threshold:
                j += 1
            if j - i >= min_len:
                out.append([i, j, float(probs[i:j].max())])
            i = j
        else:
            i += 1
    return [(int(a), int(b), float(c)) for a, b, c in out]
