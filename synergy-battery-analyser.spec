# -*- mode: python ; coding: utf-8 -*-
# Standalone binary: runs the same launcher as the `synergy-analyser` command.
#
# Build from an environment where the package is installed normally (not
# editable) so PyInstaller can find `synergy_analyser`:
#     uv sync --group dev --no-editable
#     uv run --no-sync pyinstaller synergy-battery-analyser.spec
#
# The Synergy login window is the operating system's own web view (pywebview:
# WebView2 on Windows, WebKit on macOS, WebKitGTK on Linux), run by this same
# binary as a helper process (`--webview-host`). No browser is bundled; on
# Linux, WebKitGTK comes from the system.

import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# App icon: .ico for Windows, .icns for macOS (regenerate with packaging/icon/build.sh).
ICON = 'packaging/icon/icon.icns' if sys.platform == 'darwin' else 'packaging/icon/icon.ico'

a = Analysis(
    ['packaging/launcher.py'],
    pathex=[],
    binaries=[],
    datas=[('src/static', 'static')] + collect_data_files('synergy_analyser'),
    hiddenimports=['synergy_analyser.main', 'synergy_analyser.webview_host', 'uvicorn.logging', 'uvicorn.loops.auto',
                   'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets.auto',
                   'uvicorn.lifespan.on']
                  + (collect_submodules('gi.overrides') if sys.platform.startswith('linux') else []),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
if sys.platform.startswith('linux'):
    # Use the system's GTK/WebKitGTK rather than bundled copies: WebKit runs
    # helper programs from the system install, which must match its library,
    # and the login window needs WebKitGTK installed anyway. Keep only what
    # Python itself needs. (webview_host clears the environment variables
    # PyInstaller's GTK runtime hooks set to point at bundled copies.)
    import os
    import re
    SYSTEM_STACK = re.compile(
        r'^lib(gtk|gdk|glib|gobject|gio|gmodule|girepository|pango|cairo|harfbuzz|atk|atspi|'
        r'epoxy|fontconfig|freetype|fribidi|pixman|png|jpeg|X|xcb|xkbcommon|wayland|thai|'
        r'datrie|graphite|soup|webkit|javascriptcore|cloudproviders|tinysparql|json-glib|'
        r'dbus|systemd|glycin|openraw|wmf|duktape|proxy|pxbackend|psl|nghttp|ngtcp|curl|'
        r'ssh2|gnutls|nettle|hogweed|p11-kit|tasn1|idn2|unistring|gmp|krb5|k5crypto|gssapi|'
        r'com_err|keyutils|leancrypto|icu|xml2|blkid|mount|seccomp|brotli|expat|objc|pcre2|'
        r'dconf|giognome|giognutls|giolibproxy)')
    a.binaries = [b for b in a.binaries
                  if not (SYSTEM_STACK.match(os.path.basename(b[0])) or '/gdk-pixbuf' in b[0]
                          or b[0].startswith(('lib/gio', 'lib/gdk-pixbuf')))]
    a.datas = [d for d in a.datas
               if not d[0].startswith(('gi_typelibs', 'share/', 'lib/gio', 'lib/gdk-pixbuf', 'etc/'))]

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
