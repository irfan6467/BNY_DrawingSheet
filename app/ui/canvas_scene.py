"""
QGraphicsScene for the technical drawing sheet canvas — WYSIWYG mode.

The canvas permanently shows the real template PDF as its background.
Placed items (images, callout circles) are draggable/resizable overlays
positioned in PDF-point coordinates.  Guide rectangles from the
calibration JSON are rendered as faint dashed outlines for optional
snap behavior.

The scene coordinate system maps 1:1 to PDF points via a stored scale
factor set when the template background is loaded.
"""

from __future__ import annotations

import math
import uuid
from typing import Optional

from PySide6.QtCore import Qt, QRectF, QPointF, Signal, QMimeData
from PySide6.QtGui import (
    QBrush, QColor, QPen, QPixmap, QImage, QPainter, QFont,
    QTransform, QCursor, QPainterPath, QGuiApplication,
)
from PySide6.QtWidgets import (
    QGraphicsScene,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsEllipseItem,
    QGraphicsLineItem,
    QGraphicsTextItem,
    QGraphicsItem,
    QGraphicsSceneMouseEvent,
    QGraphicsSceneDragDropEvent,
    QMenu,
)

from app.ui.theme import THEME
from app.ui.annotation_items import (
    ViewLabelItem, DetailNoteItem, SheetTitleItem,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SNAP_THRESHOLD_PTS = 10.0   # magnetic snap distance in PDF points
GUIDE_Z = -500              # z-order for guide rectangles
TEMPLATE_BG_Z = -1000       # z-order for template background
PLACED_ITEM_BASE_Z = 10     # z-order base for placed items
PREVIEW_IMAGE_Z = 500       # z-order for the crop-source preview overlay
CROP_OVERLAY_Z = 600        # z-order for crop-box overlays (above the preview)
ANCHOR_Z = 650              # z-order for anchor overlays
GUIDE_OVERLAY_Z = 900       # smart guides sit above what they measure

# The app accent is the same yellow as the selection highlight, so the
# guides carry their own colours: magenta for "these edges line up",
# teal for "this is the gap".
GUIDE_ALIGN_COLOUR = "#E0409A"
GUIDE_MEASURE_COLOUR = "#16B8A8"
GUIDE_TICK_PX = 9.0
LEADER_Z = 5                # z-order for leader lines (below images)

# How far the rotate grip snaps without a modifier.  15 degrees is the
# drafting convention; the 45 this used to use put every angle except
# the diagonals out of reach unless Ctrl was held.
ROTATION_SNAP_DEGREES = 15.0


# ---------------------------------------------------------------------------
# Resize Handle (shared by crop boxes and placed items)
# ---------------------------------------------------------------------------

class ResizeHandle(QGraphicsEllipseItem):
    """A single 8×8 resize handle for resizable items."""

    def __init__(self, position_flags: str, parent=None):
        super().__init__(-5, -5, 10, 10, parent)
        self.position_flags = position_flags

        self.setBrush(QBrush(QColor(THEME["accent_selection"])))
        self.setPen(QPen(QColor("#141414"), 2))
        self.setZValue(200)

        # Handles keep their size in screen pixels, so they stay grabbable
        # when zoomed out and don't balloon when zoomed in.
        self.setFlag(
            QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True
        )

        if position_flags in ("top_left", "bottom_right"):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif position_flags in ("top_right", "bottom_left"):
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)
        elif position_flags in ("top", "bottom"):
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        elif position_flags in ("left", "right"):
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif position_flags == "rotate":
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            # Make it circular
            self.setRect(-5, -5, 10, 10)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        parent = self.parentItem()
        if isinstance(parent, ResizableItem):
            parent.start_resize(self.position_flags, event.scenePos())
        event.accept()

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        parent = self.parentItem()
        if isinstance(parent, ResizableItem):
            parent.do_resize(self.position_flags, event.scenePos())
        event.accept()

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        parent = self.parentItem()
        if isinstance(parent, ResizableItem):
            parent.end_resize()
        event.accept()


# ---------------------------------------------------------------------------
# ResizableItem — base class with 8-handle resize logic
# ---------------------------------------------------------------------------

class ResizableItem(QGraphicsRectItem):
    """Base class providing 8-handle resize and drag functionality.
    Used by both CropBoxItem and PlacedItemGraphicsItem."""

    def __init__(self, rect: QRectF, keep_aspect_ratio: bool = False,
                 parent=None, allow_rotate: bool = True):
        super().__init__(rect, parent)

        self._keep_aspect_ratio = keep_aspect_ratio
        self._allow_rotate = allow_rotate
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)

        self._is_resizing = False
        self._resize_origin = QPointF()
        self._original_rect = QRectF()

        self._handles: dict[str, ResizeHandle] = {
            "top_left": ResizeHandle("top_left", self),
            "top_right": ResizeHandle("top_right", self),
            "bottom_right": ResizeHandle("bottom_right", self),
            "bottom_left": ResizeHandle("bottom_left", self),
        }
        if allow_rotate:
            # A crop selection has no meaningful rotation - only content does.
            self._handles["rotate"] = ResizeHandle("rotate", self)
        self._update_handles()

    def _update_handles(self) -> None:
        """Update positions of the 4 corner handles relative to current rect."""
        r = self.rect()
        self._handles["top_left"].setPos(r.topLeft())
        self._handles["top_right"].setPos(r.topRight())
        self._handles["bottom_right"].setPos(r.bottomRight())
        self._handles["bottom_left"].setPos(r.bottomLeft())
        rotate = self._handles.get("rotate")
        if rotate is not None:
            rotate.setPos(r.center().x(), r.top() - 25)
            # Keep rotate handle visually distinct
            rotate.setBrush(QBrush(QColor(THEME["accent_primary"])))
        self.setTransformOriginPoint(r.center())

    def set_handles_visible(self, visible: bool) -> None:
        """Show/hide the 8 resize handles."""
        for h in self._handles.values():
            h.setVisible(visible)

    def setRect(self, rect: QRectF) -> None:
        super().setRect(rect)
        self._update_handles()

    def start_resize(self, flags: str, scene_pos: QPointF) -> None:
        self._is_resizing = True
        self._original_rect = self.rect()
        self._original_rot = self.rotation()
        self._resize_origin = self.mapFromScene(scene_pos)

    def do_resize(self, flags: str, scene_pos: QPointF) -> None:
        if not self._is_resizing:
            return

        if flags == "rotate":
            # Calculate angle from center to mouse
            center = self.mapToScene(self._original_rect.center())
            dy = scene_pos.y() - center.y()
            dx = scene_pos.x() - center.x()
            angle = math.degrees(math.atan2(dy, dx)) + 90

            # Snap unless Ctrl is held, the same key that turns off the
            # magnetic guides.  15 degrees, not the 45 this used to use:
            # at 45 the only angles reachable without a modifier were the
            # diagonals, which is no use for setting a section on a slope.
            if not (QGuiApplication.keyboardModifiers()
                    & Qt.KeyboardModifier.ControlModifier):
                angle = round(angle / ROTATION_SNAP_DEGREES) * ROTATION_SNAP_DEGREES

            # Keep it in 0-360 so the number shown and stored is readable.
            angle %= 360.0

            self.setRotation(angle)

            # Notify scene for group rotation
            if isinstance(self, PlacedItemGraphicsItem):
                scene = self.scene()
                if hasattr(scene, "sync_group_rotation"):
                    scene.sync_group_rotation(self.placed_item_id, angle)
            return

        pos = self.mapFromScene(scene_pos)
        r = QRectF(self._original_rect)

        if self._keep_aspect_ratio and r.height() > 0.1:
            orig_aspect = r.width() / r.height()
            if flags == "top_left":
                new_w = max(10.0, r.right() - pos.x())
                new_h = new_w / orig_aspect
                r.setLeft(r.right() - new_w)
                r.setTop(r.bottom() - new_h)
            elif flags == "top_right":
                new_w = max(10.0, pos.x() - r.left())
                new_h = new_w / orig_aspect
                r.setRight(r.left() + new_w)
                r.setTop(r.bottom() - new_h)
            elif flags == "bottom_left":
                new_w = max(10.0, r.right() - pos.x())
                new_h = new_w / orig_aspect
                r.setLeft(r.right() - new_w)
                r.setBottom(r.top() + new_h)
            elif flags == "bottom_right":
                new_w = max(10.0, pos.x() - r.left())
                new_h = new_w / orig_aspect
                r.setRight(r.left() + new_w)
                r.setBottom(r.top() + new_h)
        else:
            if "top" in flags:
                r.setTop(min(pos.y(), r.bottom() - 10))
            if "bottom" in flags:
                r.setBottom(max(pos.y(), r.top() + 10))
            if "left" in flags:
                r.setLeft(min(pos.x(), r.right() - 10))
            if "right" in flags:
                r.setRight(max(pos.x(), r.left() + 10))

        self.setRect(r)

    def end_resize(self) -> None:
        self._is_resizing = False


