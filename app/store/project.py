"""项目文件：工作区 / 素材 / 分析结果索引。

工作区结构（方案 §3.6）：
    <workspace>/project.json          # 项目元数据 + 素材注册表
    <workspace>/analysis/<id>.json    # 每条素材的切点表
    <workspace>/cache/                # 胶片条等缓存
    <workspace>/shots/                # 导出（M2）
    <workspace>/clips/                # 导出（M2）

M0 简化：素材默认引用式（记录绝对路径，不复制），分析结果写 analysis/。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

SCHEMA = "scene-cut/1"
MEDIA_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".ts", ".flv", ".wmv", ".mpg", ".mpeg"}


class Project:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.analysis_dir = self.root / "analysis"
        self.cache_dir = self.root / "cache"
        self.analysis_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.file = self.root / "project.json"
        self._mt = self.file.stat().st_mtime_ns if self.file.exists() else 0
        self.data = self._load()

    def _maybe_reload(self) -> None:
        """项目文件可能被其他进程（分析子进程/外部编辑器）改写，按 mtime 重新载入。"""
        try:
            mt = self.file.stat().st_mtime_ns
        except OSError:
            return
        if mt != self._mt:
            self._mt = mt
            self.data = self._load()

    # ---------- 持久化 ----------
    def _load(self) -> dict:
        if self.file.exists():
            try:
                data = json.loads(self.file.read_text(encoding="utf-8"))
                if data.get("schema") == SCHEMA:
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return {
            "schema": SCHEMA,
            "project_id": f"p_{time.strftime('%Y%m%d_%H%M%S')}",
            "name": self.root.name or "untitled",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "assets": [],
        }

    def save(self) -> None:
        tmp = self.file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.file)
        self._mt = self.file.stat().st_mtime_ns

    # ---------- 素材 ----------
    @property
    def assets(self) -> list[dict]:
        self._maybe_reload()
        return self.data["assets"]

    def find(self, asset_id: str) -> dict | None:
        self._maybe_reload()
        return next((a for a in self.data["assets"] if a["id"] == asset_id), None)

    def find_by_path(self, path: str) -> dict | None:
        target = str(Path(path).resolve()).lower()
        return next((a for a in self.assets if str(Path(a["path"]).resolve()).lower() == target), None)

    def add(self, path: str | Path, project: str = "") -> dict:
        p = Path(path).resolve()
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"文件不存在: {p}")
        if p.suffix.lower() not in MEDIA_EXT:
            raise ValueError(f"不支持的媒体格式: {p.suffix}")
        existing = self.find_by_path(str(p))
        if existing:
            return existing
        asset = {
            "id": f"a_{len(self.assets) + 1:03d}_{abs(hash(str(p))) % 10000:04d}",
            "name": p.name,
            "path": str(p),
            "project": project,
            "size": p.stat().st_size,
            "mtime": int(p.stat().st_mtime),
            "status": "new",           # new | analyzed | failed
            "analysis": None,          # analysis/<id>.json 相对路径
            "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        self.assets.append(asset)
        self.save()
        return asset

    def scan(self, directory: str | Path, recursive: bool = True) -> list[dict]:
        root = Path(directory).resolve()
        it = root.rglob("*") if recursive else root.glob("*")
        added = []
        for p in sorted(it):
            if p.is_file() and p.suffix.lower() in MEDIA_EXT:
                try:
                    added.append(self.add(p))
                except (FileNotFoundError, ValueError, OSError):
                    continue
        return added

    def mark_analyzed(self, asset_id: str, analysis_rel: str) -> None:
        asset = self.find(asset_id)
        if asset:
            asset["status"] = "analyzed"
            asset["analysis"] = analysis_rel
            self.save()

    def mark_failed(self, asset_id: str, error: str) -> None:
        asset = self.find(asset_id)
        if asset:
            asset["status"] = "failed"
            asset["error"] = error[:400]
            self.save()

    # ---------- 读取 / 写回分析 ----------
    def analysis_path(self, asset_id: str) -> Path | None:
        asset = self.find(asset_id)
        if not asset or not asset.get("analysis"):
            return None
        return self.root / asset["analysis"]

    def save_analysis(self, asset_id: str, data: dict) -> Path | None:
        path = self.analysis_path(asset_id)
        if path is None:
            return None
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def analysis(self, asset_id: str) -> dict | None:
        asset = self.find(asset_id)
        if not asset or not asset.get("analysis"):
            return None
        path = self.root / asset["analysis"]
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def tree(self) -> dict:
        self._maybe_reload()
        groups: dict[str, list[dict]] = {}
        for a in self.data["assets"]:
            groups.setdefault(a.get("project") or "", []).append(a)
        return {
            "workspace": str(self.root),
            "project_id": self.data["project_id"],
            "name": self.data["name"],
            "groups": [
                {
                    "name": name or self.data["name"],
                    "assets": [
                        {
                            "id": a["id"], "name": a["name"], "path": a["path"],
                            "status": a["status"], "size": a["size"],
                            "shots": (self.analysis(a["id"]) or {}).get("shots") and
                                     len((self.analysis(a["id"]) or {}).get("shots", [])) or 0,
                        }
                        for a in assets
                    ],
                }
                for name, assets in groups.items()
            ],
        }
