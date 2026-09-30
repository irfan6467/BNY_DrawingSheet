"""
Phase 5 — Template registry: loads and validates SheetTemplate JSON configs.

Provides the bridge between the Phase 1 calibration tool's JSON output
and the SheetTemplate dataclass from project_state.py.

Under the redesigned model, the calibration JSON's "slots" field is
loaded as ``guide_rects`` — optional snap-guide rectangles, never
mandatory placement constraints.

DESIGN RULE (§9): No Qt imports.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from app.core.project_state import SheetTemplate
from app.utils.paths import resource_path





def load_template(json_path: str) -> SheetTemplate:
    """Load and validate a SheetTemplate from a calibration JSON file.

    Parameters
    ----------
    json_path : str
        Absolute or relative path to the JSON file produced by the
        template calibrator.

    Returns
    -------
    SheetTemplate
        Validated template instance.

    Raises
    ------
    FileNotFoundError
        If the JSON file doesn't exist.
    ValueError
        If the JSON is structurally invalid or missing required fields.
    """
    json_path = os.path.abspath(json_path)
    if not os.path.isfile(json_path):
        raise FileNotFoundError(f"Template JSON not found: {json_path}")

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Validate required top-level keys
    required_keys = {
        "template_name", "base_pdf_path", "paper_size",
        "orientation", "callout_column",
    }
    missing = required_keys - set(data.keys())
    if missing:
        raise ValueError(
            f"Template JSON missing required keys: {missing}"
        )

    # Validate callout_column shape
    cc = data["callout_column"]
    cc_required = {"x", "y", "w", "h", "max_details"}
    cc_missing = cc_required - set(cc.keys())
    if cc_missing:
        raise ValueError(
            f"callout_column missing required keys: {cc_missing}"
        )

    # Load guide_rects from the "slots" key in the JSON (backward-compatible
    # with existing calibration files).  These are optional snap guides.
    guide_rects = data.get("slots", {})
    if not isinstance(guide_rects, dict):
        guide_rects = {}

    # Resolve base_pdf_path relative to the JSON file's directory
    base_pdf = data["base_pdf_path"]
    if not os.path.isabs(base_pdf):
        json_dir = os.path.dirname(json_path)
        base_pdf = os.path.normpath(os.path.join(json_dir, base_pdf))
    data["base_pdf_path"] = base_pdf

    return SheetTemplate(
        template_name=data["template_name"],
        base_pdf_path=data["base_pdf_path"],
        paper_size=data["paper_size"],
        orientation=data["orientation"],
        guide_rects=guide_rects,
        callout_column=data["callout_column"],
    )


def load_default_template() -> Optional[SheetTemplate]:
    """Try to load the default BnY standard A1 template from the
    bundled resources directory.

    Returns None (rather than raising) if the template files aren't
    present — this lets the app start up even if resources are missing,
    with a clear log message.
    """
    json_path = resource_path(
        os.path.join("app", "resources", "templates", "bny_standard_a1.json")
    )
    if not os.path.isfile(json_path):
        return None
    try:
        return load_template(json_path)
    except (ValueError, json.JSONDecodeError):
        return None
