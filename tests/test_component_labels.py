"""Naming extracted CAD views after their own titles.

``ComponentRegion.suggested_label`` was declared on the dataclass and read
by merge_components, but nothing ever set it — so every view extracted
from a DWG arrived in the review dialog called ``comp_4f2a1b`` and the
architect had to tell the plan from the section by the thumbnail alone.
"""

import os
import sys
import unittest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from app.core.dwg_components import (  # noqa: E402
    ComponentRegion, _clean_label, _label_score, _suggest_labels,
)


class _FakeDxf:
    def __init__(self, text, insert):
        self.text = text
        self.insert = insert


class _FakeEntity:
    """Enough of an ezdxf entity for the labeller."""

    def __init__(self, kind, text, insert=(0.0, 0.0)):
        self._kind = kind
        self.dxf = _FakeDxf(text, insert)
        self.text = text

    def dxftype(self):
        return self._kind


class CleanLabelTests(unittest.TestCase):

    def test_plain_text_is_left_alone(self):
        self.assertEqual(_clean_label("PLAN VIEW"), "PLAN VIEW")

    def test_mtext_formatting_codes_are_stripped(self):
        self.assertEqual(_clean_label(r"\pxqc;SECTION A"), "SECTION A")
        self.assertEqual(_clean_label(r"{\fArial|b1;ELEVATION}"), "ELEVATION")
        self.assertEqual(_clean_label(r"FRONT\PELEVATION"), "FRONT ELEVATION")

    def test_surrounding_punctuation_and_space_go(self):
        self.assertEqual(_clean_label("  - PLAN -  "), "PLAN")

    def test_none_and_empty_are_safe(self):
        self.assertEqual(_clean_label(None), "")
        self.assertEqual(_clean_label(""), "")


class LabelScoreTests(unittest.TestCase):

    def test_view_titles_score(self):
        for title in ("PLAN", "SECTION A", "FRONT ELEVATION", "DETAIL B",
                      "ISOMETRIC VIEW"):
            with self.subTest(title=title):
                self.assertGreater(_label_score(title), 0.0)

    def test_measurements_do_not(self):
        for text in ("1250", "1:100", "450 x 300", "2400", "-", "12.5"):
            with self.subTest(text=text):
                self.assertEqual(_label_score(text), 0.0)

    def test_a_long_note_is_not_a_title(self):
        note = ("ALL DIMENSIONS TO BE VERIFIED ON SITE BEFORE ANY "
                "FABRICATION IS PUT IN HAND")
        self.assertEqual(_label_score(note), 0.0)

    def test_a_title_beats_a_bare_word(self):
        self.assertGreater(_label_score("PLAN VIEW"), _label_score("TIMBER"))


class SuggestLabelTests(unittest.TestCase):

    def _component(self, handles):
        return ComponentRegion(
            id="comp_1", bbox=(0.0, 0.0, 100.0, 100.0),
            entity_ids=list(handles), entity_count=len(handles),
        )

    def test_a_view_is_named_after_its_title(self):
        comp = self._component(["h1", "h2"])
        by_handle = {
            "h1": _FakeEntity("LINE", None),
            "h2": _FakeEntity("TEXT", "SECTION A", (10.0, -12.0)),
        }
        _suggest_labels([comp], by_handle, lambda *_: None)
        self.assertEqual(comp.suggested_label, "SECTION A")

    def test_mtext_works_as_well_as_text(self):
        comp = self._component(["h1"])
        by_handle = {"h1": _FakeEntity("MTEXT", r"\pxqc;PLAN VIEW", (5.0, -8.0))}
        _suggest_labels([comp], by_handle, lambda *_: None)
        self.assertEqual(comp.suggested_label, "PLAN VIEW")

    def test_a_dimension_value_is_not_mistaken_for_a_title(self):
        comp = self._component(["h1", "h2"])
        by_handle = {
            "h1": _FakeEntity("TEXT", "1250", (10.0, 50.0)),
            "h2": _FakeEntity("TEXT", "ELEVATION", (10.0, -10.0)),
        }
        _suggest_labels([comp], by_handle, lambda *_: None)
        self.assertEqual(comp.suggested_label, "ELEVATION")

    def test_the_lower_title_wins_a_tie(self):
        """View titles are written underneath the view on these sheets."""
        comp = self._component(["h1", "h2"])
        by_handle = {
            "h1": _FakeEntity("TEXT", "PLAN VIEW", (10.0, 200.0)),
            "h2": _FakeEntity("TEXT", "SECTION VIEW", (10.0, -20.0)),
        }
        _suggest_labels([comp], by_handle, lambda *_: None)
        self.assertEqual(comp.suggested_label, "SECTION VIEW")

    def test_a_view_with_no_lettering_stays_unnamed(self):
        comp = self._component(["h1"])
        by_handle = {"h1": _FakeEntity("LINE", None)}
        _suggest_labels([comp], by_handle, lambda *_: None)
        self.assertIsNone(comp.suggested_label)

    def test_a_missing_handle_is_not_a_crash(self):
        comp = self._component(["gone", "h1"])
        by_handle = {"h1": _FakeEntity("TEXT", "PLAN", (0.0, 0.0))}
        _suggest_labels([comp], by_handle, lambda *_: None)
        self.assertEqual(comp.suggested_label, "PLAN")

    def test_an_entity_with_no_insert_point_is_not_a_crash(self):
        class _NoInsert(_FakeEntity):
            def __init__(self):
                super().__init__("TEXT", "PLAN")
                del self.dxf.insert

        comp = self._component(["h1"])
        _suggest_labels([comp], {"h1": _NoInsert()}, lambda *_: None)
        self.assertEqual(comp.suggested_label, "PLAN")

    def test_each_component_is_named_independently(self):
        first = ComponentRegion(id="a", bbox=(0, 0, 1, 1),
                                entity_ids=["h1"], entity_count=1)
        second = ComponentRegion(id="b", bbox=(0, 0, 1, 1),
                                 entity_ids=["h2"], entity_count=1)
        by_handle = {
            "h1": _FakeEntity("TEXT", "PLAN", (0.0, 0.0)),
            "h2": _FakeEntity("TEXT", "ELEVATION", (0.0, 0.0)),
        }
        _suggest_labels([first, second], by_handle, lambda *_: None)
        self.assertEqual(first.suggested_label, "PLAN")
        self.assertEqual(second.suggested_label, "ELEVATION")


if __name__ == "__main__":
    unittest.main()
