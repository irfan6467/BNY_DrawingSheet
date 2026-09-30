"""
Attach the Excel-exported side panel PDF to the cropped drawing sheet.

The canvas in the app is the *cropped* template (drawing area only, no
side panel).  The side panel lives in a real Excel sheet embedded in the
app; when the architect clicks Done, Excel exports that sheet to a PDF
with ExportAsFixedFormat — vector text, real fonts, the logo, every
border exactly as Excel draws it.

This module glues the two together: cropped sheet on the left, Excel
panel PDF on the right, on one full-size page (the template's own page
size, 1684 x 1191 pt for the bundled A2-landscape base).

Everything stays vector — the panel is placed with show_pdf_page, not
rasterised — so the final sheet prints at full resolution.

DESIGN RULE (§9): No Qt imports.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

import pymupdf as fitz
from PIL import Image, ImageChops

from app.core.asset_importer import pixmap_to_pil
from app.utils.paths import resource_path

TEMPLATES = resource_path(os.path.join("app", "resources", "templates"))
DEFAULT_FULL = os.path.join(TEMPLATES, "bny_standard_a1.pdf")

# ---------------------------------------------------------------------------
# Placement tuning
# ---------------------------------------------------------------------------
# The panel strip is whatever horizontal space the cropped sheet leaves on
# the right of the full page.  These insets trim that strip; all values are
# PDF points.
#
# Horizontally the panel fills the strip edge to edge — the sheet in
# bny_sidepanel.xlsx is ~298 pt wide, within a point of the 297 pt the
# cropped template leaves free, so it was clearly drawn to fit.
PANEL_INSET_LEFT = 0.0
PANEL_INSET_RIGHT = 0.0

# Vertically the panel is matched to the drawing itself: its top and bottom
# borders are put exactly on the top and bottom of the cropped sheet's own
# ink, so the two halves are the same height and meet flush along the seam.
# Both are measured the same way, so "the same height" is exact rather than
# a pair of numbers that happen to agree.
#
# Set this to False to place the panel by PANEL_INSET_TOP / _BOTTOM from the
# page edges instead.
PANEL_MATCH_DRAWING_HEIGHT = True
PANEL_INSET_TOP = 0.0
PANEL_INSET_BOTTOM = 0.0

# How the exported panel fills the strip:
#   "stretch" — fill the strip exactly, so the panel is precisely as tall as
#               the drawing beside it.  The Excel sheet is a few percent
#               shorter than the strip is tall, so this scales it slightly
#               taller than wide.
#   "fit"     — preserve the panel's aspect ratio and centre it in the
#               strip.  True to Excel, but leaves a white gap.
PANEL_FIT_MODE = "stretch"

# Vertical anchor used when PANEL_FIT_MODE == "fit": "top", "center", "bottom".
PANEL_VALIGN = "center"


# ---------------------------------------------------------------------------
# Ink bounding box
# ---------------------------------------------------------------------------

def visual_bbox(page: fitz.Page, zoom: float = 2.0,
                threshold: int = 8) -> fitz.Rect:
    """Return the bounding box of everything visibly drawn on *page*.

    Excel exports onto a whole sheet of paper (A3, A4, whatever the active
    printer offers), so the panel occupies only a corner of the exported
    page and the rest is blank.  Reading the ink extent from the text and
    drawing objects overestimates it — text blocks carry padding — so we
    rasterise once and measure the non-white pixels instead.

    Falls back to the full page rect if the page turns out to be blank.
    """
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    # Read the pixmap through the shared helper: its stride is not always
    # width*n, and a page rendered with alpha carries four components, so
    # a bare frombytes("RGB", ...) shears the image and misreads the ink.
    img = pixmap_to_pil(pix)
    white = Image.new("RGB", img.size, (255, 255, 255))
    mask = ImageChops.difference(img, white).convert("L")
    box = mask.point(lambda v: 255 if v > threshold else 0).getbbox()
    if not box:
        return fitz.Rect(page.rect)
    x0, y0, x1, y1 = (v / zoom for v in box)
    return fitz.Rect(x0, y0, x1, y1) & page.rect


# ---------------------------------------------------------------------------
# Strip geometry
# ---------------------------------------------------------------------------

def _target_rect(src: fitz.Rect, strip: fitz.Rect) -> fitz.Rect:
    """Where inside *strip* the panel's *src* box should land."""
    if PANEL_FIT_MODE != "fit" or src.width <= 0 or src.height <= 0:
        return strip

    scale = min(strip.width / src.width, strip.height / src.height)
    w = src.width * scale
    h = src.height * scale
    x = strip.x0 + (strip.width - w) / 2.0
    if PANEL_VALIGN == "top":
        y = strip.y0
    elif PANEL_VALIGN == "bottom":
        y = strip.y1 - h
    else:
        y = strip.y0 + (strip.height - h) / 2.0
    return fitz.Rect(x, y, x + w, y + h)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def attach_panel_pdf(
    cropped_pdf: str,
    panel_pdf: str,
    output_path: str,
    full_base: Optional[str] = None,
) -> str:
    """Compose the final sheet: cropped drawing + Excel panel on one page.

    Parameters
    ----------
    cropped_pdf : str
        The generated drawing sheet — the cropped template with the placed
        CAD views and callouts on it.  Drawn at its natural size against
        the left edge of the page.
    panel_pdf : str
        The side panel as Excel exported it.  Only the inked part of the
        page is used; it is scaled into the strip the cropped sheet leaves
        free on the right.
    output_path : str
        Where to write the composed PDF.
    full_base : str, optional
        Template whose page size defines the final sheet.  Defaults to the
        bundled full-size base (1684 x 1191 pt).

    Returns
    -------
    str
        ``output_path``.

    Raises
    ------
    FileNotFoundError
        If any input PDF is missing.
    ValueError
        If the cropped sheet leaves no room for the panel.
    """
    cropped_pdf = os.path.abspath(cropped_pdf)
    panel_pdf = os.path.abspath(panel_pdf)
    output_path = os.path.abspath(output_path)
    full_base = os.path.abspath(full_base or DEFAULT_FULL)

    for path, label in ((cropped_pdf, "cropped sheet"),
                        (panel_pdf, "side panel"),
                        (full_base, "full template")):
        if not os.path.isfile(path):
            raise FileNotFoundError(f"{label} PDF not found: {path}")

    with fitz.open(full_base) as full:
        page_w = float(full[0].rect.width)
        page_h = float(full[0].rect.height)

    out = fitz.open()
    src_crop = fitz.open(cropped_pdf)
    src_panel = fitz.open(panel_pdf)
    try:
        crop_rect = src_crop[0].rect
        crop_w = float(crop_rect.width)

        if PANEL_MATCH_DRAWING_HEIGHT:
            drawing_ink = visual_bbox(src_crop[0])
            top, bottom = float(drawing_ink.y0), float(drawing_ink.y1)
        else:
            top, bottom = PANEL_INSET_TOP, page_h - PANEL_INSET_BOTTOM

        strip = fitz.Rect(
            crop_w + PANEL_INSET_LEFT,
            top,
            page_w - PANEL_INSET_RIGHT,
            bottom,
        )
        if strip.width <= 1.0:
            raise ValueError(
                f"The cropped sheet is {crop_w:.0f} pt wide on a "
                f"{page_w:.0f} pt page — no room left for the side panel."
            )

        page = out.new_page(width=page_w, height=page_h)

        # Drawing sheet, natural size, flush to the left edge.  Its own
        # height is scaled to the page height so a template that was
        # cropped in width only still lines up.
        page.show_pdf_page(fitz.Rect(0, 0, crop_w, page_h), src_crop, 0)

        # Side panel, cropped to its ink and scaled into the strip.
        # keep_proportion=False is what makes "stretch" actually stretch:
        # left on, PyMuPDF centres the panel in the strip and it ends up
        # shorter than the drawing next to it.
        ink = visual_bbox(src_panel[0])
        page.show_pdf_page(
            _target_rect(ink, strip), src_panel, 0,
            clip=ink,
            keep_proportion=PANEL_FIT_MODE == "fit",
        )

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        out.save(output_path, garbage=3, deflate=True)
    finally:
        src_panel.close()
        src_crop.close()
        out.close()

    return output_path


def panel_strip_size(cropped_pdf: str,
                     full_base: Optional[str] = None) -> Tuple[float, float]:
    """Return (width, height) in points of the strip left for the panel."""
    full_base = os.path.abspath(full_base or DEFAULT_FULL)
    with fitz.open(full_base) as full:
        page_w = float(full[0].rect.width)
        page_h = float(full[0].rect.height)
    with fitz.open(os.path.abspath(cropped_pdf)) as crop:
        crop_w = float(crop[0].rect.width)
    return page_w - crop_w, page_h


__all__ = ["attach_panel_pdf", "panel_strip_size", "visual_bbox"]
