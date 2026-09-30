"""
In-memory project state — the single source of truth for the current
session's assets, placed items, metadata, and template.

Phase 2 redesign: SheetSlot has been replaced by PlacedItem — a freeform
placement model where the architect drags/resizes content directly on the
canvas in final PDF-point coordinates.

DESIGN RULE (§9): This module has ZERO Qt imports.  It is a plain-Python
state container that the UI layer reads and writes.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Coordinate-format helpers
# ---------------------------------------------------------------------------

def xywh_to_xyxy(rect: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Convert (x, y, w, h) → (x1, y1, x2, y2)."""
    x, y, w, h = rect
    return (x, y, x + w, y + h)


def xyxy_to_xywh(rect: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Convert (x1, y1, x2, y2) → (x, y, w, h)."""
    x1, y1, x2, y2 = rect
    return (x1, y1, x2 - x1, y2 - y1)


# ---------------------------------------------------------------------------
# §4 — ImportedAsset
# ---------------------------------------------------------------------------
@dataclass
class ImportedAsset:
    """A single imported file normalised to raster form.

    Supported source_types: "image", "pdf_page", "dwg_render", "dxf_render"

    For DWG/DXF-derived content, ``vector_source_path`` points to a vector
    PDF file that preserves crisp line art; ``image`` is a raster preview
    for canvas display only.
    """

    id: str
    source_path: str
    source_type: str  # "image" | "pdf_page" | "dwg_render" | "dxf_render"
    image: Any  # PIL.Image.Image — typed as Any to avoid importing Pillow here
    vector_source_path: Optional[str] = None  # path to vector PDF for CAD renders

    # DWG/DXF component provenance (optional — for traceability only).
    dwg_source_path: Optional[str] = None      # original .dwg/.dxf file path
    dwg_component_id: Optional[str] = None     # ComponentRegion.id

    @staticmethod
    def make_id() -> str:
        """Generate a unique asset ID."""
        return f"asset_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# PlacedItem — freeform placement model (replaces SheetSlot)
# ---------------------------------------------------------------------------
@dataclass
class PlacedItem:
    """A single element placed on the sheet canvas by the architect.

    ``page_rect`` is the single source of truth for where this item ends
    up on the final PDF — it is set and updated by direct drag/resize
    on the WYSIWYG canvas.

    ``crop_box`` uses the unified (x1, y1, x2, y2) format in the source
    asset's native pixel space.  None means "use the full image."
    """

    id: str                       # uuid
    item_type: str                # "image" | "callout_circle"
    source_asset_id: str          # which ImportedAsset this content comes from
    crop_box: Optional[tuple]     # (x1, y1, x2, y2) in source pixels; None = full
    page_rect: tuple              # (x, y, w, h) in PDF POINTS, top-left origin
    rotation: float = 0.0         # degrees clockwise
    circular_mask: bool = False   # crop_box's square shown as a circle
    z_order: int = 0
    group_id: Optional[str] = None  # group membership for multi-select grouping

    # Callout-only fields (ignored for item_type == "image"):
    callout_id: Optional[str] = None
    description: Optional[str] = None
    leader_style: str = "dashed"
    leader_target_page_pos: Optional[tuple] = None  # (x, y) in PDF points

    @staticmethod
    def make_id() -> str:
        """Generate a unique placed-item ID."""
        return f"placed_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# §4 — Annotation  (view labels, detail notes, sheet title)
# ---------------------------------------------------------------------------
@dataclass
class Annotation:
    """A piece of sheet lettering the architect places by hand.

    Three kinds, all positioned in PDF points with a top-left origin so
    they travel straight from the canvas to the generated sheet:

    ``view_label``
        The titles under each view - "PLAN", "ELEVATION", "3D VIEW",
        "DETAIL A" - drawn with the filled triangle and the rule that
        runs out to the right of the text.
    ``detail_note``
        A dot on the drawing, a leader line, and the wording at the end
        of it: "BED PANEL MOULDING", "HOLES FOR VENTILATION".
        ``target_page_pos`` is the dot; ``page_pos`` is the text.
    ``sheet_title``
        The large name across the bottom of the sheet.
    """

    id: str
    kind: str                      # "view_label" | "detail_note" | "sheet_title"
    text: str
    page_pos: tuple                # (x, y) in PDF points - the text anchor
    target_page_pos: Optional[tuple] = None   # detail_note: the dot on the drawing
    font_size: float = 11.0
    rule_width: float = 110.0      # view_label: length of the rule after the text
    group_id: Optional[str] = None  # moves with the rest of its group

    KINDS = ("view_label", "detail_note", "sheet_title")

    @staticmethod
    def make_id() -> str:
        return f"annot_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# §4 — Callout  (kept for the crop-source two-asset relationship)
# ---------------------------------------------------------------------------
@dataclass
class Callout:
    """A circular detail callout that references TWO different source images:

    - ``crop_source_asset_id``: the asset the circular crop is cut from.
    - ``placed_item_id``: the PlacedItem whose leader-line endpoint is
      set by dragging on the canvas.

    ``crop_box`` uses the unified (x1, y1, x2, y2) format.
    """

    id: str                     # e.g. "DETAIL A"
    description: str            # e.g. "BED PANEL CURVE DETAIL"
    crop_source_asset_id: str   # which ImportedAsset the circle content is cropped from
    crop_box: tuple             # (x1, y1, x2, y2) in that asset's pixel coordinates
    placed_item_id: str         # which PlacedItem on the canvas this callout is
    leader_style: str = "dashed"  # "dashed" | "solid"


# ---------------------------------------------------------------------------
# §4 — RevisionEntry (title block)
# ---------------------------------------------------------------------------
@dataclass
class RevisionEntry:
    """A single row in the Revision History table."""
    rev_no: str = ""
    date: str = ""
    description: str = ""
    drawn_by: str = ""
    approved_by: str = ""


# ---------------------------------------------------------------------------
# §4 — ReferenceDrawing (title block)
# ---------------------------------------------------------------------------
@dataclass
class ReferenceDrawing:
    """A single row in the Reference Drawings table."""
    sr_no: str = ""
    drawing_no: str = ""
    description: str = ""


# ---------------------------------------------------------------------------
# §4 — ProjectMetadata
# ---------------------------------------------------------------------------
@dataclass
class ProjectMetadata:
    """Title-block fields for the firm's branded template."""

    client_name: str = ""
    project_title: str = ""
    sheet_title: str = ""
    job_no: str = ""
    drawing_no: str = ""
    scale: str = "1:100"
    date: str = ""
    rev_no: str = ""
    drawn_by: str = ""
    approved_by: str = ""
    paper_size: str = "A1"
    north_direction: str = ""  # e.g. "N", "NE", compass bearing
    released_for: str = ""     # comma-separated: "Preliminary,Tender,Information,Approval,Construction"
    description: str = ""       # Description / title field from Excel panel


# ---------------------------------------------------------------------------
# §4 — SheetTemplate
# ---------------------------------------------------------------------------
@dataclass
class SheetTemplate:
    """Describes a sheet layout template — the firm's branded letterhead
    plus paper size/orientation.

    ``guide_rects`` are optional snap-guide rectangles loaded from the
    calibration JSON — they are rendered as faint dashed outlines on the
    canvas purely as visual snap targets, never as hard constraints.
    """

    template_name: str
    base_pdf_path: str  # path to the firm's blank branded letterhead PDF
    paper_size: str  # e.g. "A1"
    orientation: str  # "landscape" | "portrait"
    guide_rects: dict  # slot_id -> {"x", "y", "w", "h"} — optional snap guides
    callout_column: dict  # {"x", "y", "w", "h", "max_details"}


# ---------------------------------------------------------------------------
# Undo stack entry
# ---------------------------------------------------------------------------
@dataclass
class UndoEntry:
    """Everything on the sheet at one moment, with a name for the step.

    Snapshots rather than per-field deltas: a single action can touch a
    placed item, an annotation and a group at once, and tracking each of
    those separately is how most of the app's edits ended up outside undo
    in the first place.  Imported assets are held by reference - only the
    arrangement is copied, so a snapshot stays cheap.
    """

    label: str
    placed_items: list
    annotations: list
    groups: dict


# ---------------------------------------------------------------------------
# Session container
# ---------------------------------------------------------------------------
class ProjectState:
    """Holds all session data in memory."""

    UNDO_STACK_SIZE = 20

    def __init__(self):
        self.assets: dict[str, ImportedAsset] = {}
        self.placed_items: list[PlacedItem] = []
        self.metadata: ProjectMetadata = ProjectMetadata()
        self.template: Optional[SheetTemplate] = None

        # Title block data
        self.revision_history: list[RevisionEntry] = []
        self.reference_drawings: list[ReferenceDrawing] = []
        self.callouts: list[Callout] = []
        self.annotations: list[Annotation] = []

        # Grouping for multi-select
        self.groups: dict[str, list[str]] = {}  # group_id -> [placed_item_id, ...]

        # Undo/redo history of whole-document snapshots.
        self._undo_stack: list[UndoEntry] = []
        self._redo_stack: list[UndoEntry] = []
        self._pending_change: Optional[UndoEntry] = None

        # Counts every edit, so the window can tell whether there is
        # anything worth autosaving or worth warning about on close.
        # A counter rather than a flag: the autosave remembers the value
        # it last wrote, so two saves in a row cost one write.
        self.revision: int = 0

    def touch(self) -> None:
        """Note that something about the sheet changed."""
        self.revision += 1

    # ── Asset management ──────────────────────────────────────────────

    def add_asset(self, asset: ImportedAsset) -> None:
        """Register an imported asset."""
        self.assets[asset.id] = asset
        self.touch()

    def remove_asset(self, asset_id: str) -> None:
        """Remove an asset and any placed items referencing it."""
        self.assets.pop(asset_id, None)
        removed = [
            pi.id for pi in self.placed_items
            if pi.source_asset_id == asset_id
        ]
        self.placed_items = [
            pi for pi in self.placed_items
            if pi.source_asset_id != asset_id
        ]
        self.callouts = [
            c for c in self.callouts
            if c.crop_source_asset_id != asset_id
        ]
        for item_id in removed:
            self._forget_group_member(item_id)
        self.touch()

    def get_asset(self, asset_id: str) -> Optional[ImportedAsset]:
        """Look up an asset by ID, returning None if not found."""
        return self.assets.get(asset_id)

    # ── Placed-item management ────────────────────────────────────────

    def add_placed_item(self, item: PlacedItem, record_undo: bool = True) -> None:
        """Add a placed item to the canvas."""
        if item.source_asset_id not in self.assets:
            raise KeyError(
                f"PlacedItem source_asset_id {item.source_asset_id!r} "
                f"not found in assets"
            )
        if record_undo:
            self.begin_change(f"Add {item.item_type.replace('_', ' ')}")
        self.placed_items.append(item)
        if record_undo:
            self.commit_change()

    def remove_placed_item(self, item_id: str, record_undo: bool = True) -> None:
        """Remove a placed item by its ID."""
        for i, pi in enumerate(self.placed_items):
            if pi.id == item_id:
                if record_undo:
                    self.begin_change(
                        f"Delete {pi.item_type.replace('_', ' ')}"
                    )
                self.placed_items.pop(i)
                self.callouts = [
                    c for c in self.callouts
                    if c.placed_item_id != item_id
                ]
                # The snapshot taken above was never banked, so deleting
                # an item could not be undone - and the orphaned pending
                # change then swallowed whichever edit came next.
                if record_undo:
                    self.commit_change()
                self._forget_group_member(item_id)
                return

    def update_placed_item(self, item_id: str, **kwargs) -> None:
        """Update fields on a placed item.  Records undo for the change."""
        item = self.get_placed_item(item_id)
        if item is None:
            return
        with self.change("Edit item"):
            for key, value in kwargs.items():
                if hasattr(item, key):
                    setattr(item, key, value)

    def get_placed_item(self, item_id: str) -> Optional[PlacedItem]:
        """Look up a placed item by ID."""
        for pi in self.placed_items:
            if pi.id == item_id:
                return pi
        return None

    # ── Annotations ───────────────────────────────────────────────────

    def add_annotation(self, annotation: Annotation) -> None:
        """Add a view label, detail note or sheet title."""
        if annotation.kind not in Annotation.KINDS:
            raise ValueError(f"Unknown annotation kind: {annotation.kind}")
        self.annotations.append(annotation)
        self.touch()

    def get_annotation(self, annotation_id: str) -> Optional[Annotation]:
        for a in self.annotations:
            if a.id == annotation_id:
                return a
        return None

    def remove_annotation(self, annotation_id: str) -> bool:
        for i, a in enumerate(self.annotations):
            if a.id == annotation_id:
                self.annotations.pop(i)
                self._forget_group_member(annotation_id)
                return True
        return False

    # ── Grouping ──────────────────────────────────────────────────────

    def _grouped_record(self, item_id: str):
        """A placed item or an annotation, whichever carries this id."""
        return self.get_placed_item(item_id) or self.get_annotation(item_id)

    def _forget_group_member(self, item_id: str) -> None:
        """Take a deleted id out of whatever group still lists it.

        A group that kept naming a member the sheet no longer has meant
        selecting one of its siblings tried to select a graphics item
        that had been removed from the scene.
        """
        empty = []
        for group_id, members in self.groups.items():
            if item_id in members:
                members.remove(item_id)
            if len(members) < 2:
                empty.append(group_id)
        for group_id in empty:
            for member_id in self.groups.pop(group_id, []):
                record = self._grouped_record(member_id)
                if record is not None:
                    record.group_id = None

    def create_group(self, item_ids: list[str]) -> str:
        """Group the given items.

        Ids may name placed drawings or sheet lettering: a detail circle
        and the note that labels it belong together, and grouping only
        drawings would leave the note behind whenever the circle moves.
        """
        group_id = f"group_{uuid.uuid4().hex[:8]}"
        members = []
        for item_id in item_ids:
            record = self._grouped_record(item_id)
            if record is not None:
                record.group_id = group_id
                members.append(item_id)
        self.groups[group_id] = members
        return group_id

    def ungroup(self, group_id: str) -> None:
        """Dissolve a group, clearing group_id from all members."""
        member_ids = self.groups.pop(group_id, [])
        for item_id in member_ids:
            record = self._grouped_record(item_id)
            if record is not None:
                record.group_id = None

    def get_group_members(self, group_id: str) -> list[PlacedItem]:
        """The placed drawings in a group."""
        return [pi for pi in self.placed_items if pi.group_id == group_id]

    def get_group_member_ids(self, group_id: str) -> list[str]:
        """Every id in a group, drawings and lettering alike."""
        ids = [pi.id for pi in self.placed_items if pi.group_id == group_id]
        ids += [a.id for a in self.annotations if a.group_id == group_id]
        return ids

    def group_id_for(self, item_id: str) -> Optional[str]:
        """Which group an id belongs to, if any."""
        record = self._grouped_record(item_id)
        return getattr(record, "group_id", None) if record else None

    # ── Undo / redo ───────────────────────────────────────────────────

    def _snapshot(self, label: str = "") -> UndoEntry:
        return UndoEntry(
            label=label,
            placed_items=copy.deepcopy(self.placed_items),
            annotations=copy.deepcopy(self.annotations),
            groups=copy.deepcopy(self.groups),
        )

    @staticmethod
    def _same(a: UndoEntry, b: UndoEntry) -> bool:
        return (a.placed_items == b.placed_items
                and a.annotations == b.annotations
                and a.groups == b.groups)

    def begin_change(self, label: str) -> None:
        """Remember the sheet as it is, before an edit.

        Safe to call on mouse-down: if the gesture turns out to change
        nothing, commit_change throws the snapshot away rather than
        filling the history with no-ops.
        """
        self._pending_change = self._snapshot(label)

    def commit_change(self) -> bool:
        """Bank the pending snapshot, if the edit actually changed anything."""
        pending = self._pending_change
        self._pending_change = None
        if pending is None or self._same(pending, self._snapshot()):
            return False
        self._undo_stack.append(pending)
        if len(self._undo_stack) > self.UNDO_STACK_SIZE:
            self._undo_stack.pop(0)
        # A fresh edit is a new branch of history.
        self._redo_stack.clear()
        self.touch()
        return True

    def abandon_change(self) -> None:
        """Drop the pending snapshot without recording anything."""
        self._pending_change = None

    @contextmanager
    def change(self, label: str):
        """Wrap an edit so it becomes one undo step."""
        self.begin_change(label)
        try:
            yield
        except Exception:
            self.abandon_change()
            raise
        self.commit_change()

    def _restore(self, entry: UndoEntry) -> None:
        self.placed_items = copy.deepcopy(entry.placed_items)
        self.annotations = copy.deepcopy(entry.annotations)
        self.groups = copy.deepcopy(entry.groups)
        self.touch()

    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    def undo_label(self) -> Optional[str]:
        return self._undo_stack[-1].label if self._undo_stack else None

    def redo_label(self) -> Optional[str]:
        return self._redo_stack[-1].label if self._redo_stack else None

    def undo(self) -> Optional[str]:
        """Step back one edit.  Returns what was undone."""
        if not self._undo_stack:
            return None
        entry = self._undo_stack.pop()
        # Keep where we were, so redo can come back to it.
        self._redo_stack.append(self._snapshot(entry.label))
        self._restore(entry)
        return entry.label

    def redo(self) -> Optional[str]:
        """Step forward again.  Returns what was redone."""
        if not self._redo_stack:
            return None
        entry = self._redo_stack.pop()
        self._undo_stack.append(self._snapshot(entry.label))
        self._restore(entry)
        return entry.label

    # ── Template setup ────────────────────────────────────────────────

    def init_from_template(self, template: SheetTemplate) -> None:
        """Initialise the project from a sheet template."""
        self.template = template
        self.touch()

    # ── Callout management ────────────────────────────────────────────

    def add_callout(self, callout: Callout) -> None:
        if callout.crop_source_asset_id not in self.assets:
            raise KeyError(
                f"Callout crop_source_asset_id {callout.crop_source_asset_id!r} "
                f"not found in assets"
            )
        if not any(pi.id == callout.placed_item_id for pi in self.placed_items):
            raise KeyError(
                f"Callout placed_item_id {callout.placed_item_id!r} "
                f"not found in placed_items"
            )
        self.callouts.append(callout)
        self.touch()

    def remove_callout(self, callout_id: str) -> None:
        self.callouts = [c for c in self.callouts if c.id != callout_id]
        self.touch()

    def get_callout(self, callout_id: str) -> Optional[Callout]:
        for c in self.callouts:
            if c.id == callout_id:
                return c
        return None

    # ── Readiness checks ──────────────────────────────────────────────

    def is_ready_to_generate(self) -> tuple[bool, list[str]]:
        issues = []
        if self.template is None:
            issues.append("No sheet template loaded")
        if not self.metadata.client_name:
            issues.append("Client name is empty")
        if not self.metadata.project_title:
            issues.append("Project title is empty")
        if not self.metadata.sheet_title:
            issues.append("Sheet title is empty")
        if len(self.placed_items) == 0:
            issues.append("No items placed on the sheet")
        return (len(issues) == 0, issues)
