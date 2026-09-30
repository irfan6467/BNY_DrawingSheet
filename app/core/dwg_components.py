"""
Component-level DWG extraction — spatial clustering, labeling, and rendering.

From one DWG/DXF, produce N separate, individually draggable, full-quality
image assets — one per distinct view/detail — each crisp and magnified with
dimension lines/text fully legible.

Pipeline:
  detect_components()  → spatial clustering via grid-accelerated Union-Find
  render_component()   → aspect-ratio-correct, entity-scoped matplotlib render
  compute_component_padding() → per-component DIMENSION/LEADER bbox expansion

DESIGN RULE (§9): No Qt imports.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional

from PIL import Image

LogCallback = Callable[[str, str], None]


def _noop_log(severity: str, message: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Shared render quality settings
# ---------------------------------------------------------------------------
# A CAD view is rendered at a few thousand pixels and then shown a few
# hundred wide on the sheet.  Minifying by that much averages a hairline
# into the white around it, which is why untuned renders look washed out:
# on a real drawing the darkest pixel after shrinking measured 164/255.
# Stroke width is the lever that survives the shrink, so the floor and
# the scaling below are set well above the DXF defaults.  Raise them for
# bolder output.
#
# Colours are the drawing's own.  Overriding them to black reads far
# darker at small sizes, but it is not what the file says, so it stays
# off; set CAD_FORCE_BLACK = True only if you want that trade.

CAD_MIN_LINEWEIGHT_MM = 0.60   # floor applied to every stroke
CAD_LINEWEIGHT_SCALE = 1.8     # multiplier on the drawing's own weights
CAD_FORCE_BLACK = False        # keep every layer's own colour

# The figure size those two numbers were tuned against.
#
# Stroke weights are absolute: a 0.6 mm line is 0.6 mm of a figure however
# many inches across that figure is, so the width *relative to the
# drawing* is set entirely by the figure size.  The weights above were
# tuned while ezdxf was silently resizing every figure to matplotlib's
# default — around 6.4 inches on the long side for a landscape view (and
# 4.8 for a square one, so the weight was not even consistent between
# shapes).  make_backend stops that resizing, which is what recovers the
# lost resolution, but it also means a 12 inch figure would draw those
# same strokes at half their tuned thickness and the drawing would come
# back washed out — measured darkest-pixel 177/255 against 92 before.
#
# So the weights scale with the figure.  Set the figure size for
# resolution and the numbers above for how bold the line work reads; the
# two no longer fight each other.
#
# The value is calibrated, not derived: measured on a view placed about
# 350 pt wide on the sheet, it reproduces the stroke weight a landscape
# view used to have.  That also settles a second problem.  figaspect
# handed out a different figure size for every proportion - 6.4 inches
# for a landscape view, 4.8 for a square one, 4.0 for a tall one - so
# line weight depended on the shape of the view rather than on anything
# anyone chose.  Measured at the same size on the sheet, strokes ranged
# from 2.9 px on a landscape view to 11.4 px on a tall one: four times
# the weight, on the same drawing, on the same sheet.  They now come out
# the same whatever the proportion.
CAD_TUNED_FIGURE_INCHES = 4.2

# Longest side of an extracted component, in inches, before DPI.
#
# This is NOT the resolution knob — DPI is.  Stroke weights are absolute
# (millimetres, see build_render_config), so a line keeps its physical
# width whatever the figure size: enlarging the figure makes every line
# *relatively* thinner and the drawing washes out.  Measured on a hatched
# detail shrunk to 900 px for the sheet, the darkest pixel went from 92
# at 12 inches to 177 at 18 - visibly grey, the very problem the stroke
# settings above were tuned to fix.
#
# Raising DPI instead scales the lines with the image, so the drawing
# gets more pixels at the same weight.  Leave this at 12 and change
# COMPONENT_RENDER_DPI.
COMPONENT_MAX_INCHES = 12.0

# What full-quality extraction renders at.  Together with the 12 inch
# figure and MAX_RENDER_PIXELS this lands a view at roughly 4000-4600 px
# on its long side — enough to print at 300 DPI across most of an A1
# sheet, and the vector PDF alongside carries anything beyond that.
COMPONENT_RENDER_DPI = 400


def make_backend(ax):
    """The ezdxf matplotlib backend, with its figure resizing switched off.

    ``MatplotlibBackend.finalize()`` ends with:

        width, height = plt.figaspect(data_height / data_width)
        self.ax.get_figure().set_size_inches(width, height, forward=True)

    and ``figaspect`` builds its answer from matplotlib's *default* figure
    width of 6.4 inches.  So every render in this app - components, the
    overview, the whole-drawing preview, the vector PDF - had its
    carefully chosen figure size thrown away at the last moment and
    replaced with a 6.4 inch one (4.8 for anything square).

    A component asked for at 12 inches and 300 DPI therefore came out
    1920 px on its long side instead of 3600, and a square detail 1440
    instead of 3600 - 16% of the pixels that were asked for.  That is the
    pixelation: nothing downstream was losing the detail, it was never
    rendered in the first place.

    ``adjust_figure=False`` leaves the figure the size we set it.
    """
    from ezdxf.addons.drawing import matplotlib as draw_mpl

    return draw_mpl.MatplotlibBackend(ax, adjust_figure=False)


def build_render_config(min_lineweight_mm: Optional[float] = None,
                        lineweight_scale: Optional[float] = None,
                        force_black: Optional[bool] = None,
                        white_background: bool = False,
                        figure_inches: Optional[float] = None):
    """Build the ezdxf render Configuration used for every CAD render.

    Kept in one place so the on-screen preview, the component extracts and
    the vector PDF all come out looking the same.

    ``figure_inches`` is the long side of the figure this configuration
    will draw into.  Stroke weights are scaled from
    CAD_TUNED_FIGURE_INCHES so a line keeps the same width relative to
    the drawing whatever size the figure is; see the note there.
    """
    from ezdxf.addons.drawing import config as draw_config

    if min_lineweight_mm is None:
        min_lineweight_mm = CAD_MIN_LINEWEIGHT_MM
    if lineweight_scale is None:
        lineweight_scale = CAD_LINEWEIGHT_SCALE
    if force_black is None:
        force_black = CAD_FORCE_BLACK

    if figure_inches and figure_inches > 0:
        boost = float(figure_inches) / CAD_TUNED_FIGURE_INCHES
        min_lineweight_mm *= boost
        lineweight_scale *= boost

    kwargs = dict(
        lineweight_policy=draw_config.LineweightPolicy.ABSOLUTE,
        min_lineweight=max(0.01, float(min_lineweight_mm)),
        lineweight_scaling=max(0.1, float(lineweight_scale)),
    )
    if force_black:
        kwargs["color_policy"] = draw_config.ColorPolicy.BLACK
    if white_background:
        kwargs["background_policy"] = draw_config.BackgroundPolicy.WHITE
    return draw_config.Configuration(**kwargs)


# ---------------------------------------------------------------------------
# §1 — Data model
# ---------------------------------------------------------------------------

@dataclass
class ComponentRegion:
    """A spatially coherent group of DXF entities representing one
    view / detail / section in the drawing."""

    id: str
    bbox: tuple[float, float, float, float]  # (xmin, ymin, xmax, ymax) model units
    entity_ids: list[str]                    # DXF entity handles
    entity_count: int
    suggested_label: Optional[str] = None


@dataclass
class DetectionResult:
    """Full output of the component detection pass."""

    components: list[ComponentRegion]
    unassigned_entity_ids: list[str]   # entities that didn't make it into any cluster
    unassigned_count: int
    gap_threshold: float               # the threshold that was used
    total_entities: int


# ---------------------------------------------------------------------------
# §1 — Per-entity bounding box computation
# ---------------------------------------------------------------------------

# Entity types that need slow (accurate) bbox calculation
_SLOW_BBOX_TYPES = frozenset({
    "TEXT", "MTEXT", "DIMENSION", "LEADER", "MLEADER",
})

# Entity types that should be EXCLUDED from component detection entirely
# (they're annotations, not geometry)
ANNOTATION_TYPES = frozenset({
    "TEXT", "MTEXT", "DIMENSION", "LEADER", "MLEADER",
    "ATTRIB", "ATTDEF", "TABLE",
})

# Entity types that represent actual geometry (should be clustered)
GEOMETRY_TYPES = frozenset({
    "LINE", "ARC", "CIRCLE", "ELLIPSE", "POLYLINE", "LWPOLYLINE",
    "SPLINE", "BEZIER", "CURVE", "SURFACE", "SOLID", "3DFACE",
    "POINT", "INSERT", "MINSERT",
})


def _compute_entity_bboxes(
    msp,
    cache,
    log: LogCallback = _noop_log,
) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Compute per-entity bounding boxes for all entities in a layout.

    Returns a list of (entity_handle, (xmin, ymin, xmax, ymax)) tuples.
    Entities with empty/invalid bboxes are skipped.

    Uses fast=False for TEXT/MTEXT/DIMENSION/LEADER/MLEADER to avoid
    the inaccurate text-size estimate that fast=True uses.

    For INSERT (block reference) entities, falls back to
    virtual_entities() + bbox union if the direct extents call
    returns a degenerate box.
    """
    from ezdxf import bbox as ezdxf_bbox

    results = []
    skipped = 0

    for entity in msp:
        handle = entity.dxf.handle
        dt = entity.dxftype()
        fast = dt not in _SLOW_BBOX_TYPES

        try:
            box = ezdxf_bbox.extents([entity], fast=fast, cache=cache)
        except Exception:
            skipped += 1
            continue

        # Validate the box
        if not box.has_data:
            # Special handling for INSERT: try virtual_entities fallback
            if dt == "INSERT":
                box = _insert_bbox_fallback(entity, cache)
                if box is None or not box.has_data:
                    skipped += 1
                    continue
            else:
                skipped += 1
                continue

        xmin, ymin = box.extmin.x, box.extmin.y
        xmax, ymax = box.extmax.x, box.extmax.y

        # Check for degenerate box
        if xmax - xmin < 1e-9 and ymax - ymin < 1e-9:
            if dt == "INSERT":
                box = _insert_bbox_fallback(entity, cache)
                if box is None or not box.has_data:
                    skipped += 1
                    continue
                xmin, ymin = box.extmin.x, box.extmin.y
                xmax, ymax = box.extmax.x, box.extmax.y
                if xmax - xmin < 1e-9 and ymax - ymin < 1e-9:
                    skipped += 1
                    continue
            else:
                skipped += 1
                continue

        results.append((handle, (xmin, ymin, xmax, ymax)))

    if skipped > 0:
        log("info", f"Skipped {skipped} entities with empty/invalid bounding boxes")

    return results


