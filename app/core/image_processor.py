"""
Image processing engine for callout rendering.

Implements the rendering pipeline for a single Callout:
  1. Crop crop_source_asset_id's image using crop_box
  2. Apply circular mask (Pillow)
  3. Draw circular boundary (dashed or solid per leader_style)

The coordinate-translation step (translate_anchor_to_page) has been
removed — under the WYSIWYG model, the architect drags the leader-line
endpoint directly on the canvas, so no calculation is needed.

DESIGN RULE (§9): No Qt imports.  Pure Pillow operations.
"""

from __future__ import annotations

import math
import os
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from app.utils.paths import resource_path


# Oversampling used when drawing circles, so their edges come out
# smooth rather than stair-stepped.
_SUPERSAMPLE = 4


# ---------------------------------------------------------------------------
# Bundled font path (DejaVu Sans, open-license)
# ---------------------------------------------------------------------------

def _get_bundled_font(size: int) -> ImageFont.FreeTypeFont:
    """Load the bundled DejaVu Sans font at the given size.

    Falls back gracefully: bundled font → system arial → Pillow default.
    Never silently produces illegible text.
    """
    # Try bundled font first
    font_path = resource_path(
        os.path.join("app", "resources", "fonts", "DejaVuSans.ttf")
    )
    if os.path.isfile(font_path):
        try:
            return ImageFont.truetype(font_path, size)
        except (IOError, OSError):
            pass

    # Fallback: system Arial (common on Windows)
    try:
        return ImageFont.truetype("arial.ttf", size)
    except (IOError, OSError):
        pass

    # Last resort: Pillow's built-in bitmap font
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Step 1: Crop from source asset
# ---------------------------------------------------------------------------

def crop_from_asset(
    source_image: Image.Image,
    crop_box: tuple[float, float, float, float],
) -> Image.Image:
    """Crop a rectangular region from the source image.

    Parameters
    ----------
    source_image : PIL.Image.Image
        The source asset image (typically the photorealistic reference
        photo — NOT the same as the anchor target image).
    crop_box : tuple
        (x1, y1, x2, y2) in the source image's pixel coordinates.
        This is the unified coordinate format used throughout the app.

    Returns
    -------
    PIL.Image.Image
        The cropped rectangular region.
    """
    x1, y1, x2, y2 = [int(round(v)) for v in crop_box]
    # A box dragged right-to-left or bottom-to-top arrives reversed;
    # normalise before clamping or the crop collapses to one pixel.
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    # Clamp to image bounds
    x1 = max(0, min(x1, source_image.width - 1))
    y1 = max(0, min(y1, source_image.height - 1))
    x2 = max(x1 + 1, min(x2, source_image.width))
    y2 = max(y1 + 1, min(y2, source_image.height))
    return source_image.crop((x1, y1, x2, y2))


# ---------------------------------------------------------------------------
# Step 2: Apply circular mask
# ---------------------------------------------------------------------------

def _centre_square(image: Image.Image) -> Image.Image:
    """Return the largest centred square of ``image``.

    Squashing a wide crop into a square circle would stretch the detail
    out of proportion, so trim the long axis instead.
    """
    side = min(image.width, image.height)
    if image.width == side and image.height == side:
        return image
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    return image.crop((left, top, left + side, top + side))


