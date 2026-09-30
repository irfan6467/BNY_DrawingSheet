"""
Asset importer for images and PDF pages.

Handles:
  - Direct image import (.jpg, .jpeg, .png, .bmp, .tiff) → source_type="image"
  - PDF page import via PyMuPDF rasterization → source_type="pdf_page"

CAD files (.dwg, .dxf) are handled by cad_import.py, not this module.

DESIGN RULE (§9): No Qt imports in core/ modules.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageOps

from app.core.project_state import ImportedAsset

# Pillow refuses an image beyond ~89 megapixels by default, as a guard
# against decompression bombs.  An A0 scan at 600 DPI is legitimately
# larger than that, so the ceiling is raised to one we choose - and kept,
# so a genuinely hostile file still gets refused rather than eating all
# the memory on the machine.
Image.MAX_IMAGE_PIXELS = 250_000_000

# Conditional import for PyMuPDF
try:
    import pymupdf as fitz
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False


# Supported file extensions
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"}
PDF_EXTENSIONS = {".pdf"}
ALL_SUPPORTED = IMAGE_EXTENSIONS | PDF_EXTENSIONS

LogCallback = Callable[[str, str], None]


def _noop_log(severity: str, message: str) -> None:
    pass


def pixmap_to_pil(pix) -> Image.Image:
    """A PyMuPDF pixmap as a PIL image, respecting stride and alpha.

    ``Image.frombytes("RGB", (w, h), pix.samples)`` - what every caller
    here used to do - is wrong twice over.  A pixmap rendered with an
    alpha channel carries four components per pixel, not three, so the
    buffer is read a third short and the picture shears; and MuPDF is
    free to pad each row out to ``pix.stride``, which is not always
    ``width * n``.  Either way the result is the diagonally smeared,
    washed-out image this app was producing for PDF imports.
    """
    mode = "RGBA" if pix.alpha else "RGB"
    if pix.n not in (3, 4):
        # Greyscale or CMYK: let MuPDF do the conversion, it knows the
        # colour space; frombytes here would simply guess wrong.
        import pymupdf as _fitz

        pix = _fitz.Pixmap(_fitz.csRGB, pix)
        mode = "RGBA" if pix.alpha else "RGB"

    return Image.frombytes(
        mode, (pix.width, pix.height), pix.samples, "raw", mode, pix.stride
    )


# ---------------------------------------------------------------------------
# Direct image import
# ---------------------------------------------------------------------------

def import_image(
    path: str,
    source_type: str = "image",
    log: LogCallback = _noop_log,
) -> Optional[ImportedAsset]:
    """Import a raster image file as an ImportedAsset.

    Parameters
    ----------
    path : str
        Absolute or relative path to the image file.
    source_type : str
        The source_type to tag on the asset. Normally ``"image"`` for
        direct imports, or ``"dwg_render"`` / ``"dxf_render"`` for
        CAD pipeline output.
    log : LogCallback
        Structured log callback ``(severity, message)``.

    Returns
    -------
    ImportedAsset or None
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        log("error", f"File not found: {path}")
        return None

    ext = Path(path).suffix.lower()
    if ext not in IMAGE_EXTENSIONS:
        log("warning", f"Unsupported image format: {ext} ({path})")
        return None

    try:
        img = Image.open(path)
        img.load()

        # A photo from a phone or a camera records which way up it is in
        # EXIF rather than in the pixels; without this it lands on the
        # sheet on its side.
        try:
            img = ImageOps.exif_transpose(img) or img
        except Exception:  # noqa: BLE001 - a broken EXIF block is not fatal
            pass

        # Palette, greyscale, CMYK and 16-bit modes have no direct Qt
        # equivalent, and handing one to the canvas unconverted is the
        # other way an image ends up grey and misaligned on screen.
        if img.mode in ("LA", "PA") or (
            img.mode == "P" and "transparency" in img.info
        ):
            img = img.convert("RGBA")
        elif img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")

        log("info", f"Imported image: {os.path.basename(path)} "
            f"({img.width}×{img.height} {img.mode})")
        return ImportedAsset(
            id=ImportedAsset.make_id(),
            source_path=path,
            source_type=source_type,
            image=img,
        )
    except Exception as e:
        log("error", f"Failed to load image {os.path.basename(path)}: {e}")
        return None


# ---------------------------------------------------------------------------
# PDF page import
# ---------------------------------------------------------------------------

def get_pdf_page_count(path: str, log: LogCallback = _noop_log) -> int:
    """Return the number of pages in a PDF, or 0 on error."""
    if not HAS_PYMUPDF:
        log("error", "PyMuPDF is not installed — cannot import PDF files.")
        return 0
    try:
        doc = fitz.open(path)
        count = len(doc)
        doc.close()
        return count
    except Exception as e:
        log("error", f"Failed to open PDF {os.path.basename(path)}: {e}")
        return 0


