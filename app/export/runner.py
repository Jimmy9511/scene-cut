"""导出任务：切段规划 → 命名 → 三档导出 → 清单。

方案 §7。默认平铺命名 `{kind}{index:04d}_{source}_{start_tc}`，
产物写入项目内 `clips/`（默认）或 `shots/`，并在项目根写 `_manifest/`。
"""

from __future__ import annotations

import csv
import json
import os
import threading
import time
from pathlib import Path

from app import log as logmod
from app.export.cut import cut_copy, cut_exact, cut_smart, duration, frame_count


def _tc(frame: int, fps: float, sep: str = "-") -> str:
    ms = int(round(frame * 1000.0 / max(fps, 1e-9)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}{sep}{m:02d}{sep}{s:02d}{sep}{milli:03d}"


_BAD = '<>:"/\\|?*'


def _safe(text: str, limit: int = 60) -> str:
    text = "".join("_" if c in _BAD or ord(c) < 32 else c for c in text).strip().rstrip(".")
    return text[:limit] or "clip"


def render_name(template: str, ctx: dict) -> str:
    out = template
    # {token:04d} 补零 / {token} 原样
    for key, value in ctx.items():
        if isinstance(value, int):
            padded = f"{value:04d}"
            out = out.replace("{" + key + ":04d}", padded)
        out = out.replace("{" + key + "}", str(value))
    return _safe(out, 180)


def plan(project: dict, analysis: dict, indices: list[int] | None,
         out_kind: str, naming: str) -> list[dict]:
    clips = analysis.get("clips") or []
    fps = float(analysis.get("probe", {}).get("fps") or 30.0)
    source_stem = Path(project.get("source", "")).stem
    picked = clips if not indices else [clips[i] for i in indices if 0 <= i < len(clips)]
    project_name = project.get("name") or Path(project.get("dir", "")).name
    date = time.strftime("%Y%m%d")

    segments: list[dict] = []
    used: set[str] = set()
    for n, clip in enumerate(picked, start=1):
        start, end = int(clip["start"]), int(clip["end"])
        ctx = {
            "kind": out_kind,
            "index": n,
            "source": source_stem,
            "project": project_name,
            "date": date,
            "start_tc": _tc(start, fps),
            "end_tc": _tc(end, fps),
            "start_s": f"{start / fps:.3f}",
            "end_s": f"{end / fps:.3f}",
            "start_f": start,
            "end_f": end,
            "duration": f"{(end - start) / fps:.3f}",
            "label": clip.get("label") or "clip",
            "type": "clip",
        }
        name = render_name(naming, ctx) + ".mp4"
        base, ext = os.path.splitext(name)
        candidate, i = name, 2
        while candidate.lower() in used:
            candidate = f"{base}_{i}{ext}"
            i += 1
        used.add(candidate.lower())
        segments.append({"index": n, "start": start, "end": end, "name": candidate,
                         "fps": fps, "label": clip.get("label") or ""})
    return segments


def _probe_frames(path: Path, fps: float) -> int:
    try:
        n = frame_count(path)
        if n > 0:
            return n
    except Exception:  # noqa: BLE001
        pass
    try:
        return int(round(duration(path) * fps))
    except Exception:  # noqa: BLE001
        return -1


def run_export(job: dict, project: dict, analysis: dict, segments: list[dict],
               mode: str, out_dir: Path, encoder: str = "nvenc", crf: int = 18) -> None:
    fps = float(analysis.get("probe", {}).get("fps") or 30.0)
    source = project["source"]
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir = Path(project["dir"]) / "_manifest"
    manifest_dir.mkdir(parents=True, exist_ok=True)

    job.update({"status": "running", "total": len(segments), "done": 0,
                "current": "", "results": [], "out_dir": str(out_dir)})
    started = time.perf_counter()

    for seg in segments:
        target = out_dir / seg["name"]
        job["current"] = seg["name"]
        job["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        try:
            s = seg["start"] / fps
            e = seg["end"] / fps
            if mode == "copy":
                info = cut_copy(source, s, e, target)
            elif mode == "exact":
                info = cut_exact(source, s, e, target, encoder=encoder, crf=crf,
                                 frames=seg["end"] - seg["start"])
            else:
                info = cut_smart(source, s, e, target, encoder=encoder, crf=crf, fps=fps)
            frames = _probe_frames(target, fps)
            actual_start = float(info.get("actual_start") or 0)
            is_copy = "copy" in str(info.get("mode"))
            offset = round(actual_start - seg["start"] / fps, 4) if is_copy else 0.0
            # copy 从关键帧起算，期望帧数也要按实际起点算，否则永远"不一致"
            expected = (round((seg["end"] / fps - actual_start) * fps) if is_copy
                        else seg["end"] - seg["start"])
            job["results"].append({
                "index": seg["index"], "file": seg["name"], "status": "ok",
                "start_frame": seg["start"], "end_frame": seg["end"],
                "frames": frames, "expected": expected,
                "offset_s": offset, "mode": info.get("mode", mode),
                "size": target.stat().st_size if target.exists() else 0,
            })
            logmod.detail("export", f" {seg['index']:03d} 完成 {seg['name']} "
                                    f"{frames} 帧 期望 {expected}")
        except Exception as exc:  # noqa: BLE001
            logmod.error("export", f" {seg['index']:03d} 失败 {seg['name']}: {exc}")
            job["results"].append({"index": seg["index"], "file": seg["name"],
                                   "status": "failed", "error": str(exc)[:200]})
        job["done"] += 1
        if job["done"] % 10 == 0 or job["done"] == len(segments):
            logmod.info("export", f"进度 {job['done']}/{len(segments)}")

    job["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    job["status"] = "done"
    job["current"] = ""
    ok = sum(1 for r in job["results"] if r.get("status") == "ok")
    logmod.info("export", f"导出完成 {ok}/{len(segments)} 段 用时 {job['elapsed_ms']}ms "
                          f"-> {out_dir}")
    _write_manifest(project, job, segments, mode, out_dir, fps, manifest_dir)


def _write_manifest(project: dict, job: dict, segments: list[dict], mode: str,
                    out_dir: Path, fps: float, manifest_dir: Path) -> None:
    rows = []
    for r in job.get("results", []):
        seg = next((s for s in segments if s["index"] == r["index"]), {})
        rows.append({
            "index": r["index"],
            "file": r["file"],
            "start_frame": r.get("start_frame"),
            "end_frame": r.get("end_frame"),
            "start_tc": _tc(seg.get("start", 0), fps, sep=":"),
            "end_tc": _tc(seg.get("end", 0), fps, sep=":"),
            "duration_s": round((seg.get("end", 0) - seg.get("start", 0)) / fps, 3),
            "mode": r.get("mode", mode),
            "frames": r.get("frames"),
            "expected_frames": r.get("expected"),
            "offset_s": r.get("offset_s", 0),
            "size": r.get("size", 0),
            "status": r.get("status"),
            "error": r.get("error", ""),
        })
    with (manifest_dir / "manifest.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["index"])
        writer.writeheader()
        writer.writerows(rows)
    (manifest_dir / "manifest.json").write_text(json.dumps({
        "project": project.get("name"),
        "source": project.get("source"),
        "mode": mode,
        "out_dir": str(out_dir),
        "count": len(rows),
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "rows": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
