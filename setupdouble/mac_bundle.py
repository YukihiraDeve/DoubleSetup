"""Crée l'application « SetupDouble.app » qui ouvre la fenêtre de statut.

    python -m setupdouble.mac_bundle [--dest ~/Applications] [--command app]

L'app contient un lien vers le vrai Python et un petit script de lancement,
pour que le Dock et Spotlight l'affichent sous le nom « SetupDouble » avec
sa propre icône.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

from . import __version__
from .config import ROOT

BUNDLE_ID = "com.setupdouble.app"


def draw_icon(path: Path, size: int) -> None:
    """Icône façon macOS : carré arrondi dégradé, deux écrans reliés."""
    import AppKit

    s = float(size)
    image = AppKit.NSImage.alloc().initWithSize_((s, s))
    image.lockFocus()
    inset = s * 0.1
    body = AppKit.NSMakeRect(inset, inset, s - 2 * inset, s - 2 * inset)
    radius = body.size.width * 0.225
    shape = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(body, radius, radius)

    shadow = AppKit.NSShadow.alloc().init()
    shadow.setShadowOffset_((0, -s * 0.012))
    shadow.setShadowBlurRadius_(s * 0.03)
    shadow.setShadowColor_(AppKit.NSColor.colorWithCalibratedWhite_alpha_(0, 0.35))
    AppKit.NSGraphicsContext.saveGraphicsState()
    shadow.set()
    AppKit.NSColor.blackColor().set()
    shape.fill()
    AppKit.NSGraphicsContext.restoreGraphicsState()

    def rgb(r, g, b, a=1.0):
        return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(r / 255, g / 255, b / 255, a)

    gradient = AppKit.NSGradient.alloc().initWithColors_([rgb(43, 26, 92), rgb(104, 72, 230),
                                                          rgb(52, 190, 214)])
    gradient.drawInBezierPath_angle_(shape, 60)

    white = rgb(255, 255, 255)
    line = s * 0.032
    unit = body.size.width

    def screen(x, y, w, h):
        r = AppKit.NSMakeRect(body.origin.x + x * unit, body.origin.y + y * unit, w * unit, h * unit)
        p = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, unit * 0.04, unit * 0.04)
        p.setLineWidth_(line)
        rgb(255, 255, 255, 0.14).set()
        p.fill()
        white.set()
        p.stroke()

    screen(0.16, 0.42, 0.30, 0.36)    # tour du PC
    screen(0.54, 0.46, 0.32, 0.22)    # écran du Mac
    base = AppKit.NSBezierPath.bezierPath()
    base.setLineWidth_(line)
    base.setLineCapStyle_(AppKit.NSLineCapStyleRound)
    base.moveToPoint_((body.origin.x + 0.50 * unit, body.origin.y + 0.42 * unit))
    base.lineToPoint_((body.origin.x + 0.90 * unit, body.origin.y + 0.42 * unit))
    white.set()
    base.stroke()

    # le lien entre les deux machines
    link = AppKit.NSBezierPath.bezierPath()
    link.setLineWidth_(line)
    link.setLineCapStyle_(AppKit.NSLineCapStyleRound)
    link.moveToPoint_((body.origin.x + 0.31 * unit, body.origin.y + 0.30 * unit))
    link.curveToPoint_controlPoint1_controlPoint2_(
        (body.origin.x + 0.70 * unit, body.origin.y + 0.30 * unit),
        (body.origin.x + 0.44 * unit, body.origin.y + 0.30 * unit),
        (body.origin.x + 0.57 * unit, body.origin.y + 0.30 * unit))
    rgb(255, 255, 255, 0.85).set()
    link.stroke()
    for cx in (0.31, 0.70):
        dot = AppKit.NSBezierPath.bezierPathWithOvalInRect_(AppKit.NSMakeRect(
            body.origin.x + cx * unit - line * 1.2, body.origin.y + 0.30 * unit - line * 1.2,
            line * 2.4, line * 2.4))
        white.set()
        dot.fill()
    image.unlockFocus()

    rep = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
    png = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    png.writeToFile_atomically_(str(path), True)


def make_icns(dest: Path) -> bool:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / "SetupDouble.iconset"
            iconset.mkdir()
            for base in (16, 32, 128, 256, 512):
                draw_icon(iconset / f"icon_{base}x{base}.png", base)
                draw_icon(iconset / f"icon_{base}x{base}@2x.png", base * 2)
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(dest)],
                           check=True, capture_output=True)
        return True
    except Exception as exc:  # l'icône est un plus, pas une nécessité
        print(f"Icône non générée : {exc}")
        return False


def build(dest_dir: Path, module: str = "setupdouble.app") -> Path:
    app = dest_dir / "SetupDouble.app"
    if app.exists():
        shutil.rmtree(app)
    macos = app / "Contents" / "MacOS"
    resources = app / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    python = os.path.realpath(sys.executable)
    site = sysconfig.get_paths()["purelib"]
    (macos / "python").symlink_to(python)
    launcher = macos / "SetupDouble"
    launcher.write_text(
        "#!/bin/bash\n"
        "# Généré par setupdouble.mac_bundle : ouvre la fenêtre de statut.\n"
        f'export PYTHONPATH="{ROOT}:{site}"\n'
        f'export SETUPDOUBLE_CONFIG="${{SETUPDOUBLE_CONFIG:-{ROOT}/config.toml}}"\n'
        f'cd "{ROOT}"\n'
        f'exec "$(dirname "$0")/python" -m {module}\n',
        encoding="utf-8")
    launcher.chmod(0o755)

    has_icon = make_icns(resources / "SetupDouble.icns")
    info = {
        "CFBundleName": "SetupDouble",
        "CFBundleDisplayName": "SetupDouble",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": "SetupDouble",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.utilities",
    }
    if has_icon:
        info["CFBundleIconFile"] = "SetupDouble"
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(info, f)
    # signature ad hoc : évite que macOS refuse de lancer une app modifiée
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], capture_output=True)
    subprocess.run(["/System/Library/Frameworks/CoreServices.framework/Frameworks/"
                    "LaunchServices.framework/Support/lsregister", "-f", str(app)],
                   capture_output=True)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crée SetupDouble.app")
    parser.add_argument("--dest", default=str(Path.home() / "Applications"))
    parser.add_argument("--module", default="setupdouble.app",
                        help="module lancé par l'app (setupdouble.demo pour la démo)")
    args = parser.parse_args(argv)
    dest = Path(args.dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    app = build(dest, args.module)
    print(f"Application créée : {app}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
