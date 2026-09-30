"""
Draggable sheet lettering for the WYSIWYG canvas.

Three items, matching the house style of the reference drawings:

  ViewLabelItem   filled triangle + title + rule running out to the right,
                  sat under each view ("PLAN", "ELEVATION", "3D VIEW").
  DetailNoteItem  a dot on the drawing, a leader out to the wording
                  ("BED PANEL MOULDING").  Dot and text drag separately.
  SheetTitleItem  the large name across the bottom of the sheet.

Everything is positioned in scene coordinates; main_window converts to
PDF points through the scene's scale factor when it saves back to the
project state, so what you drag is what prints.
"""

from __future__ import annotations

import math

from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import (
    QBrush, QColor, QPen, QPainter, QFont, QFontMetricsF, QPolygonF,
    QPainterPath, QGuiApplication,
)
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject

from app.ui.theme import THEME


# Sheet lettering is black on the printed drawing; the canvas shows the
# same so there are no surprises at generate time.
INK = QColor("#111111")
ANNOTATION_Z = 300


def _sheet_font(size: float, bold: bool = False) -> QFont:
    # Set the family and the pixel size separately: passing -1 as the point
    # size to the constructor makes Qt warn on every repaint.
    font = QFont()
    font.setFamily("Arial")
    font.setPixelSize(max(1, int(round(size))))
    font.setBold(bold)
    return font


class AnnotationHandle(QGraphicsItem):
    """A square grab handle, held at a constant size on screen.

    Dragging one changes the annotation geometry - how far the rule runs,
    where the leader points - and never the type size, so resizing an
    annotation cannot squash its wording.
    """

    SIZE = 7.0

    CURSORS = {
        "tl": Qt.CursorShape.SizeFDiagCursor,
        "br": Qt.CursorShape.SizeFDiagCursor,
        "tr": Qt.CursorShape.SizeBDiagCursor,
        "bl": Qt.CursorShape.SizeBDiagCursor,
    }

    def __init__(self, parent, role: str, cursor=None):
        super().__init__(parent)
        self.role = role
        if cursor is None:
            cursor = self.CURSORS.get(role, Qt.CursorShape.SizeHorCursor)
        # Deliberately not ItemIsMovable: Qt's own drag cannot follow the
        # mouse through ItemIgnoresTransformations, so the handle steers
        # its parent from scene coordinates instead.
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setCursor(cursor)
        self.setZValue(5)
        self.setVisible(False)

    def boundingRect(self) -> QRectF:
        h = self.SIZE / 2.0 + 2
        return QRectF(-h, -h, h * 2, h * 2)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor("#141414"), 1.5))
        painter.setBrush(QBrush(QColor(THEME["accent_selection"])))
        h = self.SIZE / 2.0
        painter.drawRect(QRectF(-h, -h, h * 2, h * 2))

    def _tell_parent_scene(self, method: str) -> None:
        parent = self.parentItem()
        scene = self.scene()
        if parent is not None and scene is not None and hasattr(scene, method):
            getattr(scene, method)(parent)

    def mousePressEvent(self, event) -> None:
        self._tell_parent_scene("notify_annotation_edit_began")
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        parent = self.parentItem()
        if parent is not None and hasattr(parent, "handle_dragged_to"):
            parent.handle_dragged_to(
                self.role, parent.mapFromScene(event.scenePos())
            )
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._tell_parent_scene("notify_annotation_edit_finished")
        event.accept()


