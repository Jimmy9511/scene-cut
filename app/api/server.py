"""scene-cut 本地服务：素材池 + 项目 + 预览 + 编辑。

启动：
  python -m app.api.server --workspace <工作区目录> --port 8768
素材池索引默认放在 <仓库>/appdata/pool.json，可用 --pool 指定。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from app import __version__
from app import log as logmod
from app import settings as settingsmod
from app.detect.probe import probe as probe_media
from app.errors import Cancelled
from app.media.frameservice import get_service
from app.paths import DATA_ROOT, DATA_ROOT_SOURCE, WEB_DIR, pointer_path, write_pointer
from app.store import migrate as migratemod
from app.store.pool import MEDIA_EXT, Pool
from app.store.workspace import Workspace

ROOT = Path(__file__).resolve().parents[2]
WEB = WEB_DIR

POOL: Pool | None = None
WS: Workspace | None = None
HTTPD: ThreadingHTTPServer | None = None
PROBES: dict[str, dict] = {}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
PICK_LOCK = threading.Lock()
# 素材路径 → 正在跑的 job id（去重：同一条素材同时只允许一个检测）
ACTIVE_SOURCES: dict[str, str] = {}
# job id → 取消标志（用户点“取消”时置位）
CANCELS: dict[str, threading.Event] = {}
# 单素材检测的并发闸门：默认 1 路，避免多条素材同时解码把 CPU 打满
ANALYZE_SLOTS = threading.Semaphore(int(os.getenv("SCENECUT_ANALYZE_SLOTS", "1")))


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
    server_version = f"scene-cut/{__version__}"

    def log_message(self, fmt, *args):
        return

    def handle_one_request(self) -> None:
        """统一收口：请求计时（debug）+ 客户端断开静默 + 未预期异常记日志。"""
        started = time.perf_counter()
        self._status_code = 0
        try:
            super().handle_one_request()
        except ConnectionError:
            logmod.debug("http", f"连接中断 {self.command or '?'} {self.path or ''}", throttle=0.5)
        except OSError as exc:
            if getattr(exc, "winerror", None) in (10053, 10054, 10058, 10060):
                logmod.debug("http", f"连接中断（{exc.winerror}）{self.path or ''}", throttle=0.5)
            else:
                logmod.error("http", f"{self.command} {self.path} 出错：{exc}")
                logmod.debug("http", traceback.format_exc())
                self._safe_500()
        except Exception as exc:  # noqa: BLE001
            logmod.error("http", f"{self.command or '?'} {self.path or ''} 处理失败：{exc}")
            logmod.debug("http", traceback.format_exc())
            self._safe_500()
        finally:
            if getattr(self, "command", None):
                cost = (time.perf_counter() - started) * 1000
                logmod.debug(
                    "http",
                    f"{self.command} {unquote(self.path or '')} -> {self._status_code or '-'} {cost:.0f}ms",
                    throttle=0.05,
                )

    def _safe_500(self) -> None:
        if self._status_code:
            return
        try:
            if not self.wfile.closed:
                self.send_error(500)
                self._status_code = 500
        except Exception:  # noqa: BLE001
            pass

    # ---------- 基础 ----------
    def _send(self, code, body: bytes, ctype: str, extra: dict | None = None):
        self._status_code = code
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
        if path == "/api/jobs":
            with JOBS_LOCK:
                running = [dict(j) for j in JOBS.values() if j.get("status") == "running"]
            return self._json({"jobs": running})
        if path == "/api/log/level":
            return self._json({"level": logmod.get_level(), "levels": list(logmod.LEVELS)})
        if path == "/api/settings":
            return self._settings_get()
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
            "/api/job/cancel": self._job_cancel,
            "/api/log/level": self._log_level,
            "/api/settings": self._settings_update,
            "/api/settings/data-dir": self._settings_data_dir,
            "/api/app/restart": self._app_restart,
            "/api/app/reveal": self._app_reveal,
        }
        h = handlers.get(path)
        if not h:
            return self._send(404, b"not found", "text/plain")
        return h()

    def _log_level(self):
        body = self._body()
        wanted = str(body.get("level") or "").strip()
        if not wanted:
            return self._json({"level": logmod.get_level(), "levels": list(logmod.LEVELS)})
        try:
            level = logmod.set_level(wanted)
        except ValueError as exc:
            return self._json({"error": str(exc), "levels": list(logmod.LEVELS)}, 400)
        logmod.info("log", f"日志级别切换为 {level}")
        return self._json({"level": level, "levels": list(logmod.LEVELS)})

    # ---------- 设置 / 数据目录 ----------
    def _settings_payload(self) -> dict:
        data = settingsmod.load()
        workspace = settingsmod.workspace_root(DATA_ROOT / "workspace")
        return {
            "version": __version__,
            "data_root": str(DATA_ROOT),
            "data_root_source": DATA_ROOT_SOURCE,
            "data_root_pointer": str(pointer_path()),
            "workspace": str(workspace),
            "workspace_custom": bool(data.get("workspace")),
            "pool": str(POOL.file) if POOL is not None else "",
            "log_level": logmod.get_level(),
            "levels": list(logmod.LEVELS),
            "ai_fast": bool(data.get("ai_fast")),
            "t2_sample_limit": int(data.get("t2_sample_limit") or 20000),
        }

    def _settings_get(self):
        return self._json(self._settings_payload())

    def _analyze_running(self) -> bool:
        with JOBS_LOCK:
            return any(j.get("status") == "running" and (j.get("kind") or "analyze") == "analyze"
                       for j in JOBS.values())

    def _settings_update(self):
        global WS
        body = self._body()
        changes: dict = {}

        if "workspace" in body:
            raw = str(body.get("workspace") or "").strip()
            if self._analyze_running():
                return self._json({"error": "有检测正在进行，请先取消或等待完成"}, 409)
            try:
                if raw:
                    target = migratemod.validate_dir(raw, label="工作区目录")
                    if target == Path(DATA_ROOT).resolve():
                        return self._json({"error": "工作区不能直接用数据目录本身"}, 400)
                    changes["workspace"] = str(target)
                else:
                    target = Path(DATA_ROOT) / "workspace"
                    changes["workspace"] = None
                WS = Workspace(target)
                logmod.info("settings", f"工作区切换为 {WS.root}")
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            except OSError as exc:
                return self._json({"error": f"切换工作区失败：{exc}"}, 500)

        if "ai_fast" in body:
            changes["ai_fast"] = bool(body.get("ai_fast"))
        if "t2_sample_limit" in body:
            try:
                limit = int(body.get("t2_sample_limit"))
            except (TypeError, ValueError):
                return self._json({"error": "抽帧阈值必须是整数"}, 400)
            if not 1000 <= limit <= 500000:
                return self._json({"error": "抽帧阈值需在 1000–500000 之间"}, 400)
            changes["t2_sample_limit"] = limit
        if "log_level" in body:
            try:
                changes["log_level"] = logmod.set_level(str(body.get("log_level")))
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)

        if changes:
            settingsmod.update(**changes)
            logmod.info("settings", f"设置已更新：{changes}")
        return self._json(self._settings_payload())

    def _settings_data_dir(self):
        global WS
        body = self._body()
        raw = str(body.get("path") or "").strip()
        migrate = bool(body.get("migrate"))
        if self._analyze_running():
            return self._json({"error": "有检测正在进行，请先取消或等待完成"}, 409)
        try:
            target = migratemod.validate_dir(
                raw, label="数据目录", current_root=DATA_ROOT,
                other=WS.root if WS else None)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)

        stats = {}
        if migrate:
            try:
                stats = migratemod.migrate(DATA_ROOT, target)
            except OSError as exc:
                logmod.error("settings", f"迁移失败：{exc}")
                return self._json({"error": f"迁移失败（已复制部分保留在原处，仍使用旧目录）：{exc}"}, 500)

        try:
            write_pointer(target)
        except OSError as exc:
            return self._json({"error": f"无法写入指针文件（{pointer_path()}）：{exc}"}, 500)

        logmod.warn("settings", f"数据目录已指向 {target}（迁移={'是' if migrate else '否'}），需重启生效")
        return self._json({"ok": True, "requires_restart": True, "target": str(target),
                           "stats": stats, "settings": self._settings_payload()})

    def _app_restart(self):
        flag = Path(DATA_ROOT) / "restart.flag"
        try:
            flag.write_text(str(time.time()), encoding="utf-8")
        except OSError as exc:
            return self._json({"error": f"无法写入重启标记：{exc}"}, 500)
        logmod.warn("app", "收到重启请求，服务即将关闭（桌面版会自动重新拉起）")

        def _stop() -> None:
            time.sleep(0.4)
            if HTTPD is not None:
                HTTPD.shutdown()

        threading.Thread(target=_stop, daemon=True).start()
        return self._json({"ok": True,
                           "manual_restart_hint": "若 10 秒内没有自动回到界面，请手动重新打开程序"})

    def _app_reveal(self):
        body = self._body()
        raw = str(body.get("path") or "").strip() or str(DATA_ROOT)
        target = Path(raw).resolve()
        root = Path(DATA_ROOT).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return self._json({"error": "只能打开数据目录内的路径"}, 400)
        if not target.exists():
            return self._json({"error": f"路径不存在：{target}"}, 404)
        try:
            subprocess.Popen(["explorer", str(target)])
        except OSError as exc:
            return self._json({"error": f"无法调用资源管理器：{exc}"}, 500)
        return self._json({"ok": True, "path": str(target)})


    def _job_cancel(self):
        body = self._body()
        jid = str(body.get("id") or "")
        with JOBS_LOCK:
            job = JOBS.get(jid)
            if not job:
                return self._json({"error": "没有这个作业"}, 404)
            if job.get("status") != "running":
                return self._json({"ok": False, "error": "作业已结束", "job": job}, 409)
            event = CANCELS.get(jid)
            if event is None:
                event = threading.Event()
                CANCELS[jid] = event
            event.set()
            job["cancelled"] = True
        logmod.warn("job", f"请求取消作业 {jid}（{job.get('kind') or 'analyze'}）")
        return self._json({"ok": True, "job": job})

    # ---------- 素材池 ----------
    def _pool_import(self):
        body = self._body()
        raw = str(body.get("path") or "").strip().strip('"')
        if not raw:
            return self._json({"error": "缺少 path"}, 400)
        try:
            root = POOL.add(raw)
        except (FileNotFoundError, ValueError, OSError) as exc:
            logmod.warn("pool", f"导入失败 {raw}: {exc}")
            return self._json({"error": str(exc)}, 400)
        logmod.info("pool", f"导入素材 {root['path']}（{root['kind']}）")
        return self._json({"root": root, "roots": POOL.roots})

    def _pool_remove(self):
        body = self._body()
        ok = POOL.remove(str(body.get("root") or ""))
        logmod.info("pool", f"移除素材根 {body.get('root')} -> {ok}")
        return self._json({"ok": ok})

    def _pool_rename(self):
        body = self._body()
        root = POOL.rename(str(body.get("root") or ""), str(body.get("alias") or ""))
        logmod.info("pool", f"重命名素材根 {body.get('root')} -> {root['alias'] if root else None}")
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
        logmod.info("project", f"新建项目 {project['name']} ({project['id']})")
        return self._json({"project": project, "created": True})

    def _analyze(self):
        body = self._body()
        src = str(body.get("source") or "")
        if not POOL.contains(src):
            return self._json({"error": "素材不在素材池内"}, 400)
        if not src or not Path(src).is_file():
            logmod.warn("detect", f"素材不存在，无法检测：{src}")
            return self._json({"error": f"素材不存在：{src}"}, 400)

        # 去重：同一条素材已在检测时直接复用现有作业，避免重复占用 CPU
        if not os.environ.get("SCENECUT_NO_DEDUPE"):
            with JOBS_LOCK:
                running_id = ACTIVE_SOURCES.get(src)
                running = JOBS.get(running_id) if running_id else None
                if running and running.get("status") == "running":
                    logmod.info("detect", f"复用进行中的检测 {running['id']}（{Path(src).name}）")
                    return self._json({"job": running, "reused": True,
                                       "project": WS.find_by_source(src)})

        project = WS.find_by_source(src) or WS.create(src, name=body.get("name") or None)
        pid = project["id"]
        jid = uuid.uuid4().hex[:12]
        preset = str(body.get("preset") or "实拍短片")
        use_ai = bool(body.get("ai", True))
        ai_fast = bool(settingsmod.get("ai_fast"))
        t2_limit = int(settingsmod.get("t2_sample_limit") or 20000)
        job = {"id": jid, "kind": "analyze", "project": pid, "source": src,
               "status": "running", "phase": "queued", "progress": 0.0,
               "message": "排队中", "started": time.time(), "elapsed_ms": 0}
        with JOBS_LOCK:
            JOBS[jid] = job
            ACTIVE_SOURCES[src] = jid
            CANCELS[jid] = threading.Event()
        logmod.info("detect", f"开始检测 {Path(src).name} -> 项目 {project['name']} "
                              f"preset={preset} ai={'on' if use_ai else 'off'} "
                              f"fast={'on' if ai_fast else 'off'} job={jid}")
        threading.Thread(target=_run_analysis, daemon=True,
                         args=(jid, pid, src, preset, use_ai, False, ai_fast, t2_limit)).start()
        return self._json({"job": job, "project": project, "reused": False})

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
            CANCELS[jid] = threading.Event()
        logmod.info("batch", f"批量检测开始 {len(sources)} 条 job={jid}")
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
        if project:
            logmod.info("project", f"重命名项目 -> {project['name']}")
        return self._json({"ok": project is not None, "project": project})

    def _project_remove(self):
        body = self._body()
        ok = WS.remove(str(body.get("project") or ""))
        logmod.info("project", f"删除项目 {body.get('project')} -> {ok}")
        return self._json({"ok": ok})

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
            # 没有 Range 时也要流式发送，绝不整文件读进内存（大文件会直接吃光 RAM）
            start, end, code = 0, size - 1, 200
        else:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if not m:
                return self._send(416, b"bad range", "text/plain")
            start = int(m.group(1) or 0)
            end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
            code = 206
        length = max(0, end - start + 1)
        logmod.debug("video", f"流式发送 {file.name} {start}-{end}/{size} ({length} B)")

        self._status_code = code
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if length <= 0:
            return
        try:
            with file.open("rb") as fh:
                fh.seek(start)
                remain = length
                while remain > 0:
                    chunk = fh.read(min(1 << 20, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remain -= len(chunk)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            # 播放器拖动/切走会主动断开连接，这是正常现象
            logmod.debug("video", f"客户端中断 {file.name}（剩余 {remain} B）", throttle=0.5)

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

        op = str(body.get("op") or "")
        try:
            result = apply(data, op, body)
        except ValueError as exc:
            logmod.warn("edit", f"编辑被拒绝 op={op}: {exc}")
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            logmod.error("edit", f"编辑失败 op={op}: {exc}")
            return self._json({"error": str(exc)[:200]}, 500)
        WS.save_analysis(pid, data)
        extra = ""
        for key in ("frame", "ids", "id", "pos"):
            if key in body:
                extra += f" {key}={body[key]}"
        logmod.info("edit", f"{op}{extra} -> 历史 {result.get('edit_pos')}/{result.get('edit_len')}")
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
        job = {"id": jid, "kind": "export", "project": pid, "status": "running",
               "started": time.time(), "elapsed_ms": 0, "total": len(segments),
               "done": 0, "current": "", "results": []}
        with JOBS_LOCK:
            JOBS[jid] = job
        out_dir = Path(project["dir"]) / out_kind
        logmod.info("export", f"开始导出 {project['name']} {len(segments)} 段 "
                              f"mode={mode} out={out_kind} job={jid}")
        threading.Thread(target=run_export, daemon=True,
                         args=(job, project, data, segments, mode, out_dir,
                               str(body.get("encoder") or "nvenc"),
                               int(body.get("crf") or 18))).start()
        return self._json({"job": job,
                           "planned": [{"index": s["index"], "name": s["name"]} for s in segments],
                           "dir": str(out_dir)})


def _analyze_sync(pid: str, source: str, preset: str, use_ai: bool,
                  progress=None, should_cancel=None,
                  ai_fast: bool = False, t2_sample_limit: int = 20000) -> dict:
    from app.detect.pipeline import analyze

    WS.update(pid, status="analyzing", error=None)
    WS.log(pid, f"start analyze source={source} preset={preset} ai={use_ai} fast={ai_fast}")
    directory = WS.dir_of(pid)
    result = analyze(source, directory, asset_id="analysis",
                     preset_name=preset, use_ai=use_ai,
                     progress=progress, should_cancel=should_cancel,
                     ai_fast=ai_fast, t2_sample_limit=t2_sample_limit)
    WS.update(pid, status="analyzed", analyzed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    WS.log(pid, f"done shots={result['shots']} cuts={len(result['cuts'])} "
                f"elapsed={result['elapsed_ms']}ms")
    PROBES.pop(source, None)
    return result


def _job_progress(job: dict, t0: float):
    """构造进度回调：更新 job 字段，并按 10% 步进打 info 日志。"""
    last_logged = {"pct": -10}

    def progress(phase: str, fraction: float, note: str = "") -> None:
        pct = int(max(0.0, min(1.0, fraction)) * 100)
        with JOBS_LOCK:
            job["phase"] = phase
            job["progress"] = round(max(0.0, min(1.0, fraction)), 4)
            job["message"] = note
            job["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        if pct >= last_logged["pct"] + 10:
            last_logged["pct"] = pct
            logmod.info("detect", f"{phase} {pct}% {note}".rstrip())

    return progress


def _finish_job(jid: str, source: str, **fields) -> None:
    with JOBS_LOCK:
        job = JOBS.get(jid)
        if job is not None:
            job.update(fields)
        if ACTIVE_SOURCES.get(source) == jid:
            ACTIVE_SOURCES.pop(source, None)
        CANCELS.pop(jid, None)


def _run_analysis(jid: str, pid: str, source: str, preset: str, use_ai: bool,
                  cancelled: bool = False, ai_fast: bool = False,
                  t2_sample_limit: int = 20000) -> None:
    t0 = time.perf_counter()
    event = CANCELS.get(jid) or threading.Event()

    def should_cancel() -> bool:
        return event.is_set()

    # 排队等闸门；等待期间保持 queued 状态，取消可立即退出
    acquired = False
    try:
        while not ANALYZE_SLOTS.acquire(timeout=0.5):
            if should_cancel():
                raise Cancelled("用户取消")
            with JOBS_LOCK:
                job = JOBS.get(jid)
                if job is not None:
                    job["phase"] = "queued"
                    job["message"] = "等待前面的检测结束"
                    job["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        acquired = True
        result = _analyze_sync(pid, source, preset, use_ai,
                               progress=_job_progress(JOBS.get(jid, {}), t0),
                               should_cancel=should_cancel,
                               ai_fast=ai_fast, t2_sample_limit=t2_sample_limit)
        _finish_job(jid, source, status="done", phase="done", progress=1.0,
                    message="完成", shots=result["shots"],
                    elapsed_ms=int((time.perf_counter() - t0) * 1000))
        logmod.info("detect", f"检测完成 {Path(source).name} {result['shots']} 镜头 "
                              f"{(time.perf_counter() - t0):.1f}s")
    except Cancelled:
        WS.update(pid, status="new", error=None)
        WS.log(pid, "cancelled by user")
        _finish_job(jid, source, status="cancelled", message="已取消",
                    elapsed_ms=int((time.perf_counter() - t0) * 1000))
        logmod.warn("detect", f"检测已取消 {Path(source).name}")
    except Exception as exc:  # noqa: BLE001
        WS.update(pid, status="failed", error=str(exc)[:300])
        WS.log(pid, f"failed: {exc}")
        _finish_job(jid, source, status="failed", error=str(exc)[:300],
                    message="失败", elapsed_ms=int((time.perf_counter() - t0) * 1000))
        logmod.error("detect", f"检测失败 {Path(source).name}: {exc}")
    finally:
        if acquired:
            ANALYZE_SLOTS.release()


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
        logmod.info("batch", f"[{index + 1}/{len(sources)}] 开始 {Path(src).name}")
        try:
            project = WS.find_by_source(src) or WS.create(src)
            item["project"] = project["id"]
            result = _analyze_sync(project["id"], src, preset, use_ai,
                                   ai_fast=bool(settingsmod.get("ai_fast")),
                                   t2_sample_limit=int(settingsmod.get("t2_sample_limit") or 20000))
            item.update(status="done", shots=result["shots"],
                        elapsed_ms=result["elapsed_ms"])
            logmod.info("batch", f"[{index + 1}/{len(sources)}] 完成 {Path(src).name} "
                                 f"{result['shots']} 镜头")
        except Exception as exc:  # noqa: BLE001
            item.update(status="failed", error=str(exc)[:200])
            logmod.error("batch", f"[{index + 1}/{len(sources)}] 失败 {Path(src).name}: {exc}")
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


def _recover_stale_projects() -> None:
    """启动时把"卡在 analyzing"的项目标为失败：进程被杀时状态会永久残留。"""
    if WS is None:
        return
    with JOBS_LOCK:
        active = set(ACTIVE_SOURCES.keys())
    for project in WS.list_projects():
        if project.get("status") != "analyzing":
            continue
        if project.get("source") in active:
            continue
        WS.update(project["id"], status="failed",
                  error="上次检测未完成（程序被关闭或崩溃），请重新检测")
        WS.log(project["id"], "recovered: stale analyzing -> failed")
        logmod.warn("detect", f"上次检测未完成：{project.get('name')}（已标记失败，可重新检测）")


def _self_check() -> None:
    """后台自检：把影响检测的环境信息一次打出来，省得猜。"""
    try:
        proc = subprocess.run(["ffmpeg", "-version"], capture_output=True,
                              text=True, timeout=15, encoding="utf-8", errors="replace")
        first = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
        logmod.info("env", f"ffmpeg: {first[:100]}")
    except Exception as exc:  # noqa: BLE001
        logmod.error("env", f"ffmpeg 不可用：{exc}（检测/导出会失败，请安装或放进 _internal\\_bin）")
    try:
        import cv2
        logmod.info("env", f"opencv {cv2.__version__}")
    except Exception as exc:  # noqa: BLE001
        logmod.error("env", f"opencv 不可用：{exc}")
    try:
        import torch
        logmod.info("env", f"torch {torch.__version__} cuda={torch.cuda.is_available()} "
                           f"threads={torch.get_num_threads()}")
    except Exception as exc:  # noqa: BLE001
        logmod.warn("env", f"torch 不可用（AI 渐变检测会降级为仅 T1）：{exc}")


def main(argv: list[str] | None = None) -> int:
    global POOL, WS, HTTPD
    default_ws = settingsmod.workspace_root(DATA_ROOT / "workspace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=str(default_ws))
    ap.add_argument("--pool", default=str(DATA_ROOT / "appdata" / "pool.json"))
    ap.add_argument("--port", type=int, default=8768)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--log-level", default=None,
                    help="日志级别：debug/detail/info/warning/error（默认 info）")
    args = ap.parse_args(argv)

    level = logmod.setup(args.log_level, log_dir=DATA_ROOT)
    logmod.info("app", f"scene-cut {__version__} 启动（日志级别={level}，"
                       f"数据目录={DATA_ROOT}，来源={DATA_ROOT_SOURCE}）")

    POOL = Pool(Path(args.pool))
    WS = Workspace(Path(args.workspace))
    logmod.info("app", f"工作区={WS.root}（{'自定义' if settingsmod.get('workspace') else '默认'}）")
    _recover_stale_projects()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    HTTPD = httpd
    logmod.info("app", f"监听 http://{args.host}:{args.port}/  "
                       f"素材根={len(POOL.roots)}  项目={len(WS.list_projects())}")
    print(f"scene-cut: http://{args.host}:{args.port}/  workspace={WS.root}  "
          f"pool_roots={len(POOL.roots)}  projects={len(WS.list_projects())}", flush=True)
    threading.Thread(target=_self_check, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logmod.info("app", "收到中断，退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
