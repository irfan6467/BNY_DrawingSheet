"""
Component Review Dialog — review, adjust, and finalize DWG component extraction.

After spatial clustering detects N components from a DWG file, this modal
dialog lets the architect:
  - See preview thumbnails of each detected component
  - Edit auto-assigned labels
  - Include/exclude individual components
  - Merge, split, or add custom region
  - See unassigned-entity count so nothing is silently lost
  - Confirm to render at full DPI and add to the Imported Assets tray

Output contract: each confirmed component becomes an ordinary ImportedAsset
with source_type="dwg_render" — identical drag/crop/callout behavior as any
other image import.
"""

from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import Qt, Signal, QSize, QRectF, QPointF
from PySide6.QtGui import (
    QPixmap, QImage, QColor, QPen, QBrush, QPainter, QFont,
    QMouseEvent, QPaintEvent,
)
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QSplitter,
    QWidget, QScrollArea, QLabel, QCheckBox, QLineEdit,
    QPushButton, QGroupBox, QSlider, QFrame, QMessageBox,
    QSizePolicy, QGraphicsScene, QGraphicsView,
    QGraphicsRectItem, QGraphicsPixmapItem, QToolButton,
    QProgressDialog, QApplication,
)

from app.ui.theme import THEME
from app.ui.image_bridge import pil_to_qpixmap, placeholder_pixmap
from app.ui.canvas_scene import ResizableItem
from app.core.dwg_components import (
    ComponentRegion, DetectionResult,
    render_component, render_component_preview,
    render_full_modelspace_preview,
    merge_components, split_component, create_custom_region,
)


# ---------------------------------------------------------------------------
# Color palette for component bounding boxes in the overview
# ---------------------------------------------------------------------------
_COMP_COLORS = [
    "#E74C3C", "#3498DB", "#2ECC71", "#F39C12", "#9B59B6",
    "#1ABC9C", "#E67E22", "#2980B9", "#27AE60", "#C0392B",
    "#8E44AD", "#16A085", "#D35400", "#2C3E50", "#7F8C8D",
]


def _pil_to_qpixmap(pil_img, max_size: int = 0) -> QPixmap:
    """Convert a PIL Image to QPixmap, optionally constraining size.

    See app.ui.image_bridge: the local copy this replaced left
    ``bytesPerLine`` to Qt, which pads each row to four bytes and so
    misreads any RGB buffer whose width is not a multiple of four.
    """
    if pil_img is None:
        return placeholder_pixmap(100, THEME.get("bg_panel", "#FBEFEF"))
    return pil_to_qpixmap(pil_img, max_size=max_size)


# ---------------------------------------------------------------------------
# Component card widget — one per detected component
# ---------------------------------------------------------------------------

