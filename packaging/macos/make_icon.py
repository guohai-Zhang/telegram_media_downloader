"""Draw the app icon (blue rounded square, white download arrow) into an .icns file.

Usage: python packaging/macos/make_icon.py packaging/macos/icon.icns
Needs pyobjc (installed with pywebview) and macOS iconutil.
"""

import os
import shutil
import subprocess
import sys
import tempfile

import AppKit  # pylint: disable = import-error
from Foundation import NSMakePoint, NSMakeRect  # pylint: disable = import-error

ARROW = [
    (432, 740),
    (592, 740),
    (592, 470),
    (712, 470),
    (512, 250),
    (312, 470),
    (432, 470),
]


def draw_png(size: int, path: str) -> None:
    """Render the icon at size x size pixels (design grid is 1024)."""
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(  # noqa: E501 pylint: disable = line-too-long
        None, size, size, 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0
    )
    context = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(context)
    s = size / 1024.0

    AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
        0.16, 0.63, 0.89, 1.0
    ).setFill()
    AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(100 * s, 100 * s, 824 * s, 824 * s), 185 * s, 185 * s
    ).fill()

    AppKit.NSColor.whiteColor().setFill()
    arrow = AppKit.NSBezierPath.bezierPath()
    arrow.moveToPoint_(NSMakePoint(ARROW[0][0] * s, ARROW[0][1] * s))
    for x, y in ARROW[1:]:
        arrow.lineToPoint_(NSMakePoint(x * s, y * s))
    arrow.closePath()
    arrow.fill()
    AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(292 * s, 170 * s, 440 * s, 44 * s), 22 * s, 22 * s
    ).fill()

    AppKit.NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    data.writeToFile_atomically_(path, True)


def main(out_path: str) -> None:
    """Write every size macOS expects into an iconset, then pack it."""
    tmp = tempfile.mkdtemp()
    iconset = os.path.join(tmp, "icon.iconset")
    os.makedirs(iconset)
    for base in (16, 32, 128, 256, 512):
        draw_png(base, os.path.join(iconset, f"icon_{base}x{base}.png"))
        draw_png(base * 2, os.path.join(iconset, f"icon_{base}x{base}@2x.png"))
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", out_path], check=True)
    shutil.rmtree(tmp)


if __name__ == "__main__":
    main(sys.argv[1])