class _AnnotationBase(QGraphicsObject):
    """Shared selection, dragging and hit-testing behaviour."""

    def __init__(self, annotation_id: str, parent=None):
        super().__init__(parent)
        self.annotation_id = annotation_id
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setZValue(ANNOTATION_Z)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def _selection_pen(self) -> QPen:
        pen = QPen(QColor(THEME["accent_selection"]), 1, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        return pen

    def _build_frame(self) -> None:
        """Four corner grips, the same frame a placed drawing gets."""
        self._frame_handles = {
            role: AnnotationHandle(self, role)
            for role in ("tl", "tr", "bl", "br")
        }
        self._adjusting = False

    def handles(self):
        """Grab handles this annotation offers."""
        return list(getattr(self, "_frame_handles", {}).values())

    def set_handles_visible(self, visible: bool) -> None:
        for handle in self.handles():
            handle.setVisible(visible)

    def set_font_size(self, size: float) -> None:
        """Set the type size and re-seat the grips around it."""
        self.prepareGeometryChange()
        self._font_size = max(4.0, size)
        self.update()
        self.sync_handles()

    def frame_rect(self) -> QRectF:
        """The rect the corner grips sit on."""
        return self.boundingRect()

    def sync_handles(self) -> None:
        """Put the grips back where the current geometry says they go."""
        frame = getattr(self, "_frame_handles", None)
        if not frame or self._adjusting:
            return
        self._adjusting = True
        try:
            r = self.frame_rect()
            frame["tl"].setPos(r.topLeft())
            frame["tr"].setPos(r.topRight())
            frame["bl"].setPos(r.bottomLeft())
            frame["br"].setPos(r.bottomRight())
        finally:
            self._adjusting = False

    def apply_resize(self, role: str, point: QPointF, scale_text: bool) -> None:
        """Resize to put corner ``role`` at ``point``.  Subclasses override."""

    def handle_dragged_to(self, role: str, point: QPointF) -> None:
        """A grip was dragged to ``point``, in this item's own coordinates."""
        if self._adjusting:
            return
        scale_text = bool(
            QGuiApplication.keyboardModifiers()
            & Qt.KeyboardModifier.ShiftModifier
        )
        self._adjusting = True
        try:
            self.prepareGeometryChange()
            self.apply_resize(role, point, scale_text)
            self.update()
        finally:
            self._adjusting = False
        self.sync_handles()
        scene = self.scene()
        if scene is not None and hasattr(scene, "notify_annotation_moved"):
            scene.notify_annotation_moved(self)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            scene = self.scene()
            if scene is not None and hasattr(scene, "notify_annotation_moved"):
                scene.notify_annotation_moved(self)
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.set_handles_visible(bool(value))
            if value:
                self.sync_handles()
        return super().itemChange(change, value)

    def _tell_scene(self, method: str) -> None:
        scene = self.scene()
        if scene is not None and hasattr(scene, method):
            getattr(scene, method)(self)

    def mousePressEvent(self, event) -> None:
        # Snapshot before anything moves: the position is written back to
        # the model continuously while dragging, so by mouse-up the old
        # one is already gone.
        self._tell_scene("notify_annotation_edit_began")
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        self._tell_scene("notify_annotation_edit_finished")

    def mouseDoubleClickEvent(self, event) -> None:
        """Double-click to retype the wording, in place."""
        scene = self.scene()
        if scene is not None and hasattr(scene, "annotation_double_clicked"):
            scene.annotation_double_clicked.emit(self.annotation_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class ViewLabelItem(_AnnotationBase):
    """"PLAN", "ELEVATION", "DETAIL A" - the title under a view.

    Drawn as the reference sheets do it: a small solid triangle, the
    title in caps, then a rule that continues past the text and stops.
    """

    TRIANGLE_W = 13.0
    TRIANGLE_H = 7.0
    GAP = 3.0          # triangle to text
    RULE_DROP = 3.0    # text baseline to rule

    def __init__(self, annotation_id: str, text: str, font_size: float,
                 rule_width: float, parent=None):
        super().__init__(annotation_id, parent)
        self._text = text or "VIEW"
        self._font_size = font_size
        self._rule_width = rule_width
        self._build_frame()
        self.sync_handles()

    # ── Content ───────────────────────────────────────────────────────

    def set_text(self, text: str) -> None:
        self.prepareGeometryChange()
        self._text = text or "VIEW"
        self.update()


    def set_rule_width(self, width: float) -> None:
        self.prepareGeometryChange()
        self._rule_width = max(0.0, width)
        self.update()
        self.sync_handles()


    def _text_end_x(self) -> float:
        _, _, text_w, _ = self._metrics()
        return self.TRIANGLE_W + self.GAP + text_w

    def handles(self):
        # Only the grips at the end of the rule: a view title has one thing
        # to resize, and offering four corners made it ambiguous which of
        # them ran the line out.
        frame = getattr(self, "_frame_handles", {})
        return [frame[role] for role in ("tr", "br") if role in frame]

    def sync_handles(self) -> None:
        """Seat both grips on the end of the rule."""
        frame = getattr(self, "_frame_handles", None)
        if not frame or self._adjusting:
            return
        self._adjusting = True
        try:
            end_x = self._rule_end_x()
            r = self.frame_rect()
            frame["tr"].setPos(end_x, r.top())
            frame["br"].setPos(end_x, self.RULE_DROP)
            for role in ("tl", "bl"):
                if role in frame:
                    frame[role].setVisible(False)
        finally:
            self._adjusting = False

    def _rule_end_x(self) -> float:
        return self._text_end_x() + self._rule_width

    def apply_resize(self, role, point, scale_text) -> None:
        """Drag the grip to run the rule out or pull it back.

        Shift scales the wording as well - without it the title keeps the
        size it was set at, however far the rule is dragged.
        """
        if scale_text:
            span = max(1.0, self.frame_rect().width())
            ratio = max(0.2, min(5.0, abs(point.x()) / span))
            self.set_font_size(self._font_size * ratio)
            return
        # Straight from the pointer, so the line ends where it is dropped.
        self._rule_width = max(0.0, point.x() - self._text_end_x())

    @property
    def text(self) -> str:
        return self._text

    @property
    def font_size(self) -> float:
        return self._font_size

    @property
    def rule_width(self) -> float:
        return self._rule_width

    # ── Geometry ──────────────────────────────────────────────────────

    def _metrics(self):
        font = _sheet_font(self._font_size)
        fm = QFontMetricsF(font)
        text_w = fm.horizontalAdvance(self._text)
        ascent = fm.ascent()
        return font, fm, text_w, ascent

    def boundingRect(self) -> QRectF:
        _, fm, text_w, ascent = self._metrics()
        total = self.TRIANGLE_W + self.GAP + text_w + self._rule_width
        height = ascent + self.RULE_DROP + 4
        return QRectF(-2, -ascent - 4, total + 6, height + 8)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        font, fm, text_w, ascent = self._metrics()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Solid triangle, pointing up, sitting on the rule.
        tri = QPolygonF([
            QPointF(0.0, 0.0),
            QPointF(self.TRIANGLE_W, 0.0),
            QPointF(self.TRIANGLE_W / 2.0, -self.TRIANGLE_H),
        ])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(INK))
        painter.drawPolygon(tri)

        text_x = self.TRIANGLE_W + self.GAP
        painter.setFont(font)
        painter.setPen(QPen(INK))
        painter.drawText(QPointF(text_x, 0.0), self._text)

        # The rule runs under the triangle and text and on past it.
        rule = QPen(INK, 1.4)
        rule.setCosmetic(True)
        painter.setPen(rule)
        y = self.RULE_DROP
        painter.drawLine(QPointF(0.0, y),
                         QPointF(text_x + text_w + self._rule_width, y))

        if self.isSelected():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(self._selection_pen())
            painter.drawRect(self.boundingRect())

        painter.restore()


class SheetTitleItem(_AnnotationBase):
    """The sheet name, set large across the bottom of the drawing."""

    def __init__(self, annotation_id: str, text: str, font_size: float,
                 parent=None):
        super().__init__(annotation_id, parent)
        self._text = text or "SHEET TITLE"
        self._font_size = font_size
        self._build_frame()
        self.sync_handles()

    def set_text(self, text: str) -> None:
        self.prepareGeometryChange()
        self._text = text or "SHEET TITLE"
        self.update()


    @property
    def text(self) -> str:
        return self._text

    @property
    def font_size(self) -> float:
        return self._font_size

    def apply_resize(self, role, point, scale_text) -> None:
        """A title is only type, so the frame sets its size."""
        span = max(1.0, self.frame_rect().width())
        ratio = max(0.2, min(6.0, abs(point.x()) / span))
        self._font_size = max(4.0, min(160.0, self._font_size * ratio))

    def _metrics(self):
        font = _sheet_font(self._font_size)
        fm = QFontMetricsF(font)
        return font, fm, fm.horizontalAdvance(self._text), fm.ascent()

    def boundingRect(self) -> QRectF:
        _, fm, text_w, ascent = self._metrics()
        return QRectF(-3, -ascent - 3, text_w + 8, ascent + fm.descent() + 8)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        font, _, _, _ = self._metrics()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setFont(font)
        painter.setPen(QPen(INK))
        painter.drawText(QPointF(0.0, 0.0), self._text)
        if self.isSelected():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(self._selection_pen())
            painter.drawRect(self.boundingRect())
        painter.restore()


class DetailNoteHandle(QGraphicsItem):
    """The dot end of a detail note, dragged on its own."""

    RADIUS = 4.0

    def __init__(self, parent: "DetailNoteItem"):
        super().__init__(parent)
        # Same reasoning as AnnotationHandle: steer from scene coordinates
        # rather than relying on Qt to drag an untransformed item.
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setZValue(1)

    def boundingRect(self) -> QRectF:
        r = self.RADIUS + 4
        return QRectF(-r, -r, r * 2, r * 2)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(INK))
        r = self.RADIUS
        painter.drawEllipse(QRectF(-r, -r, r * 2, r * 2))

    def _tell_scene(self, method: str) -> None:
        scene = self.scene()
        parent = self.parentItem()
        if scene is not None and parent is not None and hasattr(scene, method):
            getattr(scene, method)(parent)

    def mousePressEvent(self, event) -> None:
        self._tell_scene("notify_annotation_edit_began")
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        parent = self.parentItem()
        if isinstance(parent, DetailNoteItem):
            parent.move_target_to(parent.mapFromScene(event.scenePos()))
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._tell_scene("notify_annotation_edit_finished")
        event.accept()


