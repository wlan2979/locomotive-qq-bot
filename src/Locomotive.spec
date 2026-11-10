# -*- mode: python ; coding: utf-8 -*-
# 在 src/ 目录下执行 PyInstaller；所有路径均为相对路径，迁移机器无需改动。
from PyInstaller.utils.hooks import copy_metadata

datas = [
    ('index.html', '.'),
    ('persona.txt', '.'),
    ('THIRD_PARTY_NOTICES.txt', '.'),
    ('desktop-check.js', '.'),
]
datas += copy_metadata('aiohttp')
datas += copy_metadata('pillow')

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['py7zr.compressor'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'torch', 'rapidocr_onnxruntime', 'tkinter'],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [('O', None, 'OPTION'), ('O', None, 'OPTION')],
    name='Locomotive',
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
    icon=['locomotive.ico'],
)
