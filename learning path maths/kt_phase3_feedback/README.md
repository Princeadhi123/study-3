# Phase 3 — end-to-end MCQ feedback prototype

Phase 3 is the integration layer for the 40-question diagnostic test. It uses
Phase 2's approved private bank and deterministic scorer, then optionally adds
a cautious frozen variant-D KT estimate. It does **not** retrain or modify the
model, and it does not copy answer keys or model artifacts into this folder.

## Boundary

- Phase 1: source data and model training — closed.
- Phase 2: private inventory, text-only bank construction, approval, scoring,
  observed feed, and frozen-model loader.
- Phase 3: test sessions, ordered response capture, private event storage,
  KT input adaptation, feedback composition, and provenance.

Current bank:

```text
../kt_phase2_inference/artifacts/test_question_bank_text_only_approved_v2.json
```

That file contains private answer keys. Phase 3 references it; it must not be
copied into student payloads or committed to a public artifact store.

## Files

| file | role |
|---|---|
| `phase3_paths.py` | Phase 2/artifact locations; named to avoid a `paths.py` collision |
| `schemas.py` | public question and submitted-response contracts |
| `session_store.py` | private JSON session records under ignored `artifacts/sessions/` |
| `mcq_service.py` | starts sessions, serves halves, accepts ordered responses, checkpoints at 20/40 |
| `kt_adapter.py` | validates text coverage and converts submissions to variant-D tensors |
| `feedback_service.py` | returns observed feed plus optional, separate `model_estimate` |
| `provenance.py` | records artifact identities/hashes for reproducibility |
| `api.py` | localhost-only JSON API for sessions and researcher diagnostics |
| `demo_cli.py` | scripted local end-to-end run |
| `tests/` | synthetic-bank unit tests; no private artifacts required |

## Public and private data

Student-facing payloads contain only:

```json
{"question_id": "...", "skill_id": "...", "text": "...", "options": ["..."]}
```

Submitted response rows contain only:

```json
{"question_id": "...", "selected_index": 0}
```

The private session record additionally stores `selected_text`, `correct`,
timestamps, and checkpoint feeds. `artifacts/` is ignored because those records
may contain private answer/scoring data.

## Session lifecycle

```python
from mcq_service import MCQSessionService

service = MCQSessionService.from_default()
start = service.start_session()
session_id = start["session_id"]

# Service can accept one response at a time:
service.submit_response(session_id, {
    "question_id": "...",
    "selected_index": 0,
})

# Or a trusted caller can submit a whole ordered 20-question half:
midpoint = service.submit_half(session_id, 1, first_half_rows)
end = service.submit_half(session_id, 2, second_half_rows)
```

Rules enforced by `MCQSessionService` and Phase 2's `mcq_test.py`:

- exactly 40 approved-bank questions;
- 20 responses before midpoint and 40 before end;
- ordered question IDs;
- strict integer selected indices;
- no per-answer correctness returned during the test;
- checkpoint feed only at 20 or 40 responses;
- session is bound to the bank hash, so a changed bank cannot score old answers.

## Local JSON API

From this directory:

```bash
python api.py --host 127.0.0.1 --port 8765
```

The server deliberately binds only to localhost. Student routes return public
question fields and the `student` portion of checkpoint feedback only:

```text
POST /sessions
GET  /sessions/{session_id}/questions?half=1
GET  /sessions/{session_id}/questions?half=2
POST /sessions/{session_id}/responses
POST /sessions/{session_id}/half-submissions
GET  /sessions/{session_id}/snapshot
```

Research routes are separate and require `X-Phase3-Role: researcher`:

```text
GET  /research/sessions/{session_id}/diagnostics
POST /research/sessions/{session_id}/kt-estimate   # body: {"device": "cpu"}
```

The header keeps the route contracts separate for the local prototype; it is
**not** production authentication. Do not expose this server beyond localhost
without a real authentication/authorization layer.

## KT adapter contract

`kt_adapter.trace_responses(bank, responses)` accepts the same ordered response
rows as `score_checkpoint`, then derives private event fields from the bank:

- `skill_id` — must be in the frozen skill vocabulary;
- `item_id` — unknown IDs map to `__UNK__`;
- `content_text` — must exist in the frozen embedding table;
- `selected_text` — derived from `options[selected_index]`; every selectable
  option must exist in the embedding table;
- `correct` — derived privately from `answer_index`;
- `response_time_ms` / `rt_mask` — currently `None`/`0` unless real timing is
  added;
- `attempt` — currently 1 for this single-attempt test;
- `time_bin` — currently 0 until real inter-answer timestamps are captured.

The adapter refuses missing content or option embeddings. The model's selected
text is part of the previous-step interaction only; it is never used as the
current-step query.

KT output is returned separately as:

```json
{
  "kind": "frozen_variant_d_response_trace",
  "model_status": "uncalibrated_for_20_40_question_feed",
  "p_correct_before_each_answer": [0.0]
}
```

It is not correctness evidence, not calibrated mastery, and not a
recommendation.

## Local scripted run

From this directory:

```bash
python demo_cli.py --profile alternating --out artifacts/demo_session_result.json
python demo_cli.py --profile all_correct --kt-device cpu \
  --out artifacts/demo_all_correct_kt_result.json \
  --manifest-out artifacts/provenance_manifest.json
```

Both commands refuse to overwrite outputs. Session JSON files remain private
under `artifacts/sessions/`.

## Tests

```bash
python -m unittest discover -s tests -v
```

Most tests use a synthetic approved-shaped bank. They do not load the private
40-question artifact or the 2 GB embedding table. `test_real_bank_contract.py`
is an optional local integration test: when the approved v2 bank exists it
verifies the public payload and private scoring path, and it skips otherwise.

## Remaining work

1. Build a frontend against the localhost API, or replace `api.py` with the
   deployment framework's authenticated route layer.
2. Capture real per-answer timestamps/response durations if timing will be
   exposed to KT; otherwise keep `rt_mask=0` and disclose it.
3. Decide whether each test starts cold or resumes prior student history.
4. Evaluate scripted and human trajectories per skill; do not rely only on the
   model's aggregate AUC.
5. Keep model estimates visually and contractually separate from observed
   feedback until calibration for the 20/40-question setting is established.
