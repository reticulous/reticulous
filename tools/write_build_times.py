#!/usr/bin/env python3
"""
Write data/build_times: packed little-endian struct
  uint32_t fixed_content_max_mtime — max mtime (seconds) of any file under data/ (except this file)
  uint32_t image_built_unix        — UTC seconds when this script ran (after web deploy)
  uint32_t webroot_crc32           — CRC32 of concatenated webroot file contents (sorted paths);
                                     stable across rebuilds if .gz bytes unchanged (unlike mtimes)
"""
import os
import struct
import sys
import time
import zlib


def webroot_crc32(webroot: str) -> int:
    """Deterministic CRC32 over data/webroot files in sorted path order."""
    if not os.path.isdir(webroot):
        return 0
    h = zlib.crc32(b"")
    paths = []
    for root, _dirs, files in os.walk(webroot):
        for name in files:
            paths.append(os.path.join(root, name))
    paths.sort()
    for p in paths:
        rel = os.path.relpath(p, webroot).replace("\\", "/")
        h = zlib.crc32(rel.encode("utf-8"), h)
        with open(p, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h = zlib.crc32(chunk, h)
    return h & 0xFFFFFFFF


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: write_build_times.py <data_dir>", file=sys.stderr)
        sys.exit(1)
    data_dir = os.path.abspath(sys.argv[1])
    max_m = 0
    for root, _dirs, files in os.walk(data_dir):
        for name in files:
            if name == "build_times":
                continue
            p = os.path.join(root, name)
            try:
                max_m = max(max_m, int(os.path.getmtime(p)))
            except OSError:
                pass
    built = int(time.time())
    web_crc = webroot_crc32(os.path.join(data_dir, "webroot"))
    out = os.path.join(data_dir, "build_times")
    with open(out, "wb") as f:
        f.write(
            struct.pack(
                "<III",
                max_m & 0xFFFFFFFF,
                built & 0xFFFFFFFF,
                web_crc & 0xFFFFFFFF,
            )
        )
    print(
        "wrote {} max_mtime={} built={} webroot_crc32=0x{:08x}".format(
            out, max_m, built, web_crc
        )
    )


if __name__ == "__main__":
    main()