def _insert_bbox_fallback(entity, cache):
    """Fallback bbox computation for INSERT entities using virtual_entities().

    When bbox.extents on an INSERT returns a degenerate box (common with
    door/window/furniture/fixture blocks), explode into virtual entities
    and union their bboxes.
    """
    from ezdxf import bbox as ezdxf_bbox
    from ezdxf.math import BoundingBox

    try:
        virtual_ents = list(entity.virtual_entities())
        if not virtual_ents:
            return None
        return ezdxf_bbox.extents(virtual_ents, fast=True, cache=cache)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# §1 — Union-Find data structure
# ---------------------------------------------------------------------------

class _UnionFind:
    """Weighted quick-union with path compression."""

    def __init__(self, n: int):
        self._parent = list(range(n))
        self._rank = [0] * n

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]  # path compression
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1

    def groups(self, n: int) -> dict[int, list[int]]:
        """Return {root: [member indices]}."""
        groups: dict[int, list[int]] = defaultdict(list)
        for i in range(n):
            groups[self.find(i)].append(i)
        return groups


# ---------------------------------------------------------------------------
# §1 — Spatial grid bucketing
# ---------------------------------------------------------------------------

def _grid_key(x: float, y: float, cell_size: float) -> tuple[int, int]:
    """Map a point to its grid cell."""
    return (int(math.floor(x / cell_size)), int(math.floor(y / cell_size)))


def _entity_grid_cells(
    bbox: tuple[float, float, float, float],
    cell_size: float,
) -> set[tuple[int, int]]:
    """Return all grid cells that an expanded bbox covers."""
    xmin, ymin, xmax, ymax = bbox
    cx_min = int(math.floor(xmin / cell_size))
    cy_min = int(math.floor(ymin / cell_size))
    cx_max = int(math.floor(xmax / cell_size))
    cy_max = int(math.floor(ymax / cell_size))

    cells = set()
    for cx in range(cx_min, cx_max + 1):
        for cy in range(cy_min, cy_max + 1):
            cells.add((cx, cy))
    return cells


# ---------------------------------------------------------------------------
# §1 — Component detection via spatial clustering
# ---------------------------------------------------------------------------

def _cluster_at_gap(
    entries: list[tuple[str, tuple[float, float, float, float]]],
    gap: float,
) -> dict[int, list[int]]:
    """Single-linkage cluster of bboxes that come within ``gap`` of each other.

    Entities are bucketed into a grid and, within each cell, compared in
    x order with an early break.  The previous version compared every
    pair in a cell, which on a busy cell is quadratic for no benefit.
    """
    half = gap / 2.0
    expanded = [
        (xmin - half, ymin - half, xmax + half, ymax + half)
        for _, (xmin, ymin, xmax, ymax) in entries
    ]

    cell_size = max(gap, 1e-6)
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for idx, exp in enumerate(expanded):
        for cell in _entity_grid_cells(exp, cell_size):
            grid[cell].append(idx)

    uf = _UnionFind(len(expanded))
    for indices in grid.values():
        if len(indices) < 2:
            continue
        indices.sort(key=lambda i: expanded[i][0])
        for a in range(len(indices)):
            ia = indices[a]
            a_xmax = expanded[ia][2]
            for b in range(a + 1, len(indices)):
                ib = indices[b]
                if expanded[ib][0] > a_xmax:
                    break  # sorted by xmin - nothing further can overlap
                if uf.find(ia) == uf.find(ib):
                    continue
                if _bboxes_overlap(expanded[ia], expanded[ib]):
                    uf.union(ia, ib)
    return uf.groups(len(expanded))


def _significant_cluster_count(
    groups: dict[int, list[int]],
    entries: list[tuple[str, tuple[float, float, float, float]]],
) -> int:
    """How many clusters are big enough to be worth calling a view."""
    areas = []
    for members in groups.values():
        xmin = min(entries[m][1][0] for m in members)
        ymin = min(entries[m][1][1] for m in members)
        xmax = max(entries[m][1][2] for m in members)
        ymax = max(entries[m][1][3] for m in members)
        areas.append((xmax - xmin) * (ymax - ymin))
    if not areas:
        return 0
    biggest = max(areas) or 1.0
    return sum(1 for a in areas if a / biggest >= 0.01)


