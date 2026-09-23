#!/bin/bash
# deploy.sh <out_dir> — build the SPA into <out_dir>/dist and lay the /fixed
# files it yields out in <out_dir>/data/ (webroot/*.gz and build_times). The
# firmware build passes a dir inside its own build tree, so every target keeps
# its own bundle.
set -e
OUT="${1:?usage: deploy.sh <out_dir>}"
mkdir -p "$OUT/data/webroot"
OUT="$(cd "$OUT" && pwd)"
WEBROOT="$OUT/data/webroot"
cd "$(dirname "$0")"
HERE="$(pwd)"

# Auto-install npm deps if absent (e.g. after `idf.py reallyclean` or fresh
# checkout) OR stale: spangap-inside rewrites package.json's file: straddle
# deps to match the staged set (adding e.g. lcdmirror on an LCD+web build,
# dropping it on a headless one), so an install is also needed whenever
# package.json is newer than the last install's stamp — otherwise the SPA
# build fails to resolve a just-staged straddle's imports (or keeps bundling
# a dropped one). The file: deps are symlinks; this works offline once npm
# has run once. Steady-state builds rewrite nothing and skip the install.
if [ ! -d node_modules ] || [ package.json -nt node_modules/.package-lock.json ]; then
    echo "deploy.sh: node_modules missing or stale, running npm install"
    npm install
fi

# SPA build for device (PWA only works with trusted HTTPS)
SPANGAP_WEB_DIST="$OUT/dist" npx quasar build

# Clean old web assets from webroot/ (preserve nothing — all generated)
find "$WEBROOT" -type f -delete 2>/dev/null || true

# Gzip each built file into webroot/ (only app.js, style.css, index.html)
for f in app.js style.css index.html; do
  [ -f "$OUT/dist/$f" ] && gzip -9 -c "$OUT/dist/$f" > "$WEBROOT/${f}.gz"
done

# Binary build_times (see tools/write_build_times.py) for /fixed + sys.buildtime.fixed.
# Its content mtime spans this bundle and the tracked /fixed files in ../data.
python3 "$HERE/../tools/write_build_times.py" "$OUT/data" "$HERE/../data"

echo ""
echo "Deployed to $WEBROOT:"
ls -lhS "$WEBROOT"
echo ""
