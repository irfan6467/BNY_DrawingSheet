"""
The single place a PIL image becomes a QPixmap.

Three copies of this conversion used to live in main_window, staging_tray
and component_review_dialog, and all three built the QImage without
telling Qt how many bytes a row of the PIL buffer actually occupies.
Qt pads every scanline out to a 4-byte boundary, so for any RGB image
whose width is not a multiple of four it read one to three bytes too far
on every row: the picture sheared progressively across the frame and the
last row ran off the end of the buffer.  That is the greyish, distorted
image - and, when the overrun crossed a page boundary, the crash.

Two of the three copies also handed Qt a temporary ``bytes`` object and
never copied the result, so the pixels were freed the moment the function
returned.

Everything here is deliberate about three things:

``bytesPerLine``
    Always passed, always the PIL buffer's own stride.

``.copy()``
    Always taken while the source buffer is still alive, so the QImage
    owns its pixels rather than pointing into a dead Python object.

Size
    A component rendered from CAD at 300 DPI arrives 3600 px square -
    39 MB as PIL, another 52 MB as a 32-bit QPixmap, per item.  A dozen
    of those is enough to exhaust a 32-bit-ish address space and take the
    process with it.  ``MAX_PIXMAP_EDGE`` caps what reaches the GPU while
    the full-resolution PIL image stays in the model for the PDF, which
    is the only place the extra detail was ever used.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - Pillow is a hard dependency
    Image = None
    ImageOps = None


# Longest edge, in pixels, of a pixmap handed to the canvas.  4096 is the
# smallest maximum texture size in common use, and an A1 sheet at 200 DPI
# is 4677 px wide, so nothing on screen can show more detail than this.
MAX_PIXMAP_EDGE = 4096

# Pillow refuses images beyond this many pixels as a decompression-bomb
# guard.  Left at Pillow's default the app dies on a large legitimate
# scan; raised to an explicit ceiling it refuses politely instead.
MAX_IMAGE_PIXELS = 250_000_000


def configure_pillow_limits() -> None:
    """Raise Pillow's bomb guard to a ceiling we choose ourselves."""
    if Image is not None:
        Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


def normalise(pil_img, apply_exif: bool = True):
    """Return *pil_img* in a mode Qt can take, upright if EXIF says so.

    Palette images, greyscale, CMYK scans and 16-bit TIFFs all arrive
    from ``Image.open`` in modes that have no QImage equivalent.  Passing
    one straight to ``tobytes("raw", "RGB")`` produces a buffer whose
    length does not match what Qt expects - which is the other half of
    the distorted-image report.
    """
    if pil_img is None:
        return None

    img = pil_img

    # A phone photo records its orientation in EXIF rather than in the
    # pixels; without this it lands on the sheet on its side.
    if apply_exif and ImageOps is not None:
        try:
            img = ImageOps.exif_transpose(img) or img
        except Exception:
            img = pil_img

    if img.mode in ("RGB", "RGBA"):
        return img
    if img.mode in ("LA", "PA") or (
        img.mode == "P" and "transparency" in getattr(img, "info", {})
    ):
        return img.convert("RGBA")
    return img.convert("RGB")


def pil_to_qimage(pil_img) -> QImage:
    """Convert a PIL image to a QImage that owns its own pixels."""
    if pil_img is None:
        return QImage()

    img = normalise(pil_img)
    if img.mode == "RGBA":
        fmt = QImage.Format.Format_RGBA8888
        raw = "RGBA"
        channels = 4
    else:
        fmt = QImage.Format.Format_RGB888
        raw = "RGB"
        channels = 3

    data = img.tobytes("raw", raw)
    bytes_per_line = img.width * channels

    # The copy has to happen here, while `data` is still referenced by
    # this frame.  A QImage built on a Python buffer does not take
    # ownership of it.
    qimg = QImage(data, img.width, img.height, bytes_per_line, fmt).copy()
    return qimg


def pil_to_qpixmap(pil_img, max_size: int = 0) -> QPixmap:
    """Convert a PIL image to a QPixmap, downscaled to fit *max_size*.

    ``max_size`` is the longest edge to allow; 0 means the module-wide
    ``MAX_PIXMAP_EDGE``.  Scaling is done in Pillow, with a proper
    averaging filter, before the pixels ever reach Qt: it halves the peak
    memory and keeps hairlines in a CAD render from dropping out.
    """
    if pil_img is None:
        return QPixmap()

    limit = max_size if max_size > 0 else MAX_PIXMAP_EDGE

    try:
        img = normalise(pil_img)
        longest = max(img.width, img.height)
        if limit > 0 and longest > limit:
            scale = limit / float(longest)
            img = img.resize(
                (max(1, int(round(img.width * scale))),
                 max(1, int(round(img.height * scale)))),
                Image.Resampling.LANCZOS,
            )
        return QPixmap.fromImage(pil_to_qimage(img))
    except (MemoryError, OSError, ValueError):
        # A truncated or absurdly large file should cost the architect
        # one thumbnail, not the session.
        return QPixmap()


def placeholder_pixmap(size: int, colour: str) -> QPixmap:
    """A flat square, for an asset whose image could not be read."""
    from PySide6.QtGui import QColor

    pm = QPixmap(max(1, size), max(1, size))
    pm.fill(QColor(colour))
    return pm


# ---------------------------------------------------------------------------
# Canvas pixmap cache
# ---------------------------------------------------------------------------

class PixmapCache:
    """Reuse pixmaps across canvas rebuilds.

    ``_rebuild_canvas_items`` runs on every undo, redo and crop, and it
    used to re-convert every placed image from PIL each time.  On a sheet
    carrying a handful of CAD renders that is hundreds of megabytes of
    churn per keystroke, which is what made Ctrl+Z feel like a hang.
    """

    def __init__(self, max_entries: int = 48):
        self._entries: dict[tuple, QPixmap] = {}
        self._order: list[tuple] = []
        self._max_entries = max_entries

    def get(self, key: tuple) -> Optional[QPixmap]:
        pm = self._entries.get(key)
        if pm is not None:
            # Move to the back of the eviction queue.
            try:
                self._order.remove(key)
            except ValueError:
                pass
            self._order.append(key)
        return pm

    def put(self, key: tuple, pixmap: QPixmap) -> QPixmap:
        if key in self._entries:
            try:
                self._order.remove(key)
            except ValueError:
                pass
        self._entries[key] = pixmap
        self._order.append(key)
        while len(self._order) > self._max_entries:
            evicted = self._order.pop(0)
            self._entries.pop(evicted, None)
        return pixmap

    def discard_asset(self, asset_id: str) -> None:
        """Drop every entry derived from one asset."""
        stale = [k for k in self._entries if k and k[0] == asset_id]
        for key in stale:
            self._entries.pop(key, None)
            try:
                self._order.remove(key)
            except ValueError:
                pass

    def clear(self) -> None:
        self._entries.clear()
        self._order.clear()
