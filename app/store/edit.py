"""镜头编辑：以 clips（人工结果）为准的边界操作 + 撤销栈。

设计（方案 §5.1/§5.4）：
- shots[] 是检测器产物，只读；clips[] 是人的决定，所有编辑都改 clips。
- 内部统一用「边界数轴」bounds = [clip.start...] + [最后一个 end] 表示。
- 每次编辑把 clips 快照压进 analysis["edit_history"]（上限 50），支持撤销/重做。
"""

from __future__ import annotations

from typing import Any

HISTORY_LIMIT = 50


def get_clips(analysis: dict) -> list[dict]:
    """取 clips；老数据没有 clips 时用 shots 生成一份。"""
    clips = analysis.get("clips") or []
    if clips:
        return [dict(c) for c in clips]
    shots = analysis.get("shots") or []
    total = int(analysis.get("probe", {}).get("nb_frames") or 0) or (
        shots[-1]["end"] if shots else 0)
    return [
        {"id": f"k_{i + 1:03d}", "start": s["start"], "end": s["end"],
         "state": "auto", "label": "", "from_shots": [i]}
        for i, s in enumerate(shots)
    ] or [{"id": "k_001", "start": 0, "end": max(1, total), "state": "auto",
           "label": "", "from_shots": []}]


def total_frames(analysis: dict) -> int:
    shots = analysis.get("shots") or []
    if shots:
        return int(shots[-1]["end"])
    probe = analysis.get("probe") or {}
    return int(probe.get("nb_frames") or 1)


def _bounds(clips: list[dict]) -> list[int]:
    return [c["start"] for c in clips] + [clips[-1]["end"]]


def _rebuild(bounds: list[int], old: list[dict]) -> list[dict]:
    """按新边界重建 clips，尽量继承重叠最多的旧片段的 label/state。"""
    bounds = sorted({int(b) for b in bounds})
    out: list[dict] = []
    for i in range(len(bounds) - 1):
        s, e = bounds[i], bounds[i + 1]
        if e <= s:
            continue
        best, best_ov = None, 0
        for c in old:
            ov = min(e, c["end"]) - max(s, c["start"])
            if ov > best_ov:
                best, best_ov = c, ov
        out.append({
            "id": f"k_{i + 1:03d}",
            "start": s, "end": e,
            "state": (best or {}).get("state", "manual"),
            "label": (best or {}).get("label", ""),
            "from_shots": (best or {}).get("from_shots", []),
        })
    return out


def _snapshot(clips: list[dict]) -> list[dict]:
    keys = ("id", "start", "end", "state", "label", "from_shots")
    return [{k: c.get(k) for k in keys} for c in clips]


def _ensure_history(analysis: dict, clips: list[dict]) -> tuple[list, int]:
    """hist[i] = 第 i 个状态快照；pos 指向当前状态。"""
    hist = analysis.get("edit_history")
    if not hist:
        hist = [_snapshot(clips)]
        analysis["edit_history"] = hist
        analysis["edit_pos"] = 0
    pos = int(analysis.get("edit_pos", len(hist) - 1))
    pos = max(0, min(pos, len(hist) - 1))
    analysis["edit_pos"] = pos
    return hist, pos


def apply(analysis: dict, op: str, payload: dict[str, Any]) -> dict:
    """执行编辑；返回 {"clips", "history_pos", "history_len"}。"""
    clips = get_clips(analysis)
    total = total_frames(analysis)
    hist, pos = _ensure_history(analysis, clips)

    if op == "undo":
        return _undo(analysis, hist, pos)
    if op == "redo":
        return _redo(analysis, hist, pos)

    bounds = _bounds(clips)

    if op == "split":
        frame = int(payload.get("frame", 0))
        if 0 < frame < total and all(abs(frame - b) > 1 for b in bounds):
            bounds = sorted(bounds + [frame])
            clips = _rebuild(bounds, clips)
    elif op == "merge":
        # 合并选中片段：删掉它们之间的边界（bounds[i] 是第 i 段的起点）
        ids = sorted({int(i) for i in payload.get("indices", [])})
        if len(ids) >= 2:
            drop = set(range(ids[0] + 1, ids[-1] + 1))
            bounds = [b for i, b in enumerate(bounds) if i not in drop]
            clips = _rebuild(bounds, clips)
    elif op == "remove_boundary":
        frame = int(payload.get("frame", 0))
        bounds = [b for b in bounds if abs(b - frame) > 1 or b in (0, total)]
        clips = _rebuild(bounds, clips)
    elif op == "move":
        frame = int(payload.get("frame", 0))
        to = int(payload.get("to", 0))
        if frame in bounds:
            idx = bounds.index(frame)
            if idx > 0:
                bounds[idx] = max(bounds[idx - 1] + 1, min(to, bounds[-1] - 1))
                clips = _rebuild(bounds, clips)
    elif op == "label":
        ids = [int(i) for i in payload.get("indices", [])]
        text = str(payload.get("label", ""))[:40]
        clips = [dict(c) for c in clips]
        for i in ids:
            if 0 <= i < len(clips):
                clips[i]["label"] = text
    elif op == "state":
        ids = [int(i) for i in payload.get("indices", [])]
        value = str(payload.get("state", "confirmed"))
        clips = [dict(c) for c in clips]
        for i in ids:
            if 0 <= i < len(clips):
                clips[i]["state"] = value
    else:
        raise ValueError(f"未知操作: {op}")

    clips = _clamp(clips, total)
    analysis["clips"] = clips
    hist, pos = _push(analysis, hist, pos, clips)
    return {"clips": clips, "history_pos": pos, "history_len": len(hist)}


def _push(analysis: dict, hist: list, pos: int, clips: list[dict]) -> tuple[list, int]:
    # 丢弃 redo 分支
    del hist[pos + 1:]
    hist.append(_snapshot(clips))
    if len(hist) > HISTORY_LIMIT:
        drop = len(hist) - HISTORY_LIMIT
        del hist[:drop]
    pos = len(hist) - 1
    analysis["edit_history"] = hist
    analysis["edit_pos"] = pos
    return hist, pos


def _clamp(clips: list[dict], total: int) -> list[dict]:
    out = []
    for c in clips:
        s = max(0, min(int(c["start"]), total))
        e = max(s + 1, min(int(c["end"]), total))
        c = dict(c)
        c["start"], c["end"] = s, e
        out.append(c)
    return out


def _undo(analysis: dict, hist: list, pos: int) -> dict:
    if pos <= 0:
        clips = [dict(c) for c in hist[pos]]
        return {"clips": clips, "history_pos": pos, "history_len": len(hist)}
    pos -= 1
    analysis["edit_pos"] = pos
    clips = [dict(c) for c in hist[pos]]
    analysis["clips"] = clips
    return {"clips": clips, "history_pos": pos, "history_len": len(hist)}


def _redo(analysis: dict, hist: list, pos: int) -> dict:
    if pos >= len(hist) - 1:
        clips = [dict(c) for c in hist[pos]]
        return {"clips": clips, "history_pos": pos, "history_len": len(hist)}
    pos += 1
    analysis["edit_pos"] = pos
    clips = [dict(c) for c in hist[pos]]
    analysis["clips"] = clips
    return {"clips": clips, "history_pos": pos, "history_len": len(hist)}
