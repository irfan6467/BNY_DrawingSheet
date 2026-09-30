"""Render quality for CAD views — the clarity and pixelation fixes.

Four separate defects met here:

1. ``MatplotlibBackend.finalize()`` ends by calling ``plt.figaspect`` and
   ``set_size_inches``, which replaces whatever figure size the caller
   chose with one built from matplotlib's 6.4 inch default.  Every render
   in the app was silently shrunk: a component asked for at 12 inches and
   300 DPI came out 1920 px on its long side instead of 3600, and a
   square one 1440.  The detail was never drawn, so nothing downstream
   could recover it.

2. Because figaspect picks a different size per proportion (6.4 wide for
   landscape, 4.8 for square, 4.0 for tall), and stroke weights are
   absolute millimetres, line weight depended on the shape of the view.
   Measured at the same size, strokes ran from 1.0 to 2.0 parts per
   thousand of the drawing - twice the weight for no reason.

3. Extracted components were raster only.  The whole-drawing import made
   a vector PDF and set ``vector_source_path``; component extraction, the
   workflow on the toolbar, did not - so views pulled out of a DWG
   printed as bitmaps.

4. With every item carrying vector line art, nothing is drawn on the
   ReportLab overlay, and ReportLab emits a zero-page PDF - which the
   compositor then indexed into.
"""

import os
import shutil
import sys
import tempfile
import unittest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import ezdxf  # noqa: E402
from ezdxf import bbox as ezdxf_bbox  # noqa: E402
from PIL import Image  # noqa: E402

import app.core.dwg_components as dc  # noqa: E402


def _quiet(*_args) -> None:
    pass


def _drawing(width: float, height: float, directory: str):
    """A rectangle with evenly spaced verticals, for measuring strokes."""
    path = os.path.join(directory, f"d_{int(width)}x{int(height)}.dxf")
    doc = ezdxf.new(dxfversion="R2010")
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (width, 0), (width, height), (0, height), (0, 0)])
    step = max(20, int(width / 10))
    for x in range(step, int(width), step):
        msp.add_line((x, height * 0.15), (x, height * 0.85))
    doc.saveas(path)

    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    cache = ezdxf_bbox.Cache()
    component = max(
        dc.detect_components(msp, cache, log=_quiet).components,
        key=lambda c: c.entity_count,
    )
    return doc, msp, cache, component


def _stroke_ppt(img: Image.Image) -> float:
    """Mean stroke width, in parts per thousand of the long side.

    Normalised by the long side rather than the width: the sheet fits a
    view by its longest dimension, and measuring against width alone
    makes a tall view's strokes look thicker for no real reason.
    """
    grey = img.convert("L")
    pixels = grey.load()
    w, h = grey.size
    row = h // 2
    runs, start = [], None
    for x in range(w):
        dark = pixels[x, row] < 128
        if dark and start is None:
            start = x
        elif not dark and start is not None:
            runs.append(x - start)
            start = None
    longest = max(w, h)
    inner = [r for r in runs if r < longest * 0.03]
    if not inner:
        return 0.0
    return sum(inner) / len(inner) / longest * 1000.0