# ---------------------------------------------------------------------------
# CropBoxItem — visual crop-box selection overlay
# ---------------------------------------------------------------------------

class CropBoxItem(ResizableItem):
    """Visual representation of a crop-box selection on the canvas.

    A circular selection is drawn as an actual circle with centre
    crosshairs; a rectangular one as a dashed box with thirds guides.
    Either can be dragged and resized after it is drawn, and a circular
    one stays square while you do.
    """

    MIN_SIDE = 8.0   # scene units - below this a crop is not useful

    def __init__(self, rect: QRectF, is_circular: bool = False, parent=None):
        if is_circular:
            rect = self._squared(rect)
        super().__init__(
            rect,
            keep_aspect_ratio=is_circular,
            parent=parent,
            allow_rotate=False,
        )
        self.is_circular = is_circular

        colour = QColor(THEME["accent_selection"])
        self._outline = colour
        self._fill = QColor(colour)
        self._fill.setAlpha(30 if is_circular else 40)

        # Qt still uses these for the bounding rect and hit testing even
        # though paint() below does the drawing.
        self.setPen(QPen(colour, 2))
        self.setBrush(QBrush(self._fill))
        self.setZValue(CROP_OVERLAY_Z)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    # ── Geometry ──────────────────────────────────────────────────────

    @staticmethod
    def _squared(rect: QRectF) -> QRectF:
        """The largest square sharing ``rect``'s centre."""
        side = min(rect.width(), rect.height())
        centre = rect.center()
        return QRectF(
            centre.x() - side / 2.0,
            centre.y() - side / 2.0,
            side,
            side,
        )

    def setRect(self, rect: QRectF) -> None:
        if self.is_circular:
            rect = self._squared(rect)
        super().setRect(rect)
        self._notify_scene()

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._notify_scene()
        return super().itemChange(change, value)

    def end_resize(self) -> None:
        super().end_resize()
        self._notify_scene()

    def _notify_scene(self) -> None:
        """Republish the crop rect so a box that was nudged or resized
        after being drawn is the one that actually gets used."""
        scene = self.scene()
        if scene is not None and hasattr(scene, "notify_crop_box_changed"):
            scene.notify_crop_box_changed(self)

    def scene_rect(self) -> QRectF:
        """This box's rect in scene coordinates."""
        return self.sceneTransform().mapRect(self.rect())

    # ── Painting ──────────────────────────────────────────────────────

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        if self.is_circular:
            path.addEllipse(self.rect())
        else:
            path.addRect(self.rect())
        return path

    def paint(self, painter: QPainter, option, widget=None) -> None:
        r = self.rect()
        if r.isEmpty():
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Cosmetic pens hold their width in screen pixels, so the outline
        # does not turn into a hairline when zoomed out or a slab when in.
        outline = QPen(self._outline, 2)
        outline.setCosmetic(True)
        guide = QPen(QColor(self._outline), 1, Qt.PenStyle.DashLine)
        guide.setCosmetic(True)

        painter.setBrush(QBrush(self._fill))

        if self.is_circular:
            painter.setPen(outline)
            painter.drawEllipse(r)

            # Crosshairs: the detail is aimed by its centre, so show it.
            centre = r.center()
            painter.setPen(guide)
            painter.drawLine(QPointF(r.left(), centre.y()),
                             QPointF(r.right(), centre.y()))
            painter.drawLine(QPointF(centre.x(), r.top()),
                             QPointF(centre.x(), r.bottom()))
        else:
            outline.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(outline)
            painter.drawRect(r)

            # Rule-of-thirds guides help line a crop up with the drawing.
            painter.setPen(guide)
            for i in (1, 2):
                x = r.left() + r.width() * i / 3.0
                y = r.top() + r.height() * i / 3.0
                painter.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
                painter.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))

        painter.restore()


# ---------------------------------------------------------------------------
# PlacedItemGraphicsItem — a placed image/callout on the canvas
# ---------------------------------------------------------------------------

