"""Target grouping and baseline boundary regression tests, using fake events."""
import unittest

from phase4_common import smoothed
from prepare_replay import group_blocks, match_ordered_bank


def event(index, split="test_warm", skill=2):
    return {"item_instance": f"item{index}", "skill": skill,
            "content_text": f"content{index}", "split": split}


def lookup_for(events):
    return {(e["item_instance"], e["skill"], e["content_text"]):
            {"question": {"question_id": f"q{index}"}}
            for index, e in enumerate(events)}


class PreparationTests(unittest.TestCase):
    def test_group_before_filter_never_invents_selected_blocks(self):
        events = [event(i) for i in range(11)]
        lookup = lookup_for(events[1:])
        blocks = group_blocks(events, lookup, 9, 12, 10)
        self.assertEqual(len(blocks), 1)
        self.assertFalse(blocks[0]["fully_selected"])
        self.assertEqual(blocks[0]["target_ids"], [f"12:{i}" for i in range(10)])

    def test_context_and_other_skills_do_not_change_same_skill_grouping(self):
        events = [event(99, "context"), event(1), event(100, skill=3),
                  event(2, "test_cold_item")]
        blocks = group_blocks(events, lookup_for(events), 7, 4, 2)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["kind"], "mixed")
        self.assertEqual(blocks[0]["target_ids"], ["4:1", "4:3"])

    def test_ordered_matches_are_contiguous_not_assembled(self):
        events = [event(1), event(2), event(3)]
        keys = [(e["item_instance"], e["skill"], e["content_text"]) for e in events]
        self.assertEqual(match_ordered_bank(events, keys[:2]), 1)
        self.assertEqual(match_ordered_bank(events, [keys[0], keys[2]]), 0)

    def test_training_prior_smoothing_and_fallback(self):
        self.assertEqual(smoothed([0, 0]), 0.5)
        self.assertEqual(smoothed([0, 0], 0.8), 0.8)
        self.assertEqual(smoothed([3, 4]), 4 / 6)


if __name__ == "__main__":
    unittest.main()