def import_pdf_page(
    path: str,
    page_number: int = 0,
    dpi: int = 200,
    log: LogCallback = _noop_log,
) -> Optional[ImportedAsset]:
    """Rasterize a single page of a PDF file and return as ImportedAsset."""
    if not HAS_PYMUPDF:
        log("error", "PyMuPDF is not installed — cannot import PDF files.")
        return None

    path = os.path.abspath(path)
    if not os.path.isfile(path):
        log("error", f"File not found: {path}")
        return None

    doc = None
    try:
        doc = fitz.open(path)
        if page_number < 0 or page_number >= len(doc):
            log("error", f"Page {page_number} out of range "
                f"(PDF has {len(doc)} pages): {path}")
            return None

        page = doc[page_number]
        # An A0 page at 200 DPI is already 66 megapixels; a poster-sized
        # one at the same setting is enough to exhaust memory outright.
        # Step the resolution down rather than refusing the import.
        dpi = _fit_dpi_to_budget(page.rect, dpi, log)
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix)

        img = pixmap_to_pil(pix)

        log("info", f"Imported PDF page {page_number + 1} from "
            f"{os.path.basename(path)} ({img.width}×{img.height} at {dpi} DPI)")

        return ImportedAsset(
            id=ImportedAsset.make_id(),
            source_path=path,
            source_type="pdf_page",
            image=img,
        )
    except Exception as e:
        log("error", f"Failed to rasterize PDF page {page_number} "
            f"from {os.path.basename(path)}: {e}")
        return None
    finally:
        # A document left open holds its whole page cache, and on Windows
        # it also keeps a lock on the file the architect may want to
        # replace.  The old code only closed it on the success path.
        if doc is not None:
            try:
                doc.close()
            except Exception:  # noqa: BLE001
                pass


# The most pixels one imported page may occupy.  200 DPI over an A1 sheet
# is 22 megapixels; this leaves headroom for A0 while still refusing the
# kind of page that would otherwise consume a gigabyte per import.
MAX_PAGE_PIXELS = 80_000_000

# Importing a long PDF one asset per page fills the tray and the machine's
# memory at the same time.  Past this, the import stops and says so.
MAX_PDF_PAGES = 40


def _fit_dpi_to_budget(page_rect, dpi: int, log: LogCallback) -> int:
    """Lower *dpi* until the rasterised page fits MAX_PAGE_PIXELS."""
    width_pts = float(page_rect.width)
    height_pts = float(page_rect.height)
    if width_pts <= 0 or height_pts <= 0:
        return dpi

    pixels = (width_pts * dpi / 72.0) * (height_pts * dpi / 72.0)
    if pixels <= MAX_PAGE_PIXELS:
        return dpi

    scale = (MAX_PAGE_PIXELS / pixels) ** 0.5
    reduced = max(72, int(dpi * scale))
    log("warning",
        f"Page is {width_pts:.0f}×{height_pts:.0f} pt — importing at "
        f"{reduced} DPI instead of {dpi} to keep it within memory.")
    return reduced


def import_all_pdf_pages(
    path: str,
    dpi: int = 200,
    log: LogCallback = _noop_log,
) -> list[ImportedAsset]:
    """Rasterize every page of a PDF and return a list of ImportedAssets."""
    count = get_pdf_page_count(path, log)
    if count > MAX_PDF_PAGES:
        log("warning",
            f"{os.path.basename(path)} has {count} pages — importing the "
            f"first {MAX_PDF_PAGES}.  Import the rest separately if needed.")
        count = MAX_PDF_PAGES

    assets = []
    for i in range(count):
        asset = import_pdf_page(path, i, dpi, log)
        if asset:
            assets.append(asset)
    return assets


# ---------------------------------------------------------------------------
# Unified import dispatcher
# ---------------------------------------------------------------------------

def import_file(
    path: str,
    log: LogCallback = _noop_log,
) -> list[ImportedAsset]:
    """Auto-detect file type and import accordingly.

    Returns a list because PDF files may produce multiple assets.
    CAD files (.dwg, .dxf) are NOT handled here — use cad_import.py.
    """
    path = os.path.abspath(path)
    ext = Path(path).suffix.lower()

    if ext in IMAGE_EXTENSIONS:
        asset = import_image(path, log=log)
        return [asset] if asset else []
    elif ext in PDF_EXTENSIONS:
        return import_all_pdf_pages(path, log=log)
    else:
        log("warning", f"Unsupported file type: {ext} — "
            f"supported: {', '.join(sorted(ALL_SUPPORTED))}")
        return []
