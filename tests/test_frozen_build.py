"""Behaviour that only differs once the app is packaged as an exe.

A PyInstaller windowed build has no console: ``sys.stdout`` and
``sys.stderr`` are both None.  Anything that reaches for them raises
AttributeError, and an AttributeError raised out of a Qt slot ends the
process without a dialog or a traceback — so this class of bug is
invisible when running from source and fatal in the only build the
architect actually uses.
"""

import io
import os
import sys
import unittest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

_app = QApplication.instance() or QApplication([])

from app.core import crash_guard  # noqa: E402


class _NoConsole:
    """The stdio a windowed build actually has: none."""

    def __enter__(self):
        self._out, self._err = sys.stdout, sys.stderr
        sys.stdout = None
        sys.stderr = None
        return self

    def __exit__(self, *exc):
        sys.stdout, sys.stderr = self._out, self._err
        return False


class SidePanelLoggingTests(unittest.TestCase):
    """The side panel's log fallback used to be a bare print()."""

    def setUp(self):
        from app.ui.excel_side_panel import ExcelStyleSidePanel

        self.panel = ExcelStyleSidePanel()
        # A parent chain with no _log on it, which is what forces the
        # fallback: during startup and teardown the panel is not always
        # hanging off the main window.
        self.orphan = QWidget()
        self.panel.setParent(self.orphan)

    def tearDown(self):
        self.panel.setParent(None)
        self.panel.deleteLater()
        self.orphan.deleteLater()

    def test_logging_without_a_console_does_not_raise(self):
        with _NoConsole():
            self.panel._log("error", "packaged build, no stdout")
            self.panel._log("info", "still fine")

    def test_the_message_is_not_simply_lost(self):
        marker = "frozen-build-log-marker"
        with _NoConsole():
            self.panel._log("info", marker)
        with open(crash_guard.log_path(), encoding="utf-8",
                  errors="replace") as handle:
            self.assertIn(marker, handle.read()[-4000:])

    def test_a_parent_whose_log_raises_falls_through(self):
        class _Broken(QWidget):
            def _log(self, *_args):
                raise RuntimeError("panel gone")

        broken = _Broken()
        self.panel.setParent(broken)
        try:
            with _NoConsole():
                self.panel._log("error", "parent log is broken")
        finally:
            self.panel.setParent(self.orphan)
            broken.deleteLater()

    def test_a_working_parent_log_is_preferred(self):
        received = []

        class _Host(QWidget):
            def _log(self, level, message):
                received.append((level, message))

        host = _Host()
        self.panel.setParent(host)
        try:
            self.panel._log("warning", "to the panel")
        finally:
            self.panel.setParent(self.orphan)
            host.deleteLater()

        self.assertEqual(received, [("warning", "to the panel")])


class CrashGuardTests(unittest.TestCase):
    """The safety net has to go up in a build with no stderr to attach to."""

    def test_the_logger_builds_without_a_stderr(self):
        crash_guard._logger = None
        try:
            with _NoConsole():
                logger = crash_guard.get_logger()
                logger.info("built without a console")
            self.assertTrue(logger.handlers)
        finally:
            crash_guard._logger = None
            crash_guard.get_logger()

    def test_reporting_an_error_without_a_console_does_not_raise(self):
        seen = []
        crash_guard.set_report_hook(lambda s, d: seen.append(s))
        try:
            with _NoConsole():
                try:
                    raise ValueError("no console")
                except ValueError:
                    crash_guard._handle(*sys.exc_info(), source="test")
        finally:
            crash_guard.set_report_hook(None)
        self.assertTrue(any("no console" in s for s in seen))

    def test_a_failing_report_hook_does_not_escape(self):
        def broken(_summary, _detail):
            raise RuntimeError("the reporter itself failed")

        crash_guard.set_report_hook(broken)
        try:
            try:
                raise ValueError("original")
            except ValueError:
                crash_guard._handle(*sys.exc_info(), source="test")
        finally:
            crash_guard.set_report_hook(None)


class ResourcePathTests(unittest.TestCase):
    """Every bundled file must be reachable through resource_path."""

    def test_dev_mode_resolves_to_the_project_root(self):
        from app.utils.paths import resource_path

        for rel in (
            os.path.join("app", "ui", "theme.qss.template"),
            os.path.join("app", "resources", "templates",
                         "bny_standard_a1.json"),
            os.path.join("app", "resources", "templates",
                         "bny_standard_a1.pdf"),
            os.path.join("app", "resources", "templates",
                         "bny_sidepanel.xlsx"),
            os.path.join("app", "resources", "fonts", "DejaVuSans.ttf"),
            os.path.join("app", "resources", "icons", "import.svg"),
        ):
            with self.subTest(resource=rel):
                self.assertTrue(os.path.isfile(resource_path(rel)),
                                f"{rel} would be missing from the exe")

    def test_a_frozen_bundle_resolves_against_meipass(self):
        from app.utils import paths

        had = hasattr(sys, "_MEIPASS")
        previous = getattr(sys, "_MEIPASS", None)
        sys._MEIPASS = os.path.join("X:", "bundle")
        try:
            resolved = paths.resource_path(os.path.join("app", "x.txt"))
            self.assertTrue(resolved.startswith(sys._MEIPASS))
        finally:
            if had:
                sys._MEIPASS = previous
            else:
                del sys._MEIPASS

    def test_the_theme_loads_from_its_bundled_template(self):
        from app.ui.theme import THEME, load_theme

        qss = load_theme()
        self.assertGreater(len(qss), 200)
        # Every placeholder resolved: str.format raises on an unknown key,
        # so reaching here proves that much — this checks the values
        # actually landed rather than the file being a stub.
        self.assertIn(THEME["accent_primary"], qss)
        self.assertIn(THEME["bg_app"], qss)

    def test_the_default_template_loads(self):
        from app.core.template_registry import load_default_template

        template = load_default_template()
        self.assertIsNotNone(template, "the bundled sheet template is missing")
        self.assertTrue(os.path.isfile(template.base_pdf_path))


if __name__ == "__main__":
    unittest.main()
