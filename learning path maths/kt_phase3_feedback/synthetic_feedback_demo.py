"""Print private synthetic feedback review packages.

The fixtures below are synthetic literals for local review only; they are
not measured results from any student. Default mode uses the rules
baseline and fixed deterministic wording. ``--mock`` exercises the
injected-callback path with local mock objects only -- they are not real
models, make no network calls, and produce no real provider metadata.
``--jev`` is an explicit opt-in that routes candidate selection through
the hosted TypeSafe Jev API (TYPESAFE_API_KEY required; two private
synthetic requests for the end checkpoints, while the single-candidate
midpoint is resolved locally without a call). ``--aitta`` is an explicit
opt-in that routes opening phrasing through the hosted Aitta
OpenAI-compatible endpoint (AITTA_API_KEY/AITTA_BASE_URL required; three
private synthetic requests, one per package). ``--jev`` and ``--aitta``
may be combined; ``--mock`` cannot be combined with either hosted flag.
Hosted flags are user opt-in only; configure credentials outside the
project tree before any hosted run. Output is JSON on stdout; no
artifacts are written or overwritten.
"""
import argparse
import copy
import json
from pathlib import Path

from aitta_generator import AittaGenerator
from jev_selector import SELECTION_PROMPT_VERSION, JevSelector
from synthetic_feedback import PROMPT_VERSION, run_synthetic_feedback

END_SKILLS = [
    {"skill_id": "skill_a", "skill_name": "Arithmetic",
     "correct": 9, "out_of": 10},
    {"skill_id": "skill_b", "skill_name": "Prices",
     "correct": 5, "out_of": 10},
    {"skill_id": "skill_c", "skill_name": "Fractions",
     "correct": 7, "out_of": 10},
    {"skill_id": "skill_d", "skill_name": "Percentages",
     "correct": 6, "out_of": 10},
]
MIDPOINT_SKILLS = [
    {"skill_id": "skill_a", "skill_name": "Arithmetic",
     "correct": 4, "out_of": 5},
    {"skill_id": "skill_b", "skill_name": "Prices",
     "correct": 2, "out_of": 5},
    {"skill_id": "skill_c", "skill_name": "Fractions",
     "correct": 3, "out_of": 5},
    {"skill_id": "skill_d", "skill_name": "Percentages",
     "correct": 3, "out_of": 5},
]


def make_input(audience: str, checkpoint: str,
               skills: list[dict]) -> dict:
    return {"schema": "phase3_synthetic_feedback_input_v1",
            "data_origin": "synthetic",
            "audience": audience,
            "checkpoint": checkpoint,
            "skills": [dict(row) for row in skills]}


class MockSelector:
    """Local test double for the selector seam; not a real provider."""

    def select(self, payload: dict) -> dict:
        return {"candidate_id": payload["candidates"][0]["candidate_id"]}


class MockGenerator:
    """Local test double for the generator seam; not a real model."""

    def generate(self, payload: dict) -> dict:
        opening = ("Keep going when you feel ready."
                   if payload["checkpoint"] == "midpoint"
                   else "Thank you for completing this assessment.")
        return {"candidate_id": payload["selected_candidate"]
                ["candidate_id"],
                "opening": opening}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mock", action="store_true",
        help="route selection/phrasing through local mock callbacks "
             "(not real models, no network calls; cannot be combined "
             "with --jev or --aitta)")
    parser.add_argument(
        "--jev", action="store_true",
        help="route candidate selection through the hosted TypeSafe Jev "
             "API using TYPESAFE_API_KEY; makes two private synthetic "
             "requests (the end checkpoints only -- the single-candidate "
             "midpoint performs no call)")
    parser.add_argument(
        "--jev-env-file", type=Path, metavar="PATH",
        help="optional private Jev key file outside the project; "
             "only valid together with --jev")
    parser.add_argument(
        "--aitta", action="store_true",
        help="route opening phrasing through the hosted Aitta "
             "OpenAI-compatible endpoint using AITTA_API_KEY and "
             "AITTA_BASE_URL; makes three private synthetic requests "
             "(one per package)")
    args = parser.parse_args()
    if args.mock and (args.jev or args.aitta):
        parser.error("--mock cannot be combined with --jev or --aitta")
    if args.jev_env_file is not None and not args.jev:
        parser.error("--jev-env-file requires --jev")
    selector = None
    generator = None
    if args.mock:
        selector = MockSelector()
        generator = MockGenerator()
    if args.jev:
        try:
            if args.jev_env_file is not None:
                selector = JevSelector.from_env(env_path=args.jev_env_file)
            else:
                selector = JevSelector.from_env()
        except Exception:
            parser.error(
                "TYPESAFE_API_KEY is unavailable; set it in the "
                "environment or a private key file outside the project")
    if args.aitta:
        try:
            generator = AittaGenerator.from_env()
        except Exception:
            parser.error(
                "AITTA_API_KEY/AITTA_BASE_URL are unavailable; set them "
                "in the environment or the repository-root .env file")
    if args.mock:
        mode = "mock_callbacks"
    elif args.jev and args.aitta:
        mode = "jev_and_aitta"
    elif args.jev:
        mode = "jev_selector"
    elif args.aitta:
        mode = "aitta_generator"
    else:
        mode = "deterministic"
    cases = [("student", "midpoint", MIDPOINT_SKILLS),
             ("student", "end", END_SKILLS),
             ("teacher", "end", END_SKILLS)]
    packages = []
    for audience, checkpoint, skills in cases:
        result = run_synthetic_feedback(
            make_input(audience, checkpoint, skills),
            selector=selector, generator=generator)
        package = {"case": f"{audience}_{checkpoint}",
                   "review": result}
        if args.jev:
            package["selector_metadata"] = {
                "prompt_version": SELECTION_PROMPT_VERSION,
                **copy.deepcopy(selector.last_metadata)}
        if args.aitta:
            if result["trace"]["fallback_reason"] in (
                    "selector_error", "invalid_selection"):
                package["generator_metadata"] = {
                    "prompt_version": PROMPT_VERSION,
                    "status": "skipped_selector_failure",
                    "model_version": None, "usage": None}
            else:
                package["generator_metadata"] = {
                    "prompt_version": PROMPT_VERSION,
                    **copy.deepcopy(generator.last_metadata)}
        packages.append(package)
    print(json.dumps({
        "protocol": "phase3_synthetic_feedback_demo",
        "mode": mode,
        "note": ("synthetic fixtures, not measured student results; "
                 "all drafts require human review"),
        "packages": packages,
    }, indent=2))


if __name__ == "__main__":
    main()
