"""scene-cut 启动器（打包入口）。

启动本地服务 → 打开"应用窗口"（优先 Chromium 应用模式，退回默认浏览器）。
出错时写 crash log 并停住，避免双击后一闪就没。

参数：
  --browser     用默认浏览器打开（不用应用窗口）
  --no-open     只起服务，不打开任何窗口
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from pathlib import Path


def _log_path() -> Path:
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return base / "scene-cut-crash.log"


def _crash(exc: BaseException) -> None:
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        _log_path().write_text(text, encoding="utf-8")
    except OSError:
        pass
    try:
        fallback = Path(os.getenv("TEMP", ".")) / "scene-cut-crash.log"
        fallback.write_text(text, encoding="utf-8")
        print(f"错误已写入：{fallback}", flush=True)
    except OSError:
        pass
    print("\n启动失败：\n" + text, flush=True)
    try:
        input("按回车键退出…")
    except EOFError:
        pass


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _arg_port(args: list[str]) -> int | None:
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args):
            try:
                return int(args[i + 1])
            except ValueError:
                return None
        if a.startswith("--port="):
            try:
                return int(a.split("=", 1)[1])
            except ValueError:
                return None
    return None


def _prepare_bin() -> None:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    bin_dir = base / "_bin"
    if bin_dir.is_dir():
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def _find_chromium() -> str | None:
    """找 Edge / Chrome 可执行文件，用于 --app 应用窗口模式。"""
    roots = [
        os.getenv("ProgramFiles(x86)"), os.getenv("ProgramFiles"), os.getenv("LOCALAPPDATA"),
    ]
    rel = [
        r"Microsoft\Edge\Application\msedge.exe",
        r"Google\Chrome\Application\chrome.exe",
    ]
    for env in roots:
        if not env:
            continue
        for r in rel:
            p = Path(env) / r
            if p.exists():
                return str(p)
    for name in ("msedge.exe", "chrome.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _wait_ready(port: int, timeout: float = 30.0) -> bool:
    """等服务真正能返回 200 再开窗口，避免浏览器拿到空白页（且不会自动重试）。"""
    import urllib.request

    url = f"http://127.0.0.1:{port}/"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    return False


def _prewarm_profile(exe: str, profile: Path) -> None:
    """用无头 Edge 先把 profile 初始化一次，避免新 profile 首次启动白屏。"""
    if (profile / "Local State").exists():
        return
    profile.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [exe, "--headless=new", f"--user-data-dir={profile}",
             "--no-first-run", "--no-default-browser-check",
             "--disable-gpu", "about:blank"],
            timeout=25, capture_output=True,
        )
    except Exception:  # noqa: BLE001
        pass


def _pick_profile(data_root: Path) -> Path:
    """选一个当前权限确实能写的 profile 目录。

    历史遗留：早期以 Administrator 运行时创建的 browser-profile，
    普通权限下写不进去，Edge 会白屏并误报“elevated instance”。
    """
    base = data_root / "browser-profile"
    candidates = [base]
    for i in range(1, 4):
        candidates.append(data_root / f"browser-profile-{i}")
    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return cand
        except OSError as exc:
            print(f"[窗口] 配置目录不可写（{cand}）：{exc}", flush=True)
            continue
    return base


def _clear_stale_locks(profile: Path) -> None:
    """清掉 Chromium 的进程单例锁，避免被已退出/提权的旧实例挡住。"""
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        target = profile / name
        try:
            if target.exists() or target.is_symlink():
                target.unlink()
                print(f"[窗口] 已清理残留锁：{name}", flush=True)
        except OSError:
            pass


def _is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _open_window(url: str, data_root: Path, elevated: bool = False) -> bool:
    exe = _find_chromium()
    if not exe:
        print("[窗口] 未找到 Edge/Chrome，无法打开应用窗口", flush=True)
        return False
    profile = _pick_profile(data_root)
    _clear_stale_locks(profile)
    print(f"[窗口] 浏览器: {exe}", flush=True)
    print(f"[窗口] 独立配置: {profile}", flush=True)
    print(f"[窗口] 当前权限: {'管理员' if elevated else '普通'}", flush=True)
    _prewarm_profile(exe, profile)

    args = [
        f"--app={url}",
        "--window-size=1560,980",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "--disable-features=Translate,msEdgeFirstRunExperience,msEdgeStartupBoost",
        "--disable-gpu",
        "--disable-software-rasterizer",
    ]

    if elevated:
        # 提权状态下用受限令牌启动，避免与普通权限 Edge 抢会话/沙箱失败
        args.append("--no-sandbox")
        cmd = "runas /trustlevel:0x20000 " + subprocess.list2cmdline([exe] + args)
        print(f"[窗口] 受限令牌启动: {cmd}", flush=True)
        try:
            subprocess.Popen(["cmd", "/c", cmd])
            return True
        except OSError as exc:
            print(f"[窗口] 受限令牌启动失败: {exc}", flush=True)

    try:
        print(f"[窗口] 启动命令: {subprocess.list2cmdline([exe] + args)}", flush=True)
        subprocess.Popen([exe] + args)
        return True
    except OSError as exc:
        print(f"[窗口] 启动失败: {exc}", flush=True)
        return False


def _window_titles() -> list[str]:
    """枚举顶层窗口标题（用于确认应用窗口是否真的把页面加载出来了）。"""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        titles: list[str] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                titles.append(buf.value)
            return True

        user32.EnumWindows(cb, 0)
        return titles
    except Exception:  # noqa: BLE001
        return []


def _wait_window_loaded(marker: str = "M0 预览", timeout: float = 10.0) -> bool:
    """等应用窗口把页面标题刷出来；白屏窗口标题会一直是 127.0.0.1_/。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(marker in t for t in _window_titles()):
            return True
        time.sleep(0.5)
    return False


