"""数据目录迁移与路径校验。

只在"界面里改数据目录"时使用：把工作区 / 素材池索引 / 日志 / 设置复制到新根，
保留旧目录（不删除），由上层写入指针并提示重启。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from app import log as logmod

# 迁移时复制的内容（webview2 / browser-profile 是缓存，不搬）
MIGRATE_DIRS = ("workspace", "logs")
MIGRATE_FILES = ("appdata/pool.json", "settings.json")


def dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def validate_dir(raw: str, *, label: str = "目录", current_root: Path | None = None,
                 other: Path | None = None) -> Path:
    """校验并创建目标目录；不合法时抛 ValueError（消息直接展示给用户）。"""
    text = (raw or "").strip().strip('"')
    if not text:
        raise ValueError(f"请填写{label}路径")
    target = Path(text).expanduser()
    if not target.is_absolute():
        raise ValueError(f"{label}必须是绝对路径：{target}")
    target = target.resolve()
    if target.exists() and not target.is_dir():
        raise ValueError(f"{label}不是文件夹：{target}")
    if current_root is not None and target == Path(current_root).resolve():
        raise ValueError(f"{label}与当前路径相同，无需修改")
    if current_root is not None:
        root = Path(current_root).resolve()
        if _inside(target, root) or _inside(root, target):
            raise ValueError(f"{label}不能与当前数据目录互相嵌套：{root}")
    if other is not None:
        other = Path(other).resolve()
        if _inside(target, other) or _inside(other, target):
            raise ValueError(f"{label}不能与工作区目录互相嵌套：{other}")
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise ValueError(f"{label}不可写：{target}（{exc}）") from exc
    return target


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def migrate(src_root: Path, dst_root: Path) -> dict:
    """复制数据到新根，返回统计。失败抛 OSError（已复制的文件保留）。"""
    src_root = Path(src_root).resolve()
    dst_root = Path(dst_root).resolve()
    stats = {"copied_bytes": 0, "files": 0, "workspace_rewritten": False}

    for name in MIGRATE_DIRS:
        src = src_root / name
        if not src.exists():
            continue
        size = dir_size(src)
        logmod.info("migrate", f"复制 {name}（{size / 1e6:.1f} MB）→ {dst_root / name}")
        shutil.copytree(src, dst_root / name, dirs_exist_ok=True)
        stats["copied_bytes"] += size

    for rel in MIGRATE_FILES:
        src = src_root / rel
        if not src.exists():
            continue
        dst = dst_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        stats["copied_bytes"] += src.stat().st_size

    # settings.json 里的 workspace 若在新根之外（旧根内），改写到新根的等价位置
    settings_file = dst_root / "settings.json"
    if settings_file.exists():
        try:
            data = json.loads(settings_file.read_text(encoding="utf-8"))
            ws = data.get("workspace")
            if ws:
                ws_path = Path(str(ws))
                try:
                    ws_path = ws_path.resolve()      # 兼容 8.3 短路径 / 符号链接
                except OSError:
                    pass
                if _inside(ws_path, src_root):
                    new_ws = dst_root / ws_path.relative_to(src_root)
                    data["workspace"] = str(new_ws)
                    stats["workspace_rewritten"] = True
                    settings_file.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass

    stats["files"] = sum(1 for p in dst_root.rglob("*") if p.is_file())
    logmod.info("migrate", f"完成：{stats['files']} 个文件，{stats['copied_bytes'] / 1e6:.1f} MB")
    return stats
