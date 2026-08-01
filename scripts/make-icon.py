#!/usr/bin/python3
"""Build an ICNS container from OmniTerm.iconset PNG representations."""

import struct
import sys
from pathlib import Path

ICON_TYPES = {
    "icon_16x16.png": b"icp4",
    "icon_32x32.png": b"icp5",
    "icon_32x32@2x.png": b"icp6",
    "icon_128x128.png": b"ic07",
    "icon_256x256.png": b"ic08",
    "icon_512x512.png": b"ic09",
    "icon_512x512@2x.png": b"ic10",
}


def main():
    source, output = map(Path, sys.argv[1:3])
    chunks = []
    for filename, kind in ICON_TYPES.items():
        payload = (source / filename).read_bytes()
        chunks.append(kind + struct.pack(">I", len(payload) + 8) + payload)
    body = b"".join(chunks)
    output.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)


if __name__ == "__main__":
    main()
