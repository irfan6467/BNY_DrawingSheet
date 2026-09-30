"""
Unified CAD import pipeline — handles both DWG and DXF files.

Pipeline:
  .dwg file → ODA File Converter → DXF → ezdxf → shared processing
  .dxf file → ezdxf directly → shared processing (skips ODA entirely)

Both paths converge into process_dxf_document() which handles:
  - Layout selection
  - Component detection (via dwg_components.detect_components)
  - Component review dialog
  - Per-component rendering
  - ImportedAsset creation

Key design decisions:
  1. Vector PDF output → lines stay crisp at any zoom.
  2. Paper Space Layout support → uses the drafter's composed layout.
  3. Bundled font fallback → reliable text rendering.
  4. Line-weight derived from actual output DPI, not hardcoded.
  5. DXF files skip ODA entirely — no external dependency needed.

DESIGN RULE (§9): No Qt imports.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

from app.core.project_state import ImportedAsset
from app.utils.paths import resource_path

LogCallback = Callable[[str, str], None]


def _noop_log(severity: str, message: str) -> None:
    pass


# Longest side, in inches, of a whole-drawing render before DPI.  Kept
# separate from the per-component size because a whole sheet holds many
# more views and needs the extra room.
WHOLE_DRAWING_INCHES = 20.0


def subprocess_flags() -> dict:
    """Keyword arguments that keep a helper process out of the way.

    A windowed PyInstaller build has no console, so every ``subprocess``
    call opens a black window that flashes over the drawing and steals
    focus.  It also has no usable stdin, and a child that inherits the
    missing handle can fail to start at all.
    """
    kwargs: dict = {"stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kwargs


# ---------------------------------------------------------------------------
# ODA File Converter discovery (only needed for .dwg files)
# ---------------------------------------------------------------------------

# ODA installs to a version-named folder, e.g.
#   C:\Program Files\ODA\ODAFileConverter 27.1.0\OdaFileConverter.exe
# so the version is never assumed - these patterns match any of them.
_ODA_GLOBS = (
    os.path.join("ODA", "**", "Oda*FileConverter.exe"),
    os.path.join("ODA*", "**", "Oda*FileConverter.exe"),
    os.path.join("*ODA*", "Oda*FileConverter.exe"),
)

# Set this to point the app straight at a converter, for machines set up
# by a script rather than by hand.
_ODA_ENV_VAR = "ODA_FILE_CONVERTER"


def _oda_search_roots() -> list:
    """Places worth looking for an ODA install, most likely first."""
    roots = []
    for name in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        value = os.environ.get(name)
        if value:
            roots.append(value)

    # Some sites install to a data drive instead.
    for letter in "CDEF":
        drive = f"{letter}:\\"
        if os.path.isdir(drive):
            roots.append(drive)
            roots.append(os.path.join(drive, "Program Files"))

    seen = set()
    unique = []
    for root in roots:
        key = os.path.normcase(os.path.abspath(root))
        if key not in seen and os.path.isdir(root):
            seen.add(key)
            unique.append(root)
    return unique


def _newest(paths: list) -> Optional[str]:
    """Prefer the highest version when several are installed."""
    if not paths:
        return None
    return sorted(paths, key=lambda p: os.path.normcase(p))[-1]


def find_oda_converter(log: LogCallback = _noop_log, custom_path: Optional[str] = None) -> Optional[str]:
    """Locate the ODA File Converter executable.
    
    Only needed for .dwg files. DXF files are parsed directly by ezdxf.
    """
    if custom_path and os.path.isfile(custom_path):
        log("info", f"Using custom ODA File Converter path: {custom_path}")
        return custom_path

    from_env = os.environ.get(_ODA_ENV_VAR)
    if from_env and os.path.isfile(from_env):
        log("info", f"Using {_ODA_ENV_VAR}: {from_env}")
        return from_env

    for name in ("ODAFileConverter", "OdaFileConverter"):
        found = shutil.which(name)
        if found:
            log("info", f"Found ODA File Converter on PATH: {found}")
            return found

    for root in _oda_search_roots():
        matches = []
        for pattern in _ODA_GLOBS:
            matches.extend(
                glob.glob(os.path.join(root, pattern), recursive=True)
            )
        best = _newest([m for m in matches if os.path.isfile(m)])
        if best:
            log("info", f"Found ODA File Converter: {best}")
            return best

    log("info",
        "ODA File Converter not found. DWG files need it; DXF files import "
        "directly without it. Install it from "
        "https://www.opendesign.com/guestfiles/oda_file_converter, or set "
        f"{_ODA_ENV_VAR} to the full path of OdaFileConverter.exe.")
    return None


# ---------------------------------------------------------------------------
# DWG → DXF conversion via ODA
# ---------------------------------------------------------------------------

def convert_dwg_to_dxf(
    dwg_path: str,
    output_dir: str,
    oda_path: Optional[str] = None,
    log: LogCallback = _noop_log,
) -> Optional[str]:
    """Convert a .dwg file to .dxf using ODA File Converter."""
    if oda_path is None:
        oda_path = find_oda_converter(log)
    if oda_path is None:
        return None

    dwg_path = os.path.abspath(dwg_path)
    if not os.path.isfile(dwg_path):
        log("error", f"DWG file not found: {dwg_path}")
        return None

    dwg_name = os.path.basename(dwg_path)
    os.makedirs(output_dir, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp_in_dir:
        isolated_dwg_path = os.path.join(tmp_in_dir, dwg_name)
        shutil.copy2(dwg_path, isolated_dwg_path)

        cmd = [
            oda_path,
            tmp_in_dir, output_dir,
            "ACAD2018", "DXF", "0", "1",
        ]

        log("info", f"Converting DWG → DXF: {dwg_name}")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
                **subprocess_flags(),
            )

            dxf_name = Path(dwg_name).stem + ".dxf"
            dxf_path = os.path.join(output_dir, dxf_name)

            if os.path.isfile(dxf_path):
                log("success", f"DWG → DXF conversion succeeded: {dxf_name}")
                return dxf_path
            else:
                stderr = result.stderr.strip() if result.stderr else "(no error output)"
                log("error",
                    f"ODA File Converter did not produce output for {dwg_name}. "
                    f"Stderr: {stderr}")
                return None

        except FileNotFoundError:
            log("error", f"ODA File Converter executable not found at: {oda_path}")
            return None
        except subprocess.TimeoutExpired:
            log("error", f"ODA File Converter timed out converting {dwg_name}")
            return None
        except Exception as e:
            log("error", f"ODA conversion failed for {dwg_name}: {e}")
            return None


# ---------------------------------------------------------------------------
# Layout picker helper
# ---------------------------------------------------------------------------

def _layout_has_drawable(layout) -> bool:
    """Whether a layout holds anything worth rendering.

    A viewport is not content: a paper space layout is usually a sheet
    border with viewports looking into model space, and on its own it
    renders as an empty frame.
    """
    try:
        for entity in layout:
            if entity.dxftype() != "VIEWPORT":
                return True
    except Exception:  # noqa: BLE001 - a layout we cannot walk is no use
        return False
    return False


def _pick_layout(doc, log: LogCallback = _noop_log, layout_callback=None):
    """Pick the best layout to render from a DXF document.

    Prefers Paper Space Layouts over raw Modelspace.  If multiple
    non-Model layouts exist and a layout_callback is provided, it's
    called with the list of layout names to let the user choose.

    Empty layouts are skipped.  AutoCAD creates Layout1 and Layout2 in
    every new drawing whether or not anything is put on them, so a file
    with all its geometry in model space - which is most of them - has a
    paper space layout that this used to choose regardless.  The render
    then had nothing in it and the import failed outright with "DXF
    layout is empty", having never looked at model space at all.
    """
    layout_names = [
        name for name in doc.layout_names()
        if name.upper() != "MODEL"
    ]

    # Only offer layouts that have something on them.
    populated = []
    for name in layout_names:
        try:
            if _layout_has_drawable(doc.layout(name)):
                populated.append(name)
        except Exception:  # noqa: BLE001
            continue

    if not populated:
        if layout_names:
            log("info", "Paper Space layouts are empty — using Modelspace")
        else:
            log("info", "No Paper Space layouts found — using Modelspace")
        return doc.modelspace()

    if len(populated) == 1:
        chosen = populated[0]
        log("info", f"Using Paper Space layout: {chosen}")
        return doc.layout(chosen)

    # Multiple layouts — use callback or default to first
    if layout_callback:
        chosen = layout_callback(populated)
        if chosen and chosen in populated:
            log("info", f"User selected layout: {chosen}")
            return doc.layout(chosen)

    # Default to first non-Model layout
    chosen = populated[0]
    log("info", f"Multiple layouts found ({', '.join(populated)}). "
        f"Using first: {chosen}")
    return doc.layout(chosen)


# ---------------------------------------------------------------------------
# Font configuration for ezdxf rendering
# ---------------------------------------------------------------------------

def _configure_fonts():
    """Configure ezdxf font mapping with bundled fallback fonts."""
    font_dir = resource_path(os.path.join("app", "resources", "fonts"))
    dejavu_path = os.path.join(font_dir, "DejaVuSans.ttf")

    font_mapping = {}
    if os.path.isfile(dejavu_path):
        common_shx = [
            "simplex.shx", "romans.shx", "txt.shx",
            "isocp.shx", "isocpeur.shx", "monotxt.shx",
            "gothic.shx", "scripts.shx", "complex.shx",
        ]
        for shx in common_shx:
            font_mapping[shx] = dejavu_path

    return font_mapping, dejavu_path if os.path.isfile(dejavu_path) else None


# ---------------------------------------------------------------------------
# Shared ezdxf rendering: DXF → Vector PDF
# ---------------------------------------------------------------------------

def render_dxf_to_vector_pdf(
    dxf_path: str,
    output_pdf_path: str,
    dpi: int = 300,
    log: LogCallback = _noop_log,
    layout_callback=None,
) -> Optional[str]:
    """Render a .dxf file to a vector PDF using ezdxf's matplotlib backend."""
    try:
        import ezdxf
    except ImportError:
        log("error", "ezdxf is not installed — cannot render DXF files.")
        return None

    try:
        from ezdxf.addons.drawing import matplotlib as draw_mpl
    except ImportError:
        log("error", "ezdxf drawing addon not available.")
        return None

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        log("error", "matplotlib is not installed.")
        return None

    dxf_path = os.path.abspath(dxf_path)
    if not os.path.isfile(dxf_path):
        log("error", f"DXF file not found: {dxf_path}")
        return None

    try:
        doc = ezdxf.readfile(dxf_path)
        log("info", f"Parsed DXF: {os.path.basename(dxf_path)} "
            f"(version: {doc.dxfversion})")
    except Exception as e:
        log("error", f"Failed to read DXF {os.path.basename(dxf_path)}: {e}")
        return None

    try:
        layout = _pick_layout(doc, log, layout_callback)

        from ezdxf.bbox import extents
        bbox = extents(layout, fast=True)
        if not bbox.has_data:
            log("error", "DXF layout is empty or bounding box calculation failed.")
            return None

        width_pts = bbox.extmax.x - bbox.extmin.x
        height_pts = bbox.extmax.y - bbox.extmin.y
        if height_pts == 0 or width_pts == 0:
            log("error", "DXF layout has zero width or height.")
            return None

        aspect = width_pts / height_pts
        fig_width = WHOLE_DRAWING_INCHES
        fig_height = fig_width / aspect

        fig = plt.figure(figsize=(fig_width, fig_height), dpi=dpi)
        fig.patch.set_facecolor('white')
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_facecolor('white')

        pad_x = width_pts * 0.05
        pad_y = height_pts * 0.05
        ax.set_xlim(bbox.extmin.x - pad_x, bbox.extmax.x + pad_x)
        ax.set_ylim(bbox.extmin.y - pad_y, bbox.extmax.y + pad_y)

        from ezdxf.addons.drawing import Frontend, RenderContext
        from app.core.dwg_components import build_render_config, make_backend

        backend = make_backend(ax)

        ctx = RenderContext(doc)
        font_mapping, fallback_font = _configure_fonts()

        config = build_render_config(
            figure_inches=max(fig_width, fig_height)
        )
        Frontend(ctx, out=backend, config=config).draw_layout(layout)

        ax.set_aspect("equal")
        ax.axis("off")

        fig.savefig(output_pdf_path, format="pdf", bbox_inches="tight",
                    pad_inches=0.1, facecolor="white")
        plt.close(fig)

        log("success", f"Rendered DXF to vector PDF: {output_pdf_path}")
        return output_pdf_path

    except Exception as e:
        log("error", f"Failed to render DXF {os.path.basename(dxf_path)}: {e}")
        try:
            plt.close("all")
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Shared ezdxf rendering: DXF → Raster preview
# ---------------------------------------------------------------------------

