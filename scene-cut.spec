# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir）。

产物：dist/scene-cut/scene-cut.exe
  - 双击启动本地服务并自动打开浏览器
  - 数据（工作区/素材池）写 %LOCALAPPDATA%\\SceneCut
  - 若把 ffmpeg.exe/ffprobe.exe 放进 dist/scene-cut/_internal/_bin/，会自动优先使用
"""

from PyInstaller.utils.hooks import collect_all

datas = [("web", "web")]
binaries = []
hiddenimports = []

for pkg in ("transnetv2_pytorch", "scenedetect"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "matplotlib", "pytest", "IPython", "notebook", "PySide2", "PyQt5",
        "tensorflow", "keras", "tensorboard", "torchvision", "torchaudio",
        "sklearn", "scipy", "h5py", "grpc", "onnxruntime",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="scene-cut",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="scene-cut",
)