def _choose_gap_threshold(
    entries: list[tuple[str, tuple[float, float, float, float]]],
    diagonal: float,
    log: LogCallback,
) -> float:
    """Pick the gap that splits the drawing most stably.

    A fixed percentage of the diagonal cannot know how far apart this
    particular drawing spaces its views: too small and one elevation
    shatters into its own panels, too large and neighbouring views fuse.
    Sweeping the gap and keeping the value that holds the same answer
    over the widest run finds the drawing's own natural spacing.
    """
    candidates = [diagonal * f for f in
                  (0.004, 0.006, 0.009, 0.013, 0.02, 0.03, 0.045, 0.065)]
    counts = []
    for gap in candidates:
        groups = _cluster_at_gap(entries, gap)
        counts.append(_significant_cluster_count(groups, entries))

    # Longest run of candidates agreeing on the same cluster count,
    # ignoring the degenerate "everything is one blob" answer.
    best_run = (0, 0, 0)  # (length, start, count)
    run_start = 0
    for i in range(1, len(counts) + 1):
        if i == len(counts) or counts[i] != counts[run_start]:
            length = i - run_start
            if counts[run_start] > 1 and length > best_run[0]:
                best_run = (length, run_start, counts[run_start])
            run_start = i

    if best_run[0] == 0:
        chosen = diagonal * 0.02
        log("info", f"Gap threshold {chosen:.1f} (no stable split found; "
                    f"counts across sweep: {counts})")
        return chosen

    length, start, count = best_run
    chosen = candidates[start + length // 2]
    log("info", f"Gap threshold {chosen:.1f} - {count} views stable across "
                f"{length} of {len(candidates)} probes (counts: {counts})")
    return chosen


def _absorb_fragments(
    components: list[ComponentRegion],
    log: LogCallback,
    containment: float = 0.7,
) -> list[ComponentRegion]:
    """Fold stray fragments into the view they sit inside.

    A door swing arc or a stray panel line often ends up as its own
    two-entity 'component'.  If most of its box lies inside a bigger
    one, it belongs to that view.
    """
    if len(components) < 2:
        return components

    def area(b):
        return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])

    ordered = sorted(components, key=lambda c: area(c.bbox), reverse=True)
    kept: list[ComponentRegion] = []
    absorbed = 0

    for comp in ordered:
        own = area(comp.bbox)
        host = None
        for candidate in kept:
            ox = min(comp.bbox[2], candidate.bbox[2]) - max(comp.bbox[0], candidate.bbox[0])
            oy = min(comp.bbox[3], candidate.bbox[3]) - max(comp.bbox[1], candidate.bbox[1])
            if ox <= 0 or oy <= 0:
                continue
            if own <= 1e-9 or (ox * oy) / own >= containment:
                host = candidate
                break
        if host is None:
            kept.append(comp)
            continue
        host.entity_ids.extend(comp.entity_ids)
        host.entity_count = len(host.entity_ids)
        host.bbox = (
            min(host.bbox[0], comp.bbox[0]), min(host.bbox[1], comp.bbox[1]),
            max(host.bbox[2], comp.bbox[2]), max(host.bbox[3], comp.bbox[3]),
        )
        absorbed += 1

    if absorbed:
        log("info", f"Absorbed {absorbed} fragment(s) into the view containing them")
    return kept


# How far beyond a component's own extents its dimensions may sit, as a
# fraction of that component's diagonal.  Measurements are drawn just
# outside what they measure, so this is deliberately short: a generous
# reach pulls in a neighbour's dimensions and drags the box over it.
ANNOTATION_REACH_FRACTION = 0.12

# Never let a box come closer than this fraction of the gap to the
# neighbouring geometry it is growing towards.
NEIGHBOUR_CLEARANCE = 0.15


def _clamp_box_off_neighbours(box, neighbours):
    """Pull a grown box back so it does not cover a neighbour's geometry.

    Only the sides that actually run into a neighbour move, and they stop
    just short of it, so the component keeps as much of its own lettering
    as it can without claiming someone else's.
    """
    x0, y0, x1, y1 = box
    for nx0, ny0, nx1, ny1 in neighbours:
        if x1 <= nx0 or x0 >= nx1 or y1 <= ny0 or y0 >= ny1:
            continue  # no overlap with this neighbour

        # How far the box would have to give on each side to clear it.
        give_left = x1 - nx0      # pull our right edge back to nx0
        give_right = nx1 - x0     # push our left edge out to nx1
        give_up = y1 - ny0
        give_down = ny1 - y0
        smallest = min(give_left, give_right, give_up, give_down)

        margin = smallest * NEIGHBOUR_CLEARANCE
        if smallest == give_left:
            x1 = min(x1, nx0 - margin)
        elif smallest == give_right:
            x0 = max(x0, nx1 + margin)
        elif smallest == give_up:
            y1 = min(y1, ny0 - margin)
        else:
            y0 = max(y0, ny1 + margin)

    return (x0, y0, x1, y1)


def include_related_annotations(component, msp, cache, reach=None,
                                by_handle=None, neighbours=None) -> int:
    """Add the dimensions and labels belonging to this component.

    Measured against the component's current extents, then the extents are
    widened to cover whatever was taken in - a dimension that sits outside
    the box would otherwise be drawn off the edge of the render.

    Returns how many entities were added.
    """
    from ezdxf import bbox as ezdxf_bbox

    x0, y0, x1, y1 = component.bbox
    if reach is None:
        reach = max(math.hypot(x1 - x0, y1 - y0) * ANNOTATION_REACH_FRACTION,
                    1.0)

    existing = set(component.entity_ids)
    entities = by_handle.values() if by_handle is not None else msp
    added = 0
    nx0, ny0, nx1, ny1 = x0, y0, x1, y1

    for entity in entities:
        handle = entity.dxf.handle
        if handle in existing or entity.dxftype() not in ANNOTATION_TYPES:
            continue
        try:
            box = ezdxf_bbox.extents([entity], fast=False, cache=cache)
        except Exception:
            continue
        if not box.has_data:
            continue

        cx = (box.extmin.x + box.extmax.x) / 2.0
        cy = (box.extmin.y + box.extmax.y) / 2.0
        # Distance is measured to the ORIGINAL box, so taking one dimension
        # in cannot drag the reach out to the next view along.
        dx = max(x0 - cx, 0.0, cx - x1)
        dy = max(y0 - cy, 0.0, cy - y1)
        if math.hypot(dx, dy) > reach:
            continue

        # A dimension sitting on another view is that view's, not ours.
        if neighbours and any(
            box.extmin.x < nx1 and box.extmax.x > nx0
            and box.extmin.y < ny1 and box.extmax.y > ny0
            for nx0, ny0, nx1, ny1 in neighbours
        ):
            continue

        component.entity_ids.append(handle)
        existing.add(handle)
        nx0 = min(nx0, box.extmin.x)
        ny0 = min(ny0, box.extmin.y)
        nx1 = max(nx1, box.extmax.x)
        ny1 = max(ny1, box.extmax.y)
        added += 1

    grown = (nx0, ny0, nx1, ny1)
    if neighbours:
        grown = _clamp_box_off_neighbours(grown, neighbours)
    component.bbox = grown
    component.entity_count = len(component.entity_ids)
    return added


# Words that mark a piece of lettering as a view title rather than a
# dimension, a note or a material call-out.  Taken from the titles the
# firm's own sheets use.
_VIEW_TITLE_WORDS = (
    "PLAN", "ELEVATION", "SECTION", "DETAIL", "VIEW", "ISOMETRIC",
    "PERSPECTIVE", "AXONOMETRIC", "SCHEDULE", "LAYOUT",
)

# Longer than this and it is a note, not a title.
_MAX_LABEL_CHARS = 40


