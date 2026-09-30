"""The date picker has to open somewhere the architect can see it.

The calendar raised for the revision-history DATE cells is a frameless
top-level window, so nothing clips it to the app — moved past the edge of
the display it is simply cut off by the desktop.  ``_show_calendar``
checked the bottom edge and nothing else: no left, no right, and it
clamped the top to zero, which is wrong the moment a second monitor puts
the desktop origin somewhere other than this screen's top-left.

The DATE column sits near the right of the side panel and the side panel
is the right-hand edge of the window, so on a maximised app this was the
normal case rather than an edge case: the picker opened half off the
screen with most of the month invisible.
"""

import os
import sys
import unittest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication, QCalendarWidget  # noqa: E402

_app = QApplication.instance() or QApplication([])

from app.ui.excel_side_panel import ExcelStyleSidePanel  # noqa: E402

CELL_HEIGHT = 20


class CalendarPlacementTests(unittest.TestCase):

    def setUp(self):
        self.popup = QCalendarWidget(None)
        self.popup.adjustSize()
        self.area = QGuiApplication.primaryScreen().availableGeometry()

    def tearDown(self):
        self.popup.deleteLater()

    def _place(self, x, y):
        return ExcelStyleSidePanel._popup_position(self.popup, x, y, CELL_HEIGHT)

    def _assert_on_screen(self, x, y, note=""):
        w, h = self.popup.width(), self.popup.height()
        self.assertGreaterEqual(x, self.area.left(), f"off the left {note}")
        self.assertGreaterEqual(y, self.area.top(), f"off the top {note}")
        self.assertLessEqual(x + w - 1, self.area.right(),
                             f"off the right {note}")
        self.assertLessEqual(y + h - 1, self.area.bottom(),
                             f"off the bottom {note}")

    # ── The reported case ─────────────────────────────────────────────

    def test_a_cell_at_the_bottom_right_stays_on_screen(self):
        """What the screenshot showed: most of the month cut off."""
        x, y = self._place(self.area.right() - 40, self.area.bottom() - 30)
        self._assert_on_screen(x, y, "(bottom-right cell)")

    def test_a_cell_near_the_right_edge_stays_on_screen(self):
        x, y = self._place(self.area.right() - 40, self.area.center().y())
        self._assert_on_screen(x, y, "(right-edge cell)")

    # ── Every other edge ──────────────────────────────────────────────

    def test_every_edge_and_corner_is_handled(self):
        margin = 30
        points = {
            "centre": (self.area.center().x(), self.area.center().y()),
            "top-left": (self.area.left() + margin, self.area.top() + margin),
            "top-right": (self.area.right() - margin, self.area.top() + margin),
            "bottom-left": (self.area.left() + margin,
                            self.area.bottom() - margin),
            "bottom-right": (self.area.right() - margin,
                             self.area.bottom() - margin),
        }
        for label, (cx, cy) in points.items():
            with self.subTest(corner=label):
                x, y = self._place(cx, cy)
                self._assert_on_screen(x, y, f"({label})")

    def test_a_cell_scrolled_off_the_screen_still_gives_a_visible_popup(self):
        """Excel can report a cell outside the visible desktop."""
        for label, (cx, cy) in {
            "far right": (self.area.right() + 500, self.area.center().y()),
            "far left": (self.area.left() - 500, self.area.center().y()),
            "far below": (self.area.center().x(), self.area.bottom() + 500),
            "far above": (self.area.center().x(), self.area.top() - 500),
        }.items():
            with self.subTest(position=label):
                x, y = self._place(cx, cy)
                self._assert_on_screen(x, y, f"({label})")

    # ── Behaviour, not just containment ───────────────────────────────

    def test_it_opens_below_the_cell_when_there_is_room(self):
        cx, cy = self.area.center().x(), self.area.top() + 50
        x, y = self._place(cx, cy)
        self.assertEqual(y, cy + CELL_HEIGHT,
                         "should hang directly under the cell")
        self.assertEqual(x, cx, "should line up with the cell's left edge")

    def test_it_flips_above_the_cell_when_there_is_no_room_below(self):
        cy = self.area.bottom() - 30
        x, y = self._place(self.area.center().x(), cy)
        self.assertLess(y, cy, "should have flipped above the cell")
        self._assert_on_screen(x, y)

    def test_a_popup_taller_than_the_screen_is_not_pushed_off_the_top(self):
        self.popup.setFixedHeight(self.area.height() + 200)
        x, y = self._place(self.area.center().x(), self.area.bottom() - 10)
        self.assertGreaterEqual(y, self.area.top())

    def test_placement_is_stable_when_called_twice(self):
        point = (self.area.right() - 40, self.area.bottom() - 30)
        self.assertEqual(self._place(*point), self._place(*point))


class ScreenSelectionTests(unittest.TestCase):
    """The popup is clamped to the screen the cell is on."""

    def test_the_result_lands_on_some_real_screen(self):
        popup = QCalendarWidget(None)
        popup.adjustSize()
        try:
            for screen in QGuiApplication.screens():
                area = screen.availableGeometry()
                x, y = ExcelStyleSidePanel._popup_position(
                    popup, area.right() - 20, area.bottom() - 20, CELL_HEIGHT)
                self.assertTrue(
                    any(s.availableGeometry().contains(x, y)
                        for s in QGuiApplication.screens()),
                    f"popup at ({x}, {y}) is on no screen at all",
                )
        finally:
            popup.deleteLater()


if __name__ == "__main__":
    unittest.main()
