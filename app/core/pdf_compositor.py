"""
PDF compositor — simplified serializer for the WYSIWYG model.

Pipeline:
  1. Load the base template PDF via pypdf.
  2. Iterate ``state.placed_items`` — each item's ``page_rect`` is already
     in final PDF-point coordinates (set by the architect on the canvas).
  3. For image items: draw at page_rect. If the asset has a vector_source_path,
     merge the vector PDF at the correct position/scale.
  4. For callout circles: draw the circle image and leader line from the
     PlacedItem's leader_target_page_pos.
  5. Draw title-block text overlay.
  6. Merge overlay onto base page.

There is no more fit/fill branching, no slot lookup, no coordinate
translation — the compositor draws exactly what the architect placed.

DESIGN RULE (§9): No Qt imports.
"""

from __future__ import annotations

import io
import math
import os
from typing import Optional

from PIL import Image
from pypdf import PdfReader, PdfWriter, Transformation
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas

from app.core.project_state import (
    PlacedItem,
    ProjectMetadata,
    ProjectState,
    SheetTemplate,
)
from app.core.image_processor import (
    crop_from_asset,
    apply_circular_mask,
    draw_circle_boundary,
)


# ---------------------------------------------------------------------------
# Helper: PIL Image → ReportLab ImageReader
# ---------------------------------------------------------------------------