def _clean_label(raw: str) -> str:
    """Strip the formatting codes MTEXT carries and tidy the wording."""
    text = str(raw or "")
    # MTEXT inline codes: \pxqc, \A1;, {\fArial|b0;...} and the like.
    text = re.sub(r"\\[A-Za-z][^;\\]*;", " ", text)
    text = re.sub(r"[{}]", " ", text)
    text = text.replace("\\P", " ").replace("\\~", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -_:.")


def _label_score(text: str) -> float:
    """How much a piece of lettering looks like a view title."""
    upper = text.upper()
    if not upper or len(upper) > _MAX_LABEL_CHARS:
        return 0.0
    # A pure measurement or a scale is never a title.  The text has
    # already been upper-cased, so the "x" in "450 x 300" arrives as "X".
    if re.fullmatch(r"[\d\s.,:/xX×'\"-]+", upper):
        return 0.0

    score = 0.0
    if any(word in upper for word in _VIEW_TITLE_WORDS):
        score += 10.0
    if upper == text:           # already in capitals, as titles are
        score += 2.0
    if len(upper.split()) <= 4:
        score += 1.0
    return score


def _suggest_labels(
    components: list[ComponentRegion],
    by_handle: dict,
    log: LogCallback,
) -> None:
    """Name each view after the lettering that sits with it.

    ``ComponentRegion.suggested_label`` was declared and read but nothing
    ever wrote it, so every extracted view arrived in the review dialog
    called comp_4f2a1b - and the architect had to work out which was the
    plan and which the section from the thumbnails alone.

    The candidates are the TEXT and MTEXT entities the component already
    owns, since _attach_annotations has just given each view the lettering
    that belongs to it.  The lowest one wins a tie, because a view title
    is written underneath the view on these drawings.
    """
    for comp in components:
        best_text = None
        best_key = None

        for handle in comp.entity_ids:
            entity = by_handle.get(handle)
            if entity is None or entity.dxftype() not in ("TEXT", "MTEXT"):
                continue

            raw = getattr(entity.dxf, "text", None)
            if raw is None:
                # MTEXT keeps its content on the entity, not on .dxf.
                raw = getattr(entity, "text", None)
            text = _clean_label(raw)
            if not text:
                continue

            score = _label_score(text)
            if score <= 0.0:
                continue

            try:
                y = float(entity.dxf.insert[1])
            except Exception:  # noqa: BLE001 - not every text has an insert
                y = 0.0

            # Higher score first, then whichever sits lowest on the sheet.
            key = (score, -y)
            if best_key is None or key > best_key:
                best_key, best_text = key, text

        if best_text:
            comp.suggested_label = best_text

    named = sum(1 for c in components if c.suggested_label)
    if named:
        log("info", f"Named {named} of {len(components)} views from their titles")


def _attach_annotations(
    components: list[ComponentRegion],
    annotations: list[tuple[str, tuple[float, float, float, float]]],
    gap_threshold: float,
    log: LogCallback,
) -> None:
    """Give each dimension and label to the view it annotates.

    Clustering deliberately ignores annotation entities so long dimension
    runs cannot bridge two views - but they were then left out of the
    components entirely, so extracts came through with no dimensions.

    A dimension belongs to whichever view it sits nearest, and only if it
    is close: measurements are drawn just outside what they measure.  The
    view then grows to cover what it took in, stopping short of any other
    view's geometry so one box never claims another's space.
    """
    if not components or not annotations:
        return

    # The geometry each view occupies, before anything grows.
    geometry = {comp.id: tuple(comp.bbox) for comp in components}
    grown = {comp.id: list(comp.bbox) for comp in components}

    attached = 0
    for handle, (xmin, ymin, xmax, ymax) in annotations:
        cx = (xmin + xmax) / 2.0
        cy = (ymin + ymax) / 2.0

        # Skip anything lying on a view's geometry that is not the nearest
        # one - that lettering belongs to the view it sits on.
        best = None
        best_distance = None
        for comp in components:
            bx0, by0, bx1, by1 = geometry[comp.id]
            dx = max(bx0 - cx, 0.0, cx - bx1)
            dy = max(by0 - cy, 0.0, cy - by1)
            distance = math.hypot(dx, dy)
            if best_distance is None or distance < best_distance:
                best, best_distance = comp, distance
        if best is None or best_distance is None:
            continue

        bx0, by0, bx1, by1 = geometry[best.id]
        reach = max(
            math.hypot(bx1 - bx0, by1 - by0) * ANNOTATION_REACH_FRACTION,
            gap_threshold * 0.5,
        )
        if best_distance > reach:
            continue

        best.entity_ids.append(handle)
        best.entity_count = len(best.entity_ids)
        box = grown[best.id]
        box[0] = min(box[0], xmin)
        box[1] = min(box[1], ymin)
        box[2] = max(box[2], xmax)
        box[3] = max(box[3], ymax)
        attached += 1

    # Now settle the boxes, each one keeping clear of the others' geometry.
    for comp in components:
        others = [geometry[c.id] for c in components if c.id != comp.id]
        comp.bbox = _clamp_box_off_neighbours(tuple(grown[comp.id]), others)

    if attached:
        log("info", f"Attached {attached} dimension/label entities to their views")


def detect_components(
    msp,
    cache,
    gap_threshold: Optional[float] = None,
    log: LogCallback = _noop_log,
) -> DetectionResult:
    """Auto-detect components via spatial clustering.

    Uses grid-accelerated Union-Find to merge entities whose expanded
    bounding boxes overlap.

    Parameters
    ----------
    msp
        ezdxf Modelspace or Layout.
    cache
        Shared ezdxf.bbox.Cache for reuse across detection, labeling,
        and padding.
    gap_threshold : float or None
        Maximum gap (in model units) between entities to merge them
        into the same cluster.  If None, auto-calculated as 4% of
        the modelspace extents diagonal.
    log : LogCallback

    Returns
    -------
    DetectionResult
        Detected components, unassigned entities, and metadata.
    """
    # Step 1: compute per-entity bboxes
    entity_bboxes = _compute_entity_bboxes(msp, cache, log)

    # One pass over the layout, held by handle.  This used to be a linear
    # scan of every entity inside a loop over every entity - on a 3.5k
    # entity drawing that is twelve million comparisons before any
    # clustering starts.
    by_handle = {e.dxf.handle: e for e in msp}
    total_entities = len(by_handle)

    # Cluster on geometry only: a dimension line running between two
    # views would otherwise stitch them into one component.  The
    # annotations are handed back to their view further down.
    geometry_bboxes = []
    annotation_bboxes = []
    for handle, bbox in entity_bboxes:
        entity = by_handle.get(handle)
        if entity is None:
            continue
        dt = entity.dxftype()
        if dt in GEOMETRY_TYPES:
            geometry_bboxes.append((handle, bbox))
        elif dt in ANNOTATION_TYPES:
            annotation_bboxes.append((handle, bbox))

    entity_bboxes = geometry_bboxes

    if not entity_bboxes:
        log("warning", "No valid entity bounding boxes — nothing to cluster")
        all_handles = list(by_handle)
        return DetectionResult(
            components=[],
            unassigned_entity_ids=all_handles,
            unassigned_count=len(all_handles),
            gap_threshold=0.0,
            total_entities=total_entities,
        )

    # Step 2: compute overall extents for auto gap_threshold
    all_xmin = min(b[0] for _, b in entity_bboxes)
    all_ymin = min(b[1] for _, b in entity_bboxes)
    all_xmax = max(b[2] for _, b in entity_bboxes)
    all_ymax = max(b[3] for _, b in entity_bboxes)
    diagonal = math.hypot(all_xmax - all_xmin, all_ymax - all_ymin)

    if gap_threshold is None:
        gap_threshold = _choose_gap_threshold(entity_bboxes, diagonal, log)

    # Steps 3-6: expand, bucket and union in one shared routine.
    half_gap = gap_threshold / 2.0
    expanded = [
        (handle,
         (xmin - half_gap, ymin - half_gap, xmax + half_gap, ymax + half_gap),
         (xmin, ymin, xmax, ymax))
        for handle, (xmin, ymin, xmax, ymax) in entity_bboxes
    ]
    groups = _cluster_at_gap(entity_bboxes, gap_threshold)

    # Step 7: build ComponentRegions
    components: list[ComponentRegion] = []
    assigned_handles: set[str] = set()

    # Find the largest cluster area for relative filtering
    cluster_areas = {}
    for root, members in groups.items():
        xmin = min(expanded[m][2][0] for m in members)
        ymin = min(expanded[m][2][1] for m in members)
        xmax = max(expanded[m][2][2] for m in members)
        ymax = max(expanded[m][2][3] for m in members)
        area = (xmax - xmin) * (ymax - ymin)
        cluster_areas[root] = (area, xmin, ymin, xmax, ymax, members)

    max_area = max((a for a, *_ in cluster_areas.values()), default=1.0)
    if max_area < 1e-9:
        max_area = 1.0

    for root, (area, xmin, ymin, xmax, ymax, members) in cluster_areas.items():
        entity_ids = [expanded[m][0] for m in members]
        entity_count = len(entity_ids)
        relative_area = area / max_area if max_area > 0 else 0

        # Stricter filtering: drop tiny clusters with very few entities
        # BUT preserve clusters that have meaningful geometry (lines, arcs, etc.)
        min_entity_threshold = 2  # Need at least 2 entities to be a real component
        min_area_threshold = 0.005  # At least 0.5% of max area
        
        # Count actual geometry entities ( LINES, ARCS, CIRCLES, POLYLINES, etc. )
        # Skip pure annotation entities (TEXT, MTEXT, DIMENSION) for counting
        geometry_count = 0
        for eid in entity_ids:
            entity = by_handle.get(eid)
            if entity is not None and entity.dxftype() in (
                "LINE", "ARC", "CIRCLE", "ELLIPSE", "POLYLINE",
                "LWPOLYLINE", "SPLINE", "BEZIER", "SURFACE", "SOLID",
            ):
                geometry_count += 1
        
        # Filter: require either enough entities OR enough area OR enough geometry
        if entity_count < min_entity_threshold and relative_area < min_area_threshold and geometry_count < 3:
            continue

        comp = ComponentRegion(
            id=f"comp_{uuid.uuid4().hex[:6]}",
            bbox=(xmin, ymin, xmax, ymax),
            entity_ids=entity_ids,
            entity_count=entity_count,
        )
        components.append(comp)
        assigned_handles.update(entity_ids)

    # Step 8: track unassigned entities
    all_handles_set = {h for h, _ in entity_bboxes}
    unassigned = list(all_handles_set - assigned_handles)

    # Step 9: Merge overlapping components (post-processing)
    # Sometimes distinct components get merged incorrectly, or adjacent components
    # should remain separate. We'll split components that have suspicious gaps.
    components = _post_process_components(components, msp, gap_threshold, log)
    components = _absorb_fragments(components, log)
    _attach_annotations(components, annotation_bboxes, gap_threshold, log)
    _suggest_labels(components, by_handle, log)

    log("info", f"Detected {len(components)} components from {len(entity_bboxes)} entities "
        f"({len(unassigned)} unassigned)")

    return DetectionResult(
        components=components,
        unassigned_entity_ids=unassigned,
        unassigned_count=len(unassigned),
        gap_threshold=gap_threshold,
        total_entities=total_entities,
    )


def _post_process_components(
    components: list[ComponentRegion],
    msp,
    gap_threshold: float,
    log: LogCallback,
) -> list[ComponentRegion]:
    """Post-process detected components to improve quality.

    1. Split components that span too large an area (likely merged distinct views)
    2. Remove components that are too small to be meaningful
    3. Remove duplicate/near-duplicate components

    Returns a refined list of components.
    """
    if not components:
        return components

    refined = []
    
    # Compute stats for adaptive filtering
    areas = [(c.bbox[2] - c.bbox[0]) * (c.bbox[3] - c.bbox[1]) for c in components]
    max_area = max(areas) if areas else 1.0
    min_area = min(areas) if areas else 0.0
    
    for comp in components:
        xmin, ymin, xmax, ymax = comp.bbox
        width = xmax - xmin
        height = ymax - ymin
        area = width * height
        
        # Skip degenerate components
        if width < 1e-6 or height < 1e-6:
            log("info", f"Skipping degenerate component {comp.id}: {width:.4f}×{height:.4f}")
            continue
        
        relative_area = area / max_area if max_area > 0 else 0
        
        # If component is suspiciously large (>80% of max area), try to split it
        # This happens when distinct views are incorrectly merged
        if relative_area > 0.8 and comp.entity_count > 10:
            # Try to find natural split points by looking at entity density
            split_result = _try_split_large_component(comp, msp, gap_threshold)
            if split_result:
                refined.extend(split_result)
                log("info", f"Split large component {comp.id} into {len(split_result)} parts")
                continue
        
        # Filter out very small components (noise)
        # but keep them if they have meaningful geometry
        if relative_area < 0.002 and comp.entity_count < 3:
            # Check if it has any lines/arcs (real geometry)
            has_geometry = False
            for entity in msp:
                if entity.dxf.handle in comp.entity_ids:
                    if entity.dxftype() in ("LINE", "ARC", "CIRCLE", "POLYLINE", "LWPOLYLINE"):
                        has_geometry = True
                    break
            if not has_geometry:
                log("info", f"Filtering tiny component {comp.id}: area={relative_area:.4f}, entities={comp.entity_count}")
                continue
        
        refined.append(comp)
    
    
    # Remove near-duplicate components (those with very similar bboxes)
    refined = _remove_duplicate_components(refined, log)
    
    return refined


def _try_split_large_component(
    component: ComponentRegion,
    msp,
    gap_threshold: float,
) -> Optional[list[ComponentRegion]]:
    """Try to split a large component into smaller natural parts.

    Looks for gaps in entity distribution along X and Y axes.
    Returns split components if successful, None if not worth splitting.
    """
    xmin, ymin, xmax, ymax = component.bbox
    width = xmax - xmin
    height = ymax - ymin
    
    if width < 1e-6 or height < 1e-6:
        return None
    
    # Collect entity centers
    centers_x = []
    centers_y = []
    entity_bboxes = {}
    
    for entity in msp:
        if entity.dxf.handle in component.entity_ids:
            dt = entity.dxftype()
            if dt in ("LINE", "ARC", "CIRCLE", "ELLIPSE", "POLYLINE", "LWPOLYLINE", "SPLINE"):
                try:
                    from ezdxf import bbox as ezdxf_bbox
                    box = ezdxf_bbox.extents([entity], fast=True)
                    if box.has_data:
                        ecx = (box.extmin.x + box.extmax.x) / 2.0
                        ecy = (box.extmin.y + box.extmax.y) / 2.0
                        centers_x.append(ecx)
                        centers_y.append(ecy)
                        entity_bboxes[entity.dxf.handle] = (box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y)
                except Exception:
                    pass
    
    if len(centers_x) < 5:
        return None  # Not enough entities to meaningfully split
    
    # Sort centers and look for gaps
    centers_x.sort()
    centers_y.sort()
    
    # Find the largest gap in X
    max_gap_x = 0
    max_gap_x_pos = None
    for i in range(1, len(centers_x)):
        gap = centers_x[i] - centers_x[i-1]
        if gap > max_gap_x:
            max_gap_x = gap
            max_gap_x_pos = (centers_x[i] + centers_x[i-1]) / 2.0
    
    # Find the largest gap in Y
    max_gap_y = 0
    max_gap_y_pos = None
    for i in range(1, len(centers_y)):
        gap = centers_y[i] - centers_y[i-1]
        if gap > max_gap_y:
            max_gap_y = gap
            max_gap_y_pos = (centers_y[i] + centers_y[i-1]) / 2.0
    
    
    # Only split if we found a significant gap (>10% of the dimension)
    significant_gap_x = max_gap_x > width * 0.15
    significant_gap_y = max_gap_y > height * 0.15
    
    if not significant_gap_x and not significant_gap_y:
        return None  # No significant gap found
    
    # Choose the axis with the larger relative gap
    if significant_gap_x and (not significant_gap_y or max_gap_x / width > max_gap_y / height):
        # Split along X axis
        divider = max_gap_x_pos if max_gap_x_pos else (xmin + xmax) / 2.0
        comp_a, comp_b = split_component(component, divider, "x", entity_bboxes)
        
        # Only return splits if both parts have entities
        if comp_a.entity_count > 0 and comp_b.entity_count > 0:
            return [comp_a, comp_b]
    elif significant_gap_y:
        # Split along Y axis
        divider = max_gap_y_pos if max_gap_y_pos else (ymin + ymax) / 2.0
        comp_a, comp_b = split_component(component, divider, "y", entity_bboxes)
        
        if comp_a.entity_count > 0 and comp_b.entity_count > 0:
            return [comp_a, comp_b]
    
    return None


def _remove_duplicate_components(
    components: list[ComponentRegion],
    log: LogCallback,
) -> list[ComponentRegion]:
    """Remove near-duplicate components (those with very similar bboxes)."""
    if len(components) < 2:
        return components
    
    kept = []
    used = set()
    
    for i, comp_a in enumerate(components):
        if i in used:
            continue
        
        keep = True
        for j, comp_b in enumerate(components):
            if j <= i or j in used:
                continue
            
            # Check if bboxes are nearly identical
            axmin, aymin, axmax, aymax = comp_a.bbox
            bxmin, bymin, bxmax, bymax = comp_b.bbox
            
            # Calculate overlap percentage
            overlap_xmin = max(axmin, bxmin)
            overlap_ymin = max(aymin, bymin)
            overlap_xmax = min(axmax, bxmax)
            overlap_ymax = min(aymax, bymax)
            
            if overlap_xmax > overlap_xmin and overlap_ymax > overlap_ymin:
                overlap_area = (overlap_xmax - overlap_xmin) * (overlap_ymax - overlap_ymin)
                a_area = (axmax - axmin) * (aymax - aymin)
                b_area = (bxmax - bxmin) * (bymax - bymin)
                
                # If overlap is >90% of the smaller component's area, they're duplicates
                min_area = min(a_area, b_area)
                if min_area > 0 and overlap_area / min_area > 0.9:
                    # Keep the one with more entities or larger area
                    if comp_b.entity_count > comp_a.entity_count or b_area > a_area:
                        if i not in used:
                            used.add(i)
                        keep = False
                        break
        
        if keep:
            kept.append(comp_a)
        else:
            used.add(i)
    
    
    if len(kept) < len(components):
        log("info", f"Removed {len(components) - len(kept)} duplicate components")
    
    return kept


def _bboxes_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    """Check if two (xmin, ymin, xmax, ymax) bboxes overlap."""
    return not (
        a[2] < b[0] or  # a.xmax < b.xmin
        b[2] < a[0] or  # b.xmax < a.xmin
        a[3] < b[1] or  # a.ymax < b.ymin
        b[3] < a[1]     # b.ymax < a.ymin
    )


# ---------------------------------------------------------------------------
# §2 — Auto-label components using nearby text
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# §5 — Per-component padding for dimension legibility
# ---------------------------------------------------------------------------

def compute_component_padding(
    component: ComponentRegion,
    msp,
    cache,
    base_padding_pct: float = 0.05,
) -> float:
    """Compute padding per-component, expanding beyond the base if
    DIMENSION, LEADER, or MLEADER entities' bboxes extend beyond the
    core geometry bbox.

    Returns a padding_pct >= base_padding_pct.
    """
    from ezdxf import bbox as ezdxf_bbox

    cxmin, cymin, cxmax, cymax = component.bbox
    cw = cxmax - cxmin
    ch = cymax - cymin
    if cw < 1e-9 or ch < 1e-9:
        return base_padding_pct

    member_ids = set(component.entity_ids)
    max_overshoot = 0.0

    for entity in msp:
        if entity.dxf.handle not in member_ids:
            continue
        dt = entity.dxftype()
        if dt not in ("DIMENSION", "LEADER", "MLEADER"):
            continue

        try:
            box = ezdxf_bbox.extents([entity], fast=False, cache=cache)
        except Exception:
            continue
        if not box.has_data:
            continue

        # Check how far this entity extends beyond the core bbox
        overshoot_left = max(0, cxmin - box.extmin.x)
        overshoot_right = max(0, box.extmax.x - cxmax)
        overshoot_bottom = max(0, cymin - box.extmin.y)
        overshoot_top = max(0, box.extmax.y - cymax)

        max_overshoot = max(
            max_overshoot,
            overshoot_left / cw,
            overshoot_right / cw,
            overshoot_bottom / ch,
            overshoot_top / ch,
        )

    # Ensure padding covers the full overshoot plus a small margin
    needed_pct = max_overshoot + 0.02  # 2% margin beyond the overshoot
    return max(base_padding_pct, needed_pct)


# ---------------------------------------------------------------------------
# §3 — Render a single component at target DPI
# ---------------------------------------------------------------------------

def render_component(
    doc,
    msp,
    component: ComponentRegion,
    target_dpi: int = 300,
    padding_pct: Optional[float] = None,
    cache=None,
    log: LogCallback = _noop_log,
) -> Optional[Image.Image]:
    """Render a single component to a PIL Image.

    Two critical corrections vs. naive rendering:

    (a) Aspect-ratio fix: fig.set_size_inches(width, height) matching the
        figure to the padded bbox aspect ratio. Setting ax.set_xlim/ylim
        alone reproduces the stretch/blur bug.

    (b) Entity scoping via filter_func: only draw entities belonging to
        this component, preventing visual bleed from neighboring views.

    Parameters
    ----------
    doc : ezdxf document
    msp : ezdxf layout (modelspace)
    component : ComponentRegion
    target_dpi : int
        Output DPI. Use 300 for full quality, ~72 for preview thumbnails.
    padding_pct : float or None
        Override padding. If None, computed per-component via
        compute_component_padding().
    cache : ezdxf.bbox.Cache or None
    log : LogCallback

    Returns
    -------
    PIL.Image.Image or None
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        log("error", "matplotlib is not installed — cannot render components.")
        return None

    try:
        from ezdxf.addons.drawing import matplotlib as draw_mpl
        from ezdxf.addons.drawing import Frontend, RenderContext
        from ezdxf.addons.drawing import config as draw_config
    except ImportError:
        log("error", "ezdxf drawing addon not available.")
        return None

    # Padding, padded bbox and figure size — shared with the vector
    # render so the two cannot drift apart.
    frame = _component_frame(component, msp, padding_pct, cache)
    if frame is None:
        log("warning", f"Component {component.id} has zero extent — skipping render")
        return None
    bounds, fig_width, fig_height = frame

    # 18 inches at 300 DPI is 5400 px on the long side, and a square
    # component is then 29 megapixels — 87 MB as RGB before Qt has a copy
    # of its own.  Extract a dozen of those and the process runs out of
    # memory partway through, which is one of the ways this app was
    # closing itself.  The budget below trades resolution for staying
    # alive; the vector PDF alongside is what carries the detail into
    # print, so nothing is lost by bounding the bitmap.
    target_dpi = _fit_dpi_to_budget(fig_width, fig_height, target_dpi,
                                    component.id, log)

    # (a) CRITICAL: match figure size to bbox aspect ratio
    # This prevents the stretch/blur bug.
    fig = _draw_component_figure(
        doc, msp, component, bounds, fig_width, fig_height
    )
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name

        fig.savefig(tmp_path, dpi=target_dpi, facecolor="white")

        img = Image.open(tmp_path)
        img.load()

        log("info", "Rendered component %s: %d x %d at %d DPI"
            % (component.id, img.width, img.height, target_dpi))
        return img

    except Exception as e:
        log("error", f"Failed to render component {component.id}: {e}")
        return None

    finally:
        # pyplot keeps a global reference to every figure it makes, so one
        # that is not closed is never collected.  Drawing used to happen
        # outside the try, which meant any failure there leaked the figure
        # and its canvas for the rest of the session.
        import matplotlib.pyplot as _plt

        try:
            _plt.close(fig)
        except Exception:  # noqa: BLE001
            pass
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _component_frame(component, msp, padding_pct, cache):
    """The padded model rectangle and figure size for one component.

    Shared by the raster and the vector renders so the two come out with
    exactly the same aspect.  They have to: the compositor scales the
    vector page to fit the item's page_rect with ``min(sx, sy)``, so an
    aspect that disagreed with the canvas preview by even a little would
    print the view smaller than it looked, with a white margin.
    """
    if padding_pct is None:
        padding_pct = compute_component_padding(
            component, msp, cache, base_padding_pct=0.05
        )

    cxmin, cymin, cxmax, cymax = component.bbox
    cw = cxmax - cxmin
    ch = cymax - cymin
    if cw < 1e-9 or ch < 1e-9:
        return None

    pad_x = cw * padding_pct
    pad_y = ch * padding_pct
    bounds = (cxmin - pad_x, cymin - pad_y, cxmax + pad_x, cymax + pad_y)

    width_data = bounds[2] - bounds[0]
    height_data = bounds[3] - bounds[1]
    aspect = width_data / height_data
    if aspect > 1:
        fig_width = COMPONENT_MAX_INCHES
        fig_height = fig_width / aspect
    else:
        fig_height = COMPONENT_MAX_INCHES
        fig_width = fig_height * aspect

    return bounds, fig_width, fig_height


def _draw_component_figure(doc, msp, component, bounds,
                           fig_width: float, fig_height: float):
    """Build the matplotlib figure for one component, ready to be saved."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from ezdxf.addons.drawing import Frontend, RenderContext

    xmin, ymin, xmax, ymax = bounds

    fig = plt.figure(figsize=(fig_width, fig_height))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)

    # We do NOT use bbox_inches="tight" because that would crop out the user's manual padding!
    ax.margins(0)
    ax.set_facecolor("white")
    fig.patch.set_facecolor("white")
    ax.set_aspect("equal")
    ax.axis("off")

    # (b) CRITICAL: scope which entities are drawn via filter_func
    member_ids = set(component.entity_ids)

    def only_this_component(entity):
        return entity.dxf.handle in member_ids

    backend = make_backend(ax)
    ctx = RenderContext(doc)
    cfg = build_render_config(
        white_background=True,
        figure_inches=max(fig_width, fig_height),
    )

    Frontend(ctx, out=backend, config=cfg).draw_layout(
        msp, finalize=True, filter_func=only_this_component
    )
    return fig


