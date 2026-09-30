"""
Staging tray — drag-enabled asset thumbnail panel.

Imported assets appear here as thumbnails. The architect drags them
onto the WYSIWYG canvas to place them on the sheet. This replaces the
old "select asset → select slot → click Assign" workflow from
SlotAssignmentPanel.

Supports:
- Icon-mode thumbnail display with source-type labels
- Drag-and-drop onto the canvas via QMimeData
- Right-click context menu (Remove, preview info)
- Click-to-select for preview / crop workflows
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QMimeData, QByteArray, Signal, QSize
from PySide6.QtGui import QPixmap, QImage, QDrag, QPainter, QColor, QFont, QPen
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QListWidget, QListWidgetItem,
    QGroupBox, QLabel, QMenu, QPushButton,
)

from app.ui.theme import THEME
from app.ui.image_bridge import pil_to_qpixmap, placeholder_pixmap
from app.core.project_state import ProjectState


def _pil_to_qpixmap(pil_img, max_size: int = 120) -> QPixmap:
    """A thumbnail for the tray.

    The conversion itself lives in image_bridge, which is the only copy
    that tells Qt the PIL buffer's real stride and takes ownership of the
    pixels.  The version that used to be here did neither, which is why a
    thumbnail of an image whose width was not a multiple of four came out
    sheared and grey - and why dragging one could take the app with it.
    """
    if pil_img is None:
        return placeholder_pixmap(max_size, THEME.get("bg_panel", "#2B2B2B"))
    return pil_to_qpixmap(pil_img, max_size=max_size)


class StagingTray(QWidget):
    """Panel showing imported asset thumbnails for drag-and-drop placement.

    Signals
    -------
    asset_selected(str)
        Emitted with asset_id when the user clicks a thumbnail.
    asset_remove_requested(str)
        Emitted when the user requests to remove an asset.
    """

    asset_selected = Signal(str)
    asset_remove_requested = Signal(str)

    def __init__(self, project_state: ProjectState, parent=None):
        super().__init__(parent)
        self._state = project_state

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        # Header
        header = QLabel("Assets")
        header.setObjectName("panelTitle")
        layout.addWidget(header)

        hint = QLabel("Drag an asset directly onto the drawing sheet.")
        hint.setObjectName("helpText")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._empty_state = QLabel("No assets yet\n\nDrag files here or use Import to get started")
        self._empty_state.setObjectName("emptyState")
        self._empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_state.setWordWrap(True)
        layout.addWidget(self._empty_state)

        # Retained as an icon-mode view for the existing drag MIME contract, styled as cards.
        self._asset_list = QListWidget()
        self._asset_list.setObjectName("assetCardGrid")
        self._asset_list.setViewMode(QListWidget.ViewMode.IconMode)
        self._asset_list.setIconSize(QSize(116, 116))
        self._asset_list.setGridSize(QSize(142, 168))
        self._asset_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._asset_list.setWrapping(True)
        self._asset_list.setSpacing(10)
        self._asset_list.setDragEnabled(True)
        self._asset_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._asset_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        layout.addWidget(self._asset_list, 1)

        # Header
        header = QLabel("Drag assets onto the sheet to place them")
        header.setObjectName("helpText")
        header.setWordWrap(True)
        # Wire signals
        self._asset_list.currentItemChanged.connect(self._on_item_selected)
        self._asset_list.customContextMenuRequested.connect(self._on_context_menu)

        # Info label
        self._info_label = QLabel("No assets imported")
        self._info_label.setObjectName("helpText")
        layout.addWidget(self._info_label)

        # Override startDrag to use our custom MIME data
        self._asset_list.startDrag = self._start_drag

    def refresh(self) -> None:
        """Refresh the thumbnail list from the current project state."""
        self._asset_list.clear()

        for asset_id, asset in self._state.assets.items():
            # Create thumbnail
            pixmap = _pil_to_qpixmap(asset.image, max_size=100)

            # Create list item
            type_label = asset.source_type.replace("_", " ").title()
            dims = ""
            if asset.image:
                dims = f"\n{asset.image.width}×{asset.image.height}"

            item = QListWidgetItem()
            item.setIcon(pixmap)
            item.setText(f"{type_label}{dims}")
            item.setData(Qt.ItemDataRole.UserRole, asset_id)

            tooltip_parts = [
                f"Asset: {asset_id}",
                f"Type: {asset.source_type}",
                f"Source: {asset.source_path}",
            ]
            if asset.vector_source_path:
                tooltip_parts.append(f"Vector PDF: {asset.vector_source_path}")
            if asset.image:
                tooltip_parts.append(f"Size: {asset.image.width}×{asset.image.height}")
            if asset.dwg_component_id:
                tooltip_parts.append(f"Component ID: {asset.dwg_component_id}")

            item.setToolTip("\n".join(tooltip_parts))
            self._asset_list.addItem(item)

        count = len(self._state.assets)
        self._empty_state.setVisible(count == 0)
        self._asset_list.setVisible(count > 0)
        self._info_label.setText(
            f"{count} asset{'s' if count != 1 else ''} imported"
            if count > 0 else "No assets imported"
        )

    def _on_item_selected(self, current, previous) -> None:
        if current:
            asset_id = current.data(Qt.ItemDataRole.UserRole)
            if asset_id:
                self.asset_selected.emit(asset_id)

    def _on_context_menu(self, pos) -> None:
        item = self._asset_list.itemAt(pos)
        if not item:
            return

        asset_id = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        remove_action = menu.addAction("Remove from Project")
        action = menu.exec(self._asset_list.mapToGlobal(pos))

        if action == remove_action:
            self.asset_remove_requested.emit(asset_id)

    def _start_drag(self, supported_actions) -> None:
        """Custom drag implementation that carries the asset ID as MIME data."""
        item = self._asset_list.currentItem()
        if not item:
            return

        asset_id = item.data(Qt.ItemDataRole.UserRole)
        if not asset_id:
            return

        # Create drag with asset ID as custom MIME data
        drag = QDrag(self._asset_list)
        mime = QMimeData()
        mime.setData("application/x-asset-id", QByteArray(asset_id.encode("utf-8")))

        # Use the item's icon as the drag pixmap
        icon = item.icon()
        if not icon.isNull():
            drag_pixmap = icon.pixmap(QSize(80, 80))
            drag.setPixmap(drag_pixmap)
            drag.setHotSpot(drag_pixmap.rect().center())

        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)
