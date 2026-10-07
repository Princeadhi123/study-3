"""No current-answer or future-interaction information reaches the predictor."""
import unittest

import numpy as np

from replay_inputs import POST_RESPONSE_FIELDS, prediction_inputs


def encoded():
    result = {key: np.array([1, 2, 3, 4], dtype=np.float32)
              for key in POST_RESPONSE_FIELDS}
    result.update(skill=np.array([2, 3, 2, 9]), item=np.array([4, 5, 6, 9]),
                  content_idx=np.array([10, 11, 12, 13]), split=np.array([0, 2, 2, 2]),
                  attn_mask=np.ones(4, dtype=np.float32))
    return result


class InputBoundaryTests(unittest.TestCase):
    def test_current_answer_and_future_mutations_leave_both_inputs_identical(self):
        original = encoded()
        mutated = {key: values.copy() for key, values in original.items()}
        for key in POST_RESPONSE_FIELDS:
            mutated[key][2] = 999
        for key in mutated:
            mutated[key][3] = 999
        for history in (False, True):
            a, position_a = prediction_inputs(original, 2, history)
            b, position_b = prediction_inputs(mutated, 2, history)
            self.assertEqual(position_a, position_b)
            for key in a:
                np.testing.assert_array_equal(a[key], b[key])

    def test_history_retains_only_preceding_responses_and_original_position(self):
        raw = encoded()
        result, position = prediction_inputs(raw, 2, True)
        self.assertEqual(position, 2)
        np.testing.assert_array_equal(result["correct"][:2], raw["correct"][:2])
        self.assertEqual(result["correct"][2], 0)
        self.assertEqual(result["attn_mask"].tolist(), [1, 1, 1, 0])
        self.assertEqual(result["skill"].tolist(), [2, 3, 2, 0])

    def test_no_history_keeps_query_only_at_start_position(self):
        result, position = prediction_inputs(encoded(), 2, False)
        self.assertEqual(position, 0)
        self.assertEqual(result["skill"].tolist(), [2, 0, 0, 0])
        self.assertEqual(result["content_idx"].tolist(), [12, 0, 0, 0])
        self.assertEqual(result["attn_mask"].tolist(), [1, 0, 0, 0])
        for key in POST_RESPONSE_FIELDS:
            self.assertEqual(result[key][0], 1 if key == "attempt" else 0)


if __name__ == "__main__":
    unittest.main()