def render_component_vector(
    doc,
    msp,
    component: ComponentRegion,
    output_pdf_path: str,
    padding_pct: Optional[float] = None,
    cache=None,
    log: LogCallback = _noop_log,
) -> Optional[str]:
    """Render one component to a vector PDF.

    The raster is a screen proxy; this is what should reach the printed
    sheet.  Extracted views used to be raster only - the whole-drawing
    import produced a vector PDF and set ``vector_source_path``, but
    component extraction, which is the workflow on the toolbar, did not.
    So every view pulled out of a DWG printed as a bitmap: hairlines
    turned into soft grey ramps and anything blown up past the resolution
    it happened to be rendered at showed its pixels.

    The compositor already prefers ``vector_source_path`` when it is set
    (see pdf_compositor._merge_vector_source), so this is all that was
    missing.  Geometry comes from _component_frame, the same call the
    raster uses, so the two agree exactly.

    Returns the path written, or None if it could not be produced - in
    which case the raster still prints, just as before.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    frame = _component_frame(component, msp, padding_pct, cache)
    if frame is None:
        return None
    bounds, fig_width, fig_height = frame

    fig = None
    try:
        os.makedirs(os.path.dirname(os.path.abspath(output_pdf_path)),
                    exist_ok=True)
        fig = _draw_component_figure(
            doc, msp, component, bounds, fig_width, fig_height
        )
        # No bbox_inches="tight": trimming to the ink would change the
        # aspect and drop the padding, and the compositor fits this page
        # into the item's frame on the sheet.
        fig.savefig(output_pdf_path, format="pdf", facecolor="white")
        log("info", f"Vector PDF for {component.id}: "
                    f"{fig_width:.1f} x {fig_height:.1f} in")
        return output_pdf_path
    except Exception as e:  # noqa: BLE001 - the raster still works
        log("warning",
            f"Could not make a vector PDF for {component.id}: {e}")
        return None
    finally:
        if fig is not None:
            try:
                plt.close(fig)
            except Exception:  # noqa: BLE001
                pass


# The most pixels a single rendered component may occupy.  An A1 sheet at
# 200 DPI is 15 megapixels in total, so a component larger than this
# cannot show more detail on the sheet or on screen.
MAX_RENDER_PIXELS = 16_000_000


def _fit_dpi_to_budget(fig_width: float, fig_height: float, dpi: int,
                       component_id: str, log: LogCallback) -> int:
    """Lower *dpi* until the rendered bitmap fits MAX_RENDER_PIXELS."""
    pixels = (fig_width * dpi) * (fig_height * dpi)
    if pixels <= MAX_RENDER_PIXELS or pixels <= 0:
        return dpi

    scale = (MAX_RENDER_PIXELS / pixels) ** 0.5
    reduced = max(72, int(dpi * scale))
    log("info",
        f"Rendering {component_id} at {reduced} DPI instead of {dpi} — "
        "the full resolution would not fit in memory.")
    return reduced


def render_component_preview(
    doc,
    msp,
    component: ComponentRegion,
    preview_dpi: int = 72,
    cache=None,
    log: LogCallback = _noop_log,
) -> Optional[Image.Image]:
    """Render a component at preview quality (lower DPI).

    Uses the exact same render_component() code path — just a lower
    target_dpi — so the thumbnail the architect approves in the review
    dialog matches what gets rendered at full DPI.
    """
    return render_component(
        doc, msp, component,
        target_dpi=preview_dpi,
        cache=cache,
        log=log,
    )


# ---------------------------------------------------------------------------
# §3 — Render full modelspace at low DPI for overview
# ---------------------------------------------------------------------------

# The overview is drawn with this much slack around the extents.  The
# review dialog maps model coordinates onto the image using the same
# figure, so the two must agree exactly.
PREVIEW_PAD_FRACTION = 0.03
# Longest side of the overview figure, in inches.
PREVIEW_MAX_INCHES = 16.0


def modelspace_preview_bounds(msp):
    """The model rectangle the overview image covers, or None."""
    from ezdxf.bbox import extents

    box = extents(msp, fast=True)
    if not box.has_data:
        return None
    width = box.extmax.x - box.extmin.x
    height = box.extmax.y - box.extmin.y
    if width < 1e-9 or height < 1e-9:
        return None
    pad_x = width * PREVIEW_PAD_FRACTION
    pad_y = height * PREVIEW_PAD_FRACTION
    return (box.extmin.x - pad_x, box.extmin.y - pad_y,
            box.extmax.x + pad_x, box.extmax.y + pad_y)


def render_full_modelspace_preview(
    doc,
    msp,
    preview_dpi: int = 72,
    log: LogCallback = _noop_log,
) -> Optional[Image.Image]:
    """Render the whole modelspace at low DPI for the review dialog.

    No entity scoping - this shows everything.

    Returns
    -------
    (PIL.Image.Image, tuple) or (None, None)
        The image and the (xmin, ymin, xmax, ymax) model rectangle it
        covers.  The caller needs the second value to place component
        boxes on the image; working it out independently is what put
        every box in the wrong place.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        log("error", "matplotlib is not installed.")
        return None, None

    try:
        from ezdxf.addons.drawing import matplotlib as draw_mpl
        from ezdxf.addons.drawing import Frontend, RenderContext
        from ezdxf.addons.drawing import config as draw_config
        from ezdxf.bbox import extents
    except ImportError:
        log("error", "ezdxf drawing addon not available.")
        return None, None

    fig = None
    tmp_path = None
    try:
        bounds = modelspace_preview_bounds(msp)
        if bounds is None:
            log("error", "Modelspace is empty or has zero extent.")
            return None, None

        xmin, ymin, xmax, ymax = bounds
        width_pts = xmax - xmin
        height_pts = ymax - ymin

        # A figure sized in drawing units would be thousands of inches
        # across; scale the long side to something sane and let the other
        # follow, so the image keeps the bounds' aspect exactly.
        aspect = width_pts / height_pts
        if aspect >= 1.0:
            fig_w = PREVIEW_MAX_INCHES
            fig_h = PREVIEW_MAX_INCHES / aspect
        else:
            fig_h = PREVIEW_MAX_INCHES
            fig_w = PREVIEW_MAX_INCHES * aspect

        fig = plt.figure(figsize=(fig_w, fig_h), dpi=preview_dpi)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_facecolor("white")
        fig.patch.set_facecolor("white")

        backend = make_backend(ax)
        ctx = RenderContext(doc)
        # The overview thumbnail is tiny, so it needs the same black,
        # weighted strokes as everything else to stay readable.
        cfg = build_render_config(white_background=True,
                                  figure_inches=max(fig_w, fig_h))
        Frontend(ctx, out=backend, config=cfg).draw_layout(msp, finalize=True)

        # After drawing, not before: the backend sets its own limits while
        # finalizing, which would replace anything set earlier.  An equal
        # aspect keeps the drawing undistorted; "datalim" makes matplotlib
        # widen the data range to fill the axes rather than shrink the axes,
        # so the axes still cover the figure edge to edge.
        ax.set_aspect("equal", adjustable="datalim")
        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymin, ymax)
        ax.margins(0)
        ax.axis("off")

        # Draw once so that widening is applied, then read the range back.
        # What the axes end up covering is the truth the overview boxes have
        # to be measured against - not what was asked for.
        fig.canvas.draw()
        covered = (float(ax.get_xlim()[0]), float(ax.get_ylim()[0]),
                   float(ax.get_xlim()[1]), float(ax.get_ylim()[1]))

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name

        # No bbox_inches="tight": trimming to the ink would break the
        # correspondence between image pixels and model coordinates.
        fig.savefig(tmp_path, dpi=preview_dpi, facecolor="white")

        img = Image.open(tmp_path)
        img.load()

        log("info", "Rendered modelspace overview: %d x %d covering "
                    "(%.0f, %.0f) - (%.0f, %.0f)"
            % (img.width, img.height, covered[0], covered[1],
               covered[2], covered[3]))
        return img, covered

    except Exception as e:
        log("error", f"Failed to render modelspace preview: {e}")
        return None, None

    finally:
        # Close only our own figure.  plt.close("all") - what the failure
        # path used to do - would also destroy any figure the caller was
        # partway through building.
        if fig is not None:
            try:
                plt.close(fig)
            except Exception:  # noqa: BLE001
                pass
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Merge / Split / Add Custom Region helpers (used by review dialog)
# ---------------------------------------------------------------------------