def _pil_to_reportlab(img: Image.Image) -> ImageReader:
    """Convert a PIL Image to a ReportLab-compatible ImageReader."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return ImageReader(buf)


# ---------------------------------------------------------------------------
# Draw a single placed image item
# ---------------------------------------------------------------------------

def _draw_placed_image(
    c: rl_canvas.Canvas,
    item: PlacedItem,
    state: ProjectState,
    page_height: float,
) -> None:
    """Draw a placed image item on the ReportLab overlay canvas.

    The item's page_rect is (x, y, w, h) in PDF points with top-left
    origin.  ReportLab uses bottom-left origin, so we flip Y.
    """
    asset = state.get_asset(item.source_asset_id)
    if asset is None or asset.image is None:
        return

    img = asset.image

    # Apply crop if specified
    if item.crop_box:
        img = crop_from_asset(img, item.crop_box)
        if getattr(item, "circular_mask", False):
            # A circular crop must print as a circle, not as its
            # bounding square.
            img = apply_circular_mask(
                img, output_size=min(img.width, img.height)
            )

    x, y, w, h = item.page_rect

    # Convert top-left Y to ReportLab bottom-left Y
    rl_y = page_height - y - h

    c.saveState()
    
    # ReportLab rotates around the origin (0,0). To rotate around center:
    cx = x + w / 2.0
    cy = rl_y + h / 2.0
    c.translate(cx, cy)
    # Qt is clockwise, ReportLab is counter-clockwise, so negate the angle
    if hasattr(item, 'rotation') and item.rotation:
        c.rotate(-item.rotation)
    
    c.drawImage(
        _pil_to_reportlab(img),
        -w/2.0, -h/2.0,
        width=w, height=h,
        preserveAspectRatio=False,
        mask="auto",
    )
    c.restoreState()


# ---------------------------------------------------------------------------
# Merge a vector PDF (for DWG-derived content)
# ---------------------------------------------------------------------------

def _merge_vector_source(
    base_page,
    item: PlacedItem,
    state: ProjectState,
    page_height: float,
) -> None:
    """Merge a vector PDF (DWG render) onto the base page at the item's
    page_rect position.

    Uses pypdf's merge_transformed_page with a Transformation matrix
    to position and scale the vector content.
    """
    asset = state.get_asset(item.source_asset_id)
    if asset is None or not asset.vector_source_path:
        return
    if not os.path.isfile(asset.vector_source_path):
        return

    try:
        vec_reader = PdfReader(asset.vector_source_path)
        vec_page = vec_reader.pages[0]
    except Exception:
        return

    # Get vector page dimensions
    vec_box = vec_page.mediabox
    vec_w = float(vec_box.width)
    vec_h = float(vec_box.height)

    if vec_w <= 0 or vec_h <= 0:
        return

    x, y, w, h = item.page_rect

    # Scale factors
    sx = w / vec_w
    sy = h / vec_h
    scale = min(sx, sy)  # maintain aspect ratio

    # Centering offset
    scaled_w = vec_w * scale
    scaled_h = vec_h * scale
    offset_x = x + (w - scaled_w) / 2.0
    # Convert top-left Y to pypdf bottom-left Y
    offset_y = page_height - y - h + (h - scaled_h) / 2.0

    # Build transformation: scale then translate
    ctm = Transformation().scale(scale, scale).translate(offset_x / scale, offset_y / scale)
    
    # Apply rotation around center
    if hasattr(item, 'rotation') and item.rotation:
        cx = x + w / 2.0
        cy = offset_y + (scaled_h / 2.0)
        # We need to translate to center, rotate, and translate back
        # The transformation matrix applies transformations in reverse order.
        rot_ctm = Transformation().translate(cx, cy).rotate(-item.rotation).translate(-cx, -cy)
        # Apply rot_ctm after the scale/translate
        # In pypdf, transformations are concatenated. 
        # Actually pypdf Transformation handles this elegantly.
        ctm = Transformation().scale(scale, scale).translate(offset_x / scale, offset_y / scale)
        # Wait, the translation above already puts it at (offset_x, offset_y).
        # We should just rotate around the center of the bounding box.
        ctm = ctm.translate(scaled_w/2.0, scaled_h/2.0).rotate(-item.rotation).translate(-scaled_w/2.0, -scaled_h/2.0)
    
    base_page.merge_transformed_page(vec_page, ctm, over=True)


# ---------------------------------------------------------------------------
# Draw a callout circle + leader line
# ---------------------------------------------------------------------------

def _draw_callout_item(
    c: rl_canvas.Canvas,
    item: PlacedItem,
    state: ProjectState,
    page_height: float,
) -> None:
    """Draw a callout circle image and its leader line."""
    asset = state.get_asset(item.source_asset_id)
    if asset is None or asset.image is None:
        return

    # Render the callout circle
    if item.crop_box:
        cropped = crop_from_asset(asset.image, item.crop_box)
    else:
        cropped = asset.image

    circle_size = int(max(item.page_rect[2], item.page_rect[3]))
    masked = apply_circular_mask(cropped, output_size=max(circle_size * 2, 100))
    ringed = draw_circle_boundary(
        masked,
        leader_style=item.leader_style,
        sheet_diameter_pts=float(circle_size) if circle_size else None,
    )

    x, y, w, h = item.page_rect
    rl_y = page_height - y - h

    # Draw circle image
    c.drawImage(
        _pil_to_reportlab(ringed),
        x, rl_y,
        width=w, height=h,
        mask="auto",
    )

    # Draw leader line
    if item.leader_target_page_pos:
        lx, ly = item.leader_target_page_pos

        # Circle center in RL coordinates
        rl_cx = x + w / 2.0
        rl_cy = page_height - y - h / 2.0
        radius = min(w, h) / 2.0

        # Target in RL coordinates
        rl_lx = lx
        rl_ly = page_height - ly

        # Calculate start point on circle edge
        dx = rl_lx - rl_cx
        dy = rl_ly - rl_cy
        dist = math.sqrt(dx * dx + dy * dy)

        if dist > radius + 1:
            start_x = rl_cx + (dx / dist) * radius
            start_y = rl_cy + (dy / dist) * radius

            c.saveState()
            c.setStrokeColorRGB(107 / 255, 63 / 255, 105 / 255)
            c.setLineWidth(1.5)

            if item.leader_style == "dashed":
                c.setDash(6, 4)

            c.line(start_x, start_y, rl_lx, rl_ly)
            c.restoreState()

    # Draw label
    if item.callout_id:
        label_x = x
        label_y = y + h + 4
        rl_label_y = page_height - label_y

        c.saveState()
        c.setFillColorRGB(107 / 255, 63 / 255, 105 / 255)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(label_x, rl_label_y, item.callout_id)

        if item.description:
            c.setFont("Helvetica", 6)
            c.drawString(label_x, rl_label_y - 10, item.description)
        c.restoreState()





# ---------------------------------------------------------------------------
# Main compositor
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Sheet lettering: view labels, detail notes, sheet title
# ---------------------------------------------------------------------------

_ANNOTATION_FONT = "Helvetica"
_ANNOTATION_FONT_BOLD = "Helvetica-Bold"


def _draw_view_label(c, annot, page_height: float) -> None:
    """Triangle, title and the rule that runs out past it."""
    x, y = annot.page_pos
    rl_y = page_height - y
    size = max(4.0, float(annot.font_size))

    tri_w = size * 1.2
    tri_h = size * 0.65
    gap = size * 0.28

    c.saveState()
    c.setFillColorRGB(0.07, 0.07, 0.07)
    c.setStrokeColorRGB(0.07, 0.07, 0.07)

    path = c.beginPath()
    path.moveTo(x, rl_y)
    path.lineTo(x + tri_w, rl_y)
    path.lineTo(x + tri_w / 2.0, rl_y + tri_h)
    path.close()
    c.drawPath(path, stroke=0, fill=1)

    text_x = x + tri_w + gap
    c.setFont(_ANNOTATION_FONT, size)
    c.drawString(text_x, rl_y, annot.text)

    text_w = c.stringWidth(annot.text, _ANNOTATION_FONT, size)
    c.setLineWidth(max(0.7, size * 0.11))
    rule_y = rl_y - size * 0.28
    c.line(x, rule_y, text_x + text_w + float(annot.rule_width), rule_y)
    c.restoreState()


def _draw_sheet_title(c, annot, page_height: float) -> None:
    x, y = annot.page_pos
    c.saveState()
    c.setFillColorRGB(0.07, 0.07, 0.07)
    c.setFont(_ANNOTATION_FONT, max(4.0, float(annot.font_size)))
    c.drawString(x, page_height - y, annot.text)
    c.restoreState()


def _draw_detail_note(c, annot, page_height: float) -> None:
    """Wording, a leader with a short shoulder, and the dot it points at."""
    x, y = annot.page_pos
    rl_y = page_height - y
    size = max(4.0, float(annot.font_size))
    lines = str(annot.text).splitlines() or [""]

    # Bold, to match the canvas: a detail note is a call-out, and a
    # regular weight loses itself against the drawing behind it.
    font = _ANNOTATION_FONT_BOLD

    c.saveState()
    c.setFillColorRGB(0.07, 0.07, 0.07)
    c.setStrokeColorRGB(0.07, 0.07, 0.07)
    c.setFont(font, size)

    leading = size * 1.2
    for i, line in enumerate(lines):
        c.drawString(x, rl_y - i * leading, line)

    if annot.target_page_pos:
        tx, ty = annot.target_page_pos
        t_rl_y = page_height - ty
        width = max(
            (c.stringWidth(ln, font, size) for ln in lines),
            default=0.0,
        )
        block_bottom = rl_y - (len(lines) - 1) * leading - size * 0.25
        # Leave the block on whichever side the dot is, as on the
        # reference sheets, then run straight to it.
        start_x = x + width if tx >= x + width / 2.0 else x
        shoulder_x = start_x + (size * 0.5 if tx >= start_x else -size * 0.5)

        c.setLineWidth(max(0.5, size * 0.07))
        c.line(start_x, block_bottom, shoulder_x, block_bottom)
        c.line(shoulder_x, block_bottom, tx, t_rl_y)

        dot = max(1.1, size * 0.16)
        c.circle(tx, t_rl_y, dot, stroke=0, fill=1)

    c.restoreState()


def _draw_annotations(c, state, page_height: float) -> None:
    """Draw every piece of sheet lettering the architect placed."""
    painters = {
        "view_label": _draw_view_label,
        "sheet_title": _draw_sheet_title,
        "detail_note": _draw_detail_note,
    }
    for annot in getattr(state, "annotations", []) or []:
        painter = painters.get(annot.kind)
        if painter is None:
            continue
        try:
            painter(c, annot, page_height)
        except Exception:
            # One malformed note must not cost the architect the sheet.
            continue


def generate_technical_sheet(
    state: ProjectState,
    output_path: str,
) -> str:
    """Generate the final technical drawing sheet PDF.

    Iterates ``state.placed_items`` — every item's ``page_rect`` is already
    in final PDF-point coordinates set by the architect on the WYSIWYG
    canvas.  No fit/fill/slot logic.

    Parameters
    ----------
    state : ProjectState
    output_path : str

    Returns
    -------
    str
        The output path on success.
    """
    template = state.template
    if template is None:
        raise ValueError("No sheet template loaded")

    if not os.path.isfile(template.base_pdf_path):
        raise FileNotFoundError(
            f"Base template PDF not found: {template.base_pdf_path}"
        )

    # Read base template
    reader = PdfReader(template.base_pdf_path)
    base_page = reader.pages[0]

    media_box = base_page.mediabox
    page_width = float(media_box.width)
    page_height = float(media_box.height)

    # Create ReportLab overlay canvas
    overlay_buf = io.BytesIO()
    c = rl_canvas.Canvas(overlay_buf, pagesize=(page_width, page_height))

    # ── Draw all placed items ─────────────────────────────────────────
    # Sort by z_order so items layer correctly
    sorted_items = sorted(state.placed_items, key=lambda pi: pi.z_order)

    for item in sorted_items:
        if item.item_type == "image":
            # Check for vector source first
            asset = state.get_asset(item.source_asset_id)
            if asset and asset.vector_source_path and os.path.isfile(asset.vector_source_path):
                # Vector merge happens directly on the base page after overlay
                pass  # handled below
            else:
                _draw_placed_image(c, item, state, page_height)

        elif item.item_type == "callout_circle":
            _draw_callout_item(c, item, state, page_height)

    # ── Sheet lettering, on top of the drawings ───────────────────────
    _draw_annotations(c, state, page_height)

    # ── Finalise raster overlay ───────────────────────────────────────
    # showPage() explicitly: ReportLab only emits a page on save() when
    # something was actually drawn, so a sheet whose every item is vector
    # line art — which is now the normal case for views extracted from a
    # DWG — produced a PDF with no pages at all, and the merge below died
    # on an index error before the architect saw anything.
    c.showPage()
    c.save()
    overlay_buf.seek(0)

    # ── Merge raster overlay onto base template ───────────────────────
    overlay_reader = PdfReader(overlay_buf)
    if len(overlay_reader.pages):
        base_page.merge_page(overlay_reader.pages[0])

    # ── Merge vector sources (DWG renders) ────────────────────────────
    for item in sorted_items:
        if item.item_type == "image":
            asset = state.get_asset(item.source_asset_id)
            if asset and asset.vector_source_path and os.path.isfile(asset.vector_source_path):
                _merge_vector_source(base_page, item, state, page_height)

    # ── Write output ──────────────────────────────────────────────────
    writer = PdfWriter()
    writer.add_page(base_page)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "wb") as f:
        writer.write(f)

    return output_path
