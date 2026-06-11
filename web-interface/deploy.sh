#!/bin/bash
set -e
cd "$(dirname "$0")"
mkdir -p ../data/webroot
WEBROOT="$(cd ../data/webroot && pwd)"

# Auto-install npm deps if absent (e.g. after `idf.py reallyclean` or fresh
# checkout). The spangap-browser dep is `file:` so npm symlinks it into
# node_modules; this works offline once npm has run once.
if [ ! -d node_modules ]; then
    echo "deploy.sh: node_modules missing, running npm install"
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
