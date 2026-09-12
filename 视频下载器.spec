# -*- mode: python ; coding: utf-8 -*-

import sys
import os

# PyInstaller 打包时需要的隐藏导入
hidden_imports = [
    'PySide6.QtMultimedia',
    'PySide6.QtMultimediaWidgets',
    'PySide6.QtWebEngineCore',
    'PySide6.QtWebEngineWidgets',
    'PySide6.QtNetwork',
    'PySide6.QtSvg',
    'PySide6.QtXml',
    'urllib',
    'urllib.request',
    'urllib.parse',
    'http',
    'http.client',
    'ssl',
    'threading',
    'json',
    'config',
    'agnes_video',
    'gen_area',
    'player_widgets',
    'link_module',
    'video_analyzer',
    'ocr_engine',
    'downloader',
    'm3u8_downloader',
    'mitm_proxy',
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'setuptools'],
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
    name='视频下载器',
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
