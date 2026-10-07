# Research content graphs (drafts pending educator review)

`research_content_graph.py` validates and renders the frozen bounded content
graphs for offline educator reading. It is presentation and structural
checking only; mapping, description and annotation content are author-owned
frozen artifacts.

## Captures

- **v3 (default):** `artifacts/bounded_content_graph_v3_20261007/` - the
  descriptive-task graph with the reviewed 24-question practice pool. It is
  authored by `research_content_descriptions_v3.py`, which transforms the
  frozen v2 graph and replaces only the 20-question `practice_pool` source
  with `practice_pool_v2`. The pool now has six exercises per broad skill;
  four divisibility exercises are synthetic, user-review-accepted, pending
  formal educator review, and not historical student content.
- **v2 (preserved):** `artifacts/bounded_content_graph_v2_20261006/` - the
  descriptive-task update authored by `research_content_descriptions.py`.
  Exercises gain a format, a plain-language mathematical task and
  task-requirement notes. Annotations are 7 proposed
  procedural/conceptual support links (not strict prerequisites or a
  learning order) plus 4 cautious option interpretations ("consistent with",
  cannot infer cause). Root metadata records automated, user-supplied
  Gemini review input - not independent educator validation; no model
  version is known and no LLM authority is granted or expanded.
- **v1 (preserved):** `artifacts/bounded_content_graph_20261006/` - the
  original hypothesis graph authored by `research_content_catalog.py`,
  including its rendered `index.html`, kept byte-for-byte unchanged. Render
  it again with `--capture artifacts/bounded_content_graph_20261006`.

## Exact source scope

The preserved v1 and v2 graphs bind to the frozen sources in
`artifacts/shadow_smoke_20261006/` (40 warm, 40 cold and 20 practice
questions, via `research_content_catalog.SOURCE_HASHES`). The v3 graph binds
the same warm and cold banks plus
`artifacts/synthetic_practice_supplement_20261006/practice_pool_v2_private.json`
(24 practice questions). Its manifest records each source path and SHA-256.
Embedded question records are verified equal to those sources. They contain
no responses, no KT output, and no student data.

## Boundaries

- Offline research only; `used_for_student_advice` is false and the loader
  refuses to render on any hash, binding, record or pending-review-state
  violation.
- Descriptive content map, not a mastery or misconception diagnosis.
  Task requirements are not separately measured student skills; an
  incorrect choice does not establish which step was difficult or why.
- Supporting concepts are **not assessed**; skill/topic headings are broader
  than the actual exercises (e.g. fraction division is not necessarily
  assessed).
- All annotations are illustrative and not exhaustive; none are used for
  routing, student claims, or approval. Support links are hypotheses, not a
  validated teaching sequence.
- The synthetic session runtime reads the frozen v3 capture through
  `research_runtime.py`. Only assessed concepts form warm/cold taxonomies;
  teacher-only context uses their task descriptions and the 24-question
  practice source. Proposed support links and possible-error annotations do
  not enter session routing, student payloads or provider context.
- Formal approval, multi-question practice delivery and teacher release
  remain pending. Teacher review does not approve learner delivery. Active
  conformal computation/display has been removed; historical evidence remains
  on disk.
- Educator review of the mapping is the next step; zero formal educator
  reviews are complete. User review accepted the four synthetic questions
  for research use, but that is not independent educator validation.

## View

The rendered v3 page already exists; open it locally (no network
resources):

```text
artifacts/bounded_content_graph_v3_20261007/index.html
```

To re-render after an approved future capture change, pass a NEW output
path - the renderer refuses to overwrite any existing file:

```bash
python research_content_graph.py --output artifacts/bounded_content_graph_v3_20261007/index_v4.html
```

Running without `--output` stops with a refusal because the default
`index.html` is present.
