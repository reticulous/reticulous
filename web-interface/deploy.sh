#!/bin/bash
set -e
cd "$(dirname "$0")"
mkdir -p ../data/webroot
WEBROOT="$(cd ../data/webroot && pwd)"

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
npx quasar build

# Clean old web assets from webroot/ (preserve nothing — all generated)
find "$WEBROOT" -type f -delete 2>/dev/null || true

# Gzip each built file into data/webroot/ (only app.js, style.css, index.html)
cd dist/spa
for f in app.js style.css index.html; do
  [ -f "$f" ] && gzip -9 -c "$f" > "$WEBROOT/${f}.gz"
done

# Binary build_times (see tools/write_build_times.py) for /fixed + sys.buildtime.fixed
python3 "$(cd "$(dirname "$0")/../tools" && pwd)/write_build_times.py" "$(cd "$(dirname "$0")/../data" && pwd)"

echo ""
echo "Deployed to data/webroot/:"
ls -lhS "$WEBROOT"
echo ""