def _supersample_factor(size: int, limit: int = 4096) -> int:
    """How far to oversample a ``size``-px drawing before averaging down."""
    if size <= 0:
        return 1
    return max(1, min(_SUPERSAMPLE, limit // size))


def _circle_mask(size: int) -> Image.Image:
    """An anti-aliased circular mask of ``size`` px.

    Pillow's ellipse has hard pixel edges, which reads as a visibly
    jagged rim on a detail circle.  Drawing it oversampled and scaling
    down averages the boundary into a clean edge.
    """
    factor = _supersample_factor(size)
    big = size * factor
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    if factor == 1:
        return mask
    return mask.resize((size, size), Image.Resampling.LANCZOS)


def apply_circular_mask(
    cropped: Image.Image,
    output_size: int = 0,
) -> Image.Image:
    """Apply a circular mask to a cropped image, producing a circular
    crop on a transparent background.

    Parameters
    ----------
    cropped : PIL.Image.Image
        The rectangular crop from step 1.
    output_size : int
        Diameter of the output circle in pixels.  If 0, uses the
        shorter dimension of the crop.

    Returns
    -------
    PIL.Image.Image
        RGBA image with the circular crop and transparent surround.
    """
    if output_size <= 0:
        output_size = min(cropped.width, cropped.height)
    output_size = max(1, int(output_size))

    # Square off first so the circle shows the detail undistorted.
    square = _centre_square(cropped).resize(
        (output_size, output_size), Image.Resampling.LANCZOS
    )

    mask = _circle_mask(output_size)

    # Composite onto transparent background
    result = Image.new("RGBA", (output_size, output_size), (0, 0, 0, 0))
    result.paste(square.convert("RGBA"), (0, 0), mask)
    return result


# ---------------------------------------------------------------------------
# Step 3: Draw circular boundary
# ---------------------------------------------------------------------------

# Detail-circle ring, expressed the way a drafter thinks about it.
RING_WIDTH_MM = 1.6      # stroke weight on the printed sheet
RING_DASH_MM = 3.2       # length of one dash at sheet scale
RING_GAP_RATIO = 0.42    # gap as a fraction of the dash pitch
_PT_PER_MM = 72.0 / 25.4


def draw_circle_boundary(
    image: Image.Image,
    leader_style: str = "dashed",
    ring_color: tuple = (17, 17, 17),  # sheet ink - matches the drawings
    ring_width: Optional[int] = None,
    sheet_diameter_pts: Optional[float] = None,
    ring_width_mm: float = RING_WIDTH_MM,
    dash_mm: float = RING_DASH_MM,
) -> Image.Image:
    """Draw the detail circle's boundary ring.

    The weight and the dash pitch are given in millimetres *as printed*.
    ``sheet_diameter_pts`` is how wide the circle lands on the sheet; with
    it the ring comes out the same weight whatever resolution the crop was
    rendered at.  Without it the image is assumed to be 2 px per point,
    which is what the callers here produce.

    Parameters
    ----------
    image : PIL.Image.Image
        The circular-masked RGBA image from step 2.
    leader_style : str
        ``"dashed"`` or ``"solid"``.
    ring_color : tuple
        RGB color for the ring.
    ring_width : int, optional
        Explicit stroke in pixels, overriding ``ring_width_mm``.

    Returns
    -------
    PIL.Image.Image
        The image with the boundary ring drawn on it.
    """
    result = image.convert("RGBA")
    size = min(result.width, result.height)
    if size < 4:
        return result.copy()

    px_per_pt = (size / sheet_diameter_pts) if sheet_diameter_pts else 2.0
    if px_per_pt <= 0:
        px_per_pt = 2.0

    if ring_width is None:
        width = ring_width_mm * _PT_PER_MM * px_per_pt
    else:
        width = float(ring_width)
    width = max(1.0, width)

    factor = _supersample_factor(size)
    big = size * factor

    # Draw the ring oversampled on its own layer: arcs drawn straight
    # onto the crop come out stair-stepped, and the dashes join badly.
    ring = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(ring)
    stroke = max(1, int(round(width * factor)))

    # Inset by half the stroke so the ring's outer edge lands on the
    # rim of the crop rather than being sliced off by the bitmap edge.
    pad = stroke / 2.0
    box = (pad, pad, big - 1 - pad, big - 1 - pad)
    colour = tuple(ring_color) + (255,)

    if leader_style == "solid":
        draw.ellipse(box, outline=colour, width=stroke)
    else:
        # Keep the dash length fixed in millimetres, so a big circle gets
        # more dashes rather than longer ones and the ring reads the same
        # at any size.
        diameter_mm = (size / px_per_pt) / _PT_PER_MM
        circumference_mm = math.pi * max(diameter_mm, 1e-6)
        pitch_mm = max(dash_mm, 0.2)
        count = int(round(circumference_mm / pitch_mm))
        count = max(24, min(360, count))

        step = 360.0 / count
        drawn = step * (1.0 - RING_GAP_RATIO)
        for i in range(count):
            start_angle = i * step
            draw.arc(
                box,
                start=start_angle,
                end=start_angle + drawn,
                fill=colour,
                width=stroke,
            )

    if factor > 1:
        ring = ring.resize((size, size), Image.Resampling.LANCZOS)

    result = result.copy()
    result.alpha_composite(ring)
    return result


# ---------------------------------------------------------------------------
# Leader line drawing (used by standalone rendering, not PDF compositor)
# ---------------------------------------------------------------------------

def draw_leader_line(
    page_image: Image.Image,
    circle_center: tuple[float, float],
    circle_radius: float,
    anchor_page_pos: tuple[float, float],
    leader_style: str = "dashed",
    line_color: tuple = (107, 63, 105),
    line_width: int = 2,
) -> Image.Image:
    """Draw a leader line from the circle's edge to the anchor point.

    The line starts from the edge of the circle (not the center) and
    extends to the target position.
    """
    result = page_image.copy()
    draw = ImageDraw.Draw(result)

    cx, cy = circle_center
    ax, ay = anchor_page_pos

    dx = ax - cx
    dy = ay - cy
    dist = math.sqrt(dx * dx + dy * dy)

    if dist < circle_radius + 1:
        return result

    start_x = cx + (dx / dist) * circle_radius
    start_y = cy + (dy / dist) * circle_radius

    if leader_style == "solid":
        draw.line(
            [(start_x, start_y), (ax, ay)],
            fill=line_color,
            width=line_width,
        )
    else:
        _draw_dashed_line(draw, start_x, start_y, ax, ay,
                          line_color, line_width, dash_len=12, gap_len=6)

    return result


def _draw_dashed_line(
    draw: ImageDraw.ImageDraw,
    x1: float, y1: float,
    x2: float, y2: float,
    color: tuple,
    width: int,
    dash_len: int = 12,
    gap_len: int = 6,
) -> None:
    """Draw a dashed line segment."""
    dx = x2 - x1
    dy = y2 - y1
    length = math.sqrt(dx * dx + dy * dy)
    if length < 1:
        return

    ux = dx / length
    uy = dy / length

    pos = 0.0
    drawing = True
    while pos < length:
        seg = dash_len if drawing else gap_len
        end_pos = min(pos + seg, length)

        if drawing:
            sx = x1 + ux * pos
            sy = y1 + uy * pos
            ex = x1 + ux * end_pos
            ey = y1 + uy * end_pos
            draw.line([(sx, sy), (ex, ey)], fill=color, width=width)

        pos = end_pos
        drawing = not drawing


# ---------------------------------------------------------------------------
# Callout label rendering
# ---------------------------------------------------------------------------

def render_callout_label(
    image: Image.Image,
    position: tuple[float, float],
    callout_id: str,
    description: str,
    text_color: tuple = (107, 63, 105),
    font_size_id: int = 16,
    font_size_desc: int = 12,
) -> Image.Image:
    """Render the callout ID (e.g. "DETAIL A") and description text.

    Uses the bundled DejaVu Sans font for reliable rendering on all
    machines, rather than depending on OS font resolution.
    """
    result = image.copy()
    draw = ImageDraw.Draw(result)

    font_id = _get_bundled_font(font_size_id)
    font_desc = _get_bundled_font(font_size_desc)

    x, y = position

    # Draw callout ID (bold-ish — draw twice offset by 1px)
    draw.text((x, y), callout_id, fill=text_color, font=font_id)
    draw.text((x + 1, y), callout_id, fill=text_color, font=font_id)

    # Draw description below
    id_bbox = draw.textbbox((x, y), callout_id, font=font_id)
    desc_y = id_bbox[3] + 4
    draw.text((x, desc_y), description, fill=text_color, font=font_desc)

    return result
