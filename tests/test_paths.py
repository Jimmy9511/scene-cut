"""路径/指针/目录校验的单测（不依赖服务与 ffmpeg）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import paths
from app.store import migrate as migratemod


class PointerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_exe = paths.EXE_DIR
        self._tmp = tempfile.TemporaryDirectory()
        paths.EXE_DIR = Path(self._tmp.name)

    def tearDown(self) -> None:
        paths.EXE_DIR = self._old_exe
        self._tmp.cleanup()

    def test_roundtrip(self) -> None:
        target = Path(self._tmp.name) / "data"
        written = paths.write_pointer(target)
        self.assertEqual(written, paths.pointer_path())
        self.assertEqual(paths.read_pointer(), target.resolve())

    def test_invalid_content_ignored(self) -> None:
        pointer = paths.pointer_path()
        pointer.write_text("relative/path", encoding="utf-8")
        self.assertIsNone(paths.read_pointer())
        pointer.write_text("   ", encoding="utf-8")
        self.assertIsNone(paths.read_pointer())
        pointer.unlink()
        self.assertIsNone(paths.read_pointer())


class ValidateDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "data").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_accepts_new_dir(self) -> None:
        target = migratemod.validate_dir(str(self.root / "new" / "nested"))
        self.assertTrue(target.is_dir())

    def test_rejects_file(self) -> None:
        f = self.root / "file.txt"
        f.write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            migratemod.validate_dir(str(f))

    def test_rejects_relative(self) -> None:
        with self.assertRaises(ValueError):
            migratemod.validate_dir("relative-dir")

    def test_rejects_nested_in_current_root(self) -> None:
        with self.assertRaises(ValueError):
            migratemod.validate_dir(str(self.root / "data" / "inside"),
                                    current_root=self.root / "data")

    def test_rejects_same_path(self) -> None:
        with self.assertRaises(ValueError):
            migratemod.validate_dir(str(self.root / "data"), current_root=self.root / "data")


class MigrateTests(unittest.TestCase):
    def test_copies_and_rewrites_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "old"
            dst = Path(tmp) / "new"
            (src / "workspace" / "proj").mkdir(parents=True)
            (src / "workspace" / "proj" / "project.json").write_text("{}", encoding="utf-8")
            (src / "appdata").mkdir()
            (src / "appdata" / "pool.json").write_text('{"roots": []}', encoding="utf-8")
            (src / "settings.json").write_text(
                '{"log_level": "info", "workspace": "%s"}'
                % str(src / "workspace").replace("\\", "\\\\"),
                encoding="utf-8",
            )

            stats = migratemod.migrate(src, dst)
            self.assertTrue((dst / "workspace" / "proj" / "project.json").exists())
            self.assertTrue((dst / "appdata" / "pool.json").exists())
            self.assertTrue(stats["workspace_rewritten"])
            settings = (dst / "settings.json").read_text(encoding="utf-8")
            self.assertIn(str(dst / "workspace").replace("\\", "\\\\"), settings)


if __name__ == "__main__":
    unittest.main()
