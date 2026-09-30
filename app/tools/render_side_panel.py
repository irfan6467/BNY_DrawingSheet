"""
In-process side-panel renderer: cell values -> overlay PDF -> merged final.

Takes the cells the user edited in the Excel-style side panel (keyed by
Excel coordinate, e.g. {"B24": "Some project"}), renders them onto an
overlay at the exact Excel layout positions (column widths + row heights
read from bny_sidepanel.xlsx via openpyxl), then merges that overlay onto
the provided cropped base PDF to produce a final A1-size PDF.

No COM / no Excel instance required.  Only openpyxl, reportlab, pymupdf,
pypdf, PIL.
"""

from __future__ import annotations

import os
import sys
from typing import Dict, Optional, Tuple

from io import BytesIO

import openpyxl
from openpyxl.utils import get_column_letter
import pymupdf as fitz
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.colors import black as rl_black
from pypdf import PdfReader, PdfWriter
from PIL import Image

from app.core.asset_importer import pixmap_to_pil

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
TEMPLATES = os.path.join(PROJECT, "resources", "templates")

DEFAULT_EXCEL = os.path.join(TEMPLATES, "bny_sidepanel.xlsx")
DEFAULT_CROPPED = os.path.join(TEMPLATES, "bny_standard_a1_cropped.pdf")
DEFAULT_FULL = os.path.join(TEMPLATES, "bny_standard_a1.pdf")


# ---------------------------------------------------------------------------
# Layout reader (mirrors the panel widget's layout logic)
# ---------------------------------------------------------------------------

def _cm_to_pts(cm: float) -> float:
    return cm * 72.0 / 2.54


def _load_layout(path: str):
    """Return (ws, col_pts, row_pts, merged_lookup).

    col_pts[c] = width of column c in PDF points.
    row_pts[r] = height of row r in PDF points.
    """
    wb = openpyxl.load_workbook(path)
    ws = wb.active
    col_pts = {}
    for c in range(1, ws.max_column + 1):
        cd = ws.column_dimensions.get(get_column_letter(c))
        w = cd.width if cd and cd.width else 8.43  # Excel char units
        px = w * 7.0 + 5.0                        # chars -> px (Excel)
        mm = px * 25.4 / 96.0 / 10.0             # px -> cm
        col_pts[c] = _cm_to_pts(mm / 10.0)
    row_pts = {}
    for r in range(1, ws.max_row + 1):
        rd = ws.row_dimensions.get(r)
        h = rd.height if rd and rd.height else 15.0  # points
        row_pts[r] = h
    merged_lookup = {}
    for mr in ws.merged_cells.ranges:
        for rr in range(mr.min_row, mr.max_row + 1):
            for cc in range(mr.min_col, mr.max_col + 1):
                merged_lookup[(rr, cc)] = (mr.min_row, mr.max_row, mr.min_col, mr.max_col)
    return ws, col_pts, row_pts, merged_lookup


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _excel_to_pdf(ws, col_pts, row_pts, merged_lookup, row: int, col: int,
                  anchor_x: float, anchor_y: float) -> Tuple[float, float, float, float]:
    """Return (x, y, w, h) PDF pts of the top-left cell of the merged extent
    containing (row, col), anchored at (anchor_x, anchor_y)."""
    mr = merged_lookup.get((row, col))
    if mr:
        mr_min_row, mr_max_row, mr_min_col, mr_max_col = mr
    else:
        mr_min_row = mr_max_row = row
        mr_min_col = mr_max_col = col
    x = anchor_x + sum(col_pts[i] for i in range(1, mr_min_col)) if mr_min_col > 1 else anchor_x
    y = anchor_y + sum(row_pts[i] for i in range(1, mr_min_row)) if mr_min_row > 1 else anchor_y
    w = sum(col_pts[i] for i in range(mr_min_col, mr_max_col + 1))
    h = sum(row_pts[i] for i in range(mr_min_row, mr_max_row + 1))
    return x, y, w, h


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_MIN_CELL_FONT_PT = 4.0


def _fit_font_size(c, lines, font_name, size, avail_w, avail_h):
    """Shrink a cell's type until the whole value fits inside it.

    Text longer than its cell used to run past the border and be clipped
    there.  Excel's own ShrinkToFit now handles the live panel; this
    keeps the fallback renderer in step with it.
    """
    size = float(size) if size else 10.0
    if avail_w <= 0 or avail_h <= 0:
        return size

    for _ in range(60):
        widest = max(
            (c.stringWidth(ln, font_name, size) for ln in lines), default=0.0
        )
        tall = len(lines) * size * 1.15
        if (widest <= avail_w and tall <= avail_h) or size <= _MIN_CELL_FONT_PT:
            break
        size = max(_MIN_CELL_FONT_PT, size - 0.25)
    return size


def _font_for_cell(cell) -> str:
    name = (cell.font.name or "Arial").strip().lower()
    if name in ("tahoma", "tahoma mt"):
        return "Helvetica"
    if name in ("times new roman", "times", "time new roman"):
        return "Times-Roman"
    if name in ("arial mt", "arial", "arial black"):
        return "Helvetica-Bold" if cell.font.bold else "Helvetica"
    return "Helvetica"