class FigureSizeTests(unittest.TestCase):
    """The figure the caller asks for is the figure that gets rendered."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_backend_does_not_resize_the_figure(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from ezdxf.addons.drawing import Frontend, RenderContext

        doc, msp, _cache, component = _drawing(400, 300, self.tmp)

        fig = plt.figure(figsize=(12.0, 9.0))
        try:
            ax = fig.add_axes([0, 0, 1, 1])
            backend = dc.make_backend(ax)
            Frontend(
                ctx=RenderContext(doc), out=backend,
                config=dc.build_render_config(white_background=True),
            ).draw_layout(msp, finalize=True)

            width, height = fig.get_size_inches()
            self.assertAlmostEqual(width, 12.0, places=3)
            self.assertAlmostEqual(height, 9.0, places=3)
        finally:
            plt.close(fig)

    def test_the_stock_backend_still_resizes(self):
        """Guards the premise — if ezdxf ever stops doing this, say so."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from ezdxf.addons.drawing import Frontend, RenderContext
        from ezdxf.addons.drawing import matplotlib as draw_mpl

        doc, msp, _cache, _component = _drawing(400, 300, self.tmp)

        fig = plt.figure(figsize=(12.0, 9.0))
        try:
            ax = fig.add_axes([0, 0, 1, 1])
            Frontend(
                ctx=RenderContext(doc),
                out=draw_mpl.MatplotlibBackend(ax),
                config=dc.build_render_config(white_background=True),
            ).draw_layout(msp, finalize=True)
            self.assertLess(
                max(fig.get_size_inches()), 12.0,
                "ezdxf no longer resizes the figure — make_backend's "
                "adjust_figure=False may no longer be needed",
            )
        finally:
            plt.close(fig)

    def test_render_reaches_the_resolution_it_is_asked_for(self):
        doc, msp, cache, component = _drawing(400, 300, self.tmp)
        img = dc.render_component(doc, msp, component, target_dpi=300,
                                  cache=cache, log=_quiet)
        # 12 inches at 300 DPI, within the pixel budget for 4:3.
        self.assertGreaterEqual(max(img.size), 3500)

    def test_a_square_view_is_not_the_worst_affected_any_more(self):
        """figaspect gave a square view only 4.8 inches — 1440 px."""
        doc, msp, cache, component = _drawing(400, 400, self.tmp)
        img = dc.render_component(doc, msp, component, target_dpi=300,
                                  cache=cache, log=_quiet)
        self.assertGreaterEqual(max(img.size), 3500)

    def test_the_pixel_budget_still_bounds_memory(self):
        doc, msp, cache, component = _drawing(400, 400, self.tmp)
        img = dc.render_component(doc, msp, component, target_dpi=1200,
                                  cache=cache, log=_quiet)
        self.assertLessEqual(img.width * img.height, dc.MAX_RENDER_PIXELS * 1.05)

    def test_a_preview_dpi_is_not_inflated(self):
        doc, msp, cache, component = _drawing(400, 300, self.tmp)
        img = dc.render_component(doc, msp, component, target_dpi=72,
                                  cache=cache, log=_quiet)
        self.assertLess(max(img.size), 1200)