def render_dxf_to_image(
    dxf_path: str,
    dpi: int = 300,
    log: LogCallback = _noop_log,
    layout_callback=None,
) -> Optional[Image.Image]:
    """Render a .dxf file to a PIL Image for on-screen preview."""
    try:
        import ezdxf
    except ImportError:
        log("error", "ezdxf is not installed — cannot render DXF files.")
        return None

    try:
        from ezdxf.addons.drawing import matplotlib as draw_mpl
    except ImportError:
        log("error", "ezdxf drawing addon not available.")
        return None

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        log("error", "matplotlib is not installed.")
        return None

    dxf_path = os.path.abspath(dxf_path)
    if not os.path.isfile(dxf_path):
        log("error", f"DXF file not found: {dxf_path}")
        return None

    try:
        doc = ezdxf.readfile(dxf_path)
    except Exception as e:
        log("error", f"Failed to read DXF {os.path.basename(dxf_path)}: {e}")
        return None

    try:
        layout = _pick_layout(doc, log, layout_callback)

        from ezdxf.bbox import extents
        bbox = extents(layout, fast=True)
        if not bbox.has_data:
            log("error", "DXF layout is empty.")
            return None

        width_pts = bbox.extmax.x - bbox.extmin.x
        height_pts = bbox.extmax.y - bbox.extmin.y
        if height_pts == 0 or width_pts == 0:
            log("error", "DXF layout has zero dimensions.")
            return None

        aspect = width_pts / height_pts
        fig_width = WHOLE_DRAWING_INCHES
        fig_height = fig_width / aspect

        # A whole drawing at 20 inches and 300 DPI is 6000 px on its long
        # side - 83 MB as RGB for a single asset, and nothing capped it.
        # make_backend is what made that size real (ezdxf used to shrink
        # every figure to about 6.4 inches), so the budget has to be
        # applied here too now that it is.  The vector PDF alongside
        # carries the detail into print regardless.
        from app.core.dwg_components import _fit_dpi_to_budget

        dpi = _fit_dpi_to_budget(fig_width, fig_height, dpi,
                                 os.path.basename(dxf_path), log)

        fig = plt.figure(figsize=(fig_width, fig_height), dpi=dpi)
        fig.patch.set_facecolor('white')
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_facecolor('white')

        pad_x = width_pts * 0.05
        pad_y = height_pts * 0.05
        ax.set_xlim(bbox.extmin.x - pad_x, bbox.extmax.x + pad_x)
        ax.set_ylim(bbox.extmin.y - pad_y, bbox.extmax.y + pad_y)

        from ezdxf.addons.drawing import Frontend, RenderContext
        from app.core.dwg_components import build_render_config, make_backend

        backend = make_backend(ax)

        ctx = RenderContext(doc)
        config = build_render_config(
            figure_inches=max(fig_width, fig_height)
        )
        Frontend(ctx, out=backend, config=config).draw_layout(layout)

        ax.set_aspect("equal")
        ax.axis("off")

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name

        fig.savefig(tmp_path, dpi=dpi, bbox_inches="tight",
                    pad_inches=0.1, facecolor="white")
        plt.close(fig)

        img = Image.open(tmp_path)
        img.load()

        try:
            os.unlink(tmp_path)
        except OSError:
            pass

        log("success", f"Rendered DXF to preview: "
            f"{img.width}×{img.height} at {dpi} DPI")
        return img

    except Exception as e:
        log("error", f"Failed to render DXF {os.path.basename(dxf_path)}: {e}")
        try:
            plt.close("all")
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Shared function: process a parsed ezdxf document (DWG and DXF converge here)
# ---------------------------------------------------------------------------

