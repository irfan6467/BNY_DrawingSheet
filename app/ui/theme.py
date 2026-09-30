"""
Design-system theme for the Technical Drawing Sheet Automation app.

All 16 semantic color tokens are derived from the client's three palette
swatches (Blush Rose / Plum Mauve / Cream Teal Lavender), with three
deliberate exceptions documented inline.

Usage
-----
    from app.ui.theme import THEME, load_theme

    app = QApplication(sys.argv)
    app.setStyleSheet(load_theme())

Every UI module must import colors from ``THEME`` rather than hardcoding
hex values — this is the single source of truth for the app's visual
identity.
"""

import os

from app.utils.paths import resource_path


# ---------------------------------------------------------------------------
# Semantic color tokens
# ---------------------------------------------------------------------------
THEME: dict[str, str] = {
    # ── Backgrounds ────────────────────────────────────────────────────
    # Main window / top-level background — warm cream
    "bg_app": "#141414",

    # Dock panels (slot assignment, callout manager, metadata, log) —
    # light blue
    "bg_panel": "#202020",

    # Floating panels and contextual controls sit one elevation above the rail.
    "bg_surface_elevated": "#262626",

    # QGraphicsView viewport / canvas background.
    "bg_canvas": "#2A2A2A",

    # ── Accent / interactive ───────────────────────────────────────────
    # The studio yellow, lightened: the old #F5C518 was heavy against the
    # dark chrome, especially across whole buttons and selection outlines.
    # Primary buttons, active tab, selected-slot border
    "accent_primary": "#FFD966",

    # Primary button hover state
    "accent_primary_hover": "#FFE699",

    # Secondary buttons, callout ring default color
    "accent_secondary": "#E8C34A",

    # Active crop-box marquee fill/outline, selection handles
    "accent_selection": "#FFD966",

    # ── Borders ────────────────────────────────────────────────────────
    # Dividers, input field borders — subtle pink
    "border_subtle": "#414141",

    # Panel outlines, group-box frames — soft pink
    "border_strong": "#5A5A5A",

    # ── Status / log ──────────────────────────────────────────────────
    # Log-panel info messages, progress bar fill — powder teal
    "status_info": "#FFD966",

    # Contrast-adjusted derivative of status_info
    "status_success": "#59C36A",

    # Warning messages
    "status_warning": "#FFD966",

    # Error messages
    "status_error": "#EF5350",

    # ── Typography ─────────────────────────────────────────────────────
    # Body text on light backgrounds — deep blue (same as accent_primary)
    "text_primary": "#F2F2F2",

    # Muted / help text — medium blue
    "text_secondary": "#A8A8A8",

    # Text on accent_primary-colored buttons — warm cream for contrast
    "text_inverse": "#111111",

    # Shared geometry token used by QSS and custom floating widgets.
    "radius": "10px",
}


# ---------------------------------------------------------------------------
# QSS loader
# ---------------------------------------------------------------------------
def load_theme() -> str:
    """Read the QSS template and return a fully-resolved stylesheet string.

    The template file uses Python ``str.format()`` placeholders that map
    1-to-1 to ``THEME`` keys (e.g. ``{bg_app}``).  This function loads
    the template through :func:`resource_path` so it resolves correctly
    both during development and inside a PyInstaller/Nuitka bundle.

    Returns
    -------
    str
        A complete QSS stylesheet ready to pass to
        ``QApplication.setStyleSheet()``.
    """
    template_path = resource_path(os.path.join("app", "ui", "theme.qss.template"))
    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()
    return template.format(**THEME)