class PlacedItemGraphicsItem(ResizableItem):
    """A placed image or callout circle on the WYSIWYG canvas.

    This is the visual representation of a PlacedItem from the data model.
    The item is draggable and resizable; its position/size in scene
    coordinates maps directly to PDF-point coordinates via the scene's
    scale factor.
    """

    # Shrink the working copy only once the source is this much larger
    # than the space it occupies on screen.
    LOD_SLACK = 1.4

    def __init__(self, pixmap: QPixmap, rect: QRectF, placed_item_id: str,
                 parent=None):
        super().__init__(rect, keep_aspect_ratio=True, parent=parent)
        self._placing_programmatically = True
        self.placed_item_id = placed_item_id
        self._source_pixmap = pixmap
        self._lod_pixmap: Optional[QPixmap] = None
        self._lod_key: Optional[tuple] = None

        self.setPen(QPen(Qt.PenStyle.NoPen))
        self._placing_programmatically = False
        self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.setZValue(PLACED_ITEM_BASE_Z)

        # The pixmap child item renders the actual image
        self._pixmap_item = QGraphicsPixmapItem(self)
        self._pixmap_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._pixmap_item.setPixmap(self._source_pixmap)
        self._pixmap_item.setZValue(-1)  # Below the handle layer
        self._update_pixmap_transform()

        # Handles hidden until selected
        self.set_handles_visible(False)

    def _view_scale(self) -> float:
        """Scene-to-screen scale of the view showing this item."""
        scene = self.scene()
        if scene is None:
            return 1.0
        views = scene.views()
        if not views:
            return 1.0
        return abs(views[0].transform().m11()) or 1.0

    def _update_pixmap_transform(self) -> None:
        """Scale the pixmap to fill the current rect.

        A CAD render is often several thousand pixels wide and lands in a
        frame a few hundred wide.  Leaving that minification to the item
        transform makes Qt point-sample a handful of texels per output
        pixel, so hairlines fall between samples and the drawing looks
        washed out until you zoom in.  Pre-shrinking with a proper
        averaging filter keeps those lines dark.
        """
        r = self.rect()
        if r.width() < 1 or r.height() < 1:
            return
        pw = self._source_pixmap.width()
        ph = self._source_pixmap.height()
        if pw < 1 or ph < 1:
            return

        # How many device pixels the item actually occupies.
        view_scale = self._view_scale()
        target_w = max(1, int(round(r.width() * view_scale)))
        target_h = max(1, int(round(r.height() * view_scale)))

        # Only worth doing when the source really is bigger than the space
        # it has to live in; never upscale, that only costs memory.
        if pw > target_w * self.LOD_SLACK and ph > target_h * self.LOD_SLACK:
            # Snap to a power-of-two ladder so small zoom nudges reuse the
            # cached copy instead of rescaling on every tick.
            # Round the reduction DOWN to the ladder step, so the working
            # copy is never smaller than the space it has to fill - one
            # step too far and it gets scaled back up and goes soft.
            step = 2.0 ** math.floor(math.log2(max(pw / target_w, 1.0)))
            cache_w = max(1, int(pw / step))
            cache_h = max(1, int(ph / step))
            if self._lod_key != (cache_w, cache_h):
                self._lod_key = (cache_w, cache_h)
                self._lod_pixmap = self._source_pixmap.scaled(
                    cache_w, cache_h,
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._pixmap_item.setPixmap(self._lod_pixmap)
            shown = self._lod_pixmap
        else:
            if self._lod_key is not None:
                self._lod_key = None
                self._lod_pixmap = None
                self._pixmap_item.setPixmap(self._source_pixmap)
            shown = self._source_pixmap

        sw = max(1, shown.width())
        sh = max(1, shown.height())
        self._pixmap_item.setTransform(
            QTransform.fromScale(r.width() / sw, r.height() / sh)
        )
        self._pixmap_item.setPos(r.topLeft())

    def setRect(self, rect: QRectF) -> None:
        super().setRect(rect)
        self._update_pixmap_transform()

    def update_pixmap(self, pixmap: QPixmap) -> None:
        """Replace the displayed pixmap (e.g. after a crop change)."""
        self._source_pixmap = pixmap
        self._lod_pixmap = None
        self._lod_key = None
        self._pixmap_item.setPixmap(pixmap)
        self._update_pixmap_transform()

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and self.scene():
            scene = self.scene()
            if hasattr(scene, "update_alignment_guides"):
                scene.update_alignment_guides(self, value)
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedChange:
            self.set_handles_visible(bool(value))
            if value:
                # Selection highlight
                self.setPen(QPen(QColor(THEME["accent_selection"]), 2, Qt.PenStyle.SolidLine))
            else:
                self.setPen(QPen(Qt.PenStyle.NoPen))
        return super().itemChange(change, value)

    def shape(self) -> QPainterPath:
        # Override shape so the entire interior is clickable despite having NoBrush
        path = QPainterPath()
        path.addRect(self.rect())
        return path

    def end_resize(self) -> None:
        super().end_resize()
        scene = self.scene()
        if hasattr(scene, "item_resized"):
            scene.item_resized.emit(self.placed_item_id)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        scene = self.scene()
        if hasattr(scene, "clear_alignment_guides"):
            scene.clear_alignment_guides()
        if hasattr(scene, "item_moved"):
            scene.item_moved.emit(self.placed_item_id)
            
        # Also notify group movement
        if hasattr(scene, "sync_group_position"):
            scene.sync_group_position(self.placed_item_id)

    def get_page_rect_pts(self, scale_factor: float) -> tuple:
        """Return the current unrotated frame in PDF points (x, y, w, h).

        ``page_rect`` is the frame *before* rotation - both the compositor
        and the canvas turn the item about that rect's own centre, using
        the separate ``rotation`` field.  Reading it back with
        ``sceneTransform().mapRect()``, as this used to, returns the
        axis-aligned bounding box of the turned rect instead: for a view
        at 45 degrees that is half again as wide.  Every drag then wrote
        the larger box back into the model, and the next rebuild turned
        *that* - so a rotated view grew a little more each time it was
        touched, and printed bigger than it looked.

        Rotation is about the rect's centre, so the centre is the one
        point it leaves alone; the frame is rebuilt around it.
        """
        r = self.rect()
        centre = self.mapToScene(r.center())

        # Any scale actually applied to the item, rotation factored out.
        transform = self.sceneTransform()
        scale_x = math.hypot(transform.m11(), transform.m12()) or 1.0
        scale_y = math.hypot(transform.m21(), transform.m22()) or 1.0

        width = r.width() * scale_x
        height = r.height() * scale_y

        return (
            (centre.x() - width / 2.0) * scale_factor,
            (centre.y() - height / 2.0) * scale_factor,
            width * scale_factor,
            height * scale_factor,
        )


# ---------------------------------------------------------------------------
# LeaderLineItem — draggable leader line for callouts
# ---------------------------------------------------------------------------

class LeaderLineItem(QGraphicsLineItem):
    """A leader line connecting a callout circle to its target point.
    The far end (arrow tip) is draggable."""

    def __init__(self, start: QPointF, end: QPointF, style: str = "dashed", parent=None):
        super().__init__(start.x(), start.y(), end.x(), end.y(), parent)
        self.placed_item_id: str = ""  # set by caller

        color = QColor(THEME.get("accent_primary", "#6B3F69"))
        pen = QPen(color, 2)
        if style == "dashed":
            pen.setDashPattern([6, 4])
        self.setPen(pen)
        self.setZValue(LEADER_Z)

        # Arrow-tip handle at the far end
        self._tip_handle = QGraphicsEllipseItem(-5, -5, 10, 10, self)
        self._tip_handle.setBrush(QBrush(color))
        self._tip_handle.setPen(QPen(Qt.PenStyle.NoPen))
        self._tip_handle.setPos(end)
        self._tip_handle.setCursor(Qt.CursorShape.CrossCursor)
        self._tip_handle.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)

    def update_start(self, pos: QPointF) -> None:
        line = self.line()
        self.setLine(pos.x(), pos.y(), line.x2(), line.y2())

    def update_end(self, pos: QPointF) -> None:
        line = self.line()
        self.setLine(line.x1(), line.y1(), pos.x(), pos.y())
        self._tip_handle.setPos(pos)

    def get_end_pos(self) -> QPointF:
        """Return the current position of the leader-line tip."""
        return self._tip_handle.pos()


