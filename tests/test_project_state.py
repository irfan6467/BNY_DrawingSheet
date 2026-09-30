import os
import sys
import unittest


project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from app.core.project_state import Callout, ImportedAsset, PlacedItem, ProjectState


class ProjectStateTests(unittest.TestCase):
    def test_callouts_are_initialized_and_removed_with_placed_items(self):
        state = ProjectState()
        asset = ImportedAsset(
            id="asset_1",
            source_path="source.png",
            source_type="image",
            image=object(),
        )
        item = PlacedItem(
            id="placed_1",
            item_type="callout_circle",
            source_asset_id=asset.id,
            crop_box=(0, 0, 10, 10),
            page_rect=(0, 0, 20, 20),
        )
        callout = Callout(
            id="DETAIL 1",
            description="Detail",
            crop_source_asset_id=asset.id,
            crop_box=(0, 0, 10, 10),
            placed_item_id=item.id,
        )

        state.add_asset(asset)
        state.add_placed_item(item)
        state.add_callout(callout)
        state.remove_placed_item(item.id)

        self.assertEqual([], state.callouts)

    def test_remove_asset_without_callouts_does_not_crash(self):
        state = ProjectState()
        asset = ImportedAsset(
            id="asset_1",
            source_path="source.png",
            source_type="image",
            image=object(),
        )

        state.add_asset(asset)
        state.remove_asset(asset.id)

        self.assertEqual([], state.callouts)


if __name__ == "__main__":
    unittest.main()
