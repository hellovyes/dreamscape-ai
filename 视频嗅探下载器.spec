# -*- mode: python ; coding: utf-8 -*-
import os

# 以 spec 文件所在目录为基准，避免从其他目录执行打包时定位到错误的 ocr_engine_runtime
try:
    base = SPECPATH  # PyInstaller 在编译 spec 时保证提供的变量
except Exception:
    base = os.path.dirname(os.path.abspath(__file__))

datas = []
binaries = []
hiddenimports = ["cryptography", "cryptography.x509",
                 "cryptography.hazmat.primitives",
                 "cryptography.hazmat.primitives.asymmetric.rsa",
                 "cryptography.hazmat.primitives.serialization"]


def add_tree(src, out):
    for item in os.listdir(src):
        ab = os.path.join(src, item)
        ob = os.path.join(out, item)
        if os.path.isdir(ab):
            add_tree(ab, ob)
        else:
            datas.append((ab, out))


# ---- SubtitleOCR 运行时（subocr.dll / onnxruntime / DirectML / 模型）----
run = os.path.join(base, "ocr_engine_runtime")


def add_runtime(src, out):
    for item in os.listdir(src):
        ab = os.path.join(src, item)
        ob = os.path.join(out, item)
        if os.path.isdir(ab):
            # 直接放进 ocr_engine_runtime 子目录，保持相对结构
            add_tree(ab, os.path.join("ocr_engine_runtime", item))
        else:
            datas.append((ab, "ocr_engine_runtime"))


if os.path.isdir(run):
    add_runtime(run, "ocr_engine_runtime")

# ---- 转码工具（HEVC→H.264，供 subocr 解码）----
for tname in ("ffmpeg.exe", "ffprobe.exe"):
    tp = os.path.join(base, tname)
    if os.path.isfile(tp):
        datas.append((tp, "."))

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='视频嗅探下载器',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)