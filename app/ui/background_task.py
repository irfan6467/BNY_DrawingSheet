"""
Run the slow parts off the UI thread.

Three things in this app block for long enough that Windows paints the
title bar grey and offers to close the program:

* the ODA conversion of a .dwg, given two minutes before it gives up;
* the startup health check, which runs ODA once to see whether it works;
* rendering CAD components, which is matplotlib at 300 DPI per component.

All three ran on the thread that draws the window.  That is the whole of
the "not responding" report - the work was progressing fine, the app just
had no way to say so.

The worker below runs one callable on a QThread and reports back through
signals.  It is deliberately small: no thread pool, no cancellation of
work already inside a subprocess, because the one thing it must never do
is add a way for a background thread to touch a widget.  The callable
gets no Qt objects and returns plain data; everything else happens in the
slot that receives ``finished``.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import QApplication, QProgressDialog

from app.core.crash_guard import get_logger

# Every task that has not finished yet.  A QThread destroyed while it is
# still running is undefined behaviour - Qt says so out loud ("QThread:
# Destroyed while thread is still running") and can abort the process on
# the spot.  Closing the window mid-import has to wait for the worker, so
# the live ones are tracked here rather than being left to the garbage
# collector to find.
_ACTIVE: list = []
_HOOKED = False


def wait_for_all(timeout_ms: int = 10_000) -> None:
    """Let any running task finish.  Called before the app shuts down."""
    for task in list(_ACTIVE):
        try:
            task.wait(timeout_ms)
        except RuntimeError:
            # Already torn down on the C++ side; nothing left to wait for.
            pass
    _ACTIVE.clear()


def _install_quit_hook() -> None:
    """Wait for workers on quit however the app got there.

    Registered from the first task rather than relying on each caller to
    remember: the one case that matters is the unusual shutdown - a
    logout, a Close All - and that is exactly the case a caller forgets.
    """
    global _HOOKED
    if _HOOKED:
        return
    application = QApplication.instance()
    if application is None:
        return
    application.aboutToQuit.connect(lambda: wait_for_all(5000))
    _HOOKED = True


class _Worker(QObject):
    """Runs one callable and emits its result."""

    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, work: Callable[..., Any]):
        super().__init__()
        self._work = work

    def run(self) -> None:
        try:
            result = self._work(self.progress.emit)
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            get_logger().exception("Background task failed")
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)


class BackgroundTask(QObject):
    """A single piece of slow work with a modal progress dialog.

    ``work`` is called on a worker thread with one argument: a ``report``
    callable it may use to push a status line back to the dialog.  It must
    not touch any widget.

    ``on_done``/``on_error`` run on the UI thread once the work ends.
    """

    def __init__(
        self,
        parent,
        title: str,
        message: str,
        work: Callable[[Callable[[str], None]], Any],
        on_done: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(parent)
        self._on_done = on_done
        self._on_error = on_error

        self._dialog = QProgressDialog(message, "", 0, 0, parent)
        self._dialog.setWindowTitle(title)
        self._dialog.setWindowModality(Qt.WindowModality.WindowModal)
        # There is nothing safe to cancel mid-conversion, so the button is
        # removed rather than offered and ignored.
        self._dialog.setCancelButton(None)
        self._dialog.setMinimumDuration(400)
        self._dialog.setAutoClose(False)
        self._dialog.setAutoReset(False)

        self._thread = QThread(self)
        self._worker = _Worker(work)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._dialog.setLabelText)
        self._worker.finished.connect(self._succeeded)
        self._worker.failed.connect(self._errored)

    def start(self) -> None:
        _install_quit_hook()
        _ACTIVE.append(self)
        self._thread.start()

    def wait(self, timeout_ms: int = 10_000) -> None:
        """Block until the worker is done.  Shutdown only."""
        if self._thread.isRunning():
            self._thread.quit()
            if not self._thread.wait(timeout_ms):
                get_logger().warning(
                    "A background task did not finish in time; "
                    "shutting down anyway"
                )
        try:
            self._dialog.reset()
            self._dialog.close()
        except RuntimeError:
            pass

    # ── Completion, always on the UI thread ───────────────────────────

    def _teardown(self) -> None:
        if self in _ACTIVE:
            _ACTIVE.remove(self)
        self._dialog.reset()
        self._dialog.close()
        self._thread.quit()
        self._thread.wait(5000)

    def _succeeded(self, result) -> None:
        self._teardown()
        if self._on_done is not None:
            self._on_done(result)
        self.deleteLater()

    def _errored(self, message: str) -> None:
        self._teardown()
        if self._on_error is not None:
            self._on_error(message)
        self.deleteLater()


def run_with_wait_cursor(func, *args, **kwargs):
    """For work too short to justify a thread but long enough to notice."""
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        return func(*args, **kwargs)
    finally:
        QApplication.restoreOverrideCursor()
