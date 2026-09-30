"""
QGraphicsView for the technical drawing sheet canvas — WYSIWYG mode.

Supports these switchable interaction modes:
  1. SELECT     — drag/resize placed items on the canvas (default)
  2. CROP_BOX   — rubber-band rectangle drag (for source-image cropping)
  3. LEADER_DRAG — drag a leader-line endpoint

Pan/zoom is always available regardless of mode:
  - Mouse wheel  → zoom in/out
  - Middle-drag  → pan
  - Space + drag → pan

Hold Ctrl to temporarily disable magnetic snap for precise manual placement.
"""

from enum import Enum, auto

from PySide6.QtCore import Qt, QPointF, QRectF, Signal, QTimer, QEvent
from PySide6.QtGui import QBrush, QColor, QMouseEvent, QWheelEvent, QPainter, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QGraphicsView, QPinchGesture

from app.ui.theme import THEME
from app.ui.canvas_scene import CanvasScene


class InteractionMode(Enum):
    """Canvas interaction modes."""
    SELECT = auto()      # Default: select, drag, resize placed items
    CROP_BOX = auto()    # Rectangle-drag for rectangular crop selection
    CIRCULAR_CROP = auto()  # Circular-drag for circular crop selection (callouts)
    LEADER_DRAG = auto() # Drag a leader-line endpoint


