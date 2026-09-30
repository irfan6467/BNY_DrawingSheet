"""
Keep the window on screen when something goes wrong.

PySide6 6.5 and later treat a Python exception that escapes a slot or a
reimplemented virtual method as fatal: it calls ``qFatal`` and the
process is gone, with no dialog, no traceback and no chance to save.
That is the "it just closes" report - every unhandled error in a button
handler, a drop handler or a paint routine ended the session outright.

Three things go up here, in this order, before any widget exists:

1. ``sys.excepthook``/``threading.excepthook`` - record the traceback,
   tell the architect, and let the event loop carry on.
2. A Qt message handler, so Qt's own warnings land in the same log
   rather than on a stdout that a windowed build does not have.
3. A faulthandler dump target, which is the only thing that leaves a
   trace when the fault is in C++ and Python never gets a say.

The log lives beside the autosave, under %LOCALAPPDATA%, so a crash
report is one folder for the architect to send on.
"""

from __future__ import annotations

import datetime as _dt
import faulthandler
import functools
import logging
import os
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler
from typing import Callable, Optional

APP_DIR_NAME = "TechnicalDrawingSheet"

_logger: Optional[logging.Logger] = None
_report_hook: Optional[Callable[[str, str], None]] = None
_fault_log = None
_seen_signatures: set = set()


# ---------------------------------------------------------------------------
# Where our files live
# ---------------------------------------------------------------------------