def merge_components(
    components: list[ComponentRegion],
) -> ComponentRegion:
    """Merge multiple components into one by unioning entity_ids and bboxes."""
    all_ids: list[str] = []
    xmins, ymins, xmaxs, ymaxs = [], [], [], []

    for comp in components:
        all_ids.extend(comp.entity_ids)
        xmins.append(comp.bbox[0])
        ymins.append(comp.bbox[1])
        xmaxs.append(comp.bbox[2])
        ymaxs.append(comp.bbox[3])

    merged_bbox = (min(xmins), min(ymins), max(xmaxs), max(ymaxs))

    # Use the first component's label if available
    label = None
    for comp in components:
        if comp.suggested_label:
            label = comp.suggested_label
            break

    return ComponentRegion(
        id=f"comp_{uuid.uuid4().hex[:6]}",
        bbox=merged_bbox,
        entity_ids=list(set(all_ids)),  # deduplicate
        entity_count=len(set(all_ids)),
        suggested_label=label,
    )


def split_component(
    component: ComponentRegion,
    divider_pos: float,
    axis: str,  # "x" or "y"
    entity_bboxes: dict[str, tuple[float, float, float, float]],
) -> tuple[ComponentRegion, ComponentRegion]:
    """Split a component along a dividing line.

    Partitions entity_ids by which side of the line each entity's
    bbox center falls on.
    """
    group_a: list[str] = []
    group_b: list[str] = []

    for eid in component.entity_ids:
        if eid not in entity_bboxes:
            group_a.append(eid)  # fallback: keep in first group
            continue

        ebbox = entity_bboxes[eid]
        if axis == "x":
            center = (ebbox[0] + ebbox[2]) / 2.0
        else:
            center = (ebbox[1] + ebbox[3]) / 2.0

        if center < divider_pos:
            group_a.append(eid)
        else:
            group_b.append(eid)

    def _make_comp(ids: list[str]) -> ComponentRegion:
        if not ids:
            return ComponentRegion(
                id=f"comp_{uuid.uuid4().hex[:6]}",
                bbox=(0, 0, 0, 0),
                entity_ids=[],
                entity_count=0,
            )
        bboxes = [entity_bboxes[eid] for eid in ids if eid in entity_bboxes]
        if not bboxes:
            return ComponentRegion(
                id=f"comp_{uuid.uuid4().hex[:6]}",
                bbox=component.bbox,
                entity_ids=ids,
                entity_count=len(ids),
            )
        return ComponentRegion(
            id=f"comp_{uuid.uuid4().hex[:6]}",
            bbox=(
                min(b[0] for b in bboxes),
                min(b[1] for b in bboxes),
                max(b[2] for b in bboxes),
                max(b[3] for b in bboxes),
            ),
            entity_ids=ids,
            entity_count=len(ids),
        )

    return _make_comp(group_a), _make_comp(group_b)


def create_custom_region(
    region_bbox: tuple[float, float, float, float],
    entity_bboxes: dict[str, tuple[float, float, float, float]],
) -> ComponentRegion:
    """Create a custom region from a user-drawn rectangle.

    Any entities whose individual bbox falls inside the region become
    that region's entity_ids.
    """
    rxmin, rymin, rxmax, rymax = region_bbox
    ids: list[str] = []

    for eid, ebbox in entity_bboxes.items():
        # Entity is "inside" if its bbox is fully contained
        # or its center falls inside the region
        ecx = (ebbox[0] + ebbox[2]) / 2.0
        ecy = (ebbox[1] + ebbox[3]) / 2.0
        if rxmin <= ecx <= rxmax and rymin <= ecy <= rymax:
            ids.append(eid)

    return ComponentRegion(
        id=f"comp_{uuid.uuid4().hex[:6]}",
        bbox=region_bbox,
        entity_ids=ids,
        entity_count=len(ids),
    )
