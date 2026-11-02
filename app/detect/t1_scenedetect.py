"""T1 统计检测（PySceneDetect 0.7.2）。

单次解码同时跑 ContentDetector + AdaptiveDetector（实测 540 帧 1080p 约 1.5 s，
分三次跑要 4.6 s）。ThresholdDetector 暂不启用：淡入淡出由融合层的黑场区间检测
处理（见 fusion._merge_black_regions），少一遍解码。
"""

from __future__ import annotations

from dataclasses import dataclass

from scenedetect import AdaptiveDetector, ContentDetector, SceneManager, open_video


@dataclass(frozen=True)
class T1Result:
    content: list[int]      # 合并后的场景起始帧（Content + Adaptive 并集）
    adaptive: list[int]     # 预留：当前与 content 合并，便于后续拆分统计
    fades: list[int]        # 预留：淡入淡出改由黑场区间检测

    @property
    def candidates(self) -> list[int]:
        return [f for f in sorted(set(self.content)) if f > 0]


def detect_t1(path: str, preset: dict | None = None) -> T1Result:
    p = preset or {}
    content_thr = float(p.get("content_threshold", 27.0))
    adaptive_thr = float(p.get("adaptive_threshold", 3.0))
    min_content = float(p.get("min_content_val", 15.0))
    min_len = int(p.get("min_scene_len", 15))

    video = open_video(str(path))
    manager = SceneManager()
    manager.add_detector(ContentDetector(threshold=content_thr, min_scene_len=min_len))
    manager.add_detector(AdaptiveDetector(
        adaptive_threshold=adaptive_thr,
        min_content_val=min_content,
        min_scene_len=min_len,
    ))
    manager.detect_scenes(video, show_progress=False)

    starts: list[int] = []
    for scene in manager.get_scene_list():
        frame = scene[0].frame_num
        if frame > 0:
            starts.append(int(frame))
    return T1Result(content=sorted(set(starts)), adaptive=[], fades=[])
