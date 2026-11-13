"""工作区与项目：每个素材检测时自动建项目文件夹。

<workspace>/<项目名>/
    project.json      项目元数据（源路径、状态、预设、导出设置）
    analysis.json     解释文件：shots / clips / 编辑历史
    logs/detect.log   检测日志
    shots/ clips/     导出产物（M2）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

PROJECT_SCHEMA = "scene-cut/project/1"
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WRITE_LOCK = threading.Lock()


def _safe_name(name: str, fallback: str = "project") -> str:
    name = _BAD.sub("_", name).strip().rstrip(".")
    name = name[:60] or fallback
    if name.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(COM|LPT)[1-9]", name.upper()):
        name = f"_{name}"
    return name


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------- 列表 ----------
    def list_projects(self) -> list[dict]:
        out: list[dict] = []
        for d in sorted(self.root.iterdir()):
            meta = d / "project.json"
            if not d.is_dir() or not meta.exists():
                continue
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("schema") != PROJECT_SCHEMA:
                continue
            analysis = d / "analysis.json"
            data["dir"] = str(d)
            data["has_analysis"] = analysis.exists()
            data["shots"] = _shot_count(analysis)
            out.append(data)
        return out

    def find_by_source(self, source: str | Path) -> dict | None:
        target = str(Path(source).resolve()).lower()
        for p in self.list_projects():
            if str(Path(p.get("source", "")).resolve()).lower() == target:
                return p
        return None

    def get(self, pid: str) -> dict | None:
        for p in self.list_projects():
            if p["id"] == pid:
                return p
        return None

    def dir_of(self, pid: str) -> Path | None:
        p = self.get(pid)
        return Path(p["dir"]) if p else None

    # ---------- 创建 / 改名 / 删除 ----------
    def create(self, source: str | Path, name: str | None = None) -> dict:
        src = Path(source).resolve()
        if not src.exists():
            raise FileNotFoundError(f"素材不存在: {src}")
        base = _safe_name(name or src.stem)
        directory = self.root / base
        i = 2
        while directory.exists():
            directory = self.root / f"{base}_{i}"
            i += 1
        directory.mkdir(parents=True)
        (directory / "logs").mkdir(exist_ok=True)
        data = {
            "schema": PROJECT_SCHEMA,
            "id": f"p_{uuid.uuid4().hex[:10]}",
            "name": directory.name,
            "source": str(src),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "status": "new",             # new | analyzing | analyzed | failed
            "preset": "实拍短片",
            "export": {"mode": "smart", "naming": "{kind}{index:04d}_{source}_{start_tc}"},
        }
        self._write(directory, data)
        return {**data, "dir": str(directory)}

    def rename(self, pid: str, name: str) -> dict | None:
        project = self.get(pid)
        if not project:
            return None
        old = Path(project["dir"])
        new_name = _safe_name(name)
        new = self.root / new_name
        if new != old:
            if new.exists():
                i = 2
                while (self.root / f"{new_name}_{i}").exists():
                    i += 1
                new = self.root / f"{new_name}_{i}"
            old.rename(new)
        data = json.loads((new / "project.json").read_text(encoding="utf-8"))
        data["name"] = new.name
        self._write(new, data)
        return {**data, "dir": str(new)}

    def remove(self, pid: str) -> bool:
        project = self.get(pid)
        if not project:
            return False
        shutil.rmtree(project["dir"], ignore_errors=True)
        return True

    # ---------- 元数据 ----------
    def _write(self, directory: Path, data: dict) -> None:
        tmp = directory / "project.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, directory / "project.json")

    def update(self, pid: str, **fields) -> dict | None:
        project = self.get(pid)
        if not project:
            return None
        directory = Path(project["dir"])
        with _WRITE_LOCK:                    # 并发检测时避免丢字段（读-改-写）
            data = json.loads((directory / "project.json").read_text(encoding="utf-8"))
            data.update(fields)
            self._write(directory, data)
        return {**data, "dir": str(directory)}

    # ---------- 解释文件 ----------
    def analysis_path(self, pid: str) -> Path | None:
        d = self.dir_of(pid)
        return d / "analysis.json" if d else None

    def load_analysis(self, pid: str) -> dict | None:
        path = self.analysis_path(pid)
        if not path or not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def save_analysis(self, pid: str, data: dict) -> Path | None:
        path = self.analysis_path(pid)
        if path is None:
            return None
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def log(self, pid: str, message: str) -> None:
        d = self.dir_of(pid)
        if not d:
            return
        (d / "logs").mkdir(exist_ok=True)
        with (d / "logs" / "detect.log").open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")


def _shot_count(analysis: Path) -> int:
    if not analysis.exists():
        return 0
    try:
        return len(json.loads(analysis.read_text(encoding="utf-8")).get("shots", []))
    except (json.JSONDecodeError, OSError):
        return 0
