"""The PIL-to-Qt conversion, which is where the distorted images came from.

The bug these cover: ``QImage(buffer, w, h, format)`` pads every scanline
out to a four-byte boundary.  A PIL RGB buffer is packed tight, so for any
width that is not a multiple of four Qt read one to three bytes too many
per row - the picture sheared progressively down the frame, went grey
where it ran into the next row's data, and read past the end of the
allocation on the last row.
"""

import os
import sys
import unittest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from app.ui.image_bridge import (  # noqa: E402
    MAX_PIXMAP_EDGE, PixmapCache, normalise, pil_to_qimage, pil_to_qpixmap,
)

_app = QGuiApplication.instance() or QGuiApplication([])


def _checkerboard(width: int, height: int, mode: str = "RGB") -> Image.Image:
    """An image whose every pixel is a function of its coordinates.

    Any stride error shows up as a pixel that does not match the formula,
    which a flat colour or a simple gradient would hide.
    """
    img = Image.new(mode, (width, height))
    for y in range(height):
        for x in range(width):
            value = ((x * 7) % 256, (y * 11) % 256, ((x + y) * 13) % 256)
            img.putpixel((x, y), value + (255,) if mode == "RGBA" else value)
    return img


class StrideTests(unittest.TestCase):
    """Every width, including the three Qt would pad."""

    def test_rgb_survives_every_row_alignment(self):
        for width in (64, 65, 66, 67, 101, 1003):
            with self.subTest(width=width):
                source = _checkerboard(width, 13)
                qimg = pil_to_qimage(source)

                self.assertEqual(qimg.width(), width)
                self.assertEqual(qimg.height(), 13)

                for y in range(13):
                    for x in range(width):
                        expected = source.getpixel((x, y))
                        colour = qimg.pixelColor(x, y)
                        self.assertEqual(
                            (colour.red(), colour.green(), colour.blue()),
                            expected,
                            f"pixel ({x}, {y}) drifted at width {width}",
                        )

    def test_rgba_survives_every_row_alignment(self):
        for width in (33, 34, 35, 36):
            with self.subTest(width=width):
                source = _checkerboard(width, 9, mode="RGBA")
                qimg = pil_to_qimage(source)
                for y in range(9):
                    for x in range(width):
                        r, g, b, _ = source.getpixel((x, y))
                        colour = qimg.pixelColor(x, y)
                        self.assertEqual(
                            (colour.red(), colour.green(), colour.blue()),
                            (r, g, b),
                        )

    def test_qimage_owns_its_pixels(self):
        """The buffer must not be a view on a freed Python object.

        The staging tray built its QImage on a temporary ``bytes`` and
        never copied it, so the pixels were released the moment the
        function returned - garbage on screen, and an access violation
        once the allocator reused the page.
        """
        import gc

        source = _checkerboard(103, 7)
        qimg = pil_to_qimage(source)
        del source
        gc.collect()

        colour = qimg.pixelColor(102, 6)
        self.assertEqual(
            (colour.red(), colour.green(), colour.blue()),
            ((102 * 7) % 256, (6 * 11) % 256, ((102 + 6) * 13) % 256),
        )


class ModeTests(unittest.TestCase):
    """Modes with no direct Qt equivalent have to be converted first."""

    def test_palette_and_greyscale_convert(self):
        for mode in ("P", "L", "1", "CMYK", "I;16"):
            with self.subTest(mode=mode):
                source = Image.new(mode, (37, 11))
                pixmap = pil_to_qpixmap(source)
                self.assertFalse(pixmap.isNull(), f"{mode} produced nothing")
                self.assertEqual(pixmap.width(), 37)

    def test_palette_with_transparency_keeps_its_alpha(self):
        source = Image.new("P", (8, 8))
        source.info["transparency"] = 0
        self.assertEqual(normalise(source).mode, "RGBA")

    def test_none_is_not_a_crash(self):
        self.assertTrue(pil_to_qpixmap(None).isNull())
        self.assertTrue(pil_to_qimage(None).isNull())


class SizeCapTests(unittest.TestCase):
    """A 300 DPI CAD render must not reach the GPU at full size."""

    def test_large_image_is_capped(self):
        source = Image.new("RGB", (MAX_PIXMAP_EDGE * 2, 100))
        pixmap = pil_to_qpixmap(source)
        self.assertLessEqual(max(pixmap.width(), pixmap.height()),
                             MAX_PIXMAP_EDGE)

    def test_cap_keeps_the_aspect_ratio(self):
        source = Image.new("RGB", (8000, 2000))
        pixmap = pil_to_qpixmap(source)
        self.assertAlmostEqual(
            pixmap.width() / pixmap.height(), 4.0, places=2
        )

    def test_small_image_is_left_alone(self):
        source = Image.new("RGB", (120, 80))
        pixmap = pil_to_qpixmap(source)
        self.assertEqual((pixmap.width(), pixmap.height()), (120, 80))


class PixmapCacheTests(unittest.TestCase):

    def test_hit_and_miss(self):
        cache = PixmapCache(max_entries=3)
        pixmap = pil_to_qpixmap(Image.new("RGB", (4, 4)))
        self.assertIsNone(cache.get(("a", 1)))
        cache.put(("a", 1), pixmap)
        self.assertIsNotNone(cache.get(("a", 1)))

    def test_evicts_the_least_recently_used(self):
        cache = PixmapCache(max_entries=2)
        pixmap = pil_to_qpixmap(Image.new("RGB", (4, 4)))
        cache.put(("a", 1), pixmap)
        cache.put(("b", 1), pixmap)
        cache.get(("a", 1))            # "a" is now the more recent
        cache.put(("c", 1), pixmap)    # pushes "b" out
        self.assertIsNotNone(cache.get(("a", 1)))
        self.assertIsNone(cache.get(("b", 1)))

    def test_discarding_an_asset_drops_all_its_variants(self):
        cache = PixmapCache()
        pixmap = pil_to_qpixmap(Image.new("RGB", (4, 4)))
        cache.put(("asset_1", "full"), pixmap)
        cache.put(("asset_1", "cropped"), pixmap)
        cache.put(("asset_2", "full"), pixmap)
        cache.discard_asset("asset_1")
        self.assertIsNone(cache.get(("asset_1", "full")))
        self.assertIsNone(cache.get(("asset_1", "cropped")))
        self.assertIsNotNone(cache.get(("asset_2", "full")))


if __name__ == "__main__":
    unittest.main()