class DetailNoteItem(_AnnotationBase):
    """Wording on the sheet with a leader running to a dot on the drawing.

    The item's own position is the text; the dot is a child handle, so
    the note and the thing it points at are moved independently, the way
    the reference sheets place them.
    """

    def __init__(self, annotation_id: str, text: str, font_size: float,
                 target_offset: QPointF, parent=None):
        super().__init__(annotation_id, parent)
        self._text = text or "NOTE"
        self._font_size = font_size
        self._handle = DetailNoteHandle(self)
        self._handle.setPos(target_offset)
        self._handle.setVisible(True)
        self._build_frame()
        self.sync_handles()

    # ── Content ───────────────────────────────────────────────────────

    def set_text(self, text: str) -> None:
        self.prepareGeometryChange()
        self._text = text or "NOTE"
        self.update()


    @property
    def text(self) -> str:
        return self._text

    @property
    def font_size(self) -> float:
        return self._font_size

    def target_scene_pos(self) -> QPointF:
        return self.mapToScene(self._handle.pos())

    def set_target_scene_pos(self, pos: QPointF) -> None:
        self._handle.setPos(self.mapFromScene(pos))

    def set_handles_visible(self, visible: bool) -> None:
        for handle in self.handles():
            handle.setVisible(visible)
        # The dot is part of the drawing, not just a grip, so it stays
        # on whether or not the note is selected.
        self._handle.setVisible(True)

    def apply_resize(self, role, point, scale_text) -> None:
        """Drag any corner and the leader follows it.

        The dot is what the note points at, so resizing the frame runs the
        leader out to the corner being dragged.  The wording stays where it
        is at the size it was set - Shift scales the type instead.
        """
        if scale_text:
            span = max(1.0, self._text_block().width())
            ratio = max(0.2, min(5.0, abs(point.x()) / span))
            self.set_font_size(self._font_size * ratio)
            return

        block = self._text_block()
        x, y = point.x(), point.y()
        # Keep the dot clear of the wording, or the leader has nowhere to go.
        if block.left() - 4 < x < block.right() + 4:
            x = block.right() + 4 if x >= block.center().x() else block.left() - 4
        if block.top() - 4 < y < block.bottom() + 4:
            y = block.bottom() + 4 if y >= block.center().y() else block.top() - 4
        self._handle.setPos(QPointF(x, y))

    def move_target_to(self, point: QPointF) -> None:
        """Put the dot at ``point`` (this item's coordinates) and redraw."""
        self.prepareGeometryChange()
        self._handle.setPos(point)
        self.update()
        self.sync_handles()
        scene = self.scene()
        if scene is not None and hasattr(scene, "notify_annotation_moved"):
            scene.notify_annotation_moved(self)

    # ── Geometry ──────────────────────────────────────────────────────

    def _metrics(self):
        # Detail notes are called out on the sheet, so they are set bold -
        # at small sizes a regular weight disappears next to the drawing.
        font = _sheet_font(self._font_size, bold=True)
        fm = QFontMetricsF(font)
        lines = self._text.split("\n")
        width = max((fm.horizontalAdvance(ln) for ln in lines), default=0.0)
        return font, fm, width, lines

    def _text_block(self) -> QRectF:
        _, fm, width, lines = self._metrics()
        height = fm.height() * len(lines)
        return QRectF(0.0, -fm.ascent(), width, height)

    def boundingRect(self) -> QRectF:
        block = self._text_block()
        target = self._handle.pos()
        rect = block.united(QRectF(target.x() - 8, target.y() - 8, 16, 16))
        return rect.adjusted(-4, -4, 6, 6)


    def _leader_start(self) -> QPointF:
        """Leave the text block from the side the dot is on."""
        block = self._text_block()
        target = self._handle.pos()
        y = block.bottom()
        x = block.right() if target.x() >= block.center().x() else block.left()
        return QPointF(x, y)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        font, fm, _, lines = self._metrics()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        painter.setFont(font)
        painter.setPen(QPen(INK))
        y = 0.0
        for line in lines:
            painter.drawText(QPointF(0.0, y), line)
            y += fm.height()

        leader = QPen(INK, 1.0)
        leader.setCosmetic(True)
        painter.setPen(leader)
        start = self._leader_start()
        target = self._handle.pos()
        # A short horizontal shoulder off the text, then straight to the
        # dot - the way a hand-drafted leader is built.
        shoulder = QPointF(start.x() + (6.0 if target.x() >= start.x() else -6.0),
                           start.y())
        painter.drawLine(start, shoulder)
        painter.drawLine(shoulder, target)

        if self.isSelected():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(self._selection_pen())
            painter.drawRect(self.boundingRect())

        painter.restore()

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addRect(self._text_block().adjusted(-4, -4, 4, 4))
        return path