class StrokeWeightTests(unittest.TestCase):
    """Line weight must not depend on the shape of the view."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_weight_is_consistent_across_proportions(self):
        widths = {}
        for label, (w, h) in (("landscape", (400, 300)),
                              ("square", (400, 400)),
                              ("wide", (600, 300)),
                              ("tall", (300, 600))):
            doc, msp, cache, component = _drawing(w, h, self.tmp)
            img = dc.render_component(
                doc, msp, component, target_dpi=dc.COMPONENT_RENDER_DPI,
                cache=cache, log=_quiet,
            )
            widths[label] = _stroke_ppt(img)

        measured = [v for v in widths.values() if v > 0]
        self.assertEqual(len(measured), 4, f"no strokes found: {widths}")
        spread = max(measured) / min(measured)
        self.assertLess(
            spread, 1.35,
            f"line weight still depends on the view's shape: {widths}",
        )

    def test_scaling_the_figure_keeps_the_relative_weight(self):
        """The whole point of CAD_TUNED_FIGURE_INCHES."""
        doc, msp, cache, component = _drawing(400, 300, self.tmp)
        original = dc.COMPONENT_MAX_INCHES
        try:
            dc.COMPONENT_MAX_INCHES = 8.0
            small = _stroke_ppt(dc.render_component(
                doc, msp, component, target_dpi=300, cache=cache, log=_quiet))
            dc.COMPONENT_MAX_INCHES = 16.0
            large = _stroke_ppt(dc.render_component(
                doc, msp, component, target_dpi=300, cache=cache, log=_quiet))
        finally:
            dc.COMPONENT_MAX_INCHES = original

        self.assertGreater(small, 0)
        self.assertGreater(large, 0)
        self.assertLess(max(small, large) / min(small, large), 1.35,
                        f"weight drifted with figure size: {small} vs {large}")


class VectorOutputTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_component_produces_a_vector_pdf(self):
        doc, msp, cache, component = _drawing(400, 300, self.tmp)
        out = os.path.join(self.tmp, "c.pdf")
        result = dc.render_component_vector(doc, msp, component, out,
                                            cache=cache, log=_quiet)
        self.assertEqual(result, out)
        self.assertTrue(os.path.isfile(out))
        with open(out, "rb") as handle:
            self.assertTrue(handle.read(5).startswith(b"%PDF"))

    def test_the_vector_carries_paths_not_a_bitmap(self):
        import pymupdf

        doc, msp, cache, component = _drawing(400, 300, self.tmp)
        out = os.path.join(self.tmp, "c.pdf")
        dc.render_component_vector(doc, msp, component, out,
                                   cache=cache, log=_quiet)
        page = pymupdf.open(out)[0]
        self.assertEqual(len(page.get_images(full=True)), 0,
                         "the vector PDF embedded a bitmap")
        self.assertGreater(len(page.get_drawings()), 5)

    def test_vector_and_raster_agree_on_aspect(self):
        """Or the compositor fits it with a white margin down one side."""
        import pymupdf

        for w, h in ((400, 300), (400, 400), (300, 600)):
            with self.subTest(shape=(w, h)):
                doc, msp, cache, component = _drawing(w, h, self.tmp)
                img = dc.render_component(
                    doc, msp, component, target_dpi=200, cache=cache, log=_quiet)
                out = os.path.join(self.tmp, f"c_{w}x{h}.pdf")
                dc.render_component_vector(doc, msp, component, out,
                                           cache=cache, log=_quiet)
                box = pymupdf.open(out)[0].rect
                raster_aspect = img.width / img.height
                vector_aspect = box.width / box.height
                self.assertAlmostEqual(raster_aspect, vector_aspect, places=2)

    def test_a_zero_extent_component_is_declined_not_fatal(self):
        doc, msp, cache, component = _drawing(400, 300, self.tmp)
        component.bbox = (10.0, 10.0, 10.0, 10.0)
        out = os.path.join(self.tmp, "zero.pdf")
        self.assertIsNone(dc.render_component_vector(
            doc, msp, component, out, cache=cache, log=_quiet))


class LayoutPickerTests(unittest.TestCase):
    """AutoCAD makes Layout1 in every drawing, used or not."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_empty_paper_space_falls_back_to_modelspace(self):
        from app.core.cad_import import _pick_layout

        path = os.path.join(self.tmp, "m.dxf")
        doc = ezdxf.new(dxfversion="R2010")
        doc.modelspace().add_line((0, 0), (100, 100))
        doc.saveas(path)
        doc = ezdxf.readfile(path)

        layout = _pick_layout(doc, log=_quiet)
        self.assertEqual(len(list(layout)), 1,
                         "picked an empty paper space over the drawing")

    def test_a_populated_paper_space_is_preferred(self):
        from app.core.cad_import import _pick_layout

        path = os.path.join(self.tmp, "p.dxf")
        doc = ezdxf.new(dxfversion="R2010")
        doc.modelspace().add_line((0, 0), (100, 100))
        doc.layout("Layout1").add_circle((5, 5), 3)
        doc.saveas(path)
        doc = ezdxf.readfile(path)

        layout = _pick_layout(doc, log=_quiet)
        kinds = {e.dxftype() for e in layout}
        self.assertIn("CIRCLE", kinds)

    def test_a_paper_space_holding_only_a_viewport_does_not_count(self):
        from app.core.cad_import import _layout_has_drawable

        path = os.path.join(self.tmp, "v.dxf")
        doc = ezdxf.new(dxfversion="R2010")
        doc.modelspace().add_line((0, 0), (100, 100))
        doc.saveas(path)
        doc = ezdxf.readfile(path)

        self.assertFalse(_layout_has_drawable(doc.layout("Layout1")))
        self.assertTrue(_layout_has_drawable(doc.modelspace()))


if __name__ == "__main__":
    unittest.main()
