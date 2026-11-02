"""scene-cut 本地服务：素材池 + 项目 + 预览 + 编辑。

启动：
  python -m app.api.server --workspace <工作区目录> --port 8768
素材池索引默认放在 <仓库>/appdata/pool.json，可用 --pool 指定。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from app.detect.probe import probe as probe_media
from app.media.frameservice import get_service
from app.paths import DATA_ROOT, WEB_DIR
from app.store.pool import MEDIA_EXT, Pool
from app.store.workspace import Workspace

ROOT = Path(__file__).resolve().parents[2]
WEB = WEB_DIR

POOL: Pool | None = None
WS: Workspace | None = None
PROBES: dict[str, dict] = {}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
PICK_LOCK = threading.Lock()


def _probe(path: str) -> dict:
    if path not in PROBES:
        PROBES[path] = probe_media(path)
    return PROBES[path]


def _parse_indices(raw: str) -> list[int] | None:
    """支持 "0,3,5" 与 "0-9" 混合。"""
    if not raw:
        return None
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                out.extend(range(int(a), int(b) + 1))
            except ValueError:
                continue
        else:
            try:
                out.append(int(part))
            except ValueError:
                continue
    return sorted(set(out)) or None


def _resolve(qs: dict) -> tuple[dict | None, str | None]:
    """把请求解析成 (project, media_path)。project 可能是 None（素材池直接预览）。"""
    pid = (qs.get("project") or [""])[0]
    if pid and WS:
        project = WS.get(pid)
        if project:
            return project, project["source"]
    raw = (qs.get("path") or [""])[0]
    if raw and POOL and POOL.contains(raw):
        return None, str(Path(raw).resolve())
    return None, None


class Handler(BaseHTTPRequestHandler):
    server_version = "scene-cut/0.9"

    def log_message(self, fmt, *args):
        return

    # ---------- 基础 ----------
    def _send(self, code, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _file(self, path: Path):
        if not path.exists() or not path.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        return self._send(200, path.read_bytes(), ctype)

    # ---------- GET ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query)

        if path == "/":
            return self._file(WEB / "index.html")
        if path.startswith("/static/"):
            return self._file(WEB / path[len("/static/"):])
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")

        if path == "/api/pool":
            return self._json({"roots": POOL.tree() if POOL else []})
        if path == "/api/projects":
            return self._json(WS.list_projects() if WS else [])
        if path == "/api/job":
            job = JOBS.get((qs.get("id") or [""])[0])
            return self._json(job) if job else self._json({"error": "no job"}, 404)
        if path == "/api/analysis":
            return self._analysis(qs)
        if path == "/api/frame":
            return self._frame(qs)
        if path == "/api/video":
            return self._video(qs)
        if path == "/api/strip":
            return self._strip(qs)
        if path == "/api/export/preview":
            return self._export_preview(qs)
        if path == "/api/export/file":
            return self._export_file(qs)
        if path == "/api/export/frame":
            return self._export_frame(qs)
        if path == "/api/export/last":
            return self._export_last(qs)
        if not path.startswith("/api/"):
            return self._file(WEB / "index.html")
        return self._send(404, b"not found", "text/plain")

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        handlers = {
            "/api/pool/import": self._pool_import,
            "/api/pool/remove": self._pool_remove,
            "/api/pool/rename": self._pool_rename,
            "/api/pick": self._pick,
            "/api/projects/analyze": self._analyze,
            "/api/batch/analyze": self._batch_analyze,
            "/api/batch/export": self._batch_export,
            "/api/projects/rename": self._project_rename,
            "/api/projects/remove": self._project_remove,
            "/api/projects/create": self._project_create,
            "/api/edit": self._edit,
            "/api/export": self._export,
        }
        h = handlers.get(path)
        if not h:
            return self._send(404, b"not found", "text/plain")
        return h()

    # ---------- 素材池 ----------
    def _pool_import(self):
        body = self._body()
        raw = str(body.get("path") or "").strip().strip('"')
        if not raw:
            return self._json({"error": "缺少 path"}, 400)
        try:
            root = POOL.add(raw)
        except (FileNotFoundError, ValueError, OSError) as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"root": root, "roots": POOL.roots})

    def _pool_remove(self):
        body = self._body()
        ok = POOL.remove(str(body.get("root") or ""))
        return self._json({"ok": ok})

    def _pool_rename(self):
        body = self._body()
        root = POOL.rename(str(body.get("root") or ""), str(body.get("alias") or ""))
        return self._json({"ok": root is not None, "root": root})

    def _pick(self):
        """弹出系统文件/目录选择框（本机桌面会话可用）。"""
        body = self._body()
        mode = str(body.get("mode") or "dir")
        if not PICK_LOCK.acquire(blocking=False):
            return self._json({"error": "已有选择框打开"}, 409)
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            if mode == "dir":
                value = filedialog.askdirectory(title="选择要导入素材池的目录")
            elif mode == "files":
                value = filedialog.askopenfilenames(
                    title="选择素材文件",
                    filetypes=[("视频", " ".join(f"*{e}" for e in sorted(MEDIA_EXT))), ("全部", "*.*")])
                value = list(value)
            else:
                value = filedialog.askopenfilename(
                    title="选择素材文件",
                    filetypes=[("视频", " ".join(f"*{e}" for e in sorted(MEDIA_EXT))), ("全部", "*.*")])
            root.destroy()
            return self._json({"paths": value if isinstance(value, list) else [value] if value else []})
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": f"无法打开选择框: {exc}"}, 500)
        finally:
            PICK_LOCK.release()

    # ---------- 项目 ----------
    def _project_create(self):
        body = self._body()
        src = str(body.get("source") or "")
        if not POOL.contains(src):
            return self._json({"error": "素材不在素材池内"}, 400)
        existing = WS.find_by_source(src)
        if existing:
            return self._json({"project": existing, "created": False})
        try:
            project = WS.create(src, name=body.get("name") or None)
        except (FileNotFoundError, OSError) as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"project": project, "created": True})

    def _analyze(self):
        body = self._body()
        src = str(body.get("source") or "")
        if not POOL.contains(src):
            return self._json({"error": "素材不在素材池内"}, 400)
        project = WS.find_by_source(src) or WS.create(src, name=body.get("name") or None)
        pid = project["id"]
        jid = uuid.uuid4().hex[:12]
        with JOBS_LOCK:
            JOBS[jid] = {"id": jid, "project": pid, "status": "running",
                         "started": time.time(), "elapsed_ms": 0}
        threading.Thread(target=_run_analysis, daemon=True,
                         args=(jid, pid, src, str(body.get("preset") or "实拍短片"),
                               bool(body.get("ai", True)))).start()
        return self._json({"job": JOBS[jid], "project": project})

    def _batch_analyze(self):
        body = self._body()
        sources = [str(s) for s in (body.get("sources") or []) if POOL.contains(str(s))]
        if not sources:
            return self._json({"error": "没有可检测的素材（需在素材池内）"}, 400)
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "kind": "batch-analyze", "status": "running",
               "total": len(sources), "done": 0, "elapsed_ms": 0, "started": time.time(),
               "items": [{"source": s, "name": Path(s).name, "status": "pending"} for s in sources]}
        with JOBS_LOCK:
            JOBS[jid] = job
        threading.Thread(target=_run_batch_analyze, daemon=True,
                         args=(job, sources, str(body.get("preset") or "实拍短片"),
                               bool(body.get("ai", True)),
                               int(body.get("workers") or 2))).start()
        return self._json({"job": job})

    def _batch_export(self):
        body = self._body()
        ids = [str(i) for i in (body.get("projects") or [])]
        projects = [p for p in (WS.get(i) for i in ids) if p]
        if not projects:
            return self._json({"error": "没有可导出的项目"}, 400)
        mode = str(body.get("mode") or "smart")
        out_kind = str(body.get("out") or "clips")
        if mode not in ("copy", "smart", "exact") or out_kind not in ("clips", "shots"):
            return self._json({"error": "参数不合法"}, 400)
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "kind": "batch-export", "status": "running",
               "total": len(projects), "done": 0, "elapsed_ms": 0, "started": time.time(),
               "items": [{"project": p["id"], "name": p["name"], "status": "pending"}
                         for p in projects]}
        with JOBS_LOCK:
            JOBS[jid] = job
        threading.Thread(target=_run_batch_export, daemon=True,
                         args=(job, projects, mode, out_kind,
                               str(body.get("naming") or Handler.DEFAULT_NAMING),
                               int(body.get("workers") or 2))).start()
        return self._json({"job": job})

    def _project_rename(self):
        body = self._body()
        project = WS.rename(str(body.get("project") or ""), str(body.get("name") or ""))
        return self._json({"ok": project is not None, "project": project})

    def _project_remove(self):
        body = self._body()
        return self._json({"ok": WS.remove(str(body.get("project") or ""))})

    # ---------- 预览 / 编辑 ----------
    def _analysis(self, qs):
        project, media = _resolve(qs)
        if not media:
            return self._json({"error": "no media"}, 404)
        if project:
            data = WS.load_analysis(project["id"])
            if data:
                from app.store.edit import get_clips

                if not data.get("clips"):
                    data["clips"] = get_clips(data)
                data["edit_pos"] = int(data.get("edit_pos", len(data.get("edit_history", []))))
                data["edit_len"] = len(data.get("edit_history", []))
                data.pop("edit_history", None)
                data["project"] = {k: project[k] for k in ("id", "name", "source", "status")}
                return self._json(data)
        meta = _probe(media)
        return self._json({"schema": "scene-cut/1", "asset_id": "preview",
                           "source": {"path": media}, "probe": meta,
                           "run": {"engine": "none"}, "shots": [], "clips": [],
                           "project": None})

    def _frame(self, qs):
        project, media = _resolve(qs)
        if not media:
            return self._send(404, b"no media", "text/plain")
        try:
            frame = int((qs.get("frame") or ["0"])[0])
            width = int((qs.get("width") or ["960"])[0])
        except ValueError:
            return self._send(400, b"bad frame", "text/plain")
        meta = _probe(media)
        key = f"{media}|{width}"
        svc = get_service(key, media, float(meta["fps"] or 30.0),
                          int(meta["nb_frames"] or 1), width=width)
        try:
            data = svc.frame(frame)
        except RuntimeError as exc:
            return self._send(500, str(exc).encode(), "text/plain")
        if (qs.get("prefetch") or ["1"])[0] == "1":
            svc.prefetch(frame)
        return self._send(200, data, "image/jpeg")

    def _video(self, qs):
        _, media = _resolve(qs)
        if not media or not Path(media).exists():
            return self._send(404, b"no media", "text/plain")
        file = Path(media)
        size = file.stat().st_size
        ctype = mimetypes.guess_type(file.name)[0] or "video/mp4"
        rng = self.headers.get("Range")
        if not rng:
            return self._send(200, file.read_bytes(), ctype, {"Accept-Ranges": "bytes"})
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if not m:
            return self._send(416, b"bad range", "text/plain")
        start = int(m.group(1) or 0)
        end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
        length = max(0, end - start + 1)
        self.send_response(206)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with file.open("rb") as fh:
            fh.seek(start)
            remain = length
            while remain > 0:
                chunk = fh.read(min(1 << 20, remain))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remain -= len(chunk)

    def _strip(self, qs):
        project, media = _resolve(qs)
        if not media:
            return self._send(404, b"no media", "text/plain")
        meta = _probe(media)
        tiles = max(4, min(60, int((qs.get("tiles") or ["40"])[0])))
        start = float((qs.get("start") or ["0"])[0])
        end = float((qs.get("end") or [str(meta["duration"])])[0])
        end = max(end, start + 0.5)
        cache_dir = (Path(project["dir"]) / "cache") if project else (DATA_ROOT / "appdata" / "cache")
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = f"strip_{abs(hash(media)) % 10**8}_{tiles}_{start:.2f}_{end:.2f}.jpg"
        out = cache_dir / key
        if not out.exists():
            step = max((end - start) / tiles, 0.04)
            proc = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", media,
                "-vf", f"fps=1/{step:.6f},scale=160:-2,tile={tiles}x1",
                "-frames:v", "1", "-q:v", "4", str(out),
            ], capture_output=True)
            if proc.returncode != 0 or not out.exists():
                return self._send(500, proc.stderr[-300:], "text/plain")
        return self._send(200, out.read_bytes(), "image/jpeg")

    def _edit(self):
        body = self._body()
        pid = str(body.get("project") or "")
        data = WS.load_analysis(pid) if pid else None
        if data is None:
            return self._json({"error": "项目未分析"}, 404)
        from app.store.edit import apply

        try:
            result = apply(data, str(body.get("op") or ""), body)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": str(exc)[:200]}, 500)
        WS.save_analysis(pid, data)
        return self._json(result)

    # ---------- 导出产物回看 ----------
    def _clip_path(self, qs: dict) -> Path | None:
        pid = (qs.get("project") or [""])[0]
        name = (qs.get("name") or [""])[0]
        project = WS.get(pid) if pid else None
        if not project or not name or Path(name).name != name:
            return None
        base = Path(project["dir"])
        for sub in ("clips", "shots"):
            candidate = base / sub / name
            if candidate.exists():
                return candidate
        return None

    def _export_file(self, qs: dict) -> None:
        file = self._clip_path(qs)
        if not file:
            return self._send(404, b"no clip", "text/plain")
        size = file.stat().st_size
        ctype = mimetypes.guess_type(file.name)[0] or "video/mp4"
        rng = self.headers.get("Range")
        if not rng:
            return self._send(200, file.read_bytes(), ctype, {"Accept-Ranges": "bytes"})
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if not m:
            return self._send(416, b"bad range", "text/plain")
        start = int(m.group(1) or 0)
        end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
        length = max(0, end - start + 1)
        self.send_response(206)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with file.open("rb") as fh:
            fh.seek(start)
            remain = length
            while remain > 0:
                chunk = fh.read(min(1 << 20, remain))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remain -= len(chunk)

    def _export_frame(self, qs: dict) -> None:
        file = self._clip_path(qs)
        if not file:
            return self._send(404, b"no clip", "text/plain")
        try:
            frame = int((qs.get("frame") or ["0"])[0])
            width = int((qs.get("width") or ["320"])[0])
        except ValueError:
            return self._send(400, b"bad params", "text/plain")
        proc = subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(frame, 0) / 30.0:.4f}", "-i", str(file),
            "-frames:v", "1", "-vf", f"scale={width}:-2",
            "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "5", "pipe:1",
        ], capture_output=True)
        if proc.returncode != 0 or not proc.stdout:
            return self._send(500, proc.stderr[-200:], "text/plain")
        return self._send(200, proc.stdout, "image/jpeg")

    # ---------- 导出 ----------
    DEFAULT_NAMING = "{kind}{index:04d}_{source}_{start_tc}"

    def _export_last(self, qs: dict) -> None:
        """读取项目 _manifest/manifest.json，用于"查看上次导出结果"。"""
        pid = (qs.get("project") or [""])[0]
        project = WS.get(pid) if pid else None
        if not project:
            return self._json({"error": "no project"}, 404)
        path = Path(project["dir"]) / "_manifest" / "manifest.json"
        if not path.exists():
            return self._json({"results": [], "count": 0})
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return self._json({"error": str(exc)[:120]}, 500)
        results = [{
            "file": r.get("file"),
            "frames": r.get("frames"),
            "expected": r.get("expected_frames"),
            "offset_s": r.get("offset_s", 0),
            "size": r.get("size", 0),
            "status": r.get("status"),
            "error": r.get("error", ""),
        } for r in data.get("rows", [])]
        return self._json({"results": results, "count": len(results),
                           "mode": data.get("mode"), "exported_at": data.get("exported_at")})

    def _export_preview(self, qs):
        pid = (qs.get("project") or [""])[0]
        project = WS.get(pid) if pid else None
        data = WS.load_analysis(pid) if project else None
        if not project or data is None:
            return self._json({"error": "项目未分析"}, 404)
        from app.export.runner import plan

        indices = _parse_indices((qs.get("indices") or [""])[0])
        out_kind = (qs.get("out") or ["clips"])[0]
        naming = (qs.get("naming") or [self.DEFAULT_NAMING])[0]
        segments = plan(project, data, indices, out_kind, naming)
        return self._json({"count": len(segments),
                           "segments": [{k: s[k] for k in ("index", "start", "end", "name")}
                                        for s in segments[:200]],
                           "dir": str(Path(project["dir"]) / out_kind)})

    def _export(self):
        body = self._body()
        pid = str(body.get("project") or "")
        project = WS.get(pid) if pid else None
        data = WS.load_analysis(pid) if project else None
        if not project or data is None:
            return self._json({"error": "项目未分析"}, 404)
        from app.export.runner import plan, run_export

        mode = str(body.get("mode") or "smart")
        if mode not in ("copy", "smart", "exact"):
            return self._json({"error": f"未知导出模式: {mode}"}, 400)
        out_kind = str(body.get("out") or "clips")
        if out_kind not in ("clips", "shots"):
            return self._json({"error": f"未知输出目录: {out_kind}"}, 400)
        naming = str(body.get("naming") or self.DEFAULT_NAMING)
        indices = body.get("indices") or None
        segments = plan(project, data, [int(i) for i in indices] if indices else None,
                        out_kind, naming)
        if not segments:
            return self._json({"error": "没有可导出的镜头"}, 400)
        WS.update(pid, export={"mode": mode, "out": out_kind, "naming": naming})

        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "project": pid, "status": "running", "started": time.time(),
               "elapsed_ms": 0, "total": len(segments), "done": 0, "current": "",
               "results": []}
        with JOBS_LOCK:
            JOBS[jid] = job
        out_dir = Path(project["dir"]) / out_kind
        threading.Thread(target=run_export, daemon=True,
                         args=(job, project, data, segments, mode, out_dir,
                               str(body.get("encoder") or "nvenc"),
                               int(body.get("crf") or 18))).start()
        return self._json({"job": job,
                           "planned": [{"index": s["index"], "name": s["name"]} for s in segments],
                           "dir": str(out_dir)})


def _analyze_sync(pid: str, source: str, preset: str, use_ai: bool) -> dict:
    from app.detect.pipeline import analyze

    WS.update(pid, status="analyzing")
    WS.log(pid, f"start analyze source={source} preset={preset} ai={use_ai}")
    directory = WS.dir_of(pid)
    result = analyze(source, directory, asset_id="analysis",
                     preset_name=preset, use_ai=use_ai)
    WS.update(pid, status="analyzed", analyzed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    WS.log(pid, f"done shots={result['shots']} cuts={len(result['cuts'])} "
                f"elapsed={result['elapsed_ms']}ms")
    PROBES.pop(source, None)
    return result


def _run_analysis(jid: str, pid: str, source: str, preset: str, use_ai: bool) -> None:
    t0 = time.perf_counter()
    try:
        result = _analyze_sync(pid, source, preset, use_ai)
        JOBS[jid].update({"status": "done", "shots": result["shots"],
                          "elapsed_ms": int((time.perf_counter() - t0) * 1000)})
    except Exception as exc:  # noqa: BLE001
        WS.update(pid, status="failed", error=str(exc)[:300])
        WS.log(pid, f"failed: {exc}")
        JOBS[jid].update({"status": "failed", "error": str(exc)[:300],
                          "elapsed_ms": int((time.perf_counter() - t0) * 1000)})


def _run_batch_analyze(job: dict, sources: list[str], preset: str, use_ai: bool,
                       workers: int = 2) -> None:
    """批量检测：有限并发（默认 2 路），失败隔离，逐条更新状态。"""
    from concurrent.futures import ThreadPoolExecutor

    t0 = time.perf_counter()
    lock = threading.Lock()
    workers = max(1, min(4, int(workers)))

    def work(index: int, src: str) -> None:
        item = job["items"][index]
        item["status"] = "running"
        try:
            project = WS.find_by_source(src) or WS.create(src)
            item["project"] = project["id"]
            result = _analyze_sync(project["id"], src, preset, use_ai)
            item.update(status="done", shots=result["shots"],
                        elapsed_ms=result["elapsed_ms"])
        except Exception as exc:  # noqa: BLE001
            item.update(status="failed", error=str(exc)[:200])
        finally:
            with lock:
                job["done"] = int(job.get("done", 0)) + 1
                job["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda pair: work(*pair), enumerate(sources)))
    job["status"] = "done"
    job["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)


def _run_batch_export(job: dict, projects: list[dict], mode: str,
                      out_kind: str, naming: str, workers: int = 2) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from app.export.runner import plan, run_export

    t0 = time.perf_counter()
    lock = threading.Lock()
    workers = max(1, min(4, int(workers)))

    def work(index: int, project: dict) -> None:
        item = job["items"][index]
        item["status"] = "running"
        data = WS.load_analysis(project["id"])
        if not data:
            item.update(status="failed", error="项目未分析")
            with lock:
                job["done"] = int(job.get("done", 0)) + 1
            return
        segments = plan(project, data, None, out_kind, naming)
        out_dir = Path(project["dir"]) / out_kind
        sub = {"id": f"{job['id']}-{project['id']}", "results": []}
        try:
            run_export(sub, project, data, segments, mode, out_dir)
            results = sub.get("results", [])
            ok = sum(1 for r in results if r.get("status") == "ok")
            item.update(status="done", ok=ok, failed=len(results) - ok, dir=str(out_dir))
        except Exception as exc:  # noqa: BLE001
            item.update(status="failed", error=str(exc)[:200])
        finally:
            with lock:
                job["done"] = int(job.get("done", 0)) + 1
                job["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda pair: work(*pair), enumerate(projects)))
    job["status"] = "done"
    job["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)


def main(argv: list[str] | None = None) -> int:
    global POOL, WS
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=str(DATA_ROOT / "workspace"))
    ap.add_argument("--pool", default=str(DATA_ROOT / "appdata" / "pool.json"))
    ap.add_argument("--port", type=int, default=8768)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)

    POOL = Pool(Path(args.pool))
    WS = Workspace(Path(args.workspace))
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"scene-cut: http://{args.host}:{args.port}/  workspace={WS.root}  "
          f"pool_roots={len(POOL.roots)}  projects={len(WS.list_projects())}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