def _parse_dxf_file(dxf_path: str, log: LogCallback = _noop_log):
    """Parse a DXF file into an ezdxf document.
    
    Returns (doc, dxf_path) or (None, None) on failure.
    This is the convergence point — both DWG (after ODA) and DXF arrive here.
    """
    try:
        import ezdxf
    except ImportError:
        log("error", "ezdxf is not installed — cannot process CAD files.")
        return None, None

    dxf_path = os.path.abspath(dxf_path)
    if not os.path.isfile(dxf_path):
        log("error", f"DXF file not found: {dxf_path}")
        return None, None

    try:
        doc = ezdxf.readfile(dxf_path)
        log("info", f"Parsed DXF: {os.path.basename(dxf_path)} (version: {doc.dxfversion})")
        return doc, dxf_path
    except Exception as e:
        log("error", f"Failed to read DXF {os.path.basename(dxf_path)}: {e}")
        return None, None


def process_dxf_document(
    doc,
    dxf_path: str,
    original_source_path: str,
    gap_threshold: Optional[float] = None,
    log: LogCallback = _noop_log,
):
    """Shared processing for both DWG and DXF files.
    
    This is the SINGLE code path that both import_dwg_components() and
    import_dxf_components() converge into after each has independently
    arrived at a parsed ezdxf document.
    
    Runs detect_components(), and returns everything the review dialog needs.
    
    Parameters
    ----------
    doc : ezdxf document
    dxf_path : str
        Path to the DXF file on disk (for rendering).
    original_source_path : str
        Original .dwg or .dxf path (for provenance tracking on assets).
    gap_threshold : float or None
    log : LogCallback
    
    Returns
    -------
    tuple (doc, msp, DetectionResult, cache, dxf_path, original_source_path) or None
    """
    try:
        from ezdxf import bbox as ezdxf_bbox
    except ImportError:
        log("error", "ezdxf is not installed.")
        return None

    from app.core.dwg_components import detect_components

    msp = doc.modelspace()
    cache = ezdxf_bbox.Cache()

    result = detect_components(msp, cache, gap_threshold, log)
    log("info", f"Component detection found {len(result.components)} components, "
        f"{result.unassigned_count} unassigned entities")

    return (doc, msp, result, cache, dxf_path, original_source_path)


