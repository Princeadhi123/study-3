"""Offline 20/40 feedback demo, optionally with an illustrative frozen-KT trace.

Scripted answers are not real students or evidence of predictive quality. The
optional KT trace assumes a cold start and no measured response times; never
use its probabilities as calibrated feed claims or an evaluation of feedback.
"""
import argparse
import json
from pathlib import Path

from test_feed import build_test_feed

import paths


def simulate(bank: dict, responses: dict | None = None) -> dict:
    if bank.get("protocol") != "offline_mcq_demo_only":
        raise ValueError("Expected an offline MCQ demonstration bank")
    questions = bank.get("questions")
    names = bank.get("skill_names")
    if not isinstance(questions, list) or len(questions) != 40 or not isinstance(names, dict):
        raise ValueError("Bank must contain 40 questions and skill_names")
    question_ids = [q["question_id"] for q in questions]
    if len(set(question_ids)) != 40:
        raise ValueError("Bank question IDs must be distinct")
    if responses is not None and (not isinstance(responses, dict)
                                  or set(responses) != set(question_ids)):
        raise ValueError("Responses must map every bank question ID exactly once")
    attempts = {skill: 0 for skill in names}
    answers = []
    selected = []
    for question in questions:
        skill = question["skill_id"]
        if skill not in attempts:
            raise ValueError(f"Unrecognized skill {skill!r}")
        options = question["options"]
        answer_index = question["answer_index"]
        if (not isinstance(options, list) or len(options) < 2
                or any(not isinstance(value, str) or not value.strip() for value in options)
                or len(set(options)) != len(options)
                or not isinstance(answer_index, int) or isinstance(answer_index, bool)
                or answer_index not in range(len(options))):
            raise ValueError("Invalid bank answer index or options")
        if responses is None:
            index = (answer_index if attempts[skill] % 2 == 0
                     else next(i for i in range(len(options)) if i != answer_index))
        else:
            index = responses[question["question_id"]]
            if not isinstance(index, int) or isinstance(index, bool) or index not in range(len(options)):
                raise ValueError(f"Invalid selected option for {question['question_id']}")
        selected.append(options[index])
        answers.append({"question_id": question["question_id"], "skill_id": skill,
                        "correct": index == answer_index})
        attempts[skill] += 1
    return {
        "protocol": "offline_scripted_demo" if responses is None else "offline_supplied_responses",
        "question_review_status": bank.get("review_status", "unreviewed"),
        "assumptions": ("Even-numbered attempts per skill choose the answer key; odd-numbered "
                        "attempts choose the first wrong option. Scripted results are not student evidence."
                        if responses is None else "Supplied choices are not proof of model or feed validity."),
        "midpoint": build_test_feed(answers[:20], names),
        "end": build_test_feed(answers, names),
        "_selected_text": selected,
    }


def illustrative_kt_trace(questions: list[dict], selected_text: list[str], device: str) -> dict:
    import torch
    from frozen_model import load_frozen_model

    kt = load_frozen_model(device=device)
    if len(questions) != 40 or len(selected_text) != 40:
        raise ValueError("The illustrative trace requires exactly 40 questions and selections")
    text_to_row = kt.dataset.text_to_row
    skill_idx, item_idx, content_idx, selected_idx = [], [], [], []
    for question, selected in zip(questions, selected_text):
        if question["skill_id"] not in kt.skill_vocab:
            raise ValueError(f"Skill not in frozen model: {question['skill_id']}")
        content = question["content_text"]
        if content not in text_to_row or selected not in text_to_row:
            raise ValueError("Question content or scripted selection is absent from the frozen "
                             "embedding table; refusing row-0 fallback. Use compatible "
                             "embeddings or omit --kt-device.")
        skill_idx.append(kt.skill_vocab[question["skill_id"]])
        item_idx.append(kt.item_vocab.get(question["item_id"], 1))
        content_idx.append(text_to_row[content])
        selected_idx.append(text_to_row[selected])
    batch = {
        "skill": torch.tensor([skill_idx], dtype=torch.long),
        "item": torch.tensor([item_idx], dtype=torch.long),
        "correct": torch.tensor([[int(q["correct"]) for q in questions]], dtype=torch.float),
        "rt": torch.zeros((1, 40), dtype=torch.float),
        "rt_mask": torch.zeros((1, 40), dtype=torch.float),
        "attempt": torch.ones((1, 40), dtype=torch.float),
        "content_idx": torch.tensor([content_idx], dtype=torch.long),
        "selected_text_idx": torch.tensor([selected_idx], dtype=torch.long),
        "time_bin_ids": torch.zeros((1, 40), dtype=torch.long),
        "attn_mask": torch.ones((1, 40), dtype=torch.float),
    }
    probabilities = kt.probs(batch)[0].cpu().tolist()
    return {
        "kind": "illustrative_cold_start_scenario_only",
        "p_correct_before_each_scripted_answer": [round(p, 6) for p in probabilities],
        "assumptions": "No prior student history; response times unavailable; time bins zero; "
                       "attempt numbers one; previous scripted correctness and selected text "
                       "are supplied only after each question.",
        "not_calibrated_or_validated_for_the_20_40_feed": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank", type=Path, default=paths.ARTIFACTS / "test_question_bank.json")
    parser.add_argument("--responses", type=Path,
                        help="JSON mapping every question_id to a selected option index")
    parser.add_argument("--kt-device", choices=("cpu", "cuda"),
                        help="Also score scripted scenario with frozen D; illustrative only")
    parser.add_argument("--out", type=Path, default=paths.ARTIFACTS / "test_feed_demo.json")
    args = parser.parse_args()
    bank = json.loads(paths.require(args.bank).read_text(encoding="utf-8"))
    responses = (json.loads(paths.require(args.responses).read_text(encoding="utf-8"))
                 if args.responses else None)
    demo = simulate(bank, responses)
    selected = demo.pop("_selected_text")
    if args.kt_device:
        questions = [{**q, "correct": q["options"].index(value) == q["answer_index"]}
                     for q, value in zip(bank["questions"], selected)]
        demo["kt_trace"] = illustrative_kt_trace(questions, selected, args.kt_device)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(demo, stream, ensure_ascii=False, indent=2)
    print(f"Wrote {args.out} ({demo['protocol']}; no real student validation)")


if __name__ == "__main__":
    main()
