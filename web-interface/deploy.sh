#!/bin/bash
set -e
cd "$(dirname "$0")"
WEBROOT="$(cd ../data/webroot && pwd)"

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