# ---------------------------------------------------------------------------
# AnchorPointItem (kept for crop-source anchor marking)
# ---------------------------------------------------------------------------

class AnchorPointItem(QGraphicsEllipseItem):
    """Visual marker for a single-click anchor point placement."""

    RADIUS = 6

    def __init__(self, center: QPointF, parent=None):
        r = self.RADIUS
        super().__init__(center.x() - r, center.y() - r, r * 2, r * 2, parent)
        color = QColor(THEME["accent_selection"])
        self.setPen(QPen(color, 2, Qt.PenStyle.SolidLine))
        fill = QColor(color)
        fill.setAlpha(120)
        self.setBrush(QBrush(fill))
        self.setZValue(ANCHOR_Z)


# ---------------------------------------------------------------------------
# GuideRectItem — faint snap-guide outline
# ---------------------------------------------------------------------------

class GuideRectItem(QGraphicsRectItem):
    """A faint dashed rectangle from the calibration JSON, rendered as
    a visual snap guide.  Non-interactive."""

    def __init__(self, rect: QRectF, label: str = "", parent=None):
        super().__init__(rect, parent)
        self.label = label

        color = QColor(THEME.get("status_info", "#4ECDC4"))
        color.setAlpha(80)
        pen = QPen(color, 1, Qt.PenStyle.DashDotLine)
        self.setPen(pen)
        self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.setZValue(GUIDE_Z)

        # Not interactive
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

        # Label
        if label:
            text = QGraphicsTextItem(label, self)
            text.setDefaultTextColor(color)
            font = QFont("Segoe UI", 7)
            text.setFont(font)
            text.setPos(rect.topLeft() + QPointF(2, 2))


# ---------------------------------------------------------------------------
# CanvasScene — the main WYSIWYG scene
# ---------------------------------------------------------------------------

