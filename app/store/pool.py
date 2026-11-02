"""素材池：用户导入的根目录/文件，按导入时的层级树状呈现。

规则（方案 §3.6 修订）：
- 池没有固定目录；每个导入项是一个「根」（目录或单个文件）。
- 目录根向下递归读取所有子目录与媒体文件；**不允许越过根向上追溯**。
- 单个视频文件与目录根同级。
- 池索引保存在应用数据目录，与工作区解耦。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

POOL_SCHEMA = "scene-cut/pool/1"
MEDIA_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".ts", ".flv", ".wmv", ".mpg", ".mpeg"}
MAX_NODES = 2000


class Pool:
    def __init__(self, index_file: Path):
        self.file = index_file.resolve()
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    # ---------- 持久化 ----------
    def _load(self) -> dict:
        if self.file.exists():
            try:
                d = json.loads(self.file.read_text(encoding="utf-8"))
                if d.get("schema") == POOL_SCHEMA:
                    return d
            except (json.JSONDecodeError, OSError):
                pass
        return {"schema": POOL_SCHEMA, "roots": []}

    def save(self) -> None:
        tmp = self.file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.file)

    # ---------- 根目录 ----------
    @property
    def roots(self) -> list[dict]:
        return self.data["roots"]

    def find(self, rid: str) -> dict | None:
        return next((r for r in self.roots if r["id"] == rid), None)

    def add(self, path: str | Path, alias: str = "") -> dict:
        p = Path(path).resolve()
        if not p.exists():
            raise FileNotFoundError(f"路径不存在: {p}")
        if p.is_file() and p.suffix.lower() not in MEDIA_EXT:
            raise ValueError(f"不是支持的媒体文件: {p.suffix}")
        for r in self.roots:
            if Path(r["path"]).resolve() == p:
                return r
        root = {
            "id": f"r_{len(self.roots) + 1:03d}",
            "path": str(p),
            "kind": "dir" if p.is_dir() else "file",
            "alias": alias or p.name,
            "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        self.roots.append(root)
        self.save()
        return root

    def remove(self, rid: str) -> bool:
        before = len(self.roots)
        self.data["roots"] = [r for r in self.roots if r["id"] != rid]
        if len(self.data["roots"]) != before:
            self.save()
            return True
        return False

    def rename(self, rid: str, alias: str) -> dict | None:
        root = self.find(rid)
        if root and alias.strip():
            root["alias"] = alias.strip()[:60]
            self.save()
        return root

    def contains(self, path: str | Path) -> bool:
        """安全边界：只允许根目录内部（含自身）的路径。"""
        p = Path(path).resolve()
        for r in self.roots:
            base = Path(r["path"]).resolve()
            if r["kind"] == "file":
                if p == base:
                    return True
            else:
                try:
                    p.relative_to(base)
                    return True
                except ValueError:
                    continue
        return False

    # ---------- 树 ----------
    def tree(self) -> list[dict]:
        nodes: list[dict] = []
        budget = [MAX_NODES]
        for r in self.roots:
            p = Path(r["path"])
            node = {"id": r["id"], "name": r["alias"], "path": str(p),
                    "kind": r["kind"], "root": True}
            if r["kind"] == "dir" and p.is_dir():
                node["children"] = _walk(p, budget, depth=0)
            nodes.append(node)
        return nodes


def _walk(directory: Path, budget: list[int], depth: int) -> list[dict]:
    """只向下遍历；跳过隐藏项与系统目录，避免误扫大目录。"""
    out: list[dict] = []
    if depth > 8 or budget[0] <= 0:
        return out
    try:
        entries = sorted(directory.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    except OSError:
        return out
    for entry in entries:
        if budget[0] <= 0:
            break
        if entry.name.startswith(".") or entry.name in ("$RECYCLE.BIN", "System Volume Information"):
            continue
        if entry.is_dir():
            budget[0] -= 1
            out.append({"name": entry.name, "path": str(entry), "kind": "dir",
                        "children": _walk(entry, budget, depth + 1)})
        elif entry.suffix.lower() in MEDIA_EXT:
            budget[0] -= 1
            out.append({"name": entry.name, "path": str(entry), "kind": "file",
                        "size": entry.stat().st_size})
    return out
