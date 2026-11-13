"""日志模块单测（纯标准库，CI 可直接跑）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import log as logmod


class NormalizeTests(unittest.TestCase):
    def test_known_levels(self) -> None:
        for name in ("debug", "detail", "info", "warning", "error"):
            self.assertEqual(logmod.normalize(name), name)
            self.assertEqual(logmod.normalize(name.upper()), name)

    def test_aliases(self) -> None:
        self.assertEqual(logmod.normalize("warn"), "warning")
        self.assertEqual(logmod.normalize("err"), "error")
        self.assertEqual(logmod.normalize("critical"), "error")
        self.assertEqual(logmod.normalize("verbose"), "detail")

    def test_invalid(self) -> None:
        self.assertIsNone(logmod.normalize("nope"))
        self.assertIsNone(logmod.normalize(""))
        self.assertIsNone(logmod.normalize(None))


class SetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old = logmod.get_level()
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        logmod.setup(self._old)
        self.tmp.cleanup()

    def test_setup_applies_level(self) -> None:
        self.assertEqual(logmod.setup("debug", log_dir=Path(self.tmp.name)), "debug")
        self.assertEqual(logmod.get_level(), "debug")
        self.assertTrue(logmod.is_enabled("debug"))

    def test_filtering(self) -> None:
        logmod.setup("warning", log_dir=Path(self.tmp.name))
        self.assertFalse(logmod.is_enabled("info"))
        self.assertFalse(logmod.is_enabled("detail"))
        self.assertTrue(logmod.is_enabled("warning"))
        self.assertTrue(logmod.is_enabled("error"))

    def test_file_sink_written(self) -> None:
        logmod.setup("debug", log_dir=Path(self.tmp.name))
        logmod.info("unit", "写一行")
        logmod.debug("unit", "再写一行")
        log_file = Path(self.tmp.name) / "logs" / "app.log"
        self.assertTrue(log_file.exists(), "日志文件应被创建")
        text = log_file.read_text(encoding="utf-8")
        self.assertIn("[unit] 写一行", text)
        self.assertIn("DEBUG", text)

    def test_set_level_rejects_unknown(self) -> None:
        logmod.setup("info", log_dir=Path(self.tmp.name))
        with self.assertRaises(ValueError):
            logmod.set_level("bogus")


class FormatTests(unittest.TestCase):
    def test_line_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logmod.setup("info", log_dir=Path(tmp))
            logmod.info("detect", "开始 素材=a.mp4")
            text = (Path(tmp) / "logs" / "app.log").read_text(encoding="utf-8")
            line = [x for x in text.splitlines() if "开始" in x][-1]
            self.assertRegex(line, r"^\d{2}:\d{2}:\d{2} INFO\s+\[detect\] 开始 素材=a\.mp4$")


if __name__ == "__main__":
    unittest.main()
