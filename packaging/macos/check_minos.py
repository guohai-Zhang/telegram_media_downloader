"""Fail if any Mach-O file inside an .app needs a newer macOS than the minimum.

Usage: python packaging/macos/check_minos.py dist/TelegramDownloader.app 11.0
"""

import os
import subprocess
import sys
from typing import Optional, Tuple

MACHO_MAGICS = {
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xfe\xed\xfa\xce",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
}


def version_tuple(text: str) -> Tuple[int, ...]:
    """'11.0' -> (11, 0)"""
    return tuple(int(part) for part in text.split("."))


def is_macho(path: str) -> bool:
    """True for thin or fat Mach-O binaries."""
    with open(path, "rb") as f:
        return f.read(4) in MACHO_MAGICS


def minos_of(path: str) -> Optional[str]:
    """Minimum macOS from LC_BUILD_VERSION (or the older LC_VERSION_MIN_MACOSX)."""
    lines = subprocess.run(
        ["otool", "-l", path], capture_output=True, text=True, check=False
    ).stdout.splitlines()
    for index, line in enumerate(lines):
        command = line.strip()
        key = {"cmd LC_BUILD_VERSION": "minos", "cmd LC_VERSION_MIN_MACOSX": "version"}
        if command in key:
            for following in lines[index + 1 : index + 8]:
                parts = following.split()
                if parts and parts[0] == key[command]:
                    return parts[1]
    return None


def main(app: str, minimum: str) -> int:
    """Print offending files and return 1 if any needs a newer macOS."""
    limit = version_tuple(minimum)
    too_new = []
    checked = 0
    for root, _, files in os.walk(app):
        for name in files:
            path = os.path.join(root, name)
            if os.path.islink(path) or not is_macho(path):
                continue
            checked += 1
            version = minos_of(path)
            if version and version_tuple(version) > limit:
                too_new.append((version, path))
    for version, path in too_new:
        print(f"needs macOS {version}: {path}", file=sys.stderr)
    print(f"checked {checked} Mach-O files against macOS {minimum}")
    return 1 if too_new else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
