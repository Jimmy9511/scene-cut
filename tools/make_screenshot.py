"""生成 README 截图：启动后端 → 导入合成素材并检测 → 用 Edge 无头截图。

用法：python tools/make_screenshot.py [--port 8177] [--out docs/screenshot.png]
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "scene-cut" / "scene-cut.exe"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "synthetic"
DATA = ROOT / "appdata" / "shot"


def post(port: int, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def get(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=30) as r:
        return json.load(r)


def wait_ready(port: int, timeout: float = 40) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    return False


def main(argv: list[str]) -> int:
    port = 8177
    out = ROOT / "docs" / "screenshot.png"
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
        if a == "--out" and i + 1 < len(argv):
            out = Path(argv[i + 1])

    fixture = FIXTURE_DIR / "hardcuts.mp4"
    if not fixture.exists():
        print("缺少测试素材，先运行 python tools/make_fixtures.py")
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    env = {"SCENECUT_DATA": str(DATA)}
    import os
    env_full = dict(os.environ)
    env_full.update(env)

    proc = subprocess.Popen(
        [str(DIST), "--no-open", "--port", str(port)],
        cwd=str(DIST.parent), env=env_full,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_ready(port):
            print("后端未就绪")
            return 1
        print("import:", post(port, "/api/pool/import", {"path": str(fixture.parent)}))
        job = post(port, "/api/projects/analyze", {"source": str(fixture), "ai": False,
                                                   "preset": "实拍短片"})
        jid = job["job"]["id"]
        for _ in range(120):
            state = get(port, f"/api/job?id={jid}")
            if state.get("status") in ("done", "error", "failed"):
                print("analyze:", state.get("status"))
                break
            time.sleep(1)
        time.sleep(1.5)

        url = f"http://127.0.0.1:{port}/"
        cmd = [
            EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--window-size=1680,1050",
            "--virtual-time-budget=9000",
            f"--screenshot={out}",
            url,
        ]
        print("screenshot:", " ".join(cmd))
        subprocess.run(cmd, timeout=120, capture_output=True)
        print("saved", out, out.stat().st_size if out.exists() else "MISSING")
        return 0 if out.exists() else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
