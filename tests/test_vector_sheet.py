"""Vector line art through the compositor and the project file.

Two defects:

* With every placed item carrying vector line art — which is now the
  normal case for views extracted from a DWG — nothing is drawn on the
  ReportLab overlay, and ReportLab only emits a page when something was.
  The compositor then indexed page zero of a PDF with no pages and the
  sheet failed to generate at all.

* The vector path recorded in a saved project pointed into a session
  temp folder that is gone by the next launch, so a reopened sheet
  quietly fell back to its bitmaps and printed softer than the one saved
  the day before.
"""

import os
import shutil
import sys
import tempfile
import unittest
import zipfile

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import pymupdf  # noqa: E402
from PIL import Image  # noqa: E402
from reportlab.pdfgen import canvas as rl_canvas  # noqa: E402

from app.core.pdf_compositor import generate_technical_sheet  # noqa: E402
from app.core.project_io import load_project, save_project  # noqa: E402
from app.core.project_state import (  # noqa: E402
    Annotation, ImportedAsset, PlacedItem, ProjectState, SheetTemplate,
)


def _make_vector_pdf(path: str, width: float = 400, height: float = 300) -> str:
    c = rl_canvas.Canvas(path, pagesize=(width, height))
    c.setLineWidth(2)
    for x in range(20, int(width), 20):
        c.line(x, 20, x, height - 20)
    c.rect(10, 10, width - 20, height - 20)
    c.showPage()
    c.save()
    return path


def _template(directory: str) -> SheetTemplate:
    """A blank one-page base PDF, standing in for the firm's letterhead."""
    base = os.path.join(directory, "base.pdf")
    c = rl_canvas.Canvas(base, pagesize=(1387, 1191))
    c.showPage()
    c.save()
    return SheetTemplate(
        template_name="test", base_pdf_path=base, paper_size="A1",
        orientation="landscape", guide_rects={},
        callout_column={"x": 0, "y": 0, "w": 1, "h": 1, "max_details": 1},
    )


class VectorOnlySheetTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.template = _template(self.tmp)
        self.vector = _make_vector_pdf(os.path.join(self.tmp, "v.pdf"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self, with_vector: bool, annotations: bool = False):
        state = ProjectState()
        state.init_from_template(self.template)
        state.metadata.client_name = "C"
        state.metadata.project_title = "P"
        state.metadata.sheet_title = "S"
        state.add_asset(ImportedAsset(
            id="asset_1", source_path="x.dwg", source_type="dwg_render",
            image=Image.new("RGB", (400, 300), (255, 255, 255)),
            vector_source_path=self.vector if with_vector else None,
        ))
        state.placed_items.append(PlacedItem(
            id="placed_1", item_type="image", source_asset_id="asset_1",
            crop_box=None, page_rect=(60.0, 60.0, 600.0, 450.0),
        ))
        if annotations:
            state.annotations.append(Annotation(
                id="a1", kind="view_label", text="PLAN",
                page_pos=(60.0, 540.0),
            ))
        return state

    def test_a_sheet_of_only_vector_items_generates(self):
        out = os.path.join(self.tmp, "sheet.pdf")
        generate_technical_sheet(self._state(True), out)
        self.assertTrue(os.path.isfile(out))
        self.assertEqual(len(pymupdf.open(out)), 1)

    def test_the_vector_sheet_embeds_no_bitmap(self):
        out = os.path.join(self.tmp, "sheet.pdf")
        generate_technical_sheet(self._state(True), out)
        page = pymupdf.open(out)[0]
        self.assertEqual(len(page.get_images(full=True)), 0)
        self.assertGreater(len(page.get_drawings()), 5)

    def test_without_a_vector_the_raster_is_still_drawn(self):
        out = os.path.join(self.tmp, "sheet.pdf")
        generate_technical_sheet(self._state(False), out)
        page = pymupdf.open(out)[0]
        self.assertEqual(len(page.get_images(full=True)), 1)

    def test_lettering_still_reaches_a_vector_only_sheet(self):
        out = os.path.join(self.tmp, "sheet.pdf")
        generate_technical_sheet(self._state(True, annotations=True), out)
        self.assertIn("PLAN", pymupdf.open(out)[0].get_text())

    def test_an_empty_sheet_does_not_raise(self):
        """No items at all is the degenerate form of the same bug."""
        state = ProjectState()
        state.init_from_template(self.template)
        out = os.path.join(self.tmp, "empty.pdf")
        generate_technical_sheet(state, out)
        self.assertEqual(len(pymupdf.open(out)), 1)

    def test_a_missing_vector_file_falls_back_to_the_raster(self):
        state = self._state(True)
        state.assets["asset_1"].vector_source_path = os.path.join(
            self.tmp, "gone.pdf"
        )
        out = os.path.join(self.tmp, "sheet.pdf")
        generate_technical_sheet(state, out)
        page = pymupdf.open(out)[0]
        self.assertEqual(len(page.get_images(full=True)), 1,
                         "a vanished vector must fall back, not vanish")


class VectorPersistenceTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.vector = _make_vector_pdf(os.path.join(self.tmp, "v.pdf"))
        self.project = os.path.join(self.tmp, "p.tdsheet")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self):
        state = ProjectState()
        state.add_asset(ImportedAsset(
            id="asset_1", source_path="x.dwg", source_type="dwg_render",
            image=Image.new("RGB", (120, 90), (255, 255, 255)),
            vector_source_path=self.vector,
        ))
        state.placed_items.append(PlacedItem(
            id="placed_1", item_type="image", source_asset_id="asset_1",
            crop_box=None, page_rect=(0.0, 0.0, 100.0, 75.0),
        ))
        return state

    def test_the_line_art_is_stored_in_the_project_file(self):
        save_project(self._state(), self.project)
        with zipfile.ZipFile(self.project) as archive:
            self.assertIn("assets/asset_1.pdf", archive.namelist())

    def test_it_comes_back_on_a_machine_where_the_original_is_gone(self):
        save_project(self._state(), self.project)
        os.unlink(self.vector)      # the session temp folder is cleaned up

        restored, _ = load_project(self.project)
        path = restored.assets["asset_1"].vector_source_path
        self.assertIsNotNone(path, "line art was lost on reopen")
        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as handle:
            self.assertTrue(handle.read(5).startswith(b"%PDF"))

    def test_an_asset_without_line_art_stores_none(self):
        state = self._state()
        state.assets["asset_1"].vector_source_path = None
        save_project(state, self.project)
        with zipfile.ZipFile(self.project) as archive:
            self.assertNotIn("assets/asset_1.pdf", archive.namelist())
        restored, _ = load_project(self.project)
        self.assertIsNone(restored.assets["asset_1"].vector_source_path)

    def test_a_vector_path_that_no_longer_exists_is_not_carried_forward(self):
        state = self._state()
        state.assets["asset_1"].vector_source_path = os.path.join(
            self.tmp, "never.pdf"
        )
        save_project(state, self.project)
        restored, _ = load_project(self.project)
        self.assertIsNone(restored.assets["asset_1"].vector_source_path)

    def test_a_reopened_project_still_prints_as_vector(self):
        save_project(self._state(), self.project)
        os.unlink(self.vector)
        restored, _ = load_project(self.project)

        restored.init_from_template(_template(self.tmp))
        restored.metadata.client_name = "C"
        restored.metadata.project_title = "P"
        restored.metadata.sheet_title = "S"
        restored.get_placed_item("placed_1").page_rect = (60.0, 60.0, 600.0, 450.0)

        out = os.path.join(self.tmp, "reopened.pdf")
        generate_technical_sheet(restored, out)
        page = pymupdf.open(out)[0]
        self.assertEqual(len(page.get_images(full=True)), 0,
                         "a reopened project fell back to its bitmap")


if __name__ == "__main__":
    unittest.main()
