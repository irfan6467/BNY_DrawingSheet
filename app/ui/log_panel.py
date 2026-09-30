"""
Phase 6 — Log panel: severity-colored log output dock widget.

This panel is the app's single source of truth for "what went wrong."
Must never crash silently — every error, warning, and info message from
the import pipeline (Phase 3), image processor (Phase 4), and PDF
compositor (Phase 5) flows through here.

Uses THEME status colors for per-severity text rendering.
"""

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTextEdit, QPushButton, QHBoxLayout,
)

from app.ui.theme import THEME


# ---------------------------------------------------------------------------
# Log signal bridge — connects core/ log callbacks to the Qt panel
# ---------------------------------------------------------------------------

class LogBridge(QObject):
    """Thread-safe bridge from core/ log callbacks to the Qt log panel.

    Core modules emit log events via a plain callback ``(severity, message)``.
    This bridge wraps that as a Qt signal so it's safe to call from any
    thread and delivers on the main thread.
    """
    log_message = Signal(str, str)  # (severity, message)

    def __call__(self, severity: str, message: str) -> None:
        """Callable interface matching core/'s LogCallback type."""
        self.log_message.emit(severity, message)


# ---------------------------------------------------------------------------
# Log panel widget
# ---------------------------------------------------------------------------

SEVERITY_COLORS = {
    "info": THEME["status_info"],
    "success": THEME["status_success"],
    "warning": THEME["status_warning"],
    "error": THEME["status_error"],
}

SEVERITY_PREFIXES = {
    "info": "ℹ️  INFO",
    "success": "✅ OK",
    "warning": "⚠️  WARN",
    "error": "❌ ERROR",
}

# How many lines the panel keeps on screen.  Everything is written to the
# session log on disk regardless — see app.core.crash_guard.log_path.
MAX_LOG_LINES = 2000

# Severities that also go to the file log.  "info" is included: when a
# crash report arrives, the run-up to it is the useful part.
_FILE_LOG_LEVELS = {
    "info": "info",
    "success": "info",
    "warning": "warning",
    "error": "error",
}


class LogPanel(QWidget):
    """Severity-colored scrolling log panel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("logPanel")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # Log output
        self._log_text = QTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setObjectName("logPanel")
        # A long working session imports, crops and regenerates hundreds
        # of times.  Unbounded, the document grows until the panel takes
        # a noticeable moment to repaint and the process is holding the
        # whole day's messages; past a few thousand lines nobody scrolls
        # back that far anyway, and the full history is on disk.
        self._log_text.document().setMaximumBlockCount(MAX_LOG_LINES)
        layout.addWidget(self._log_text)

        # Clear button
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        clear_btn = QPushButton("Clear Log")
        clear_btn.setObjectName("secondaryButton")
        clear_btn.clicked.connect(self._log_text.clear)
        btn_row.addWidget(clear_btn)
        layout.addLayout(btn_row)

        # Create the log bridge
        self._bridge = LogBridge()
        self._bridge.log_message.connect(self._append)

    @property
    def log_callback(self):
        """Return the callable that core/ modules use as their LogCallback."""
        return self._bridge

    def _append(self, severity: str, message: str) -> None:
        """Append a log message with severity coloring.

        Also mirrors the line to the session log file, so a crash report
        carries the run-up to the failure and not just the failure.
        """
        try:
            from app.core.crash_guard import get_logger

            level = _FILE_LOG_LEVELS.get(severity, "info")
            getattr(get_logger(), level)("%s", message)
        except Exception:  # noqa: BLE001 - logging must never break the UI
            pass

        color = SEVERITY_COLORS.get(severity, THEME["text_primary"])
        prefix = SEVERITY_PREFIXES.get(severity, severity.upper())

        # Only follow the tail when the architect is already at it.
        # Forcing the scrollbar down on every message - what this used to
        # do - yanked the view away mid-read whenever anything logged.
        scrollbar = self._log_text.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4

        self._log_text.moveCursor(QTextCursor.MoveOperation.End)
        self._log_text.setTextColor(QColor(color))
        self._log_text.append(f"[{prefix}] {message}")

        if at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def log(self, severity: str, message: str) -> None:
        """Direct log method for UI-side messages."""
        self._append(severity, message)
