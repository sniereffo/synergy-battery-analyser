# -*- mode: python ; coding: utf-8 -*-
# Standalone binary: runs the same launcher as the `synergy-analyser` command.
#
# Build from an environment where the package is installed normally (not
# editable) so PyInstaller can find `synergy_analyser`:
#     uv sync --group dev --no-editable
#     uv run --no-sync pyinstaller synergy-battery-analyser.spec
#
# Playwright's own PyInstaller hook bundles its driver (Node + the Playwright
# CLI), which the Synergy login needs. No browser is bundled: the login window
# uses the installed Chrome or Edge, or downloads Chromium once on first login.

from PyInstaller.utils.hooks import collect_data_files

a = Analysis(
    ['packaging/launcher.py'],
    pathex=[],
    binaries=[],
    datas=[('src/static', 'static')] + collect_data_files('synergy_analyser'),
    hiddenimports=['synergy_analyser.main', 'uvicorn.logging', 'uvicorn.loops.auto',
                   'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets.auto',
                   'uvicorn.lifespan.on'],
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
    name='synergy-battery-analyser',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX can break the bundled Node binary
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # the console window shows the URL and how to stop the app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
