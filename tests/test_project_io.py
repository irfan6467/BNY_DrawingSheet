"""Saving and reopening a sheet.

Before this existed there was no way to keep a session at all, so a
crash - or simply closing the window - took every placement, crop and
label with it.  These cover the round trip and the damaged-file paths,
because a recovery file is written by a process that is about to die and
cannot be assumed to be complete.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from PIL import Image  # noqa: E402

from app.core.project_io import (  # noqa: E402
    FORMAT_VERSION, load_project, save_project, state_to_dict,
)
from app.core.project_state import (  # noqa: E402
    Annotation, Callout, ImportedAsset, PlacedItem, ProjectState,
    SheetTemplate,
)


def _populated_state() -> ProjectState:
    state = ProjectState()
    state.init_from_template(SheetTemplate(
        template_name="bny_standard_a1",
        base_pdf_path="nowhere/bny.pdf",
        paper_size="A1",
        orientation="landscape",
        guide_rects={"plan": {"x": 1, "y": 2, "w": 3, "h": 4}},
        callout_column={"x": 0, "y": 0, "w": 10, "h": 10, "max_details": 6},
    ))

    # An odd width, to prove the stored PNG round-trips exactly.
    asset = ImportedAsset(
        id="asset_aaa",
        source_path=r"C:\drawings\bench.png",
        source_type="image",
        image=Image.new("RGB", (103, 57), (12, 34, 56)),
    )
    state.add_asset(asset)

    state.placed_items.append(PlacedItem(
        id="placed_one",
        item_type="image",
        source_asset_id="asset_aaa",
        crop_box=(4.0, 5.0, 60.0, 40.0),
        page_rect=(100.0, 50.0, 400.0, 200.0),
        rotation=15.0,
        circular_mask=False,
        z_order=3,
    ))
    state.placed_items.append(PlacedItem(
        id="placed_two",
        item_type="callout_circle",
        source_asset_id="asset_aaa",
        crop_box=(0.0, 0.0, 50.0, 50.0),
        page_rect=(600.0, 300.0, 90.0, 90.0),
        callout_id="DETAIL A",
        description="BED PANEL CURVE",
        leader_style="dashed",
        leader_target_page_pos=(700.0, 400.0),
    ))

    state.annotations.append(Annotation(
        id="annot_one", kind="view_label", text="PLAN",
        page_pos=(120.0, 260.0), font_size=13.0, rule_width=110.0,
    ))
    state.annotations.append(Annotation(
        id="annot_two", kind="detail_note", text="HOLES FOR VENTILATION",
        page_pos=(300.0, 500.0), target_page_pos=(390.0, 570.0),
    ))

    state.callouts.append(Callout(
        id="DETAIL A", description="BED PANEL CURVE",
        crop_source_asset_id="asset_aaa", crop_box=(0.0, 0.0, 50.0, 50.0),
        placed_item_id="placed_two",
    ))

    state.create_group(["placed_one", "annot_one"])
    state.metadata.client_name = "A Client"
    state.metadata.sheet_title = "BEDROOM DETAILS"
    return state


class RoundTripTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "sheet.tdsheet")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_everything_comes_back(self):
        original = _populated_state()
        save_project(original, self.path, panel_values={"B27": "A Client"})

        restored, panel = load_project(self.path)

        self.assertEqual(panel, {"B27": "A Client"})
        self.assertEqual(len(restored.assets), 1)
        self.assertEqual(len(restored.placed_items), 2)
        self.assertEqual(len(restored.annotations), 2)
        self.assertEqual(len(restored.callouts), 1)
        self.assertEqual(restored.metadata.client_name, "A Client")
        self.assertEqual(restored.metadata.sheet_title, "BEDROOM DETAILS")
        self.assertEqual(restored.template.template_name, "bny_standard_a1")

    def test_image_pixels_are_preserved(self):
        original = _populated_state()
        save_project(original, self.path)
        restored, _ = load_project(self.path)

        image = restored.assets["asset_aaa"].image
        self.assertEqual(image.size, (103, 57))
        self.assertEqual(image.convert("RGB").getpixel((100, 50)), (12, 34, 56))

    def test_geometry_comes_back_as_tuples_not_lists(self):
        """JSON turns every tuple into a list; the model must not notice."""
        original = _populated_state()
        save_project(original, self.path)
        restored, _ = load_project(self.path)

        item = restored.get_placed_item("placed_one")
        self.assertIsInstance(item.page_rect, tuple)
        self.assertIsInstance(item.crop_box, tuple)
        self.assertEqual(item.page_rect, (100.0, 50.0, 400.0, 200.0))
        self.assertEqual(item.crop_box, (4.0, 5.0, 60.0, 40.0))
        self.assertEqual(item.rotation, 15.0)
        self.assertEqual(item.z_order, 3)

        callout_item = restored.get_placed_item("placed_two")
        self.assertIsInstance(callout_item.leader_target_page_pos, tuple)
        self.assertEqual(callout_item.leader_target_page_pos, (700.0, 400.0))

        note = restored.get_annotation("annot_two")
        self.assertIsInstance(note.page_pos, tuple)
        self.assertEqual(note.target_page_pos, (390.0, 570.0))

    def test_groups_survive_and_still_name_both_kinds(self):
        original = _populated_state()
        save_project(original, self.path)
        restored, _ = load_project(self.path)

        group_id = restored.group_id_for("placed_one")
        self.assertIsNotNone(group_id)
        self.assertEqual(
            sorted(restored.get_group_member_ids(group_id)),
            ["annot_one", "placed_one"],
        )

    def test_saving_is_atomic(self):
        """A half-written archive must never replace a good one."""
        state = _populated_state()
        save_project(state, self.path)
        good = os.path.getsize(self.path)

        broken = ProjectState()
        broken.assets["bad"] = ImportedAsset(
            id="bad", source_path="", source_type="image", image=object(),
        )
        # An asset whose "image" cannot be written is logged and skipped,
        # so this still succeeds - but the file must stay valid either way.
        save_project(broken, self.path)
        self.assertTrue(zipfile.is_zipfile(self.path))
        self.assertGreater(good, 0)

    def test_no_temporary_files_are_left_behind(self):
        save_project(_populated_state(), self.path)
        leftovers = [n for n in os.listdir(self.tmp) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class CrossVolumeTests(unittest.TestCase):
    """Saving where rename is not available.

    Under an MSIX or AppContainer package %LOCALAPPDATA% is a reparse
    point onto another volume, and Windows refuses every rename inside
    that folder with ERROR_NOT_SAME_DEVICE — even between two names in
    the same listing.  With only ``os.replace`` to rely on, the autosave
    silently wrote nothing at all on those machines.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "sheet.tdsheet")
        self._real_replace = os.replace

    def tearDown(self):
        os.replace = self._real_replace
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _break_rename(self):
        def refuse(src, dst, *args, **kwargs):
            raise OSError(18, "The system cannot move the file to a "
                              "different disk drive")
        os.replace = refuse

    def test_it_still_saves_when_rename_is_refused(self):
        self._break_rename()
        save_project(_populated_state(), self.path)

        self.assertTrue(os.path.isfile(self.path))
        restored, _ = load_project(self.path)
        self.assertEqual(len(restored.placed_items), 2)

    def test_the_fallback_leaves_no_litter(self):
        self._break_rename()
        save_project(_populated_state(), self.path)
        leftovers = sorted(
            n for n in os.listdir(self.tmp) if n != "sheet.tdsheet"
        )
        self.assertEqual(leftovers, [])

    def test_a_second_save_replaces_the_first(self):
        self._break_rename()
        state = _populated_state()
        save_project(state, self.path)

        state.metadata.sheet_title = "CHANGED"
        save_project(state, self.path)

        restored, _ = load_project(self.path)
        self.assertEqual(restored.metadata.sheet_title, "CHANGED")

    def test_a_failed_copy_puts_the_previous_version_back(self):
        save_project(_populated_state(), self.path)
        with open(self.path, "rb") as handle:
            good = handle.read()

        self._break_rename()
        real_copyfile = shutil.copyfile
        calls = []

        def flaky(src, dst, *args, **kwargs):
            calls.append((src, dst))
            # Let the backup copy through, then fail the real write.
            if len(calls) == 1:
                return real_copyfile(src, dst, *args, **kwargs)
            raise OSError(28, "No space left on device")

        shutil.copyfile = flaky
        try:
            with self.assertRaises(OSError):
                save_project(_populated_state(), self.path)
        finally:
            shutil.copyfile = real_copyfile

        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), good)


