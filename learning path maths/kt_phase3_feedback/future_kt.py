"""Independent next-item queries from one fixed, already observed history."""
import copy
import math

import torch

from kt_adapter import build_batch, coverage_report, response_events


QUERY_KEYS = ("question_id", "skill_id", "item_id", "text", "options", "content_text")


def predict_future_candidates(bank, responses, candidates, kt):
    """Append exactly one unanswered query per forward pass, never a batch of futures."""
    synthetic = [q for q in candidates if q["item_id"].startswith("synthetic_divisibility_")]
    if synthetic:
        from research_runtime import require_augmented_embeddings
        require_augmented_embeddings()
        if any(q["item_id"] in kt.item_vocab for q in synthetic):
            raise ValueError("synthetic practice items must use the frozen UNK representation")
    if len(responses) != 40:
        raise ValueError("future practice requires 40 completed assessment responses")
    config = getattr(kt, "config", {})
    if config.get("variant") != "skill_item_content_option":
        raise ValueError("future practice requires frozen variant D")
    max_len = config.get("max_seq_len")
    if not isinstance(max_len, int) or isinstance(max_len, bool) or max_len < 41:
        raise ValueError("frozen sequence length cannot hold history plus query")
    history = response_events(bank, responses)
    coverage = coverage_report(bank, kt)
    if (coverage["missing_skill_indexes"] or coverage["missing_content_indexes"]
            or coverage["missing_option_values"]):
        raise ValueError("assessment history is not KT-text-compatible")
    candidate_coverage = coverage_report({"questions": candidates}, kt)
    if candidate_coverage["missing_skill_indexes"] or candidate_coverage["missing_content_indexes"]:
        raise ValueError("future query skill/content is not KT-compatible")
    prefix = build_batch(bank, history, kt)
    predictions = []
    for candidate in candidates:
        # An answer key/selection, even if present in the private pool, is not
        # consulted or encoded. Current response slots are fixed placeholders.
        query = {key: copy.deepcopy(candidate[key]) for key in QUERY_KEYS}
        item_index = kt.item_vocab.get(query["item_id"], 1)
        values = {
            "skill": kt.skill_vocab[query["skill_id"]], "item": item_index,
            "content_idx": kt.dataset.text_to_row[query["content_text"]],
            "selected_text_idx": 0, "correct": 0.0,
            "rt": 0.0, "rt_mask": 0.0, "attempt": 1.0,
            "time_bin_ids": 0, "attn_mask": 1.0,
        }
        batch = {
            key: torch.cat((tensor.clone(), tensor.new_tensor([[values[key]]])), dim=1)
            for key, tensor in prefix.items()
        }
        probability = kt.probs(batch)
        if hasattr(probability, "tolist"):
            probability = probability.tolist()
        if (not isinstance(probability, list) or len(probability) != 1
                or not isinstance(probability[0], list)
                or len(probability[0]) != 41):
            raise ValueError("invalid frozen KT future output shape")
        p = probability[0][-1]
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1:
            raise ValueError("invalid frozen KT future probability")
        predictions.append({
            "question_id": query["question_id"], "skill_id": query["skill_id"],
            "item_id": query["item_id"], "regime": "cold" if item_index == 1 else "warm",
            "p_correct": float(p),
        })
    return {
        "predictions": predictions,
        "provenance": {
            "variant": config["variant"], "seed": config.get("seed"),
            "checkpoint": getattr(getattr(kt, "checkpoint_path", None), "name", None),
            "history_responses": 40, "query_position": 40,
            "history_policy": "current_assessment_only_no_lifetime_history",
            "input_policy": "same_observed_prefix_plus_one_response_blind_query",
            "missing_temporal_data_policy": "unknown_response_times_zero_mask_attempt_one_time_bin_zero",
            "conformal": "not_applied_no_per_item_probability_intervals",
        },
    }
