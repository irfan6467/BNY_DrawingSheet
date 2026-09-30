# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Technical Drawing Sheet Automation.

Builds one self-contained TechnicalDrawingSheet.exe that can be copied to
another Windows machine and run with nothing installed alongside it.

    pyinstaller technical_drawing_sheet.spec               # the single exe
    pyinstaller technical_drawing_sheet.spec -- --console  # same, with a
                                                          #   console window
    pyinstaller technical_drawing_sheet.spec -- --onedir   # folder build

Output: dist/TechnicalDrawingSheet.exe  (or dist/TechnicalDrawingSheet/
for --onedir)

A onefile exe unpacks itself into a temp folder each time it starts, so
the first window takes several seconds to appear.  The folder build
starts immediately and is the better choice once it is in daily use.

The machine running the result also needs, separately:
  * Microsoft Excel  - the side panel is real Excel embedded in the
    window.  Without it the panel shows a message and PDF generation
    falls back to the in-process renderer.
  * ODA File Converter - only to open .dwg.  .dxf works without it.
Neither can be redistributed inside this bundle.
"""

import os
import sys

# SPECPATH is already the folder holding this spec, not the file.
SPEC_DIR = os.path.abspath(SPECPATH)

# `pyinstaller thisfile.spec -- --console` builds a console variant, which
# is what you want the first time you run it on another machine: a crash
# then prints a traceback instead of the window vanishing.
WANT_CONSOLE = "--console" in sys.argv
# One exe by default, so it can simply be copied and run.
ONEFILE = "--onedir" not in sys.argv

RESOURCES = os.path.join("app", "resources")


def _tree(relative_dir, *extensions):
    """Every file under a resource folder, as (source, destination) pairs."""
    found = []
    root = os.path.join(SPEC_DIR, relative_dir)
    for folder, _dirs, files in os.walk(root):
        for name in files:
            if extensions and not name.lower().endswith(extensions):
                continue
            source = os.path.join(folder, name)
            destination = os.path.relpath(os.path.dirname(source), SPEC_DIR)
            found.append((source, destination))
    return found


datas = [
    # The QSS the theme is built from.
    (os.path.join(SPEC_DIR, "app", "ui", "theme.qss.template"),
     os.path.join("app", "ui")),
]

# The sheet template, its calibration, and the side-panel workbook.  The
# workbook in particular was missing before, which would have left the
# panel dead on any machine but this one.
for name in ("bny_standard_a1.pdf",
             "bny_standard_a1.json",
             "bny_standard_a1_cropped.pdf",
             "bny_sidepanel.xlsx"):
    source = os.path.join(SPEC_DIR, RESOURCES, "templates", name)
    if os.path.isfile(source):
        datas.append((source, os.path.join(RESOURCES, "templates")))

datas += _tree(os.path.join(RESOURCES, "fonts"), ".ttf", ".otf")
datas += _tree(os.path.join(RESOURCES, "icons"), ".svg", ".png")

hiddenimports = [
    "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont",
    # ImageOps turns a phone photo the right way up from its EXIF, and
    # ImageChops measures the ink extent when the side panel is merged.
    # Both are reached through the PIL package rather than by name, which
    # PyInstaller's analysis does not always follow.
    "PIL.ImageOps", "PIL.ImageChops",
    # The crash log is a rotating file handler; logging.handlers is a
    # submodule and does not come in with `import logging`.
    "logging.handlers",
    "pymupdf", "fitz",
    "pypdf",
    "reportlab", "reportlab.pdfgen", "reportlab.lib",
    "reportlab.pdfbase", "reportlab.pdfbase.pdfmetrics",
    "openpyxl", "openpyxl.utils",
    "ezdxf", "ezdxf.addons.drawing",
    "ezdxf.addons.drawing.matplotlib",
    "matplotlib", "matplotlib.backends.backend_agg",
    "numpy",
    "PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
    # SVG toolbar icons need the image plugin.
    "PySide6.QtSvg",
]

if sys.platform == "win32":
    # The side panel drives Excel over COM.
    hiddenimports += [
        "win32com", "win32com.client", "win32com.client.dynamic",
        "pythoncom", "pywintypes",
        "win32api", "win32con", "win32gui", "win32process",
    ]

a = Analysis(
    ["main.py"],
    pathex=[SPEC_DIR],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "cv2",          # opencv is not imported anywhere; ~120 MB saved
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.Qt3DCore",
        "PySide6.QtMultimedia",
        "PySide6.QtQuick",
        "PySide6.QtQml",
        "test",
        "xmlrpc",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

_common = dict(
    name="TechnicalDrawingSheet",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX mangles some Qt and pywin32 binaries; the size saving is not
    # worth a bundle that will not start on someone else's machine.
    upx=False,
    console=WANT_CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

if ONEFILE:
    # Everything inside the executable itself.
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        runtime_tmpdir=None,
        **_common,
    )
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **_common)
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="TechnicalDrawingSheet",
    )