def _find_native_host() -> Path | None:
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent / "dist" / "scene-cut"
    host = base / "scene-cut-window.exe"
    return host if host.exists() else None


def _open_native_window(url: str, data_root: Path) -> object | None:
    """用 WebView2 原生窗口宿主打开（进程内嵌 Chromium，不启动 msedge.exe）。"""
    host = _find_native_host()
    if not host:
        return None
    wv2 = data_root / "webview2"
    try:
        wv2.mkdir(parents=True, exist_ok=True)
    except OSError:
        wv2 = Path(tempfile.gettempdir()) / "scene-cut-webview2"
        wv2.mkdir(parents=True, exist_ok=True)
    print(f"[窗口] 原生窗口: {host}", flush=True)
    print(f"[窗口] WebView2 数据: {wv2}", flush=True)
    try:
        return subprocess.Popen([str(host), url, str(wv2)])
    except OSError as exc:
        print(f"[窗口] 原生窗口启动失败: {exc}", flush=True)
        return None


def _open_tab_window(url: str) -> bool:
    """用系统默认浏览器的普通窗口打开（不指定 profile、不用 --app）。

    在部分机器上 --app + 独立 profile 会白屏，而普通窗口走已有浏览器实例，最稳。
    """
    exe = _find_chromium()
    if not exe:
        return False
    try:
        print(f"[窗口] 尝试普通窗口: {exe} --new-window {url}", flush=True)
        subprocess.Popen([exe, "--new-window", url, "--window-size=1560,980"])
        return True
    except OSError as exc:
        print(f"[窗口] 普通窗口启动失败: {exc}", flush=True)
        return False


def _write_url_shortcut(data_root: Path, url: str) -> Path:
    """写一个 .url 快捷方式：双击即用默认浏览器打开（绕过“程序自己拉浏览器”的各种坑）。"""
    target = data_root / "打开网页.url"
    try:
        data_root.mkdir(parents=True, exist_ok=True)
        target.write_text(f"[InternetShortcut]\r\nURL={url}\r\n", encoding="utf-8")
    except OSError:
        pass
    return target