# ---------------------------------------------------------------------------
# Full DWG import pipeline (whole-file, no component extraction)
# ---------------------------------------------------------------------------

def import_dwg(
    dwg_path: str,
    dpi: int = 300,
    oda_path: Optional[str] = None,
    log: LogCallback = _noop_log,
    layout_callback=None,
) -> Optional[ImportedAsset]:
    """Full DWG import pipeline:
    DWG → ODA → DXF → ezdxf → vector PDF + raster preview → ImportedAsset.
    """
    dwg_path = os.path.abspath(dwg_path)
    dwg_name = os.path.basename(dwg_path)
    log("info", f"Starting DWG import pipeline: {dwg_name}")

    with tempfile.TemporaryDirectory() as tmp_dir:
        dxf_path = convert_dwg_to_dxf(dwg_path, tmp_dir, oda_path, log)
        if dxf_path is None:
            log("error", f"DWG import aborted — DXF conversion failed for {dwg_name}")
            return None

        # Vector PDF
        dwg_dir = os.path.dirname(dwg_path)
        vector_pdf_name = Path(dwg_name).stem + "_vector.pdf"
        vector_pdf_path = os.path.join(dwg_dir, vector_pdf_name)

        vector_result = render_dxf_to_vector_pdf(
            dxf_path, vector_pdf_path, dpi, log, layout_callback
        )

        # Raster preview
        img = render_dxf_to_image(dxf_path, dpi, log, layout_callback)
        if img is None:
            log("error", f"DWG import aborted — DXF rendering failed for {dwg_name}")
            return None

    asset = ImportedAsset(
        id=ImportedAsset.make_id(),
        source_path=dwg_path,
        source_type="dwg_render",
        image=img,
        vector_source_path=vector_result,
    )
    log("success", f"DWG import complete: {dwg_name} → "
        f"{img.width}×{img.height} preview"
        + (f" + vector PDF" if vector_result else ""))
    return asset


