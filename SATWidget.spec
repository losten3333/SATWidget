# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('resources', 'resources'), ('de421.bsp', '.'), ('config.json', '.'), ('config.example.json', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# The Codex runtime exposes Poppler's versioned ICU libraries on the build
# search path.  They use versioned exports incompatible with Qt6Core, while
# PySide6 normally resolves the matching Windows ICU runtime from System32.
binaries = [
    entry for entry in a.binaries
    if entry[0].lower() not in {"icuuc.dll", "icudt78.dll"}
]

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SATWidget',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon='resources/logo.ico',
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SATWidget',
)
