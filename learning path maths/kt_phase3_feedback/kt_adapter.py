"""Adapter from submitted MCQ responses to frozen variant-D KT batches.

The adapter deliberately refuses missing content/selected-answer embeddings;
falling back to embedding row 0 would silently attach the wrong text to a
student's previous response.
"""
import math
from typing import Any

HALF_LENGTH = 20
FULL_LENGTH = 40
UNKNOWN_ITEM_INDEX = 1


def coverage_report(bank: dict, kt: Any) -> dict:
    """Check every field needed for arbitrary live MCQ selections."""
    text_to_row = kt.dataset.text_to_row
    missing_skills = []
    missing_items = []
    missing_content = []
    missing_options = []
    for index, question in enumerate(bank.get("questions", [])):
        if question.get("skill_id") not in kt.skill_vocab:
            missing_skills.append(index)
        if question.get("item_id") not in kt.item_vocab:
            missing_items.append(question.get("item_id"))
        if question.get("content_text") not in text_to_row:
            missing_content.append(index)
        missing_options.extend(
            option for option in question.get("options", [])
            if option not in text_to_row
        )
    return {
        "question_count": len(bank.get("questions", [])),
        "missing_skill_indexes": missing_skills,
        "unknown_item_ids": sorted(set(missing_items)),
        "missing_content_indexes": missing_content,
        "missing_option_values": sorted(set(missing_options)),
    }


def response_events(bank: dict, responses: list[dict]) -> list[dict]:
    """Derive private event fields from ordered student submission rows."""
    questions = bank.get("questions", [])
    if len(responses) not in (HALF_LENGTH, FULL_LENGTH):
        raise ValueError("KT traces are supported only at 20 or 40 responses")
    events = []
    for position, (row, question) in enumerate(zip(responses, questions), 1):
        if (not isinstance(row, dict)
                or set(row) != {"question_id", "selected_index"}):
            raise ValueError(f"responses[{position - 1}] is malformed")
        if row["question_id"] != question["question_id"]:
            raise ValueError("responses must follow the bank's question order")
        selected = row["selected_index"]
        if (not isinstance(selected, int) or isinstance(selected, bool)
                or selected not in range(len(question["options"]))):
            raise ValueError(f"responses[{position - 1}].selected_index is invalid")
        events.append({
            "position": position,
            "question": question,
            "selected_index": selected,
            "selected_text": question["options"][selected],
            "correct": selected == question["answer_index"],
            "response_time_ms": None,
            "attempt": 1,
            "time_bin": 0,
        })
    return events


def build_batch(bank: dict, events: list[dict], kt: Any) -> dict:
    """Build the exact tensor fields consumed by frozen variant D."""
    import torch

    text_to_row = kt.dataset.text_to_row
    skill_idx, item_idx, content_idx, selected_idx = [], [], [], []
    correct, rt, rt_mask, attempt, time_bins = [], [], [], [], []
    for event in events:
        question = event["question"]
        skill_idx.append(kt.skill_vocab[question["skill_id"]])
        item_idx.append(kt.item_vocab.get(question["item_id"], UNKNOWN_ITEM_INDEX))
        content_idx.append(text_to_row[question["content_text"]])
        selected_idx.append(text_to_row[event["selected_text"]])
        correct.append(float(event["correct"]))
        response_ms = event.get("response_time_ms")
        if isinstance(response_ms, (int, float)) and response_ms >= 0:
            rt.append(math.log1p(float(response_ms)))
            rt_mask.append(1.0)
        else:
            rt.append(0.0)
            rt_mask.append(0.0)
        attempt.append(float(event.get("attempt") or 1.0))
        time_bins.append(int(event.get("time_bin") or 0))
    return {
        "skill": torch.tensor([skill_idx], dtype=torch.long),
        "item": torch.tensor([item_idx], dtype=torch.long),
        "correct": torch.tensor([correct], dtype=torch.float),
        "rt": torch.tensor([rt], dtype=torch.float),
        "rt_mask": torch.tensor([rt_mask], dtype=torch.float),
        "attempt": torch.tensor([attempt], dtype=torch.float),
        "content_idx": torch.tensor([content_idx], dtype=torch.long),
        "selected_text_idx": torch.tensor([selected_idx], dtype=torch.long),
        "time_bin_ids": torch.tensor([time_bins], dtype=torch.long),
        "attn_mask": torch.ones((1, len(events)), dtype=torch.float),
    }


def trace_responses(bank: dict, responses: list[dict], kt: Any = None,
                    device: str = "cpu", history_responses: list[dict] | None = None) -> dict:
    """Return pre-answer probabilities with optional repeated-bank research history."""
    if kt is None:
        from frozen_model import load_frozen_model
        kt = load_frozen_model(device=device)
    coverage = coverage_report(bank, kt)
    if (coverage["missing_skill_indexes"] or coverage["missing_content_indexes"]
            or coverage["missing_option_values"]):
        raise ValueError(f"bank is not KT-text-compatible: {coverage}")
    events = response_events(bank, responses)
    if history_responses is not None:
        if not isinstance(history_responses, list) or len(history_responses) != HALF_LENGTH:
            raise ValueError("research history must be 20 ordered bank responses")
        events = response_events(bank, history_responses) + events
    max_len = getattr(kt, "config", {}).get("max_seq_len")
    if max_len is not None and len(events) > max_len:
        raise ValueError("research history exceeds frozen model sequence length")
    batch = build_batch(bank, events, kt)
    probabilities = kt.probs(batch)
    if hasattr(probabilities, "tolist"):
        probabilities = probabilities.tolist()
    probabilities = probabilities[0][-len(responses):]
    return {
        "kind": "frozen_variant_d_response_trace",
        "model_status": "uncalibrated_for_20_40_question_feed",
        "history_length": len(events) - len(responses),
        "p_correct_before_each_answer": [round(float(p), 6)
                                         for p in probabilities],
        "coverage": coverage,
        "input_policy": {
            "content_text": "required and embedded per question",
            "selected_text": "required for every selectable option and used only after submission",
            "response_time": "rt_mask=0 unless a measured response_time_ms is supplied",
            "time_bin": "0 until real inter-answer timestamps are captured",
            "unknown_item_ids": "mapped to the trained __UNK__ item embedding",
        },
    }
