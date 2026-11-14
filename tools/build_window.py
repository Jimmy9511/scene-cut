"""编译 WebView2 原生窗口宿主（C# + .NET Framework 4.8 csc，无需 .NET SDK）。

产物：
  dist/scene-cut/scene-cut-window.exe
  dist/scene-cut/Microsoft.Web.WebView2.Core.dll
  dist/scene-cut/Microsoft.Web.WebView2.WinForms.dll
  dist/scene-cut/WebView2Loader.dll
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WV2 = ROOT / "vendor" / "webview2"
CSC = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
OUT_DIR = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "dist" / "scene-cut"

CORE = WV2 / "lib" / "net462" / "Microsoft.Web.WebView2.Core.dll"
WINFORMS = WV2 / "lib" / "net462" / "Microsoft.Web.WebView2.WinForms.dll"
LOADER = WV2 / "runtimes" / "win-x64" / "native" / "WebView2Loader.dll"


def main() -> int:
    for p in (CSC, CORE, WINFORMS, LOADER):
        if not p.exists():
            print("缺少依赖:", p)
            return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    exe = OUT_DIR / "scene-cut-window.exe"
    cmd = [
        str(CSC), "/nologo", "/target:winexe", "/platform:x64", "/optimize+",
        f"/out:{exe}",
        f"/r:{CORE}",
        f"/r:{WINFORMS}",
        "/r:System.dll", "/r:System.Drawing.dll", "/r:System.Windows.Forms.dll",
        str(ROOT / "host" / "WindowHost.cs"),
    ]
    print(" ".join(f'"{c}"' if " " in c else c for c in cmd))
    proc = None
    for attempt in range(1, 4):
        # csc 偶尔会在"目录刚被 PyInstaller 重建"时拿不到写权限（杀软/索引占用），重试即可
        try:
            if exe.exists():
                exe.unlink()
        except OSError:
            pass
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        print(proc.stdout.strip())
        if proc.stderr.strip():
            print(proc.stderr.strip())
        if proc.returncode == 0 and exe.exists():
            break
        print(f"编译失败（第 {attempt} 次），1.5s 后重试…")
        time.sleep(1.5)
    if proc is None or proc.returncode != 0 or not exe.exists():
        print("编译失败：csc 连续 3 次未能写出 exe")
        print("提示：若在受限沙箱/杀软环境下遇到 CS0016，可先用普通终端手动执行：")
        print(f'  "{CSC}" /nologo /target:winexe /platform:x64 /optimize+ '
              f'/out:"{OUT_DIR / "scene-cut-window.exe"}" "{CORE}" "{WINFORMS}" '
              f'/r:System.dll /r:System.Drawing.dll /r:System.Windows.Forms.dll '
              f'"{ROOT / "host" / "WindowHost.cs"}"')
        print(f'然后把 {CORE.name} / {WINFORMS.name} / {LOADER.name} 复制到 {OUT_DIR}')
        return 1

    for src, name in ((CORE, CORE.name), (WINFORMS, WINFORMS.name), (LOADER, LOADER.name)):
        shutil.copy2(src, OUT_DIR / name)
    print(f"OK -> {exe}  ({exe.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
