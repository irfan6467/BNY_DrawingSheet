"""Canvas coordinates, which two separate bugs were quietly corrupting.

1. ``get_page_rect_pts`` read the frame back through
   ``sceneTransform().mapRect()``, which is the axis-aligned *bounding
   box* of a turned rect.  ``page_rect`` means the frame before rotation -
   the compositor turns it separately - so every drag of a rotated view
   wrote a larger rect into the model, and the next rebuild turned that.
   The view grew a little more each time it was touched.

2. ``_scale_factor`` started at 1.0 while ``_dpi`` started at 200, so
   ``pts_to_scene`` and ``scene_to_pts`` were a factor of 2.78 apart
   until a template loaded.  With the template PDF missing - which is how
   the app behaves on a machine where the resources did not ship - every
   drop landed nearly three times too far across the sheet.
"""

import math
import os
import sys
import unittest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

# QApplication, not QGuiApplication: QGraphicsScene lives in QtWidgets and
# reaches for the widget-level singletons, which a QGuiApplication never
# creates - the process segfaults rather than raising.
_app = QApplication.instance() or QApplication([])

from app.ui.canvas_scene import CanvasScene  # noqa: E402


class CoordinateTests(unittest.TestCase):

    def setUp(self):
        self.scene = CanvasScene()

    def test_points_and_scene_units_are_inverses_before_a_template_loads(self):
        for x, y in ((0.0, 0.0), (400.0, 200.0), (1387.0, 1191.0)):
            point = self.scene.pts_to_scene(x, y)
            back = self.scene.scene_to_pts(point.x(), point.y())
            self.assertAlmostEqual(back[0], x, places=6)
            self.assertAlmostEqual(back[1], y, places=6)

    def test_scale_factor_agrees_with_dpi_from_the_start(self):
        self.assertAlmostEqual(
            self.scene._scale_factor, 72.0 / self.scene._dpi, places=9
        )

    def test_a_rect_round_trips_through_the_scene(self):
        rect = self.scene.rect_pts_to_scene(100.0, 50.0, 400.0, 200.0)
        back = self.scene.rect_scene_to_pts(rect)
        for got, want in zip(back, (100.0, 50.0, 400.0, 200.0)):
            self.assertAlmostEqual(got, want, places=6)


class RotationTests(unittest.TestCase):

    def setUp(self):
        self.scene = CanvasScene()
        self.pixmap = QPixmap(400, 200)
        self.pixmap.fill()

    def _place(self, page_rect, rotation=0.0):
        self.scene.remove_placed_item("p")
        gfx = self.scene.add_placed_item(self.pixmap, page_rect, "p")
        if rotation:
            gfx.setRotation(rotation)
        return gfx

    def test_reading_back_a_rotated_frame_keeps_its_size(self):
        scale = self.scene._scale_factor
        for angle in (0, 15, 30, 45, 90, 137.5, 180, 315):
            with self.subTest(angle=angle):
                gfx = self._place((100.0, 50.0, 400.0, 200.0), angle)
                _, _, width, height = gfx.get_page_rect_pts(scale)
                self.assertAlmostEqual(width, 400.0, places=4)
                self.assertAlmostEqual(height, 200.0, places=4)

    def test_rotating_leaves_the_centre_where_it_was(self):
        scale = self.scene._scale_factor
        gfx = self._place((100.0, 50.0, 400.0, 200.0))
        x, y, w, h = gfx.get_page_rect_pts(scale)
        centre = (x + w / 2.0, y + h / 2.0)

        gfx.setRotation(37.0)
        x, y, w, h = gfx.get_page_rect_pts(scale)
        self.assertAlmostEqual(x + w / 2.0, centre[0], places=4)
        self.assertAlmostEqual(y + h / 2.0, centre[1], places=4)

    def test_a_turned_view_does_not_grow_over_repeated_rebuilds(self):
        """The compounding case: read back, re-place, read back again.

        This is what happens on every drag of a rotated item, and on
        every undo that rebuilds the canvas from the model.
        """
        scale = self.scene._scale_factor
        start = (100.0, 50.0, 400.0, 200.0)
        rect = start
        for _ in range(6):
            gfx = self._place(rect, 45.0)
            rect = gfx.get_page_rect_pts(scale)

        self.assertAlmostEqual(rect[2], start[2], places=3)
        self.assertAlmostEqual(rect[3], start[3], places=3)

    def test_the_bounding_box_really_is_bigger(self):
        """Guards the premise: at 45 degrees the two readings differ.

        If this ever stopped being true the test above would pass for the
        wrong reason.
        """
        scale = self.scene._scale_factor
        gfx = self._place((100.0, 50.0, 400.0, 200.0), 45.0)
        bounding = gfx.sceneTransform().mapRect(gfx.rect())
        frame = gfx.get_page_rect_pts(scale)

        # A 400x200 frame turned 45 degrees has a 424x424 bounding box:
        # wider than the frame, and twice as tall.  Writing that back was
        # what made a turned view grow on every touch.
        self.assertGreater(bounding.width() * scale, frame[2])
        self.assertGreater(bounding.height() * scale, frame[3] * 2.0)


class DropTests(unittest.TestCase):

    def test_a_drop_at_the_scene_origin_is_not_discarded(self):
        """QPointF(0, 0) is a null point, and the old check was truthiness.

        Dropping an asset on the very top-left corner of the sheet was
        silently ignored.
        """
        from PySide6.QtCore import QPointF

        scene = CanvasScene()
        scene._pending_drop_asset_id = "asset_1"
        scene._pending_drop_scene_pos = QPointF(0.0, 0.0)

        info = scene.get_pending_drop_info()
        self.assertIsNotNone(info)
        self.assertEqual(info[0], "asset_1")

    def test_pending_drop_is_consumed_once(self):
        from PySide6.QtCore import QPointF

        scene = CanvasScene()
        scene._pending_drop_asset_id = "asset_1"
        scene._pending_drop_scene_pos = QPointF(10.0, 10.0)
        self.assertIsNotNone(scene.get_pending_drop_info())
        self.assertIsNone(scene.get_pending_drop_info())


class PreviewScaleTests(unittest.TestCase):
    """A preview shown smaller than its asset must say so.

    Crop boxes drawn on it are stored in the asset's pixel space, so the
    scale is the only thing keeping the cap on pixmap size from silently
    cropping the wrong part of the drawing.
    """

    def test_scale_is_one_when_shown_full_size(self):
        scene = CanvasScene()
        pixmap = QPixmap(800, 600)
        pixmap.fill()
        scene.show_preview_image(pixmap, source_size=(800, 600))
        self.assertAlmostEqual(scene.preview_scale(), 1.0, places=6)

    def test_scale_reports_the_reduction(self):
        scene = CanvasScene()
        pixmap = QPixmap(1000, 500)
        pixmap.fill()
        scene.show_preview_image(pixmap, source_size=(4000, 2000))
        self.assertAlmostEqual(scene.preview_scale(), 4.0, places=6)

    def test_hiding_the_preview_resets_the_scale(self):
        scene = CanvasScene()
        pixmap = QPixmap(1000, 500)
        pixmap.fill()
        scene.show_preview_image(pixmap, source_size=(4000, 2000))
        scene.hide_preview_image()
        self.assertAlmostEqual(scene.preview_scale(), 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
