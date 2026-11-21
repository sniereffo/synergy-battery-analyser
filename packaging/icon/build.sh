#!/usr/bin/env bash
# Regenerates every icon from packaging/icon/icon.svg.
# Needs rsvg-convert (librsvg) and ImageMagick (magick). Run from the repo root.
set -euo pipefail
cd "$(dirname "$0")"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

# Full-bleed variant (tile fills the square) for small sizes: tab, Windows.
sed 's/viewBox="0 0 1024 1024"/viewBox="100 100 824 824"/' icon.svg > "$TMP/tight.svg"
cp "$TMP/tight.svg" ../../src/static/favicon.svg

png() { rsvg-convert -w "$2" -h "$2" "$1" -o "$3"; }

# Browser: favicon.ico (16/32/48) + apple-touch-icon (Safari, iOS).
for s in 16 32 48; do png "$TMP/tight.svg" $s "$TMP/f$s.png"; done
magick "$TMP/f16.png" "$TMP/f32.png" "$TMP/f48.png" ../../src/static/favicon.ico
png "$TMP/tight.svg" 180 ../../src/static/apple-touch-icon.png

# Windows .exe icon.
for s in 16 24 32 48 64 128 256; do png "$TMP/tight.svg" $s "$TMP/w$s.png"; done
magick "$TMP"/w{16,24,32,48,64,128,256}.png icon.ico

# macOS .icns (keeps the standard margin around the tile).
for s in 16 32 64 128 256 512 1024; do png icon.svg $s "$TMP/m$s.png"; done
python3 - "$TMP" icon.icns <<'PY'
import struct, sys
from pathlib import Path
tmp, out = Path(sys.argv[1]), sys.argv[2]
# ICNS element types holding PNG data, by pixel size.
types = [("icp4", 16), ("icp5", 32), ("icp6", 64), ("ic07", 128), ("ic08", 256),
         ("ic09", 512), ("ic10", 1024), ("ic11", 32), ("ic12", 64), ("ic13", 256), ("ic14", 512)]
body = b"".join(t.encode() + struct.pack(">I", 8 + len(d)) + d
                for t, s in types for d in [(tmp / f"m{s}.png").read_bytes()])
Path(out).write_bytes(b"icns" + struct.pack(">I", 8 + len(body)) + body)
PY
echo "icons written"