def _copy_clipboard(text: str) -> None:
    try:
        subprocess.run(["cmd", "/c", f'echo {text}| clip'], check=False,
                       capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _open_external(url: str) -> None:
    """用普通权限打开浏览器：管理员权限下经 explorer 转发，避免高权限浏览器实例冲突。"""
    try:
        if _is_admin():
            subprocess.Popen(["explorer.exe", url])
        else:
            webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass


def _relaunch_unelevated() -> bool:
    """以管理员权限运行时，尝试用 explorer 以普通权限重新拉起自己。

    注意：内置 Administrator 账户关闭了 UAC，explorer 也是提权的，重启无意义；
    这种情况下改用 --no-sandbox 让 Chromium 能在提权下渲染。
    """
    if not _is_admin():
        return False
    if os.environ.get("USERNAME", "").strip().lower() == "administrator":
        print("提示：当前是内置 Administrator 账户（UAC 不生效），不尝试降权，改用 --no-sandbox 运行。",
              flush=True)
        return False
    if os.getenv("SCENECUT_ALLOW_ADMIN") == "1":
        print("提示：允许以管理员权限运行（SCENECUT_ALLOW_ADMIN=1）。", flush=True)
        return False
    if os.getenv("SCENECUT_DELEVATED") == "1":
        return False
    try:
        env = dict(os.environ)
        env["SCENECUT_DELEVATED"] = "1"
        subprocess.Popen(["explorer.exe", os.path.abspath(sys.executable)],
                         env=env, close_fds=True)
    except OSError as exc:
        print(f"降权重启失败：{exc}", flush=True)
        return False
    print("检测到管理员权限，已用普通权限重新启动，本窗口即将关闭。", flush=True)
    return True


def _arg_value(args: list[str], name: str) -> str | None:
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    use_browser = "--browser" in args
    no_open = "--no-open" in args
    log_level = _arg_value(args, "--log-level")

    if _relaunch_unelevated():
        return 0

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    _prepare_bin()
    if not shutil.which("ffmpeg"):
        print("警告：PATH 中没有 ffmpeg，检测/导出会失败。请安装 ffmpeg 或放进 _internal\\_bin。", flush=True)
    if _is_admin():
        print("提示：当前以管理员权限运行。应用窗口使用独立浏览器配置，一般没问题；"
              "若浏览器报权限冲突，请改用普通权限运行。", flush=True)

    port = _arg_port(args) or _free_port()

    import app.api.server as server

    def serve() -> None:
        argv = ["--port", str(port)]
        if log_level:
            argv += ["--log-level", log_level]
        server.main(argv)

    threading.Thread(target=serve, daemon=True).start()
    ready = _wait_ready(port)
    time.sleep(0.4)

    url = f"http://127.0.0.1:{port}/"
    print("=" * 60, flush=True)
    print(f"  scene-cut 已启动： {url}", flush=True)
    print(f"  数据目录： {server.WS.root if server.WS else ''}", flush=True)
    print(f"  运行权限： {'管理员' if _is_admin() else '普通用户'}", flush=True)
    print("  关闭本窗口或按 Ctrl+C 退出", flush=True)
    print("=" * 60, flush=True)
    shortcut = _write_url_shortcut(server.DATA_ROOT, url)
    print(f"  打不开窗口时，双击这个文件即可： {shortcut}", flush=True)

    host_proc = None
    if not no_open and "--manual" in args:
        try:
            subprocess.run(["cmd", "/c", f'echo {url}| clip'], check=False)
            print(f"[提示] 地址已复制到剪贴板，请粘贴到浏览器打开：{url}", flush=True)
        except OSError:
            print(f"[提示] 请手动在浏览器打开：{url}", flush=True)
        no_open = True

    if not no_open:
        if not ready:
            print("警告：服务 30 秒内未就绪，窗口可能空白；请稍后在窗口里按 F5 刷新。", flush=True)
        if "--tab" in args:
            opened = _open_tab_window(url)
        elif "--browser" in args:
            opened = False
        else:
            host_proc = _open_native_window(url, server.DATA_ROOT)
            opened = bool(host_proc)
            if not opened:
                opened = _open_window(url, server.DATA_ROOT, _is_admin())
            if not opened:
                opened = _open_tab_window(url)
        if opened:
            ok = _wait_window_loaded()
            if not ok:
                print("应用窗口未加载出页面，已复制地址并改用默认浏览器打开。", flush=True)
                _copy_clipboard(url)
                _open_external(url)
        else:
            _open_external(url)

    try:
        while True:
            if host_proc is not None and host_proc.poll() is not None:
                print("窗口已关闭，退出。", flush=True)
                return 0
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        _crash(exc)
        raise SystemExit(1) from exc
