# -*- mode: python ; coding: utf-8 -*-
# Build:  py -m PyInstaller --noconfirm Clickwave.spec

import os, sys
sys.path.insert(0, os.path.join(SPECPATH, 'tools'))
import make_version_info
make_version_info.write()   # version_info.txt always matches clickwave/common.py APP_VERSION


a = Analysis(
    ['clickwave.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('sounds', 'sounds'),
        ('assets/clickwave.ico', 'assets'),
        ('assets/logo_28.png', 'assets'),
        ('assets/logo_64.png', 'assets'),
    ],
    hiddenimports=[],
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
    name='Clickwave',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,   # UPX-packed exes trigger far more antivirus false positives
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/clickwave.ico'],
    version='version_info.txt',
)
