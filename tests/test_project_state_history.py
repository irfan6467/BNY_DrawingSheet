"""Undo history and group bookkeeping.

``remove_placed_item`` used to take an undo snapshot and never bank it.
Two things followed: deleting an item could not be undone, and the
orphaned pending snapshot was still sitting there when the next edit
called begin_change - so that edit's own "before" state was thrown away
and undoing it took the sheet somewhere it had never been.
"""

import os
import sys
import unittest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from app.core.project_state import (  # noqa: E402
    Annotation, ImportedAsset, PlacedItem, ProjectState,
)


def _state_with_items(count: int = 3) -> ProjectState:
    state = ProjectState()
    state.add_asset(ImportedAsset(
        id="asset_1", source_path="a.png", source_type="image", image=object(),
    ))
    for i in range(count):
        state.add_placed_item(
            PlacedItem(
                id=f"placed_{i}",
                item_type="image",
                source_asset_id="asset_1",
                crop_box=None,
                page_rect=(i * 100.0, 0.0, 50.0, 50.0),
            ),
            record_undo=False,
        )
    return state


class DeleteUndoTests(unittest.TestCase):

    def test_deleting_an_item_can_be_undone(self):
        state = _state_with_items()
        state.remove_placed_item("placed_1")

        self.assertIsNone(state.get_placed_item("placed_1"))
        self.assertTrue(state.can_undo())

        state.undo()
        self.assertIsNotNone(state.get_placed_item("placed_1"))

    def test_delete_does_not_leave_a_snapshot_behind(self):
        """The orphan that corrupted whichever edit came next."""
        state = _state_with_items()
        state.remove_placed_item("placed_1")
        self.assertIsNone(state._pending_change)

    def test_the_edit_after_a_delete_undoes_to_the_right_place(self):
        state = _state_with_items()
        state.remove_placed_item("placed_1")
        state.update_placed_item("placed_0", page_rect=(999.0, 999.0, 5.0, 5.0))

        state.undo()   # the move
        self.assertEqual(
            state.get_placed_item("placed_0").page_rect, (0.0, 0.0, 50.0, 50.0)
        )
        # ...and the item deleted before it is still gone, not resurrected.
        self.assertIsNone(state.get_placed_item("placed_1"))

        state.undo()   # the delete
        self.assertIsNotNone(state.get_placed_item("placed_1"))

    def test_record_undo_false_records_nothing(self):
        state = _state_with_items()
        state.remove_placed_item("placed_1", record_undo=False)
        self.assertFalse(state.can_undo())
        self.assertIsNone(state._pending_change)


class RedoTests(unittest.TestCase):

    def test_redo_puts_a_deleted_item_back_in_the_bin(self):
        state = _state_with_items()
        state.remove_placed_item("placed_2")
        state.undo()
        self.assertIsNotNone(state.get_placed_item("placed_2"))
        state.redo()
        self.assertIsNone(state.get_placed_item("placed_2"))

    def test_a_new_edit_clears_the_redo_branch(self):
        state = _state_with_items()
        state.remove_placed_item("placed_2")
        state.undo()
        self.assertTrue(state.can_redo())
        state.update_placed_item("placed_0", rotation=10.0)
        self.assertFalse(state.can_redo())

    def test_history_is_capped(self):
        state = _state_with_items(1)
        for i in range(ProjectState.UNDO_STACK_SIZE + 10):
            state.update_placed_item("placed_0", rotation=float(i))
        self.assertLessEqual(
            len(state._undo_stack), ProjectState.UNDO_STACK_SIZE
        )


class GroupBookkeepingTests(unittest.TestCase):
    """A group that still names a deleted member is a dangling pointer.

    The canvas looks each member up to select it alongside its siblings,
    so a stale id meant reaching for a graphics item that had been taken
    off the scene.
    """

    def test_deleting_a_member_leaves_no_stale_id(self):
        state = _state_with_items()
        group_id = state.create_group(["placed_0", "placed_1", "placed_2"])
        state.remove_placed_item("placed_1")
        self.assertNotIn("placed_1", state.groups.get(group_id, []))

    def test_a_group_reduced_to_one_is_dissolved(self):
        state = _state_with_items()
        group_id = state.create_group(["placed_0", "placed_1"])
        state.remove_placed_item("placed_1")
        self.assertNotIn(group_id, state.groups)
        self.assertIsNone(state.get_placed_item("placed_0").group_id)

    def test_removing_an_asset_clears_its_items_from_groups(self):
        state = _state_with_items()
        state.add_asset(ImportedAsset(
            id="asset_2", source_path="b.png", source_type="image",
            image=object(),
        ))
        state.add_placed_item(
            PlacedItem(id="placed_other", item_type="image",
                       source_asset_id="asset_2", crop_box=None,
                       page_rect=(0.0, 0.0, 10.0, 10.0)),
            record_undo=False,
        )
        group_id = state.create_group(["placed_0", "placed_1", "placed_other"])

        state.remove_asset("asset_1")   # takes placed_0 and placed_1 with it
        self.assertEqual(state.groups.get(group_id, []), [])
        self.assertIsNone(state.get_placed_item("placed_other").group_id)

    def test_deleting_an_annotation_clears_it_from_its_group(self):
        state = _state_with_items()
        state.add_annotation(Annotation(
            id="annot_1", kind="view_label", text="PLAN", page_pos=(0.0, 0.0),
        ))
        group_id = state.create_group(["placed_0", "placed_1", "annot_1"])
        state.remove_annotation("annot_1")
        self.assertNotIn("annot_1", state.groups.get(group_id, []))


class RevisionTests(unittest.TestCase):
    """The counter the window uses to know whether to offer a save."""

    def test_a_fresh_state_starts_at_zero(self):
        self.assertEqual(ProjectState().revision, 0)

    def test_every_kind_of_edit_moves_it(self):
        state = ProjectState()
        seen = state.revision

        state.add_asset(ImportedAsset(id="a", source_path="", source_type="image",
                                      image=object()))
        self.assertGreater(state.revision, seen)
        seen = state.revision

        state.add_placed_item(PlacedItem(
            id="p", item_type="image", source_asset_id="a", crop_box=None,
            page_rect=(0.0, 0.0, 1.0, 1.0),
        ))
        self.assertGreater(state.revision, seen)
        seen = state.revision

        state.add_annotation(Annotation(
            id="n", kind="sheet_title", text="T", page_pos=(0.0, 0.0),
        ))
        self.assertGreater(state.revision, seen)
        seen = state.revision

        state.undo()
        self.assertGreater(state.revision, seen)

    def test_a_gesture_that_changes_nothing_does_not_count(self):
        state = _state_with_items(1)
        before = state.revision
        state.begin_change("drag that went nowhere")
        state.commit_change()
        self.assertEqual(state.revision, before)


if __name__ == "__main__":
    unittest.main()