# ---------------------------------------------------------------------------
# Full DXF import pipeline (whole-file, no component extraction)
# ---------------------------------------------------------------------------

def import_dxf(
    dxf_path: str,
    dpi: int = 300,
    log: LogCallback = _noop_log,
    layout_callback=None,
) -> Optional[ImportedAsset]:
    """Full DXF import pipeline — skips ODA entirely.
    DXF → ezdxf → vector PDF + raster preview → ImportedAsset.
    """
    dxf_path = os.path.abspath(dxf_path)
    dxf_name = os.path.basename(dxf_path)
    log("info", f"Starting DXF import pipeline: {dxf_name}")

    # Vector PDF
    dxf_dir = os.path.dirname(dxf_path)
    vector_pdf_name = Path(dxf_name).stem + "_vector.pdf"
    vector_pdf_path = os.path.join(dxf_dir, vector_pdf_name)

    vector_result = render_dxf_to_vector_pdf(
        dxf_path, vector_pdf_path, dpi, log, layout_callback
    )

    # Raster preview
    img = render_dxf_to_image(dxf_path, dpi, log, layout_callback)
    if img is None:
        log("error", f"DXF import aborted — rendering failed for {dxf_name}")
        return None

    asset = ImportedAsset(
        id=ImportedAsset.make_id(),
        source_path=dxf_path,
        source_type="dxf_render",
        image=img,
        vector_source_path=vector_result,
    )
    log("success", f"DXF import complete: {dxf_name} → "
        f"{img.width}×{img.height} preview"
        + (f" + vector PDF" if vector_result else ""))
    return asset


