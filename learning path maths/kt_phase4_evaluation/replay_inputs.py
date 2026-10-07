"""Response-blind model inputs, preserving the frozen dataset's encoding."""
import numpy as np

POST_RESPONSE_FIELDS = (
    "correct", "selected_text_idx", "rt", "rt_mask", "attempt", "time_bin_ids")


def prediction_inputs(encoded_record, position, use_history):
    length = len(encoded_record["skill"])
    if not 0 <= position < length or encoded_record["attn_mask"][position] != 1:
        raise ValueError("Target position is not a real event")
    source = slice(0, position + 1) if use_history else slice(position, position + 1)
    n = position + 1 if use_history else 1
    result = {}
    for key, values in encoded_record.items():
        fill = -1 if key == "split" else 1 if key == "attempt" else 0
        output = np.full_like(values, fill)
        output[:n] = values[source]
        result[key] = output
    target = n - 1
    for key in POST_RESPONSE_FIELDS:
        result[key][target] = 1 if key == "attempt" else 0
    result["attn_mask"][:] = 0
    result["attn_mask"][:n] = 1
    return result, target
