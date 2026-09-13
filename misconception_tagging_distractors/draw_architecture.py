"""Draws misconception_pipeline.png: the end-to-end architecture of the
misconception tagging pipeline (build_v2_item_context.py ->
tag_math_distractors.py stages elicit/cluster/verify/export -> join-back ->
future KT variant E). Regenerate with:  python draw_architecture.py
"""
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
from pathlib import Path

OUT = Path(__file__).parent / "misconception_pipeline.png"

C_INPUT = "#dbeafe"   # light blue  - data files
C_STAGE = "#fef3c7"   # light amber - pipeline stages
C_LLM = "#fde68a"     # amber       - LLM-touching steps
C_LOCAL = "#d1fae5"   # light green - local/free steps
C_DECIDE = "#fecaca"  # light red   - triage decision
C_OUT = "#e0e7ff"     # light indigo- outputs
C_FUTURE = "#f3e8ff"  # lavender    - future work

fig, ax = plt.subplots(figsize=(13, 19))
ax.set_xlim(0, 130)
ax.set_ylim(0, 190)
ax.axis("off")


def box(x, y, w, h, title, body, color, title_size=10, body_size=8):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.4",
                                fc=color, ec="#334155", lw=1.2))
    ax.text(x + w / 2, y + h - 2.2, title, ha="center", va="top",
            fontsize=title_size, fontweight="bold", color="#0f172a")
    if body:
        ax.text(x + w / 2, y + h - 5.4, body, ha="center", va="top",
                fontsize=body_size, color="#334155", linespacing=1.45)


def arrow(x1, y1, x2, y2, label="", color="#334155", dashed=False):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>",
                                 mutation_scale=16, lw=1.4, color=color,
                                 linestyle="--" if dashed else "-",
                                 shrinkA=2, shrinkB=2))
    if label:
        ax.text((x1 + x2) / 2 + 2, (y1 + y2) / 2, label, fontsize=7.5,
                color="#64748b", style="italic", va="center")


# ---------------- title ----------------
ax.text(65, 187, "Misconception Tagging Pipeline — Architecture",
        ha="center", fontsize=16, fontweight="bold", color="#0f172a")

# ---------------- inputs ----------------
box(5, 172, 55, 11, "INPUT A — student interaction log",
    "kt_interactions_v2_item_level.csv.gz\n13.9M rows: every student answer, every exercise type\n"
    "(built by build_v2_item_level.py, stage 3)", C_INPUT)
box(70, 172, 55, 11, "INPUT B — distractor catalog",
    "distractor_catalog.csv\n~153K unique wrong options per item\n"
    "(item_id, option_value, times_selected, ...)", C_INPUT)

# ---------------- stage 1 ----------------
box(5, 152, 55, 13, "STAGE 1 — context gathering (local, no LLM)",
    "build_v2_item_context.py\n"
    "one streaming pass over the interaction log,\n"
    "keeps most-common (question text, correct answer) per item_id\n"
    "→ reports_v2/item_context.csv", C_LOCAL)

# ---------------- stage 2 elicit ----------------
box(5, 108, 55, 38, "STAGE 2a — elicit  (LLM calls, checkpointed)",
    "for each unique (item_id, wrong option):\n\n"
    "  • exercise types processed one at a time,\n"
    "    most-impactful type first\n"
    "  • within a type: highest times_selected first\n"
    "  • LLM sees: question text + correct answer\n"
    "    + student's wrong answer\n"
    "  • LLM returns a SHORT free-text description\n"
    "    of the likely error\n"
    "    e.g. \"uses a linear unit instead of an area unit\"\n\n"
    "every result flushed to elicit.jsonl —\na killed job resumes, never re-pays for calls", C_LLM)

# ---------------- stage 2 cluster ----------------
box(5, 82, 55, 20, "STAGE 2b — cluster  (local, free, re-runnable)",
    "embed every elicited description with a local\n"
    "sentence-transformer (all-mpnet-base-v2, no API)\n\n"
    "greedy clustering by cosine similarity:\n"
    "each cluster = one discovered misconception\n"
    "  id    = MC_00001, MC_00002, ...\n"
    "  label = member closest to the cluster centroid", C_LOCAL)