# ---------------------------------------------------------------------------
# Component-level DWG import (DWG → ODA → DXF → shared processing)
# ---------------------------------------------------------------------------

def import_dwg_components(
    dwg_path: str,
    oda_path: Optional[str] = None,
    gap_threshold: Optional[float] = None,
    log: LogCallback = _noop_log,
):
    """Component extraction from DWG: DWG → ODA → DXF → process_dxf_document().
    
    Returns the same tuple as process_dxf_document() plus a tmp_dir_obj,
    or None on failure.
    """
    dwg_path = os.path.abspath(dwg_path)
    dwg_name = os.path.basename(dwg_path)
    log("info", f"Starting DWG component extraction: {dwg_name}")

    # Step 1: DWG → DXF via ODA
    tmp_dir_obj = tempfile.TemporaryDirectory()
    tmp_dir = tmp_dir_obj.name

    dxf_path = convert_dwg_to_dxf(dwg_path, tmp_dir, oda_path, log)
    if dxf_path is None:
        log("error", f"Component extraction aborted — DXF conversion failed for {dwg_name}")
        tmp_dir_obj.cleanup()
        return None

    # Step 2: Parse DXF → shared processing
    doc, parsed_path = _parse_dxf_file(dxf_path, log)
    if doc is None:
        tmp_dir_obj.cleanup()
        return None

    result = process_dxf_document(doc, dxf_path, dwg_path, gap_threshold, log)
    if result is None:
        tmp_dir_obj.cleanup()
        return None

    doc, msp, detection_result, cache, dxf_path_out, orig_path = result
    return (doc, msp, detection_result, cache, tmp_dir_obj, orig_path)


# ---------------------------------------------------------------------------
# Component-level DXF import (DXF → shared processing, no ODA)
# ---------------------------------------------------------------------------

def import_dxf_components(
    dxf_path: str,
    gap_threshold: Optional[float] = None,
    log: LogCallback = _noop_log,
):
    """Component extraction from DXF — skips ODA entirely.
    DXF → ezdxf → process_dxf_document().
    
    Same return format as import_dwg_components() for UI compatibility.
    """
    dxf_path = os.path.abspath(dxf_path)
    dxf_name = os.path.basename(dxf_path)
    log("info", f"Starting DXF component extraction: {dxf_name}")

    # Parse directly — no ODA needed
    doc, parsed_path = _parse_dxf_file(dxf_path, log)
    if doc is None:
        return None

    # Create a dummy tmp_dir_obj for API compatibility
    # (DXF doesn't need temp files, but caller expects this in the tuple)
    tmp_dir_obj = tempfile.TemporaryDirectory()

    result = process_dxf_document(doc, dxf_path, dxf_path, gap_threshold, log)
    if result is None:
        tmp_dir_obj.cleanup()
        return None

    doc, msp, detection_result, cache, dxf_path_out, orig_path = result
    return (doc, msp, detection_result, cache, tmp_dir_obj, orig_path)
