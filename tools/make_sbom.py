"""生成 CycloneDX 1.5 SBOM（JSON）。

用法：.venv\\Scripts\\python tools/make_sbom.py [输出路径]
从当前环境的已安装包读取名称/版本/许可，输出可被 SBOM 工具消费的清单。
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from importlib.metadata import distributions
from pathlib import Path


def license_of(meta) -> str:
    lic = (meta.get("License") or "").strip()
    if lic and len(lic) < 80 and "\n" not in lic:
        return lic
    for cls in meta.get_all("Classifier") or []:
        if cls.startswith("License ::"):
            return cls.split("::")[-1].strip()
    return "NOASSERTION"


def main(argv: list[str]) -> int:
    out = Path(argv[0]) if argv else Path("SBOM.json")
    comps = []
    for dist in distributions():
        meta = dist.metadata
        name = meta.get("Name")
        if not name:
            continue
        comps.append({
            "type": "library",
            "name": name,
            "version": meta.get("Version", ""),
            "purl": f"pkg:pypi/{name.lower().replace('_', '-')}@{meta.get('Version', '')}",
            "licenses": [{"license": {"name": license_of(meta)}}],
        })
    comps.sort(key=lambda c: c["name"].lower())
    doc = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tools": [{"vendor": "scene-cut", "name": "make_sbom", "version": "1"}],
            "component": {"type": "application", "name": "scene-cut", "version": "0.9.0"},
        },
        "components": comps,
    }
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写出 {out}（{len(comps)} 个组件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