def app_data_dir() -> str:
    """The per-user folder holding logs, autosaves and settings."""
    base = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("XDG_STATE_HOME")
        or os.path.expanduser("~")
    )
    path = os.path.join(base, APP_DIR_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        # A locked-down profile should not stop the app from starting;
        # fall back to the temp directory.
        import tempfile

        path = os.path.join(tempfile.gettempdir(), APP_DIR_NAME)
        os.makedirs(path, exist_ok=True)
    return path


def log_path() -> str:
    return os.path.join(app_data_dir(), "session.log")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _set_logger(logger: logging.Logger) -> None:
    global _logger
    _logger = logger


def get_logger() -> logging.Logger:
    if _logger is not None:
        return _logger

    logger = logging.getLogger("technical_drawing_sheet")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    # logging.getLogger returns the same object every time, so handlers
    # attached again would each write their own copy of every line.  The
    # module global usually prevents a second pass, but it is a global —
    # anything that clears it, a reload or a test, would silently triple
    # the log file.
    if logger.handlers:
        _set_logger(logger)
        return logger

    try:
        handler = RotatingFileHandler(
            log_path(), maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
        )
        logger.addHandler(handler)
    except OSError:
        pass

    # A console build should still show what it is doing.
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(logging.WARNING)
        logger.addHandler(stream)

    _set_logger(logger)
    return logger


# ---------------------------------------------------------------------------
# Exception handling
# ---------------------------------------------------------------------------

def _signature(exc_type, exc_value, tb) -> str:
    """Enough of an error to tell "the same one again" from a new one."""
    last = ""
    frames = traceback.extract_tb(tb)
    if frames:
        frame = frames[-1]
        last = f"{os.path.basename(frame.filename)}:{frame.lineno}"
    return f"{exc_type.__name__}:{last}:{exc_value}"


def _handle(exc_type, exc_value, exc_tb, source: str = "main") -> None:
    if exc_type is None:
        return
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return

    text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    get_logger().error("Unhandled exception (%s)\n%s", source, text)

    signature = _signature(exc_type, exc_value, exc_tb)
    first_time = signature not in _seen_signatures
    _seen_signatures.add(signature)

    if _report_hook is not None:
        try:
            _report_hook(f"{exc_type.__name__}: {exc_value}",
                         text if first_time else "")
        except Exception:  # noqa: BLE001 - the reporter must never re-raise
            get_logger().exception("The error reporter itself failed")


def install(report_hook: Optional[Callable[[str, str], None]] = None) -> None:
    """Put the safety net up.  Call once, before the first widget.

    ``report_hook(summary, traceback_text)`` is how the UI hears about an
    error; it is set later by the main window, because at import time
    there is nowhere yet to show a dialog.
    """
    global _fault_log

    get_logger().info(
        "Session started %s  python=%s  frozen=%s",
        _dt.datetime.now().isoformat(timespec="seconds"),
        sys.version.split()[0],
        getattr(sys, "frozen", False),
    )

    set_report_hook(report_hook)

    sys.excepthook = lambda t, v, tb: _handle(t, v, tb, "main")

    def _thread_hook(args):
        _handle(args.exc_type, args.exc_value, args.exc_traceback,
                f"thread {getattr(args.thread, 'name', '?')}")

    threading.excepthook = _thread_hook

    # A hard fault in Qt or a native library never reaches Python, but
    # faulthandler writes the C-level stack before the process dies.
    try:
        _fault_log = open(
            os.path.join(app_data_dir(), "crash.log"), "a", encoding="utf-8"
        )
        stamp = _dt.datetime.now().isoformat(timespec="seconds")
        _fault_log.write("\n--- session " + stamp + " ---\n")
        _fault_log.flush()
        faulthandler.enable(file=_fault_log, all_threads=True)
    except Exception:  # noqa: BLE001 - diagnostics must never block startup
        pass

    _install_qt_handler()


def set_report_hook(hook: Optional[Callable[[str, str], None]]) -> None:
    global _report_hook
    _report_hook = hook


def _install_qt_handler() -> None:
    """Route Qt's own diagnostics into the same log file."""
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:  # pragma: no cover
        return

    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def handler(mode, context, message):
        get_logger().log(levels.get(mode, logging.INFO), "Qt: %s", message)

    qInstallMessageHandler(handler)


# ---------------------------------------------------------------------------
# Guarding individual callbacks
# ---------------------------------------------------------------------------

def guard(func):
    """Wrap a slot so an error in it is reported instead of fatal.

    ``install()`` covers anything Python routes through ``sys.excepthook``,
    but an exception raised inside a Qt virtual method can be swallowed by
    the C++ frame it unwinds through before Python sees it.  Decorating
    the handler keeps the failure inside Python, where it can be logged.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:  # noqa: BLE001 - that is the entire point
            _handle(*sys.exc_info(), source=f"slot {func.__qualname__}")
            return None

    return wrapper


def guard_all(cls):
    """Apply :func:`guard` to every handler on a class that Qt calls back.

    Anything named ``_on_*`` is a signal handler by this codebase's own
    convention; the rest are named explicitly because they are reached
    from a menu, a shortcut or a drop.
    """
    extra = {
        # Import and export
        "_import_file", "_import_cad_file", "_import_cad_components",
        "_import_raster_paths", "_review_cad_components", "_generate_pdf",
        # Opening and saving.  Not guarded like the rest: _save_project
        # and _confirm_discard return a bool the caller acts on, and a
        # guarded failure returns None, which reads as "cancel" - the
        # safe way round, since it stops a close that could not save.
        "_new_project", "_open_project", "_offer_recovery",
        "_rebuild_recent_menu", "_remember_recent",
        # Editing
        "_undo", "_redo", "_delete_selected", "_group_selection",
        "_ungroup_selection", "_start_item_crop", "_apply_item_crop",
        "_cancel_item_crop", "_add_annotation", "_add_view_label",
        "_edit_annotation_text", "_remove_asset",
        "_bring_selection_to_front", "_rebuild_canvas_items",
        "_resize_annotation_text",
        # View
        "_toggle_panel", "_zoom_by", "_open_diagnostics_folder",
        "_run_startup_health_check", "_repair_dwg_support",
    }
    for name, value in list(vars(cls).items()):
        if callable(value) and (name.startswith("_on_") or name in extra):
            setattr(cls, name, guard(value))
    return cls