# ---------------- stage 3 ----------------
box(5, 54, 55, 22, "STAGE 3 — confidence triage",
    "a row is flagged UNCERTAIN if ANY of:\n"
    "  • confidence_margin < 0.08\n"
    "    (assigned cluster barely beat next-nearest)\n"
    "  • cluster_size <= 2   (tiny cluster)\n"
    "  • question text was missing", C_DECIDE)
box(70, 56, 55, 20, "verify pass (LLM critique — flagged rows only)",
    "shown assigned label + an alternative label,\n"
    "asked to argue for the alternative first, then\n"
    "verdict: CONFIRM or REPLACE: <better phrase>\n\n"
    "--verify-model: run this on a DIFFERENT Aitta model\n"
    "than stage 2a's, to decorrelate errors (same-model\n"
    "verify measured 0/78 catches on Eedi)\n\n"
    "verdict recorded for review, never silently overwrites", C_LLM, body_size=7.3)

# ---------------- stage 4 ----------------
box(5, 28, 55, 20, "STAGE 4 — export",
    "distractor_catalog_tagged.csv\n"
    "  original catalog + misconception_id/label,\n"
    "  confidence, needs_human_review flag\n\n"
    "cluster_summary.csv — discovered taxonomy,\n"
    "  ranked by total student impact", C_OUT)
box(70, 28, 55, 16, "HUMAN REVIEW",
    "human_review_queue.csv — flagged rows only,\n"
    "sorted by times_selected (highest impact first)\n"
    "you check/correct these one by one", C_OUT)

# ---------------- future ----------------
box(5, 5, 120, 17, "AFTER TAGGING — misconception-aware KT (variant E)",
    "join tagged catalog back onto the interaction log by (item_id, selected_option_value)\n"
    "→ every MCQ attempt gets a misconception_id\n"
    "→ model.py already has the slot: variant D's selected-answer TEXT embedding\n"
    "   is swapped for misconception_embed(prev_misconception_id)\n"
    "→ new question for the model: not just \"will they get it right?\" but \"WHICH error are they about to make?\"", C_FUTURE)

# ---------------- arrows ----------------
arrow(32.5, 172, 32.5, 165.9)                        # input A -> stage 1
arrow(97.5, 172, 97.5, 149, "exercise_type +\nwrong option list")  # input B down
arrow(97.5, 149, 60.5, 132)                          # input B -> stage 2a
arrow(32.5, 152, 32.5, 146.9)                        # stage 1 -> stage 2a
ax.text(63, 151, "item_context.csv joins in:\nquestion text + correct answer",
        fontsize=7.5, color="#64748b", style="italic", va="top")
arrow(32.5, 108, 32.5, 102.9)                        # 2a -> 2b
arrow(32.5, 82, 32.5, 76.9)                          # 2b -> 3
arrow(60.5, 64, 70, 64, "flagged rows only")         # 3 -> verify
arrow(97.5, 56, 97.5, 49, "verdict recorded", dashed=True)
arrow(97.5, 49, 60.5, 44)                            # verify -> export
arrow(32.5, 54, 32.5, 48.9, "confident rows")        # 3 -> export direct
arrow(60.5, 36, 70, 36, "flagged +\nsuggestions")    # export -> human review
arrow(32.5, 28, 32.5, 22.9)                          # export -> future
arrow(97.5, 28, 97.5, 22.9, "corrected labels\nfed back", dashed=True)

ax.text(65, 1, "everything in amber = Aitta LLM calls (paid, checkpointed) · green = local compute (free to re-run)",
        ha="center", fontsize=8, color="#64748b", style="italic")

fig.savefig(OUT, dpi=160, bbox_inches="tight", facecolor="white")
print(f"wrote {OUT}")
