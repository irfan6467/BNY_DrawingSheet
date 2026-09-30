"""
Technical Drawing Sheet Automation — Application Entry Point

Launches the PySide6 QApplication, applies the design-system theme,
and shows the main window.

The crash guard goes up before anything else.  PySide6 treats an
exception that escapes a slot as fatal, so without it any error in a
button handler ends the process outright — no message, no traceback, and
whatever was on the sheet gone with it.  See app/core/crash_guard.py.
"""

import sys


def main():
    # Before the first import that could fail, and before any widget
    # exists, so a failure during startup is still reported rather than
    # closing a window that never appeared.
    from app.core import crash_guard

    crash_guard.install()
    log = crash_guard.get_logger()

    from PySide6.QtWidgets import QApplication, QMessageBox

    from app.ui.image_bridge import configure_pillow_limits
    from app.ui.theme import load_theme

    configure_pillow_limits()

    app = QApplication(sys.argv)
    app.setApplicationName("Technical Drawing Sheet Automation")
    app.setOrganizationName("Black and Yellow Design Studio")

    # Apply the design-system theme globally — every widget created after
    # this inherits the stylesheet automatically.
    try:
        app.setStyleSheet(load_theme())
    except Exception:  # noqa: BLE001 - an unstyled app still works
        log.exception("Could not load the theme; using Qt's default styling")

    try:
        from app.ui.main_window import MainWindow

        window = MainWindow()
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        log.exception("The main window could not be built")
        QMessageBox.critical(
            None,
            "Could Not Start",
            "The application could not start:\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            f"Details have been written to:\n{crash_guard.log_path()}",
        )
        return 1

    # closeEvent covers the ordinary quit, but not every way the process
    # can end - a logout, a taskbar Close All, QApplication.quit() from
    # anywhere.  Those paths used to leave a headless EXCEL.EXE holding
    # the panel workbook open, and a worker thread being destroyed while
    # it was still running.
    def _shut_down():
        from app.ui import background_task

        background_task.wait_for_all(timeout_ms=5000)
        try:
            window._excel_panel.shutdown()
        except Exception:  # noqa: BLE001 - nothing may raise on the way out
            log.exception("The side panel did not shut down cleanly")

    app.aboutToQuit.connect(_shut_down)

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