def render_side_panel_merge(
    values: Dict[str, str],
    output_path: str,
    excel_path: Optional[str] = None,
    cropped_base: Optional[str] = None,
    full_base: Optional[str] = None,
) -> str:
    """Render edited cell values onto the cropped base -> final A1-size PDF.

    Parameters
    ----------
    values : dict
        Edited cell values keyed by Excel coordinate, e.g.
        {"B24": "Project X", "G31": "A1", "B26": "Client A"}.
    output_path : str
        Where to write the final merged PDF.
    excel_path : str, optional
        Path to bny_sidepanel.xlsx.  Defaults to the bundled template.
    cropped_base : str, optional
        Base PDF to merge the overlay onto.  This is usually the generated
        base PDF (template + placed CAD assets).  If omitted, the bundled
        bny_standard_a1_cropped.pdf is used directly (no placed items).
    full_base : str, optional
        Path to the full A1 template (used only to obtain the final page
        size / mediabox).  Defaults to the bundled bny_standard_a1.pdf.
    """
    excel_path = os.path.abspath(excel_path or DEFAULT_EXCEL)
    cropped_base = os.path.abspath(cropped_base or DEFAULT_CROPPED)
    full_base = os.path.abspath(full_base or DEFAULT_FULL)
    output_path = os.path.abspath(output_path)

    # --- layout ---
    ws, col_pts, row_pts, merged_lookup = _load_layout(excel_path)

    # --- geometry: anchor Excel A1 to the right-side panel region of the A1 ---
    # The A1 template's right-side panel (callout block) starts at x ~ 1394 pts.
    # We anchor Excel col A left edge at x = 1394 (so the Excel A..O block sits
    # exactly in the right panel), with y = 10.3 matching the base template.
    ANCHOR_X = 1394.0 - col_pts.get(1, 0.0)  # Excel A1 top-left corner
    ANCHOR_Y = 10.3

    # Final page size: use the full A1 template
    d_full = fitz.open(full_base)
    PAGE_W = float(d_full[0].rect.width)
    PAGE_H = float(d_full[0].rect.height)
    d_full.close()

    # --- build overlay canvas (same size as final A1 page) ---
    buf = BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    c.setPageCompression(1)

    # Border around the whole Excel block
    sheet_w = sum(col_pts.get(i, 0.0) for i in range(1, ws.max_column + 1))
    sheet_h = sum(row_pts.get(r, 0.0) for r in range(1, ws.max_row + 1))
    c.saveState()
    c.setStrokeColor(rl_black)
    c.setLineWidth(0.5)
    c.rect(ANCHOR_X, ANCHOR_Y, sheet_w, sheet_h, stroke=1, fill=0)
    c.restoreState()

    for row in range(1, ws.max_row + 1):
        for col in range(1, ws.max_column + 1):
            cell = ws.cell(row=row, column=col)
            coord = cell.coordinate
            v = values.get(coord, "")

            # Use the top-left of the merged extent for placement
            x, y, w_pt, h_pt = _excel_to_pdf(
                ws, col_pts, row_pts, merged_lookup,
                row, col, ANCHOR_X, ANCHOR_Y,
            )

            if not v or str(v).strip() == "":
                continue

            fn = _font_for_cell(cell)
            sz = cell.font.sz if cell.font.sz else 10

            lines = str(v).splitlines() or [""]
            sz = _fit_font_size(c, lines, fn, sz, w_pt - 2.4, h_pt)

            c.saveState()
            c.setFont(fn, sz)
            c.setFillColor(rl_black)

            line_h = sz * 1.15
            cy = y + h_pt - sz * 0.85  # first baseline near top of cell
            for ln in lines:
                c.drawString(x + 1.2, cy, ln)
                cy -= line_h
            c.restoreState()

    c.save()
    buf.seek(0)

    # --- merge overlay onto a FRESH A1-sized page, left-aligned cropped base ---
    # The cropped base may be 1387 pts wide; we want the final page to be
    # 1684 pts (A1).  So we build a brand-new A1 page, draw the cropped base
    # onto it left-aligned (at x=0..1387 leaving 1387..1684 free for the panel),
    # then merge the overlay (which lives at x>=1394) on top.

    tmp = BytesIO()
    rc = rl_canvas.Canvas(tmp, pagesize=(PAGE_W, PAGE_H))
    rc.setPageCompression(1)

    # Rasterize the cropped base at 2x for crisp output and draw left-aligned
    d_crop = fitz.open(cropped_base)
    crop_pix = d_crop[0].get_pixmap(matrix=fitz.Matrix(2, 2))
    d_crop.close()
    # Via the shared helper - MuPDF pads scanlines to its own stride, and
    # a bare frombytes reads straight past the end of each row.
    crop_img = pixmap_to_pil(crop_pix)
    # Draw the rasterized base so it occupies x=0..1387, y=0..1191 on the A1 page
    rc.drawInlineImage(crop_img, 0, 0, width=1387.0, height=1191.0)
    rc.showPage()
    rc.save()
    tmp.seek(0)

    base_reader = PdfReader(tmp)
    base_page = base_reader.pages[0]
    base_page.mediabox.lower_left = (0, 0)
    base_page.mediabox.upper_right = (PAGE_W, PAGE_H)

    overlay_reader = PdfReader(buf)
    overlay_page = overlay_reader.pages[0]
    overlay_page.mediabox.lower_left = (0, 0)
    overlay_page.mediabox.upper_right = (PAGE_W, PAGE_H)

    base_page.merge_page(overlay_page)

    writer = PdfWriter()
    writer.add_page(base_page)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "wb") as f:
        writer.write(f)

    return output_path


__all__ = ["render_side_panel_merge"]
