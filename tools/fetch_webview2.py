"""下载 WebView2 托管 SDK（NuGet 包）并解压到 vendor/webview2。

产物：
  vendor/webview2/lib/net45/Microsoft.Web.WebView2.Core.dll
  vendor/webview2/lib/net45/Microsoft.Web.WebView2.WinForms.dll
  vendor/webview2/runtimes/win-x64/native/WebView2Loader.dll
"""

from __future__ import annotations

import json
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "vendor" / "webview2"
INDEX = "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/index.json"


def latest_version() -> str:
    raw = urllib.request.urlopen(INDEX, timeout=30).read().decode("utf-8")
    versions = json.loads(raw)["versions"]
    stable = [v for v in versions if "-" not in v]
    return stable[-1]


def main() -> int:
    ver = latest_version()
    print("webview2 sdk version:", ver)
    OUT.mkdir(parents=True, exist_ok=True)
    nupkg = OUT / f"microsoft.web.webview2.{ver}.nupkg"
    if not nupkg.exists():
        url = f"https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/{ver}/microsoft.web.webview2.{ver}.nupkg"
        print("downloading", url)
        with urllib.request.urlopen(url, timeout=300) as r, nupkg.open("wb") as fh:
            fh.write(r.read())
    size = nupkg.stat().st_size / 1e6
    print(f"package {size:.1f} MB")

    with zipfile.ZipFile(nupkg) as zf:
        names = zf.namelist()
        wanted = [n for n in names if n.startswith(("lib/net462/", "runtimes/win-x64/native/"))]
        for n in wanted:
            zf.extract(n, OUT)
    for p in [
        OUT / "lib" / "net462" / "Microsoft.Web.WebView2.Core.dll",
        OUT / "lib" / "net462" / "Microsoft.Web.WebView2.WinForms.dll",
        OUT / "runtimes" / "win-x64" / "native" / "WebView2Loader.dll",
    ]:
        print(("OK  " if p.exists() else "MISS") , p.relative_to(ROOT))
    (OUT / "VERSION").write_text(ver, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
