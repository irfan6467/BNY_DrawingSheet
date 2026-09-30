"""
PyInstaller/Nuitka-safe resource path resolver.

When the app runs from a PyInstaller onefile bundle, bundled resources
are extracted to a temp directory exposed via sys._MEIPASS.  In dev mode
(running from source), the project root is used instead.

Every file-load in the codebase must go through resource_path() — never
use raw relative paths that assume an unpackaged dev layout.
"""

import os
import sys


def resource_path(relative_path: str) -> str:
    """Resolve *relative_path* to an absolute path that works both in
    development and inside a PyInstaller/Nuitka frozen bundle.

    Parameters
    ----------
    relative_path : str
        Path relative to the project root (dev) or the bundle's data
        directory (frozen).  Use forward slashes for portability;
        ``os.path.join`` normalises them.

    Returns
    -------
    str
        Absolute filesystem path to the resource.
    """
    # PyInstaller sets sys._MEIPASS to the temp extraction directory.
    # Nuitka uses __compiled__ but also supports _MEIPASS in onefile mode.
    base_path = getattr(sys, "_MEIPASS", None)
    if base_path is None:
        # Dev mode — project root is two levels up from this file
        # (app/utils/paths.py -> app/utils -> app -> project_root)
        base_path = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
    return os.path.join(base_path, relative_path)