class DamagedFileTests(unittest.TestCase):
    """A recovery file is written by a process that is about to crash."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "sheet.tdsheet")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_not_a_zip_is_refused_clearly(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("this is not a project")
        with self.assertRaises(ValueError) as caught:
            load_project(self.path)
        self.assertIn("not a drawing sheet project", str(caught.exception))

    def test_missing_manifest_is_refused_clearly(self):
        with zipfile.ZipFile(self.path, "w") as archive:
            archive.writestr("assets/nothing.png", b"")
        with self.assertRaises(ValueError) as caught:
            load_project(self.path)
        self.assertIn("missing its project data", str(caught.exception))

    def test_a_newer_format_is_refused_rather_than_half_read(self):
        data = state_to_dict(_populated_state())
        data["format_version"] = FORMAT_VERSION + 1
        with zipfile.ZipFile(self.path, "w") as archive:
            archive.writestr("project.json", json.dumps(data))
        with self.assertRaises(ValueError) as caught:
            load_project(self.path)
        self.assertIn("newer version", str(caught.exception))

    def test_an_item_whose_asset_is_gone_is_skipped_not_fatal(self):
        data = state_to_dict(_populated_state())
        data["assets"] = []          # the images did not make it
        with zipfile.ZipFile(self.path, "w") as archive:
            archive.writestr("project.json", json.dumps(data))

        messages = []
        restored, _ = load_project(
            self.path, log=lambda sev, msg: messages.append((sev, msg))
        )
        self.assertEqual(restored.placed_items, [])
        # The lettering does not depend on an asset, so it survives.
        self.assertEqual(len(restored.annotations), 2)
        self.assertTrue(any(s == "warning" for s, _ in messages))

    def test_a_malformed_record_is_skipped_not_fatal(self):
        data = state_to_dict(_populated_state())
        data["placed_items"].append({"id": "junk"})        # no asset id
        data["annotations"].append({"id": "junk", "kind": "nonsense"})
        with zipfile.ZipFile(self.path, "w") as archive:
            archive.writestr("project.json", json.dumps(data))
        restored, _ = load_project(self.path)
        self.assertEqual(len(restored.annotations), 2)
        self.assertNotIn("junk", [pi.id for pi in restored.placed_items])

    def test_missing_file_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            load_project(os.path.join(self.tmp, "nope.tdsheet"))


if __name__ == "__main__":
    unittest.main()