class CanvasScene(QGraphicsScene):
    """Scene managing the WYSIWYG canvas — template background, placed
    items, guide rectangles, crop boxes, and anchor points.

    Signals
    -------
    crop_box_created(QRectF)
        Emitted when a rectangle-drag crop-box selection completes.
    anchor_point_placed(QPointF)
        Emitted when a single-click anchor-point placement completes.
    item_placed(str)
        Emitted with placed_item_id when a new item is dropped on the canvas.
    item_moved(str)
        Emitted with placed_item_id when a placed item is dragged.
    item_resized(str)
        Emitted with placed_item_id when a placed item is resized.
    """

    crop_box_created = Signal(QRectF)
    anchor_point_placed = Signal(QPointF)
    annotation_moved = Signal(str)
    annotation_double_clicked = Signal(str)
    annotation_edit_began = Signal(str)
    annotation_edit_finished = Signal(str)
    item_placed = Signal(str)
    item_moved = Signal(str)
    item_resized = Signal(str)
    group_requested = Signal(list)
    item_ungroup_requested = Signal(str)
    group_moved = Signal(str)
    group_rotated = Signal(str, float)

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setBackgroundBrush(QBrush(QColor(THEME["bg_canvas"])))

        # Template background
        self._template_bg: Optional[QGraphicsPixmapItem] = None
        self._page_size_pts: tuple[float, float] = (0.0, 0.0)
        self._dpi: float = 200.0
        # Points per scene pixel.  This must always be 72/_dpi: pts_to_scene
        # multiplies by _dpi/72 and scene_to_pts multiplies by this, so the
        # two are only inverses when they agree.  It used to start at 1.0
        # against a _dpi of 200, which left them a factor of 2.78 apart
        # until a template loaded - so if the template PDF was missing, as
        # it is on a machine where the resources did not ship, every drop
        # landed nearly three times too far across the sheet.
        self._scale_factor: float = 72.0 / self._dpi

        # Placed items (graphics items, keyed by placed_item_id)
        self._placed_items: dict[str, PlacedItemGraphicsItem] = {}
        self._leader_lines: dict[str, LeaderLineItem] = {}

        # Guide rectangles
        self._guide_items: list[GuideRectItem] = []

        # Snap behaviour
        self._snap_enabled: bool = True
        
        self._active_alignment_lines: list[QGraphicsLineItem] = []

        # Crop/anchor overlays
        self._crop_boxes: list[CropBoxItem] = []
        self._anchor_points: list[AnchorPointItem] = []
        self._annotations: dict[str, object] = {}

        # Track the preview image (for source-asset cropping workflow)
        self._preview_image_item: Optional[QGraphicsPixmapItem] = None
        # Asset pixels per preview pixel; see show_preview_image.
        self._preview_scale: float = 1.0

        # What the last drop carried, read once by the main window.
        self._pending_drop_asset_id: Optional[str] = None
        self._pending_drop_scene_pos: Optional[QPointF] = None

    # ── Template background ───────────────────────────────────────────

    def set_template_background(self, pixmap: QPixmap, page_size_pts: tuple, dpi: float = 200.0) -> None:
        """Set the template PDF raster as the permanent canvas background.

        Parameters
        ----------
        pixmap : QPixmap
            The rasterized template page.
        page_size_pts : tuple
            (width, height) of the page in PDF points.
        dpi : float
            The DPI at which the pixmap was rasterized.
        """
        # Remove old background.  Reloading a template used to leave the
        # previous page's pixmap owned by nothing but still holding its
        # memory; an A1 page at 200 DPI is 60 MB of it.
        if self._template_bg is not None:
            self.removeItem(self._template_bg)
            self._template_bg.setPixmap(QPixmap())
            self._template_bg = None

        self._template_bg = self.addPixmap(pixmap)
        self._template_bg.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._template_bg.setZValue(TEMPLATE_BG_Z)
        self._template_bg.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self._template_bg.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self._template_bg.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

        self._page_size_pts = page_size_pts
        self._dpi = dpi
        # Scale factor: PDF points per scene pixel
        # At the given DPI, 1 PDF point = dpi/72 pixels
        # So 1 pixel = 72/dpi points
        self._scale_factor = 72.0 / dpi

        self.setSceneRect(QRectF(pixmap.rect()))

    def has_template(self) -> bool:
        """Whether a template background is loaded."""
        return self._template_bg is not None

    def pts_to_scene(self, x_pts: float, y_pts: float) -> QPointF:
        """Convert PDF points to scene coordinates."""
        px_per_pt = self._dpi / 72.0
        return QPointF(x_pts * px_per_pt, y_pts * px_per_pt)

    def scene_to_pts(self, x_scene: float, y_scene: float) -> tuple[float, float]:
        """Convert scene coordinates to PDF points."""
        return (x_scene * self._scale_factor, y_scene * self._scale_factor)

    def rect_pts_to_scene(self, x: float, y: float, w: float, h: float) -> QRectF:
        """Convert a PDF-point rect to scene coordinates."""
        tl = self.pts_to_scene(x, y)
        br = self.pts_to_scene(x + w, y + h)
        return QRectF(tl, br)

    def rect_scene_to_pts(self, rect: QRectF) -> tuple[float, float, float, float]:
        """Convert a scene rect to PDF-point rect (x, y, w, h)."""
        x, y = self.scene_to_pts(rect.x(), rect.y())
        x2, y2 = self.scene_to_pts(rect.x() + rect.width(), rect.y() + rect.height())
        return (x, y, x2 - x, y2 - y)


    def clear_alignment_guides(self) -> None:
        for line in self._active_alignment_lines:
            self.removeItem(line)
        self._active_alignment_lines.clear()
    # ── Guide rectangles ──────────────────────────────────────────────

    def load_guide_rects(self, guide_rects: dict) -> None:
        """Load guide rectangles from the template's guide_rects dict."""
        # Clear existing guides
        for item in self._guide_items:
            self.removeItem(item)
        self._guide_items.clear()

        for slot_id, rect_data in guide_rects.items():
            if isinstance(rect_data, list):
                for i, rd in enumerate(rect_data):
                    self._add_guide_rect(rd, f"{slot_id}[{i}]")
            elif isinstance(rect_data, dict):
                self._add_guide_rect(rect_data, slot_id)

    def _add_guide_rect(self, rd: dict, label: str) -> None:
        """Add a single guide rectangle."""
        scene_rect = self.rect_pts_to_scene(rd["x"], rd["y"], rd["w"], rd["h"])
        item = GuideRectItem(scene_rect, label)
        self.addItem(item)
        self._guide_items.append(item)

    # ── Snap logic ────────────────────────────────────────────────────

    def set_snap_enabled(self, enabled: bool) -> None:
        """Enable/disable magnetic snap to guide rectangles."""
        self._snap_enabled = enabled

    def snap_rect_to_guides(self, rect: QRectF) -> QRectF:
        """Snap a scene rect to nearby guide edges if within threshold.
        Returns the adjusted rect."""
        if not self._snap_enabled or not self._guide_items:
            return rect

        threshold = SNAP_THRESHOLD_PTS / self._scale_factor  # convert to scene pixels
        result = QRectF(rect)

        for guide in self._guide_items:
            gr = guide.rect()
            # Snap left edge
            if abs(result.left() - gr.left()) < threshold:
                result.moveLeft(gr.left())
            elif abs(result.left() - gr.right()) < threshold:
                result.moveLeft(gr.right())
            # Snap right edge
            if abs(result.right() - gr.right()) < threshold:
                delta = gr.right() - result.right()
                result.moveLeft(result.left() + delta)
            elif abs(result.right() - gr.left()) < threshold:
                delta = gr.left() - result.right()
                result.moveLeft(result.left() + delta)
            # Snap top edge
            if abs(result.top() - gr.top()) < threshold:
                result.moveTop(gr.top())
            elif abs(result.top() - gr.bottom()) < threshold:
                result.moveTop(gr.bottom())
            # Snap bottom edge
            if abs(result.bottom() - gr.bottom()) < threshold:
                delta = gr.bottom() - result.bottom()
                result.moveTop(result.top() + delta)
            elif abs(result.bottom() - gr.top()) < threshold:
                delta = gr.top() - result.bottom()
                result.moveTop(result.top() + delta)

        return result


    # ── Smart guides ──────────────────────────────────────────────────

    def _guide_targets(self, moving_item):
        """Everything a dragged item can line up against."""
        targets = []
        for item in self._placed_items.values():
            if item is not moving_item:
                targets.append(item)
        for item in self._annotations.values():
            if item is not moving_item:
                targets.append(item)
        return targets

    def _add_guide_line(self, x1, y1, x2, y2, colour, dashed=True):
        line = QGraphicsLineItem(x1, y1, x2, y2)
        pen = QPen(QColor(colour), 1.2 if dashed else 1.8,
                   Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine)
        pen.setCosmetic(True)   # a guide has to stay visible at any zoom
        line.setPen(pen)
        line.setZValue(GUIDE_OVERLAY_Z)
        self.addItem(line)
        self._active_alignment_lines.append(line)
        return line

    def _add_guide_label(self, text, x, y, colour):
        label = QGraphicsTextItem(text)
        label.setDefaultTextColor(QColor(colour))
        font = QFont("Arial")
        font.setPixelSize(11)
        label.setFont(font)
        label.setFlag(
            QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True
        )
        label.setPos(x, y)
        label.setZValue(GUIDE_OVERLAY_Z + 1)
        self.addItem(label)
        self._active_alignment_lines.append(label)
        return label

    def _guide_rect_for(self, item, new_pos):
        """An item scene rect, optionally at the position it is moving to.

        ``new_pos`` is the item position Qt is about to apply, so the
        answer is the local rect shifted by it.  Moving the rect *to*
        that point instead would drag it to the origin and report gaps
        short by the item own offset.
        """
        try:
            local = item.rect() if hasattr(item, "rect") else item.boundingRect()
            if new_pos is not None:
                return local.translated(new_pos)
            if hasattr(item, "rect"):
                return item.sceneTransform().mapRect(item.rect())
            return item.sceneBoundingRect()
        except Exception:
            return None

    def update_alignment_guides(self, moving_item, new_pos: QPointF) -> None:
        """Draw the guides for an item being dragged.

        Two kinds: edge and centre lines showing what the item is lining
        up with, and a measured line carrying the gap in sheet points to
        whatever sits nearest on each side.
        """
        self.clear_alignment_guides()
        if not self._snap_enabled:
            return

        r1 = self._guide_rect_for(moving_item, new_pos)
        if r1 is None or r1.isEmpty():
            return

        threshold = SNAP_THRESHOLD_PTS / max(self._scale_factor, 1e-9) * 0.5
        align_colour = GUIDE_ALIGN_COLOUR
        measure_colour = GUIDE_MEASURE_COLOUR

        rects = []
        for item in self._guide_targets(moving_item):
            r2 = self._guide_rect_for(item, None)
            if r2 is not None and not r2.isEmpty():
                rects.append(r2)

        self._draw_alignment_lines(r1, rects, threshold, align_colour)
        self._draw_spacing_guides(r1, rects, measure_colour)

    def _draw_alignment_lines(self, r1, rects, threshold, colour) -> None:
        """Edge and centre lines wherever the dragged item lines up."""
        seen_x, seen_y = set(), set()
        for r2 in rects:
            for x1 in (r1.left(), r1.center().x(), r1.right()):
                for x2 in (r2.left(), r2.center().x(), r2.right()):
                    if abs(x1 - x2) < threshold and round(x2, 1) not in seen_x:
                        seen_x.add(round(x2, 1))
                        self._add_guide_line(
                            x2, min(r1.top(), r2.top()) - 40,
                            x2, max(r1.bottom(), r2.bottom()) + 40, colour,
                        )
            for y1 in (r1.top(), r1.center().y(), r1.bottom()):
                for y2 in (r2.top(), r2.center().y(), r2.bottom()):
                    if abs(y1 - y2) < threshold and round(y2, 1) not in seen_y:
                        seen_y.add(round(y2, 1))
                        self._add_guide_line(
                            min(r1.left(), r2.left()) - 40, y2,
                            max(r1.right(), r2.right()) + 40, y2, colour,
                        )

    def _draw_spacing_guides(self, r1, rects, colour) -> None:
        """Measured gaps to the nearest neighbour on each side.

        Lining edges up is only half of placing a view; the other half is
        knowing how far it sits from its neighbours, so the gap is drawn
        with end ticks and labelled in sheet points.
        """
        def overlaps_rows(a, b):
            return a.top() < b.bottom() and b.top() < a.bottom()

        def overlaps_cols(a, b):
            return a.left() < b.right() and b.left() < a.right()

        left = right = above = below = None
        for r2 in rects:
            if overlaps_rows(r1, r2):
                if r2.right() <= r1.left():
                    if left is None or r2.right() > left.right():
                        left = r2
                elif r2.left() >= r1.right():
                    if right is None or r2.left() < right.left():
                        right = r2
            if overlaps_cols(r1, r2):
                if r2.bottom() <= r1.top():
                    if above is None or r2.bottom() > above.bottom():
                        above = r2
                elif r2.top() >= r1.bottom():
                    if below is None or r2.top() < below.top():
                        below = r2

        for neighbour in (left, right):
            if neighbour is None:
                continue
            top = max(r1.top(), neighbour.top())
            y = top + (min(r1.bottom(), neighbour.bottom()) - top) / 2.0
            if neighbour.right() <= r1.left():
                x_from, x_to = neighbour.right(), r1.left()
            else:
                x_from, x_to = r1.right(), neighbour.left()
            gap = abs(x_to - x_from)
            if gap < 1e-6:
                continue
            self._add_guide_line(x_from, y, x_to, y, colour, dashed=False)
            tick = GUIDE_TICK_PX / max(self._scale_factor, 1e-9) * 0.5
            self._add_guide_line(x_from, y - tick, x_from, y + tick, colour, dashed=False)
            self._add_guide_line(x_to, y - tick, x_to, y + tick, colour, dashed=False)
            self._add_guide_label(
                "%.0f" % self.scene_to_pts(gap, 0)[0],
                (x_from + x_to) / 2.0, y, colour,
            )

        for neighbour in (above, below):
            if neighbour is None:
                continue
            left_edge = max(r1.left(), neighbour.left())
            x = left_edge + (min(r1.right(), neighbour.right()) - left_edge) / 2.0
            if neighbour.bottom() <= r1.top():
                y_from, y_to = neighbour.bottom(), r1.top()
            else:
                y_from, y_to = r1.bottom(), neighbour.top()
            gap = abs(y_to - y_from)
            if gap < 1e-6:
                continue
            self._add_guide_line(x, y_from, x, y_to, colour, dashed=False)
            tick = GUIDE_TICK_PX / max(self._scale_factor, 1e-9) * 0.5
            self._add_guide_line(x - tick, y_from, x + tick, y_from, colour, dashed=False)
            self._add_guide_line(x - tick, y_to, x + tick, y_to, colour, dashed=False)
            self._add_guide_label(
                "%.0f" % self.scene_to_pts(0, gap)[1],
                x, (y_from + y_to) / 2.0, colour,
            )

    # ── Placed items ──────────────────────────────────────────────────

    def add_placed_item(self, pixmap: QPixmap, page_rect_pts: tuple,
                        placed_item_id: str) -> PlacedItemGraphicsItem:
        """Add a placed item to the canvas at the given PDF-point rect.

        Parameters
        ----------
        pixmap : QPixmap
            The raster image to display.
        page_rect_pts : tuple
            (x, y, w, h) in PDF points.
        placed_item_id : str
            The PlacedItem.id from the data model.
        """
        x, y, w, h = page_rect_pts
        scene_rect = self.rect_pts_to_scene(x, y, w, h)

        gfx_item = PlacedItemGraphicsItem(pixmap, scene_rect, placed_item_id)
        self.addItem(gfx_item)
        self._placed_items[placed_item_id] = gfx_item
        return gfx_item

    # ── Annotations (view labels, detail notes, sheet title) ──────────

    def add_annotation_item(self, annotation, scale_pts_to_scene) -> object:
        """Create the canvas item for an Annotation record.

        ``scale_pts_to_scene`` converts a PDF-point length into scene
        units so lettering keeps its printed size on screen.
        """
        self.remove_annotation_item(annotation.id)

        pos = self.pts_to_scene(annotation.page_pos[0], annotation.page_pos[1])
        font_px = scale_pts_to_scene(annotation.font_size)

        if annotation.kind == "view_label":
            item = ViewLabelItem(
                annotation.id, annotation.text, font_px,
                scale_pts_to_scene(annotation.rule_width),
            )
        elif annotation.kind == "sheet_title":
            item = SheetTitleItem(annotation.id, annotation.text, font_px)
        elif annotation.kind == "detail_note":
            target = annotation.target_page_pos or (
                annotation.page_pos[0] + 60.0, annotation.page_pos[1] + 45.0
            )
            target_scene = self.pts_to_scene(target[0], target[1])
            item = DetailNoteItem(
                annotation.id, annotation.text, font_px,
                QPointF(target_scene.x() - pos.x(), target_scene.y() - pos.y()),
            )
        else:
            return None

        item.setPos(pos)
        self.addItem(item)
        self._annotations[annotation.id] = item
        return item

    def remove_annotation_item(self, annotation_id: str) -> None:
        item = self._annotations.pop(annotation_id, None)
        if item is not None:
            self.removeItem(item)

    def clear_annotation_items(self) -> None:
        for item in list(self._annotations.values()):
            self.removeItem(item)
        self._annotations.clear()

    def get_annotation_item(self, annotation_id: str):
        return self._annotations.get(annotation_id)

    def notify_annotation_moved(self, item) -> None:
        annotation_id = getattr(item, "annotation_id", None)
        if annotation_id:
            self.annotation_moved.emit(annotation_id)

    def notify_annotation_edit_began(self, item) -> None:
        annotation_id = getattr(item, "annotation_id", None)
        if annotation_id:
            self.annotation_edit_began.emit(annotation_id)

    def notify_annotation_edit_finished(self, item) -> None:
        annotation_id = getattr(item, "annotation_id", None)
        if annotation_id:
            self.annotation_edit_finished.emit(annotation_id)

    def refresh_item_detail(self) -> None:
        """Re-pick each item's working resolution after a zoom change."""
        for gfx in self._placed_items.values():
            gfx._update_pixmap_transform()

    def remove_placed_item(self, placed_item_id: str) -> None:
        """Remove a placed item from the canvas."""
        gfx = self._placed_items.pop(placed_item_id, None)
        if gfx is not None:
            self.removeItem(gfx)
        # Also remove any associated leader line
        leader = self._leader_lines.pop(placed_item_id, None)
        if leader is not None:
            self.removeItem(leader)

    def get_placed_item_gfx(self, placed_item_id: str) -> Optional[PlacedItemGraphicsItem]:
        """Get the graphics item for a placed item."""
        return self._placed_items.get(placed_item_id)

    def get_placed_item_page_rect(self, placed_item_id: str) -> Optional[tuple]:
        """Get the current page rect (in PDF points) for a placed item."""
        gfx = self._placed_items.get(placed_item_id)
        if gfx is None:
            return None
        return gfx.get_page_rect_pts(self._scale_factor)

    # ── Leader lines ──────────────────────────────────────────────────

    def add_leader_line(self, placed_item_id: str, start_pts: tuple,
                        end_pts: tuple, style: str = "dashed") -> LeaderLineItem:
        """Add a leader line for a callout circle."""
        start_scene = self.pts_to_scene(*start_pts)
        end_scene = self.pts_to_scene(*end_pts)

        line = LeaderLineItem(start_scene, end_scene, style)
        line.placed_item_id = placed_item_id
        self.addItem(line)
        self._leader_lines[placed_item_id] = line
        return line

    def get_leader_end_pts(self, placed_item_id: str) -> Optional[tuple]:
        """Get the leader line tip position in PDF points."""
        line = self._leader_lines.get(placed_item_id)
        if line is None:
            return None
        end = line.get_end_pos()
        return self.scene_to_pts(end.x(), end.y())

    # ── Preview image (for source-asset cropping workflow) ────────────

    def show_preview_image(self, pixmap: QPixmap,
                           source_size: Optional[tuple] = None) -> None:
        """Show an asset image for cropping, overlaying the template.

        ``source_size`` is the asset's real ``(width, height)`` in pixels.
        The pixmap may be smaller — a 300 DPI CAD render is capped on its
        way to the canvas — and a crop box drawn on it has to be reported
        back in the asset's own pixel space, or the crop lands somewhere
        else entirely.  Without this the cap and the crop workflow would
        quietly disagree.
        """
        self.hide_preview_image()

        self._preview_scale = 1.0
        if source_size and pixmap.width() > 0 and source_size[0]:
            self._preview_scale = float(source_size[0]) / float(pixmap.width())

        self._preview_image_item = self.addPixmap(pixmap)
        # Above the sheet, but below the crop box drawn on top of it -
        # at a higher z the preview simply hid the selection.
        self._preview_image_item.setZValue(PREVIEW_IMAGE_Z)
        self._preview_image_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self._preview_image_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)

    def hide_preview_image(self) -> None:
        """Remove the preview image overlay."""
        if self._preview_image_item is not None:
            self.removeItem(self._preview_image_item)
            self._preview_image_item = None
        self._preview_scale = 1.0

    def has_preview(self) -> bool:
        """Whether a preview image is currently shown."""
        return self._preview_image_item is not None

    def preview_scale(self) -> float:
        """Asset pixels per preview pixel — 1.0 when shown full size."""
        return getattr(self, "_preview_scale", 1.0)

    # ── Crop-box overlays (for source-level cropping) ─────────────────

    def add_crop_box(self, rect: QRectF, is_circular: bool = False) -> CropBoxItem:
        """Add a crop-box rectangle overlay and emit the signal.
        
        Parameters
        ----------
        rect : QRectF
            The crop box rectangle.
        is_circular : bool
            If True, this is a circular crop selection (will be rendered as circle).
        """
        # Clear any existing crop boxes FIRST to avoid multiple boxes
        self.clear_crop_boxes()

        item = CropBoxItem(rect, is_circular=is_circular)
        self.addItem(item)
        self._crop_boxes.append(item)
        # Emit the item's own rect: a circular selection is squared off
        # on construction, so the drawn rect is not always what was asked for.
        self.crop_box_created.emit(item.scene_rect())
        self.update()
        return item

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        """Dim everything the active crop box throws away.

        Seeing only an outline makes it hard to judge a crop; shading the
        discarded area shows what the detail will actually contain.
        """
        super().drawForeground(painter, rect)
        if not self._crop_boxes:
            return

        box = self._crop_boxes[-1]
        source = self._preview_image_item or self._template_bg
        if source is not None:
            area = source.sceneBoundingRect()
        else:
            # No sheet or preview behind the crop - shade the placed items
            # instead, so a crop drawn straight onto an image still shows
            # what it throws away.
            area = QRectF()
            for gfx in self._placed_items.values():
                area = area.united(gfx.sceneBoundingRect())
        if area.isEmpty():
            return

        keep = QPainterPath()
        box_rect = box.scene_rect()
        if box.is_circular:
            keep.addEllipse(box_rect)
        else:
            keep.addRect(box_rect)

        discarded = QPainterPath()
        discarded.addRect(area)
        discarded = discarded.subtracted(keep)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 110)))
        painter.drawPath(discarded)
        painter.restore()

    def notify_crop_box_changed(self, item: "CropBoxItem") -> None:
        """Re-publish a crop box that was moved or resized after drawing.

        Without this the crop captured at draw time is the one used, and
        adjusting the box afterwards silently does nothing.
        """
        if item in self._crop_boxes:
            self.crop_box_created.emit(item.scene_rect())
            self.update()

    def clear_crop_boxes(self) -> None:
        """Remove all crop-box overlays from the scene."""
        for item in self._crop_boxes:
            self.removeItem(item)
        self._crop_boxes.clear()
        self.update()

    def get_last_crop_box(self) -> QRectF | None:
        """Return the true scene rect of the most recently drawn crop box."""
        if self._crop_boxes:
            return self._crop_boxes[-1].scene_rect()
        return None

    def is_circular_crop(self) -> bool:
        """Check if the current crop box is circular."""
        if self._crop_boxes:
            return self._crop_boxes[-1].is_circular
        return False

    # ── Anchor-point overlays ─────────────────────────────────────────

    def add_anchor_point(self, point: QPointF) -> AnchorPointItem:
        """Add an anchor-point marker and emit the signal."""
        item = AnchorPointItem(point)
        self.addItem(item)
        self._anchor_points.append(item)
        self.anchor_point_placed.emit(point)
        return item

    def clear_anchor_points(self) -> None:
        """Remove all anchor-point markers from the scene."""
        for item in self._anchor_points:
            self.removeItem(item)
        self._anchor_points.clear()

    def get_last_anchor_point(self) -> QPointF | None:
        """Return the position of the most recently placed anchor."""
        if self._anchor_points:
            item = self._anchor_points[-1]
            return QPointF(item.rect().center().x(), item.rect().center().y())
        return None


    def contextMenuEvent(self, event) -> None:
        # Check if right clicking on a placed item
        item = self.itemAt(event.scenePos(), QTransform())
        # Find the PlacedItemGraphicsItem
        while item and not isinstance(item, PlacedItemGraphicsItem):
            item = item.parentItem()
            
        menu = QMenu()
        
        selected_items = [i for i in self.selectedItems() if isinstance(i, PlacedItemGraphicsItem)]
        
        if len(selected_items) > 1:
            action = menu.addAction("Group Selected")
            action.triggered.connect(lambda: self._group_items(selected_items))
            
        if item and isinstance(item, PlacedItemGraphicsItem):
            action_ccw = menu.addAction("Rotate 90° CCW")
            action_ccw.triggered.connect(lambda: self._rotate_item(item, -90))
            
            action_cw = menu.addAction("Rotate 90° CW")
            action_cw.triggered.connect(lambda: self._rotate_item(item, 90))
            
            # If in a group
            # We don't have direct access to ProjectState here, so we emit a signal or handle it at MainWindow
            # Wait, better to emit signals for these actions so MainWindow can update ProjectState
            ungroup_action = menu.addAction("Ungroup")
            ungroup_action.triggered.connect(lambda: self.item_ungroup_requested.emit(item.placed_item_id))
            
        if not menu.isEmpty():
            # QGraphicsSceneContextMenuEvent provides screenPos()
            menu.exec(event.screenPos())
            
    def _rotate_item(self, item, angle):
        item.setRotation(item.rotation() + angle)
        self.item_resized.emit(item.placed_item_id)
        
    def _group_items(self, items):
        ids = [i.placed_item_id for i in items]
        self.group_requested.emit(ids)

    def selected_group_ids(self) -> list[str]:
        """Ids of everything selected that can be grouped.

        Drawings and lettering both count - a detail circle and its note
        are exactly the pair someone wants to keep together.
        """
        ids = []
        for item in self.selectedItems():
            item_id = (getattr(item, "placed_item_id", None)
                       or getattr(item, "annotation_id", None))
            if item_id and item_id not in ids:
                ids.append(item_id)
        return ids

    def group_selection(self) -> bool:
        """Group whatever is selected.  Returns False if there is too little."""
        ids = self.selected_group_ids()
        if len(ids) < 2:
            return False
        self.group_requested.emit(ids)
        return True

    def ungroup_selection(self) -> bool:
        """Break up the groups any selected item belongs to."""
        ids = self.selected_group_ids()
        if not ids:
            return False
        for item_id in ids:
            self.item_ungroup_requested.emit(item_id)
        return True
        
    def sync_group_position(self, placed_item_id: str):
        self.group_moved.emit(placed_item_id)
        
    def sync_group_rotation(self, placed_item_id: str, angle: float):
        self.group_rotated.emit(placed_item_id, angle)

    # ── Drag and drop from staging tray ───────────────────────────────

    def dragEnterEvent(self, event: QGraphicsSceneDragDropEvent) -> None:
        if event.mimeData().hasFormat("application/x-asset-id"):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QGraphicsSceneDragDropEvent) -> None:
        if event.mimeData().hasFormat("application/x-asset-id"):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QGraphicsSceneDragDropEvent) -> None:
        if event.mimeData().hasFormat("application/x-asset-id"):
            asset_id = bytes(event.mimeData().data("application/x-asset-id")).decode("utf-8")
            drop_pos = event.scenePos()
            # Emit signal — the main window will handle creating the PlacedItem
            # We store the drop info temporarily so the main window can read it
            self._pending_drop_asset_id = asset_id
            self._pending_drop_scene_pos = drop_pos
            self.item_placed.emit(asset_id)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    def get_pending_drop_info(self) -> Optional[tuple[str, QPointF]]:
        """Get the asset_id and scene position from the last drop event."""
        aid = self._pending_drop_asset_id
        pos = self._pending_drop_scene_pos
        # `is not None`, not truthiness: a drop at the very top-left of
        # the sheet gives QPointF(0, 0), and Qt's QPointF compares equal
        # to a null point, so the obvious test drops that placement.
        if aid and pos is not None:
            self._pending_drop_asset_id = None
            self._pending_drop_scene_pos = None
            return (aid, pos)
        return None

    # ── Cleanup ───────────────────────────────────────────────────────

    def clear_overlays(self) -> None:
        """Remove all interactive overlays but keep template and placed items."""
        self.clear_crop_boxes()
        self.clear_anchor_points()
        self.hide_preview_image()

    def clear_placed_items(self) -> None:
        """Remove all placed items and leader lines."""
        for gfx in list(self._placed_items.values()):
            self.removeItem(gfx)
        self._placed_items.clear()
        for line in list(self._leader_lines.values()):
            self.removeItem(line)
        self._leader_lines.clear()

    def clear_all(self) -> None:
        """Remove everything — template background, placed items, and overlays."""
        self.clear()
        self._template_bg = None
        self._placed_items.clear()
        self._leader_lines.clear()
        self._guide_items.clear()
        self._crop_boxes.clear()
        self._anchor_points.clear()
        self._preview_image_item = None
        self._preview_scale = 1.0
        # QGraphicsScene.clear() destroys the annotation items too, so a
        # stale entry here would hand out a pointer to a deleted object.
        self._annotations.clear()
        self._active_alignment_lines.clear()
        self._pending_drop_asset_id = None
        self._pending_drop_scene_pos = None
