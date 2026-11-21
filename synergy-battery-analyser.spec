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

import sys

from PyInstaller.utils.hooks import collect_data_files

# App icon: .ico for Windows, .icns for macOS (regenerate with packaging/icon/build.sh).
ICON = 'packaging/icon/icon.icns' if sys.platform == 'darwin' else 'packaging/icon/icon.ico'

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

NAME = 'synergy-battery-analyser'
common = dict(
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX can break the bundled Node binary
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,  # PyInstaller ad-hoc signs, which Apple Silicon requires
    entitlements_file=None,
    icon=ICON,
)

if sys.platform == 'darwin':
    # macOS only shows an icon on a .app, so the Mac build is
    # "Synergy Battery Analyser.app" (a folder build; one-file .apps are
    # deprecated). No console window: the page's Quit button stops it, and
    # LSUIElement keeps it out of the Dock, where a non-Cocoa app would
    # otherwise sit looking unresponsive.
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, console=False,
              disable_windowed_traceback=False, **common)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)
    app = BUNDLE(
        coll,
        name='Synergy Battery Analyser.app',
        icon=ICON,
        bundle_identifier='io.github.sniereffo.synergy-battery-analyser',
        info_plist={
            'CFBundleDisplayName': 'Synergy Battery Analyser',
            'LSUIElement': True,
            'NSHighResolutionCapable': True,
        },
    )
else:
    # Windows / Linux: one file, with a console window that shows the address
    # and how to stop the app.
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
              upx_exclude=[], runtime_tmpdir=None, console=True,
              disable_windowed_traceback=False, **common)
