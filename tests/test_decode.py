"""解码一致性单测：ffmpeg 单遍解码 vs cv2 回退路径。

需要 ffmpeg 与合成素材；CI 的 smoke job 会先跑 tools/make_fixtures.py。
本地缺少素材时自动跳过。
"""

from __future__ import annotations

import shutil
import unittest
from pathlib import Path

try:                                     # lint 环境不一定装 numpy/opencv
    import numpy as np
    from app.media.decode import decode_frames, decode_frames_ffmpeg
    _READY, _WHY = True, ""
except Exception as _exc:                # noqa: BLE001
    np = None                            # type: ignore[assignment]
    decode_frames = decode_frames_ffmpeg = None  # type: ignore[assignment]
    _READY, _WHY = False, f"缺少依赖：{_exc}"

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "synthetic" / "hardcuts.mp4"


@unittest.skipUnless(_READY, _WHY)
@unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg")
@unittest.skipUnless(FIXTURE.exists(), "缺少合成素材，先运行 tools/make_fixtures.py")
class DecodeParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rgb_cv2, cls.gray_cv2 = decode_frames(str(FIXTURE), (48, 27), (128, 72))
        cls.rgb_ff, cls.gray_ff = decode_frames_ffmpeg(str(FIXTURE), (48, 27), (128, 72))

    def test_frame_counts_match(self) -> None:
        self.assertEqual(len(self.gray_cv2), len(self.gray_ff))
        self.assertEqual(len(self.rgb_cv2), len(self.rgb_ff))

    def test_shapes_match(self) -> None:
        self.assertEqual(self.gray_cv2.shape, self.gray_ff.shape)
        self.assertEqual(self.rgb_cv2.shape, self.rgb_ff.shape)

    def test_gray_close_enough(self) -> None:
        diff = np.abs(self.gray_cv2.astype(np.int16) - self.gray_ff.astype(np.int16))
        # 两条解码路径的 yuv->rgb 取整不同，允许微小差异（均值极小）
        self.assertLess(float(diff.mean()), 2.0)
        self.assertLess(float(np.percentile(diff, 99)), 12)

    def test_progress_callback(self) -> None:
        seen: list[tuple[int, int]] = []
        decode_frames_ffmpeg(str(FIXTURE), (48, 27), (128, 72), total=540,
                             progress=lambda d, t: seen.append((d, t)))
        self.assertTrue(seen)
        self.assertEqual(seen[-1][0], seen[-1][1])


if __name__ == "__main__":
    unittest.main()
