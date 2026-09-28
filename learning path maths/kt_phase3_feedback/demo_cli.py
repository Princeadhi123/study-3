"""Run one scripted end-to-end Phase 3 MCQ session locally.

The output is a private demo artifact: feeds contain observed counts and, if
requested, an uncalibrated frozen-KT trace. Scripted answers are not student
evidence.
"""
import argparse
import json
from pathlib import Path

import phase3_paths
from feedback_service import checkpoint_result
from mcq_service import MCQSessionService
from provenance import write_manifest
from session_store import SessionStore


def scripted_index(question: dict, skill_attempt: int, profile: str) -> int:
    answer = question["answer_index"]
    option_count = len(question["options"])
    if profile == "all_correct":
        return answer
    if profile == "alternating":
        return answer if skill_attempt % 2 == 0 else (answer + 1) % option_count
    if profile == "all_incorrect":
        return (answer + 1) % option_count
    raise ValueError(f"unknown scripted profile {profile!r}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("all_correct", "alternating", "all_incorrect"),
                        default="alternating")
    parser.add_argument("--kt-device", choices=("cpu", "cuda"),
                        help="Run the frozen variant-D trace after the scripted end checkpoint")
    parser.add_argument("--out", type=Path,
                        default=phase3_paths.ARTIFACTS / "demo_session_result.json")
    parser.add_argument("--manifest-out", type=Path,
                        help="Optional Phase 3 provenance manifest path")
    args = parser.parse_args()
    if args.out.exists() or (args.manifest_out and args.manifest_out.exists()):
        raise FileExistsError("Refusing to overwrite an existing Phase 3 output")

    service = MCQSessionService.from_default(
        SessionStore(phase3_paths.SESSIONS))
    session = service.start_session()
    session_id = session["session_id"]
    responses = []
    midpoint = None
    end = None
    skill_attempts = {skill: 0 for skill in service.bank["skill_names"]}
    for half in (1, 2):
        start = (half - 1) * 20
        rows = []
        for question in service.bank["questions"][start:start + 20]:
            skill = question["skill_id"]
            rows.append({
                "question_id": question["question_id"],
                "selected_index": scripted_index(
                    question, skill_attempts[skill], args.profile),
            })
            skill_attempts[skill] += 1
        result = service.submit_half(session_id, half, rows)
        responses.extend(rows)
        if half == 1:
            midpoint = result["feed"]
        else:
            end = result["feed"]

    model_estimate = {"status": "not_requested"}
    if args.kt_device:
        model_estimate = checkpoint_result(
            service.bank, responses, include_kt=True,
            device=args.kt_device)["model_estimate"]

    output = {
        "protocol": "phase3_scripted_session_demo",
        "profile": args.profile,
        "session": service.snapshot(session_id),
        "midpoint": midpoint,
        "end": end,
        "model_estimate": model_estimate,
        "limitations": [
            "scripted answers are not student evidence",
            "KT output is uncalibrated for the 20/40-question feed",
            "no recommendation or mastery claim is made",
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(output, stream, ensure_ascii=False, indent=2)
    if args.manifest_out:
        write_manifest(args.manifest_out)
    print(f"Wrote scripted Phase 3 demo to {args.out}")


if __name__ == "__main__":
    main()