class _ComponentCard(QFrame):
    """A single component entry in the review list."""

    selection_toggled = Signal(str, bool)  # (component_id, selected)

    def __init__(self, component: ComponentRegion, color: str, parent=None,
                 on_include_changed=None):
        super().__init__(parent)
        self.component = component
        self._color = color
        self._thumbnail_label: Optional[QLabel] = None

        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Raised)
        self.setStyleSheet(f"""
            _ComponentCard {{
                background: {THEME['bg_panel']};
                border: 2px solid {color};
                border-radius: 6px;
                padding: 6px;
            }}
            _ComponentCard[selected="true"] {{
                border: 3px solid {THEME['accent_primary']};
            }}
        """)
        self.setProperty("selected", False)

        self._on_include_changed = on_include_changed

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # Header row: checkbox + color badge + entity count
        header = QHBoxLayout()

        self._include_cb = QCheckBox("Import")
        self._include_cb.setChecked(True)
        self._include_cb.setToolTip(
            "Tick to bring this component in; untick to leave it out"
        )
        self._include_cb.setStyleSheet(f"""
            QCheckBox {{
                color: {THEME['text_primary']};
                font-size: 11px;
                spacing: 5px;
            }}
            QCheckBox::indicator {{
                width: 15px;
                height: 15px;
                border: 1px solid {THEME['border_subtle']};
                border-radius: 3px;
                background: {THEME['bg_app']};
            }}
            QCheckBox::indicator:checked {{
                background: {THEME['accent_primary']};
                border: 1px solid {THEME['accent_primary']};
            }}
        """)
        self._include_cb.toggled.connect(self._on_include_toggled)
        header.addWidget(self._include_cb)

        color_badge = QLabel("●")
        color_badge.setStyleSheet(f"color: {color}; font-size: 16px;")
        color_badge.setFixedWidth(20)
        header.addWidget(color_badge)

        count_label = QLabel(f"{component.entity_count} entities")
        count_label.setStyleSheet(f"color: {THEME['text_secondary']}; font-size: 11px;")
        header.addWidget(count_label)
        header.addStretch()

        layout.addLayout(header)

        # Thumbnail
        self._thumbnail_label = QLabel()
        self._thumbnail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumbnail_label.setMinimumSize(140, 100)
        self._thumbnail_label.setMaximumHeight(160)
        self._thumbnail_label.setStyleSheet(
            f"background: white; border: 1px solid {THEME['border_subtle']}; border-radius: 4px;"
        )
        layout.addWidget(self._thumbnail_label)

        # Bbox info (small text)
        xmin, ymin, xmax, ymax = component.bbox
        bbox_text = f"Region: ({xmin:.0f}, {ymin:.0f}) → ({xmax:.0f}, {ymax:.0f})"
        self._bbox_label = QLabel(bbox_text)
        self._bbox_label.setStyleSheet(f"color: {THEME['text_secondary']}; font-size: 10px;")
        self._bbox_label.setWordWrap(True)
        layout.addWidget(self._bbox_label)

    def update_bbox_label(self, text: str) -> None:
        self._bbox_label.setText(text)

    def set_thumbnail(self, pixmap: QPixmap) -> None:
        """Set the preview thumbnail."""
        if self._thumbnail_label:
            scaled = pixmap.scaled(
                QSize(140, 140),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._thumbnail_label.setPixmap(scaled)

    def _on_include_toggled(self, included: bool) -> None:
        """Fade a card that is being left out, so the list reads at a glance."""
        if self._thumbnail_label is not None:
            self._thumbnail_label.setEnabled(included)
        if self._on_include_changed is not None:
            self._on_include_changed()

    def set_included(self, included: bool) -> None:
        self._include_cb.setChecked(bool(included))

    def is_included(self) -> bool:
        return self._include_cb.isChecked()

    def set_selected_style(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        if selected:
            self.setStyleSheet(f"""
                _ComponentCard {{
                    background: {THEME['accent_selection']};
                    border: 3px solid {THEME['accent_primary']};
                    border-radius: 6px;
                    padding: 6px;
                }}
            """)
        else:
            self.setStyleSheet(f"""
                _ComponentCard {{
                    background: {THEME['bg_panel']};
                    border: 2px solid {self._color};
                    border-radius: 6px;
                    padding: 6px;
                }}
            """)

    def mousePressEvent(self, event) -> None:
        self.selection_toggled.emit(self.component.id, True)
        super().mousePressEvent(event)


# ---------------------------------------------------------------------------
# Overview preview widget — full modelspace with colored bboxes
# ---------------------------------------------------------------------------

class AdjustableBBoxItem(ResizableItem):
    """A resizable bounding box on the overview."""
    def __init__(self, rect: QRectF, component: ComponentRegion, color: str, callback, parent=None):
        super().__init__(rect, parent)
        self.component = component
        self._callback = callback

        pen = QPen(QColor(color), 2, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        self.setPen(pen)
        brush_color = QColor(color)
        brush_color.setAlpha(30)
        self.setBrush(QBrush(brush_color))
        self.setZValue(10)

        # Make handles visible by default
        self.set_handles_visible(True)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        self._callback(self)

    def end_resize(self) -> None:
        super().end_resize()
        self._callback(self)


class _OverviewWidget(QGraphicsView):
    """Shows the full modelspace with color-coded component bounding boxes.

    Supports rubber-band selection for Add Custom Region.
    """

    custom_region_drawn = Signal(float, float, float, float)  # xmin, ymin, xmax, ymax
    split_line_drawn = Signal(float, str)  # position, axis ("x" or "y")
    bbox_adjusted = Signal(str)  # component_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setStyleSheet(f"background: {THEME['bg_canvas']};")

        self._bg_item: Optional[QGraphicsPixmapItem] = None
        self._bbox_items: list[QGraphicsRectItem] = []
        self._model_bounds: Optional[tuple] = None
        self._scale_factor: float = 1.0
        self._draw_mode: str = "none"  # "none" | "custom_region" | "split"
        self._rubber_start: Optional[QPointF] = None

    def set_overview_image(self, pil_img, model_bounds: tuple) -> None:
        """Set the full modelspace preview image.

        model_bounds: (xmin, ymin, xmax, ymax) in model units.
        """
        self._model_bounds = model_bounds
        pixmap = _pil_to_qpixmap(pil_img)

        self._scene.clear()
        self._bbox_items.clear()
        self._bg_item = self._scene.addPixmap(pixmap)
        self._bg_item.setZValue(-100)

        # Compute scale: image pixels → model units.  The image is rendered
        # to exactly these bounds, so the two axes must give the same scale;
        # if they ever diverge, trust the one that keeps the drawing inside.
        if model_bounds:
            mw = model_bounds[2] - model_bounds[0]
            mh = model_bounds[3] - model_bounds[1]
            if mw > 0 and mh > 0:
                self._scale_factor = min(
                    pixmap.width() / mw, pixmap.height() / mh
                )

        self.setSceneRect(self._scene.itemsBoundingRect())
        self.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def _component_rect(self, component: ComponentRegion) -> Optional[QRectF]:
        """Where a component's box sits on the overview image."""
        if not self._model_bounds:
            return None
        xmin, ymin, xmax, ymax = component.bbox
        mxmin, mymin = self._model_bounds[0], self._model_bounds[1]
        mh = self._model_bounds[3] - self._model_bounds[1]
        s = self._scale_factor
        return QRectF(
            (xmin - mxmin) * s,
            (mh - (ymax - mymin)) * s,   # image Y runs the other way
            (xmax - xmin) * s,
            (ymax - ymin) * s,
        )

    def refresh_component_bbox(self, component: ComponentRegion) -> None:
        """Re-seat one box after its component's extents changed."""
        rect = self._component_rect(component)
        if rect is None:
            return
        for item in self._bbox_items:
            if item.component.id == component.id:
                item.setPos(0, 0)
                item.setRect(rect)
                break

    def add_component_bbox(self, component: ComponentRegion, color: str) -> None:
        """Draw a colored bounding box for a component on the overview."""
        if not self._model_bounds:
            return

        xmin, ymin, xmax, ymax = component.bbox
        mxmin, mymin = self._model_bounds[0], self._model_bounds[1]
        mh = self._model_bounds[3] - self._model_bounds[1]
        s = self._scale_factor

        # Convert model coords to image pixel coords
        # Note: image Y is flipped (top=0) vs model Y (bottom=0)
        px = (xmin - mxmin) * s
        py = (mh - (ymax - mymin)) * s  # flip Y
        pw = (xmax - xmin) * s
        ph = (ymax - ymin) * s

        rect_item = AdjustableBBoxItem(QRectF(px, py, pw, ph), component, color, self._on_bbox_adjusted)
        self._scene.addItem(rect_item)
        self._bbox_items.append(rect_item)

    def _on_bbox_adjusted(self, item: AdjustableBBoxItem) -> None:
        # map rect back to model space
        r = item.sceneTransform().mapRect(item.rect())
        mx1, my1 = self._scene_to_model(r.topLeft())
        mx2, my2 = self._scene_to_model(r.bottomRight())

        # Y flip means scene topLeft is model ymax, scene bottomRight is model ymin
        xmin = min(mx1, mx2)
        ymin = min(my1, my2)
        xmax = max(mx1, mx2)
        ymax = max(my1, my2)

        item.component.bbox = (xmin, ymin, xmax, ymax)
        self.bbox_adjusted.emit(item.component.id)

    def clear_bboxes(self) -> None:
        for item in self._bbox_items:
            self._scene.removeItem(item)
        self._bbox_items.clear()

    def set_draw_mode(self, mode: str) -> None:
        """Set to 'custom_region', 'split', or 'none'."""
        self._draw_mode = mode
        if mode != "none":
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.unsetCursor()

    def _scene_to_model(self, scene_pos: QPointF) -> tuple[float, float]:
        """Convert scene pixel position to model coordinates."""
        if not self._model_bounds:
            return (0, 0)
        mxmin, mymin = self._model_bounds[0], self._model_bounds[1]
        mh = self._model_bounds[3] - self._model_bounds[1]
        s = self._scale_factor
        if s < 1e-9:
            return (0, 0)
        mx = scene_pos.x() / s + mxmin
        my = mh - scene_pos.y() / s + mymin  # flip Y back
        return (mx, my)

    def mousePressEvent(self, event) -> None:
        if self._draw_mode != "none" and event.button() == Qt.MouseButton.LeftButton:
            self._rubber_start = self.mapToScene(event.pos())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._draw_mode != "none" and self._rubber_start and event.button() == Qt.MouseButton.LeftButton:
            end = self.mapToScene(event.pos())
            start = self._rubber_start
            self._rubber_start = None

            if self._draw_mode == "custom_region":
                mx1, my1 = self._scene_to_model(start)
                mx2, my2 = self._scene_to_model(end)
                xmin = min(mx1, mx2)
                ymin = min(my1, my2)
                xmax = max(mx1, mx2)
                ymax = max(my1, my2)
                self.custom_region_drawn.emit(xmin, ymin, xmax, ymax)
                self.set_draw_mode("none")
            elif self._draw_mode == "split":
                mx1, my1 = self._scene_to_model(start)
                mx2, my2 = self._scene_to_model(end)
                dx = abs(mx2 - mx1)
                dy = abs(my2 - my1)
                if dx > dy:
                    # Horizontal drag → vertical split line (split on X)
                    self.split_line_drawn.emit((mx1 + mx2) / 2, "x")
                else:
                    # Vertical drag → horizontal split line (split on Y)
                    self.split_line_drawn.emit((my1 + my2) / 2, "y")
                self.set_draw_mode("none")

            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:
        """Zoom in/out with mouse wheel."""
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)
        event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._scene.sceneRect().width() > 0:
            self.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)


# ---------------------------------------------------------------------------
# Main review dialog
# ---------------------------------------------------------------------------

class ComponentReviewDialog(QDialog):
    """Modal dialog for reviewing and adjusting detected DWG components.

    Shows auto-detected components as thumbnails with editable labels
    and include/exclude checkboxes.  Provides merge, split, and
    add-custom-region tools.  Surfaces unassigned-entity count.

    On accept, the caller should render each included component at full
    DPI and add them to the project as ImportedAssets.
    """

    def __init__(
        self,
        doc,
        msp,
        detection_result: DetectionResult,
        cache,
        dwg_path: str,
        log_callback=None,
        parent=None,
    ):
        super().__init__(parent)
        self._doc = doc
        self._msp = msp
        self._result = detection_result
        self._cache = cache
        self._dwg_path = dwg_path
        self._log = log_callback or (lambda s, m: None)

        # Working copy of components (user may merge/split/add)
        self._components: list[ComponentRegion] = list(detection_result.components)
        self._entity_bboxes: dict[str, tuple] = {}  # populated lazily
        self._cards: dict[str, _ComponentCard] = {}
        self._selected_ids: list[str] = []

        # Overview image + model bounds for the preview
        self._overview_image = None
        self._model_bounds: Optional[tuple] = None

        self.setWindowTitle(f"DWG Component Extraction — {len(self._components)} Detected")
        self.setMinimumSize(1100, 700)
        self.resize(1300, 800)
        self._setup_ui()
        self._generate_previews()
        self._sync_gap_slider_to_result()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # ── Top info bar ──────────────────────────────────────────────
        info_bar = QHBoxLayout()

        summary_text = (
            f"<b>{len(self._components)}</b> components detected from "
            f"<b>{self._result.total_entities}</b> total entities"
        )
        summary_label = QLabel(summary_text)
        summary_label.setStyleSheet(f"color: {THEME['text_primary']}; font-size: 13px;")
        info_bar.addWidget(summary_label)

        info_bar.addStretch()

        # Unassigned entity warning
        if self._result.unassigned_count > 0:
            warn_label = QLabel(
                f"⚠ {self._result.unassigned_count} entities unassigned — "
                f"use 'Add Custom Region' to recover them"
            )
            warn_label.setStyleSheet(
                f"color: {THEME['status_warning']}; font-weight: bold; font-size: 12px;"
            )
            warn_label.setToolTip(
                f"{self._result.unassigned_count} entities were not assigned to any "
                f"detected component. They may be stray geometry, isolated text, or "
                f"detail views too small/far from other content to cluster.\n\n"
                f"Use 'Add Custom Region' on the overview to draw a box around any "
                f"content you want to recover."
            )
            info_bar.addWidget(warn_label)

        layout.addLayout(info_bar)

        # ── Main splitter: component list (left) + overview (right) ───
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # LEFT: scrollable component cards
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)

        left_header_row = QHBoxLayout()
        left_header = QLabel("Components")
        left_header.setStyleSheet(
            f"font-size: 14px; font-weight: bold; color: {THEME['text_primary']};"
        )
        left_header_row.addWidget(left_header)
        left_header_row.addStretch()

        # Ticking six boxes one at a time is tedious, and with nothing to
        # select all from there was no obvious way to take components out.
        select_all_btn = QPushButton("Select all")
        select_all_btn.setToolTip("Import every detected component")
        select_all_btn.clicked.connect(lambda: self._set_all_included(True))
        select_all_btn.setStyleSheet(self._tool_button_style())
        left_header_row.addWidget(select_all_btn)

        select_none_btn = QPushButton("None")
        select_none_btn.setToolTip("Leave every component out, then pick the ones you want")
        select_none_btn.clicked.connect(lambda: self._set_all_included(False))
        select_none_btn.setStyleSheet(self._tool_button_style())
        left_header_row.addWidget(select_none_btn)

        left_layout.addLayout(left_header_row)

        # Component cards scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"background: {THEME['bg_app']}; border: none;")

        self._cards_container = QWidget()
        self._cards_layout = QVBoxLayout(self._cards_container)
        self._cards_layout.setContentsMargins(4, 4, 4, 4)
        self._cards_layout.setSpacing(6)
        self._cards_layout.addStretch()

        scroll.setWidget(self._cards_container)
        left_layout.addWidget(scroll)

        splitter.addWidget(left_panel)

        # RIGHT: overview preview + tools
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)

        right_header = QLabel("Full Drawing Overview")
        right_header.setStyleSheet(
            f"font-size: 14px; font-weight: bold; color: {THEME['text_primary']};"
        )
        right_layout.addWidget(right_header)

        self._overview = _OverviewWidget()
        self._overview.custom_region_drawn.connect(self._on_custom_region_drawn)
        self._overview.split_line_drawn.connect(self._on_split_line_drawn)
        self._overview.bbox_adjusted.connect(self._on_bbox_adjusted)
        right_layout.addWidget(self._overview, 1)

        # Tools toolbar
        tools_bar = QHBoxLayout()

        self._merge_btn = QPushButton("🔗 Merge Selected")
        self._merge_btn.setToolTip("Merge 2+ selected components into one")
        self._merge_btn.setEnabled(False)
        self._merge_btn.clicked.connect(self._merge_selected)
        self._merge_btn.setStyleSheet(self._tool_button_style())
        tools_bar.addWidget(self._merge_btn)

        self._split_btn = QPushButton("✂ Split Selected")
        self._split_btn.setToolTip("Draw a line on the overview to split a component")
        self._split_btn.setEnabled(False)
        self._split_btn.clicked.connect(self._start_split)
        self._split_btn.setStyleSheet(self._tool_button_style())
        tools_bar.addWidget(self._split_btn)

        self._custom_btn = QPushButton("⬚ Add Custom Region")
        self._custom_btn.setToolTip("Draw a rectangle on the overview to create a new component")
        self._custom_btn.clicked.connect(self._start_custom_region)
        self._custom_btn.setStyleSheet(self._tool_button_style())
        tools_bar.addWidget(self._custom_btn)

        tools_bar.addStretch()

        # Gap threshold slider
        gap_label = QLabel("Gap threshold:")
        gap_label.setStyleSheet(f"color: {THEME['text_secondary']}; font-size: 11px;")
        tools_bar.addWidget(gap_label)

        self._gap_slider = QSlider(Qt.Orientation.Horizontal)
        self._gap_slider.setMinimum(1)
        self._gap_slider.setMaximum(100)
        self._gap_slider.setValue(40)  # 4% default mapped to slider 40
        self._gap_slider.setFixedWidth(120)
        self._gap_slider.setToolTip("Adjust clustering gap threshold (% of diagonal)")
        tools_bar.addWidget(self._gap_slider)

        self._gap_value_label = QLabel("4.0%")
        self._gap_value_label.setFixedWidth(40)
        self._gap_value_label.setStyleSheet(f"color: {THEME['text_secondary']}; font-size: 11px;")
        tools_bar.addWidget(self._gap_value_label)
        self._gap_slider.valueChanged.connect(
            lambda v: self._gap_value_label.setText(f"{v / 10:.1f}%")
        )

        reset_btn = QPushButton("↻ Re-detect")
        reset_btn.setToolTip("Re-run detection with the adjusted gap threshold")
        reset_btn.clicked.connect(self._redetect)
        reset_btn.setStyleSheet(self._tool_button_style())
        tools_bar.addWidget(reset_btn)

        right_layout.addLayout(tools_bar)

        splitter.addWidget(right_panel)
        splitter.setSizes([350, 750])

        layout.addWidget(splitter, 1)

        # ── Bottom button bar ─────────────────────────────────────────
        button_bar = QHBoxLayout()

        included_count = sum(1 for c in self._components)
        self._status_label = QLabel(f"{included_count} components will be imported")
        self._status_label.setStyleSheet(f"color: {THEME['text_secondary']}; font-size: 12px;")
        button_bar.addWidget(self._status_label)

        button_bar.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: {THEME['bg_panel']};
                border: 1px solid {THEME['border_strong']};
                border-radius: 4px;
                padding: 8px 20px;
                color: {THEME['text_primary']};
                font-size: 13px;
            }}
            QPushButton:hover {{
                background: {THEME['accent_selection']};
            }}
        """)
        button_bar.addWidget(cancel_btn)

        confirm_btn = QPushButton("✓ Import Selected Components")
        self._confirm_btn = confirm_btn
        confirm_btn.setDefault(True)
        confirm_btn.clicked.connect(self.accept)
        confirm_btn.setStyleSheet(f"""
            QPushButton {{
                background: {THEME['accent_primary']};
                border: none;
                border-radius: 4px;
                padding: 8px 24px;
                color: {THEME['text_inverse']};
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background: {THEME['accent_primary_hover']};
            }}
        """)
        button_bar.addWidget(confirm_btn)

        layout.addLayout(button_bar)

    def _tool_button_style(self) -> str:
        return f"""
            QPushButton {{
                background: {THEME['bg_panel']};
                border: 1px solid {THEME['border_subtle']};
                border-radius: 4px;
                padding: 5px 12px;
                color: {THEME['text_primary']};
                font-size: 11px;
            }}
            QPushButton:hover {{
                background: {THEME['accent_selection']};
                border-color: {THEME['accent_primary']};
            }}
            QPushButton:disabled {{
                color: {THEME['text_secondary']};
                background: {THEME['bg_app']};
            }}
        """

    # ── Preview generation ────────────────────────────────────────────

    def _generate_previews(self) -> None:
        """Generate overview + per-component preview thumbnails."""
        QApplication.processEvents()

        # Full modelspace overview
        # The renderer reports the model rectangle its image covers.  Working
        # that out separately here is what put every box in the wrong place:
        # matplotlib does not always honour the geometry it is asked for.
        self._overview_image, self._model_bounds = render_full_modelspace_preview(
            self._doc, self._msp, preview_dpi=72, log=self._log,
        )

        if self._overview_image:
            if self._model_bounds:
                self._overview.set_overview_image(
                    self._overview_image, self._model_bounds
                )

        # Build component cards with previews
        self._rebuild_cards()

    def _rebuild_cards(self) -> None:
        """Rebuild the component card list from current self._components."""
        # Clear existing cards
        for cid, card in self._cards.items():
            self._cards_layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()
        self._selected_ids.clear()

        # Clear overview bboxes
        self._overview.clear_bboxes()

        for idx, comp in enumerate(self._components):
            color = _COMP_COLORS[idx % len(_COMP_COLORS)]
            card = _ComponentCard(
                comp, color, on_include_changed=self._update_included_count
            )
            card.selection_toggled.connect(self._on_card_selected)

            # Generate preview thumbnail (same render path, lower DPI)
            preview = render_component_preview(
                self._doc, self._msp, comp,
                preview_dpi=72, cache=self._cache, log=self._log,
            )
            if preview:
                card.set_thumbnail(_pil_to_qpixmap(preview, max_size=140))

            self._cards[comp.id] = card
            # Insert before the stretch
            self._cards_layout.insertWidget(
                self._cards_layout.count() - 1, card
            )

            # Add bbox to overview
            if self._model_bounds:
                self._overview.add_component_bbox(comp, color)

        self._update_status()
        self.setWindowTitle(
            f"DWG Component Extraction — {len(self._components)} Detected"
        )

    def _update_status(self) -> None:
        """Update the bottom status label."""
        included = sum(1 for c in self._components if self._cards.get(c.id) and self._cards[c.id].is_included())
        self._status_label.setText(f"{included} of {len(self._components)} components will be imported")
        self._merge_btn.setEnabled(len(self._selected_ids) >= 2)
        self._split_btn.setEnabled(len(self._selected_ids) == 1)

    # ── Callbacks ─────────────────────────────────────────────────────

    def _on_bbox_adjusted(self, comp_id: str) -> None:
        """Handle a component bounding box being manually resized or moved."""
        card = self._cards.get(comp_id)
        comp = next((c for c in self._components if c.id == comp_id), None)
        if card and comp:
            xmin, ymin, xmax, ymax = comp.bbox
            card.update_bbox_label(f"Region: ({xmin:.0f}, {ymin:.0f}) → ({xmax:.0f}, {ymax:.0f})")

            # Update the component's entity list so the final render includes everything in the new box
            try:
                from ezdxf import bbox as ezdxf_bbox
                from app.core.dwg_components import _bboxes_overlap
                new_ids = []
                for e in self._msp:
                    box = ezdxf_bbox.extents([e], fast=True, cache=self._cache)
                    if box.has_data:
                        eb = (box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y)
                        if _bboxes_overlap(comp.bbox, eb):
                            new_ids.append(e.dxf.handle)
                comp.entity_ids = new_ids

                # Overlap alone drops every dimension sitting outside the
                # box, which is what made a tight box lose its measurements.
                # Pull the ones belonging to this geometry back in and widen
                # the box to cover them.
                from app.core.dwg_components import include_related_annotations
                recovered = include_related_annotations(
                    comp, self._msp, self._cache
                )
                if recovered:
                    xmin, ymin, xmax, ymax = comp.bbox
                    card.update_bbox_label(
                        f"Region: ({xmin:.0f}, {ymin:.0f}) → "
                        f"({xmax:.0f}, {ymax:.0f})  +{recovered} dims"
                    )
                    self._overview.refresh_component_bbox(comp)

                # Also update the thumbnail preview on the left panel
                from app.core.dwg_components import render_component_preview
                preview = render_component_preview(
                    self._doc, self._msp, comp,
                    preview_dpi=72, cache=self._cache, log=self._log
                )
                if preview:
                    card.set_thumbnail(_pil_to_qpixmap(preview, max_size=140))
                    
            except Exception as e:
                self._log("warning", f"Failed to recompute entities for adjusted bbox: {e}")

    # ── Card selection ────────────────────────────────────────────────

    def _on_card_selected(self, comp_id: str, selected: bool) -> None:
        """Handle a component card being clicked."""
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            # Toggle selection
            if comp_id in self._selected_ids:
                self._selected_ids.remove(comp_id)
            else:
                self._selected_ids.append(comp_id)
        else:
            # Single selection
            self._selected_ids = [comp_id]

        # Update visual state
        for cid, card in self._cards.items():
            card.set_selected_style(cid in self._selected_ids)

        self._update_status()

    # ── Merge ─────────────────────────────────────────────────────────

    def _merge_selected(self) -> None:
        """Merge all selected components into one."""
        if len(self._selected_ids) < 2:
            return

        to_merge = [c for c in self._components if c.id in self._selected_ids]
        if len(to_merge) < 2:
            return

        merged = merge_components(to_merge)

        # Remove merged components, add the new one
        self._components = [c for c in self._components if c.id not in self._selected_ids]
        self._components.append(merged)

        self._log("info", f"Merged {len(to_merge)} components into {merged.id}")
        self._rebuild_cards()

    # ── Split ─────────────────────────────────────────────────────────

    def _start_split(self) -> None:
        """Enter split mode — user draws a line on the overview."""
        if len(self._selected_ids) != 1:
            return
        self._overview.set_draw_mode("split")
        self._log("info", "Draw a line on the overview to split the selected component")

    def _on_split_line_drawn(self, position: float, axis: str) -> None:
        """Handle split line drawn on overview."""
        if len(self._selected_ids) != 1:
            return

        comp_id = self._selected_ids[0]
        comp = next((c for c in self._components if c.id == comp_id), None)
        if not comp:
            return

        # Build entity bboxes dict if needed
        self._ensure_entity_bboxes()

        part_a, part_b = split_component(comp, position, axis, self._entity_bboxes)

        if part_a.entity_count == 0 or part_b.entity_count == 0:
            QMessageBox.information(
                self, "Split Result",
                "The split line didn't divide the component — "
                "all entities fell on one side. Try a different position.",
            )
            return

        # Replace original with two new parts
        self._components = [c for c in self._components if c.id != comp_id]
        self._components.extend([part_a, part_b])

        self._log("info", f"Split {comp_id} into {part_a.id} ({part_a.entity_count} entities) "
                  f"and {part_b.id} ({part_b.entity_count} entities)")
        self._rebuild_cards()

    # ── Add Custom Region ─────────────────────────────────────────────

    def _start_custom_region(self) -> None:
        """Enter custom region mode — user draws a rectangle on overview."""
        self._overview.set_draw_mode("custom_region")
        self._log("info", "Draw a rectangle on the overview to create a custom component region")

    def _on_custom_region_drawn(self, xmin: float, ymin: float, xmax: float, ymax: float) -> None:
        """Handle custom region rectangle drawn on overview."""
        self._ensure_entity_bboxes()

        new_comp = create_custom_region(
            (xmin, ymin, xmax, ymax), self._entity_bboxes
        )

        if new_comp.entity_count == 0:
            QMessageBox.information(
                self, "Custom Region",
                "No entities found inside the drawn region. "
                "Try drawing a larger area.",
            )
            return

        self._components.append(new_comp)
        self._log("info", f"Created custom region {new_comp.id} with {new_comp.entity_count} entities")
        self._rebuild_cards()

    # ── Re-detect ─────────────────────────────────────────────────────

    def _sync_gap_slider_to_result(self) -> None:
        """Show the gap that detection actually used.

        Detection picks the gap that splits this drawing most stably, so
        parking the slider on a fixed 4% misreports it - and one click of
        Re-detect would then silently discard the better result.
        """
        result = getattr(self, "_result", None)
        threshold = getattr(result, "gap_threshold", None)
        if not threshold:
            return
        bounds = self._model_bounds
        if not bounds:
            try:
                from ezdxf.bbox import extents
                box = extents(self._msp, fast=True)
                if not box.has_data:
                    return
                bounds = (box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y)
            except Exception:
                return
        diag = math.hypot(bounds[2] - bounds[0], bounds[3] - bounds[1])
        if diag <= 0:
            return
        slider_val = int(round(threshold / diag * 1000))
        slider_val = max(self._gap_slider.minimum(),
                         min(self._gap_slider.maximum(), slider_val))
        self._gap_slider.setValue(slider_val)
        self._gap_value_label.setText(f"{slider_val / 10:.1f}%")

    def _set_all_included(self, included: bool) -> None:
        """Tick or untick every component at once."""
        for card in self._cards.values():
            card.set_included(included)
        self._update_included_count()

    def _update_included_count(self) -> None:
        """Keep the footer count honest as boxes are ticked."""
        included = sum(1 for c in self._cards.values() if c.is_included())
        self._status_label.setText(
            f"{included} of {len(self._components)} components will be imported"
        )
        if hasattr(self, "_confirm_btn"):
            self._confirm_btn.setEnabled(included > 0)

    def _redetect(self) -> None:
        """Re-run component detection with the adjusted gap threshold."""
        from app.core.dwg_components import detect_components

        slider_val = self._gap_slider.value()
        pct = slider_val / 10.0 / 100.0  # e.g. slider 40 → 4.0% → 0.04

        # Compute diagonal to get absolute threshold
        if self._model_bounds:
            mw = self._model_bounds[2] - self._model_bounds[0]
            mh = self._model_bounds[3] - self._model_bounds[1]
            diag = math.hypot(mw, mh)
        else:
            diag = 1000.0

        new_threshold = diag * pct
        self._log("info", f"Re-detecting with gap_threshold={new_threshold:.2f} ({pct*100:.1f}% of diagonal)")

        new_result = detect_components(self._msp, self._cache, new_threshold, self._log)
        self._components = list(new_result.components)
        self._result = new_result

        self._rebuild_cards()

        # Update unassigned warning
        # (The info bar was built at init, so we log instead)
        if new_result.unassigned_count > 0:
            self._log("info", f"Re-detection: {new_result.unassigned_count} entities still unassigned")

    # ── Helpers ───────────────────────────────────────────────────────

    def _ensure_entity_bboxes(self) -> None:
        """Lazily build the entity_bboxes dict for split/custom ops."""
        if self._entity_bboxes:
            return

        from app.core.dwg_components import _compute_entity_bboxes
        pairs = _compute_entity_bboxes(self._msp, self._cache, self._log)
        self._entity_bboxes = {handle: bbox for handle, bbox in pairs}

    # ── Public accessors for the caller ───────────────────────────────

    def get_included_components(self) -> list[ComponentRegion]:
        """The components ticked for import.  Only valid after accept()."""
        return [
            comp for comp in self._components
            if (self._cards.get(comp.id) is not None
                and self._cards[comp.id].is_included())
        ]
