"""
Callout manager — simplified canvas-based callout creation.

Under the new WYSIWYG model, callout creation is:
  1. Pick the crop source asset + draw a crop_box on the canvas.
  2. The circle crop is placed onto the canvas as a draggable PlacedItem.
  3. The leader line's far end is dragged directly on the canvas.

There is no more "pick a target slot from dropdown" — the architect
drags the leader tip to wherever it should point, directly on the
sheet.
"""

from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QLineEdit, QGroupBox, QComboBox, QFrame,
)

from app.ui.theme import THEME
from app.core.project_state import ProjectState, PlacedItem


class CalloutManager(QWidget):
    """Canvas-based callout creation panel.

    Signals
    -------
    request_crop_mode()
        Ask the main window to switch the canvas to crop-box mode
        and show the crop source asset as a preview overlay.
    callout_created(str)
        Emitted with placed_item_id when a new callout is fully defined
        and placed on the canvas.
    """

    request_crop_mode = Signal()
    callout_created = Signal(str)

    _letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def __init__(self, project_state: ProjectState, parent=None):
        super().__init__(parent)
        self._state = project_state

        # Current callout being built (partial state)
        self._crop_source_id: str | None = None
        self._crop_box: tuple | None = None  # (x1, y1, x2, y2) unified format

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # ── Step 1: Crop Source ───────────────────────────────────────
        step1_group = QGroupBox("Step 1: Crop From (source image)")
        step1_layout = QVBoxLayout(step1_group)

        step1_layout.addWidget(QLabel("Select the asset to crop the detail circle from:"))

        self._crop_source_combo = QComboBox()
        step1_layout.addWidget(self._crop_source_combo)

        self._crop_btn = QPushButton("Draw Crop Box on Canvas")
        self._crop_btn.setToolTip(
            "Switch canvas to crop-box mode, show the selected asset, "
            "and draw the detail region"
        )
        self._crop_btn.clicked.connect(self._start_crop)
        step1_layout.addWidget(self._crop_btn)

        self._crop_status = QLabel("No crop defined yet")
        self._crop_status.setObjectName("helpText")
        step1_layout.addWidget(self._crop_status)

        layout.addWidget(step1_group)

        # ── Arrow ─────────────────────────────────────────────────────
        arrow = QLabel("↓  Crop will be placed on canvas — drag it into position  ↓")
        arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        arrow.setObjectName("helpText")
        arrow.setWordWrap(True)
        layout.addWidget(arrow)

        # ── Callout details ───────────────────────────────────────────
        details_group = QGroupBox("Detail Info")
        details_layout = QVBoxLayout(details_group)

        detail_row = QHBoxLayout()
        detail_row.addWidget(QLabel("ID:"))
        self._callout_id = QLineEdit()
        self._callout_id.setPlaceholderText("e.g. DETAIL A")
        detail_row.addWidget(self._callout_id)
        details_layout.addLayout(detail_row)

        desc_row = QHBoxLayout()
        desc_row.addWidget(QLabel("Desc:"))
        self._description = QLineEdit()
        self._description.setPlaceholderText("e.g. BED PANEL CURVE DETAIL")
        desc_row.addWidget(self._description)
        details_layout.addLayout(desc_row)

        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("Line:"))
        self._leader_style = QComboBox()
        self._leader_style.addItems(["dashed", "solid"])
        style_row.addWidget(self._leader_style)
        details_layout.addLayout(style_row)

        layout.addWidget(details_group)

        # ── Create button ─────────────────────────────────────────────
        self._create_btn = QPushButton("Place Callout on Sheet")
        self._create_btn.setToolTip(
            "Create the callout circle and place it on the canvas. "
            "Then drag it and its leader line into position."
        )
        self._create_btn.clicked.connect(self._create_callout)
        layout.addWidget(self._create_btn)

        # ── Existing callouts list ────────────────────────────────────
        existing_group = QGroupBox("Existing Callouts")
        existing_layout = QVBoxLayout(existing_group)

        self._callout_list = QListWidget()
        existing_layout.addWidget(self._callout_list)

        remove_btn = QPushButton("Remove Selected")
        remove_btn.setObjectName("secondaryButton")
        remove_btn.clicked.connect(self._remove_selected)
        existing_layout.addWidget(remove_btn)

        layout.addWidget(existing_group)

    def refresh(self) -> None:
        """Refresh combo boxes and callout list from project state."""
        self._refresh_combos()
        self._refresh_callout_list()
        self._auto_fill_next_id()

    def _refresh_combos(self) -> None:
        self._crop_source_combo.clear()
        for asset_id, asset in self._state.assets.items():
            self._crop_source_combo.addItem(
                f"{asset.source_type}: {asset_id}", asset_id
            )

    def _refresh_callout_list(self) -> None:
        self._callout_list.clear()
        for pi in self._state.placed_items:
            if pi.item_type == "callout_circle":
                label = f"{pi.callout_id}: {pi.description}"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, pi.id)
                self._callout_list.addItem(item)

    def _auto_fill_next_id(self) -> None:
        """Auto-suggest the next DETAIL letter."""
        callouts = [pi for pi in self._state.placed_items if pi.item_type == "callout_circle"]
        existing = {c.callout_id for c in callouts if c.callout_id}
        for letter in self._letters:
            candidate = f"DETAIL {letter}"
            if candidate not in existing:
                self._callout_id.setText(candidate)
                return
        self._callout_id.setText(f"DETAIL {len(callouts) + 1}")

    # ── Step 1: Crop ──────────────────────────────────────────────────

    def _start_crop(self) -> None:
        idx = self._crop_source_combo.currentIndex()
        if idx >= 0:
            self._crop_source_id = self._crop_source_combo.currentData()
            # Request circular crop mode for callouts (like the DINING BENCH LEG example)
            self.request_crop_mode.emit()
            self._crop_status.setText(
                f"Draw CIRCULAR crop on canvas for: {self._crop_source_id}\n(Click and drag to define circle area)"
            )

    def get_crop_source_id(self) -> str | None:
        """Return the currently selected crop source asset ID."""
        return self._crop_source_id

    def receive_crop_box(self, rect: QRectF) -> None:
        """Called by main_window when a crop box is drawn on the canvas.
        Stores as unified (x1, y1, x2, y2) format."""
        self._crop_box = (
            rect.x(), rect.y(),
            rect.x() + rect.width(),
            rect.y() + rect.height(),
        )
        self._crop_status.setText(
            f"Crop: ({self._crop_box[0]:.0f}, {self._crop_box[1]:.0f}) → "
            f"({self._crop_box[2]:.0f}, {self._crop_box[3]:.0f})"
        )

    # ── Create callout ────────────────────────────────────────────────

    def _create_callout(self) -> None:
        """Create a callout PlacedItem and add it to the project.

        The circle crop is placed on the canvas at a default position in
        the callout column area.  The architect then drags it and the
        leader line into final position.
        """
        callout_id = self._callout_id.text().strip()
        description = self._description.text().strip()

        if not callout_id:
            return
        if not self._crop_source_id:
            self._crop_status.setText("⚠ Select a crop source asset first")
            return
        if not self._crop_box:
            self._crop_status.setText("⚠ Draw a crop box on the canvas first")
            return

        leader_style = self._leader_style.currentText()

        # Default placement position — use callout column if available,
        # otherwise place near the right side of the page
        template = self._state.template
        if template and template.callout_column:
            cc = template.callout_column
            callout_count = len([
                pi for pi in self._state.placed_items
                if pi.item_type == "callout_circle"
            ])
            circle_size = 100  # default size in PDF points
            cx = cc["x"] + (cc["w"] - circle_size) / 2.0
            cy = cc["y"] + callout_count * (circle_size + 20)
        else:
            circle_size = 100
            cx = 1400
            cy = 200 + len([pi for pi in self._state.placed_items if pi.item_type == 'callout_circle']) * 120

        # Create the PlacedItem
        placed_item = PlacedItem(
            id=PlacedItem.make_id(),
            item_type="callout_circle",
            source_asset_id=self._crop_source_id,
            crop_box=self._crop_box,
            page_rect=(cx, cy, circle_size, circle_size),
            callout_id=callout_id,
            description=description,
            leader_style=leader_style,
            leader_target_page_pos=(cx - 200, cy + circle_size / 2),  # default leader target
        )

        try:
            self._state.add_placed_item(placed_item)
        except KeyError:
            self._crop_status.setText("⚠ Invalid asset reference")
            return
            
        self.callout_created.emit(placed_item.id)

        # Create Callout record
        callout = Callout(
            id=callout_id,
            description=description,
            crop_source_asset_id=self._crop_source_id,
            crop_box=self._crop_box,
            placed_item_id=placed_item.id,
            leader_style=leader_style,
        )
        try:
            self._state.add_callout(callout)
        except KeyError:
            pass  # PlacedItem already added, callout is secondary

        # Reset for next callout
        self._crop_box = None
        self._crop_status.setText("No crop defined yet")

        self.refresh()
        self.callout_created.emit(placed_item.id)

    def _remove_selected(self) -> None:
        item = self._callout_list.currentItem()
        if not item:
            return
        callout_id = item.data(Qt.ItemDataRole.UserRole)

        # Find and remove the placed item associated with this callout
        callout = self._state.get_callout(callout_id)
        if callout:
            self._state.remove_placed_item(callout.placed_item_id, record_undo=True)

        self._state.remove_callout(callout_id)
        self.refresh()