class CanvasView(QGraphicsView):
    """Interactive canvas view with multiple modes and pan/zoom.

    Signals
    -------
    mode_changed(str)
        Emitted when the interaction mode switches.
    """

    mode_changed = Signal(str)
    canvas_background_clicked = Signal()
    crop_cancelled = Signal()
    # Every route that changes the scale reports it, so the readout on
    # the zoom pill is right however the architect got there.  Only the
    # pill's own buttons used to update it, so wheel-zoom, pinch, Fit and
    # 1:1 all left it showing a figure that was no longer true.
    zoom_changed = Signal(float)

    # Zoom limits
    ZOOM_MIN = 0.05
    ZOOM_MAX = 20.0
    ZOOM_FACTOR = 1.15

    # Smallest crop worth keeping, in screen pixels
    MIN_CROP_PX = 6.0

    # How far one mouse-wheel notch scrolls when not zooming
    WHEEL_SCROLL_PX = 60.0

    def __init__(self, scene: CanvasScene, parent=None):
        super().__init__(scene, parent)

        self.setBackgroundBrush(QBrush(QColor(THEME["bg_canvas"])))

        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
            | QPainter.RenderHint.TextAntialiasing
        )
        self.setViewportUpdateMode(
            QGraphicsView.ViewportUpdateMode.FullViewportUpdate
        )
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setResizeAnchor(
            QGraphicsView.ViewportAnchor.AnchorViewCenter
        )

        # Accept drag-and-drop from staging tray
        self.setAcceptDrops(True)

        # Pinch-to-zoom from the trackpad
        self.grabGesture(Qt.GestureType.PinchGesture)

        # Interaction state
        self._mode: InteractionMode = InteractionMode.SELECT
        self._is_panning: bool = False
        self._pan_start: QPointF = QPointF()
        self._space_held: bool = False
        self._ctrl_held: bool = False

        # Crop-box drag state
        self._drag_active: bool = False
        self._drag_origin: QPointF = QPointF()
        self._drag_is_circular: bool = False  # For circular crop mode
        self._drag_preview = None  # live CropBoxItem shown while dragging
        self._current_zoom: float = 1.0

        self._lod_timer = QTimer(self)
        self._lod_timer.setSingleShot(True)
        self._lod_timer.timeout.connect(self._apply_item_detail)

        # Initial mode
        self.set_mode(InteractionMode.SELECT)

    # ── Mode switching ────────────────────────────────────────────────

    @property
    def mode(self) -> InteractionMode:
        return self._mode

    def set_mode(self, mode: InteractionMode) -> None:
        """Switch the interaction mode."""
        self._mode = mode
        self._drag_active = False
        self._drag_preview = None
        if mode == InteractionMode.SELECT:
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
        elif mode == InteractionMode.CROP_BOX:
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
        elif mode == InteractionMode.CIRCULAR_CROP:
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
        elif mode == InteractionMode.LEADER_DRAG:
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.mode_changed.emit(mode.name)

    # ── Zoom ──────────────────────────────────────────────────────────

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Scroll to pan, Ctrl to zoom - the way every other canvas works.

        A trackpad reports a two-finger slide as a wheel event, so zooming
        on every wheel made the sheet leap in and out when the architect
        was only trying to move down the page.  Zoom is now deliberate:
        Ctrl (or Cmd) with the wheel, or a pinch.
        """
        modifiers = event.modifiers()
        zoom_held = bool(
            modifiers & (Qt.KeyboardModifier.ControlModifier
                         | Qt.KeyboardModifier.MetaModifier)
        )

        if zoom_held:
            delta = event.angleDelta().y() or event.angleDelta().x()
            if not delta:
                event.ignore()
                return
            self._zoom_by_steps(delta / 120.0, event.position())
            event.accept()
            return

        # Pixel deltas are what a precision trackpad sends; they give
        # smooth one-to-one panning.  A mouse wheel only sends angles.
        pixels = event.pixelDelta()
        if not pixels.isNull():
            dx, dy = pixels.x(), pixels.y()
        else:
            angle = event.angleDelta()
            dx = angle.x() / 120.0 * self.WHEEL_SCROLL_PX
            dy = angle.y() / 120.0 * self.WHEEL_SCROLL_PX

        if modifiers & Qt.KeyboardModifier.ShiftModifier and not dx:
            # Shift-wheel scrolls sideways on a plain mouse.
            dx, dy = dy, 0

        if not dx and not dy:
            event.ignore()
            return

        h = self.horizontalScrollBar()
        v = self.verticalScrollBar()
        h.setValue(h.value() - int(round(dx)))
        v.setValue(v.value() - int(round(dy)))
        event.accept()

    def _zoom_by_steps(self, steps: float, viewport_pos=None) -> None:
        """Zoom by ``steps`` wheel notches, keeping the cursor anchored."""
        if not steps:
            return
        # Clamp rather than refuse: a step that would overshoot the limit
        # used to do nothing at all, so the last part of the range was
        # unreachable and the zoom appeared to stick short of it.
        target = min(
            self.ZOOM_MAX,
            max(self.ZOOM_MIN, self._current_zoom * (self.ZOOM_FACTOR ** steps)),
        )
        factor = target / self._current_zoom
        if abs(factor - 1.0) < 1e-9:
            return
        self._current_zoom = target
        self.scale(factor, factor)
        self._refresh_item_detail()
        self.zoom_changed.emit(self._current_zoom)

    def event(self, event):
        if event.type() == QEvent.Type.Gesture:
            if self._handle_gesture(event):
                return True
        return super().event(event)

    def _handle_gesture(self, event) -> bool:
        """Pinch to zoom on a trackpad."""
        pinch = event.gesture(Qt.GestureType.PinchGesture)
        if pinch is None:
            return False
        change = pinch.changeFlags()
        if change & QPinchGesture.ChangeFlag.ScaleFactorChanged:
            factor = float(pinch.scaleFactor()) or 1.0
            new_zoom = self._current_zoom * factor
            if self.ZOOM_MIN <= new_zoom <= self.ZOOM_MAX:
                self._current_zoom = new_zoom
                self.scale(factor, factor)
                self._refresh_item_detail()
                self.zoom_changed.emit(self._current_zoom)
        event.accept()
        return True

    def zoom_to_fit(self) -> None:
        """Fit the entire scene content within the viewport."""
        scene = self.scene()
        if scene and not scene.sceneRect().isEmpty():
            self.fitInView(scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
            transform = self.transform()
            self._current_zoom = transform.m11() or 1.0
            self._refresh_item_detail()
            self.zoom_changed.emit(self._current_zoom)

    def reset_zoom(self) -> None:
        """Reset zoom to 1:1."""
        self.resetTransform()
        self._current_zoom = 1.0
        self._refresh_item_detail()
        self.zoom_changed.emit(self._current_zoom)

    def _refresh_item_detail(self) -> None:
        """Let placed items re-pick their working resolution for this zoom.

        Coalesced, because a wheel spin fires many times a second and
        rescaling a large render on every tick would stutter.
        """
        self._lod_timer.start(90)

    def _apply_item_detail(self) -> None:
        scene = self.scene()
        if not isinstance(scene, CanvasScene):
            return
        try:
            scene.refresh_item_detail()
        except RuntimeError:
            # The scene's C++ side went away between the timer being
            # started and it firing — on the way out of the app, say.
            # Reaching into a deleted object is the one thing that ends
            # the process outright rather than raising.
            pass

    def shutdown(self) -> None:
        """Stop the pending work before the scene can be torn down."""
        try:
            self._lod_timer.stop()
        except RuntimeError:
            pass

    # ── Pan (middle-drag or space+drag) ───────────────────────────────

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            # Back out of a crop that is going wrong, without having to
            # finish the drag first.
            if self._drag_active or self._drag_preview is not None:
                self._cancel_crop_drag()
                event.accept()
                return
            if self._mode in (InteractionMode.CROP_BOX,
                              InteractionMode.CIRCULAR_CROP):
                scene = self.scene()
                if isinstance(scene, CanvasScene):
                    scene.clear_crop_boxes()
                self.crop_cancelled.emit()
                event.accept()
                return
        if (event.key() == Qt.Key.Key_G
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            scene = self.scene()
            if isinstance(scene, CanvasScene):
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    scene.ungroup_selection()
                else:
                    scene.group_selection()
                event.accept()
                return

        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_held = True
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        elif event.key() == Qt.Key.Key_Control and not event.isAutoRepeat():
            self._ctrl_held = True
            # Disable snap while Ctrl is held
            scene = self.scene()
            if isinstance(scene, CanvasScene):
                scene.set_snap_enabled(False)
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_held = False
            if not self._is_panning:
                self.set_mode(self._mode)
        elif event.key() == Qt.Key.Key_Control and not event.isAutoRepeat():
            self._ctrl_held = False
            # Re-enable snap
            scene = self.scene()
            if isinstance(scene, CanvasScene):
                scene.set_snap_enabled(True)
        super().keyReleaseEvent(event)

    def _start_pan(self, event: QMouseEvent) -> None:
        self._is_panning = True
        self._pan_start = event.position()
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def _do_pan(self, event: QMouseEvent) -> None:
        delta = event.position() - self._pan_start
        self._pan_start = event.position()
        self.horizontalScrollBar().setValue(
            self.horizontalScrollBar().value() - int(delta.x())
        )
        self.verticalScrollBar().setValue(
            self.verticalScrollBar().value() - int(delta.y())
        )

    def _end_pan(self) -> None:
        self._is_panning = False
        if self._space_held:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.set_mode(self._mode)

    # ── Mouse events ──────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # Middle button always starts pan
        if event.button() == Qt.MouseButton.MiddleButton:
            self._start_pan(event)
            return

        # Space + left click also pans
        if self._space_held and event.button() == Qt.MouseButton.LeftButton:
            self._start_pan(event)
            return

        # Left click — mode-dependent
        if event.button() == Qt.MouseButton.LeftButton:
            scene = self.scene()
            if not isinstance(scene, CanvasScene):
                return

            scene_pos = self.mapToScene(event.position().toPoint())

            if self._mode == InteractionMode.SELECT:
                hit = self.itemAt(event.position().toPoint())
                if hit is None:
                    self.canvas_background_clicked.emit()

                # Shift-click builds up a selection, the way every design
                # tool does it.  Qt only honours Ctrl by default, which is
                # not what anyone reaches for.
                if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                        and hit is not None):
                    target = self._selectable_ancestor(hit)
                    if target is not None:
                        target.setSelected(not target.isSelected())
                        event.accept()
                        return

                super().mousePressEvent(event)
                return

            elif self._mode in (InteractionMode.CROP_BOX,
                                InteractionMode.CIRCULAR_CROP):
                # Landing on the box already drawn means the architect is
                # adjusting it.  Starting a fresh drag here would clear the
                # box out from under the handle they just grabbed.
                if self._crop_box_under(event.position().toPoint()):
                    super().mousePressEvent(event)
                    return

                if scene.has_preview() or scene.has_template():
                    self._drag_active = True
                    self._drag_origin = scene_pos
                    self._drag_is_circular = (
                        self._mode == InteractionMode.CIRCULAR_CROP
                    )
                    self._drag_preview = None
                    scene.clear_crop_boxes()
                event.accept()
                return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._is_panning:
            self._do_pan(event)
            return

        if self._drag_active:
            scene = self.scene()
            if isinstance(scene, CanvasScene):
                rect = self._drag_rect(
                    self.mapToScene(event.position().toPoint()),
                    event.modifiers(),
                )
                if self._drag_preview is None:
                    if rect.width() >= 1 and rect.height() >= 1:
                        self._drag_preview = scene.add_crop_box(
                            rect, is_circular=self._drag_is_circular
                        )
                else:
                    self._drag_preview.setRect(rect)
            event.accept()
            return

        super().mouseMoveEvent(event)

    def _crop_box_under(self, viewport_pos) -> bool:
        """Whether the point is on the live crop box or one of its handles."""
        from app.ui.canvas_scene import CropBoxItem

        item = self.itemAt(viewport_pos)
        while item is not None:
            if isinstance(item, CropBoxItem):
                return True
            item = item.parentItem()
        return False

    @staticmethod
    def _selectable_ancestor(item):
        """The item a click should select - handles belong to their owner."""
        while item is not None:
            if item.flags() & item.GraphicsItemFlag.ItemIsSelectable:
                return item
            item = item.parentItem()
        return None

    def _drag_rect(self, scene_pos: QPointF, modifiers) -> QRectF:
        """The crop rect for a drag from the origin to ``scene_pos``.

        A circular crop is squared off along the longer axis so the
        circle follows the drag instead of jumping to a smaller one
        centred somewhere else.  Hold Alt to grow from the centre.
        """
        origin = self._drag_origin
        from_centre = bool(modifiers & Qt.KeyboardModifier.AltModifier)

        dx = scene_pos.x() - origin.x()
        dy = scene_pos.y() - origin.y()

        if self._drag_is_circular:
            side = max(abs(dx), abs(dy))
            if from_centre:
                return QRectF(origin.x() - side, origin.y() - side,
                              side * 2, side * 2)
            dx = side if dx >= 0 else -side
            dy = side if dy >= 0 else -side
            return QRectF(origin, QPointF(origin.x() + dx,
                                          origin.y() + dy)).normalized()

        if from_centre:
            return QRectF(origin.x() - abs(dx), origin.y() - abs(dy),
                          abs(dx) * 2, abs(dy) * 2)
        return QRectF(origin, scene_pos).normalized()

    def _cancel_crop_drag(self) -> None:
        """Abandon an in-progress crop drag and remove its preview."""
        self._drag_active = False
        self._drag_preview = None
        scene = self.scene()
        if isinstance(scene, CanvasScene):
            scene.clear_crop_boxes()
        self.crop_cancelled.emit()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton):
            if self._is_panning:
                self._end_pan()
                return

        if event.button() == Qt.MouseButton.LeftButton and self._drag_active:
            self._drag_active = False
            scene = self.scene()
            if isinstance(scene, CanvasScene):
                rect = self._drag_rect(
                    self.mapToScene(event.position().toPoint()),
                    event.modifiers(),
                )
                # A minimum in screen pixels, not scene units: 5 scene
                # units is a whole region zoomed out and invisible zoomed in.
                minimum = self.MIN_CROP_PX / max(self._current_zoom, 1e-6)
                if rect.width() >= minimum and rect.height() >= minimum:
                    if self._drag_preview is not None:
                        self._drag_preview.setRect(rect)
                    else:
                        scene.add_crop_box(
                            rect, is_circular=self._drag_is_circular
                        )
                else:
                    # Treat a stray click as "clear the selection", not as
                    # a crop the size of a pinhead.
                    scene.clear_crop_boxes()
            self._drag_preview = None
            event.accept()
            return

        super().mouseReleaseEvent(event)


    # ── Drag and drop from staging tray ───────────────────────────────

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasFormat("application/x-asset-id"):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasFormat("application/x-asset-id"):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if event.mimeData().hasFormat("application/x-asset-id"):
            # Forward to the scene with correct scene coordinates
            scene = self.scene()
            if isinstance(scene, CanvasScene):
                # Map viewport position to scene coordinates
                scene_pos = self.mapToScene(event.position().toPoint())
                # Create a scene-level drop event by storing info and forwarding
                asset_id = bytes(event.mimeData().data("application/x-asset-id")).decode("utf-8")
                scene._pending_drop_asset_id = asset_id
                scene._pending_drop_scene_pos = scene_pos
                scene.item_placed.emit(asset_id)
                event.acceptProposedAction()
        else:
            super().dropEvent(event)
