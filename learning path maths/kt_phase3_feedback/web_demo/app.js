/* Synthetic demo frontend — student + teacher pages share this file.
   All dynamic text is written via textContent; nothing from the wire is
   injected as HTML. */
"use strict";

/**
 * English display wording for the hosted demo.
 * Presentation-only translation layer: original bank text and JSON are
 * preserved for provenance; these constants only adjust visible labels.
 */
const ENGLISH_SKILL_NAMES = Object.freeze({
  Peruslaskutoimitukset: "Arithmetic",
  Hinta: "Prices",
  Murtoluvut: "Fractions",
  Prosenttilaskenta: "Percentages",
  Prosenttilaskuja: "Percentages",
  "Jaollisuus, tekijät, alkuluvut": "Divisibility, factors and primes",
  "Jaollisuus, tekij�t, alkuluvut": "Divisibility, factors and primes",
  "Samanmuotoisten termien yhdistäminen": "Combining like terms",
  "Samanmuotoisten termien yhdist�minen": "Combining like terms",
  "Murtolukujen kerto- ja jakolasku": "Fraction multiplication and division",
});

const ENGLISH_QUESTION_TEXT = Object.freeze({
  // Draft translations keyed by public prompt text, never private item IDs.
  "6-0\u22c5(0+9)":
    "Calculate: 6 - 0 \u00d7 (0 + 9).",
  "10 \u20ac - 2,5 \u20ac":
    "Calculate: EUR 10 - EUR 2.50.",
  "Suurin murtoluku?":
    "Which fraction is the largest?",
  "20 % luvusta 65":
    "What is 20% of 65?",
  "8*(7+1*1)":
    "Calculate: 8 \u00d7 (7 + 1 \u00d7 1).",
  "10 litraa mansikoita maksaa 25 euroa. Kuinka paljon yksi litra mansikoita maksaa?":
    "Ten litres of strawberries cost EUR 25. How much does one litre cost?",
  "3\u20446 \u00b7 6\u20445":
    "Calculate: 3/6 \u00d7 6/5.",
  "Kuinka paljon on 25 % luvusta 100?":
    "What is 25% of 100?",
  "7+3\u22c5(6+7)":
    "Calculate: 7 + 3 \u00d7 (6 + 7).",
  "250 gramman suklaapatukka maksaa 2,20 euroa. Mik\u00e4 on suklaapatukan kilohinta?":
    "A 250-gram chocolate bar costs EUR 2.20. What is its price per kilogram?",
  "1/7 * 11":
    "Calculate: 1/7 \u00d7 11.",
  "Mist\u00e4 raham\u00e4\u00e4r\u00e4st\u00e4 140 % on 42 \u20ac?":
    "140% of what amount of money is EUR 42?",
  "7*13+1*3":
    "Calculate: 7 \u00d7 13 + 1 \u00d7 3.",
  "Maria ostaa kolme litraa j\u00e4\u00e4tel\u00f6\u00e4. Kymmenen litraa j\u00e4\u00e4tel\u00f6\u00e4 maksaa 25 euroa. Kuinka monta euroa Marian ostama j\u00e4\u00e4tel\u00f6 maksaa?":
    "Maria buys three litres of ice cream. Ten litres cost EUR 25. How much does Maria's ice cream cost?",
  "2/4 + 2/4":
    "Calculate: 2/4 + 2/4.",
  "Mik\u00e4 on prosenttiluvun 25 % prosenttikerroin?":
    "Write 25% as a decimal multiplier.",
  "450 : 10":
    "Calculate: 450 \u00f7 10.",
  "8 litraa vaniljaj\u00e4\u00e4tel\u00f6\u00e4 maksaa 32 euroa. Kuinka paljon maksaa 9 litraa vaniljaj\u00e4\u00e4tel\u00f6\u00e4?":
    "Eight litres of vanilla ice cream cost EUR 32. How much do nine litres cost?",
  "4/10 : 2":
    "Calculate: 4/10 \u00f7 2.",
  "Tuotteen hintaa korotettiin 20 % eli 64 \u20ac. Mik\u00e4 oli alkuper\u00e4inen hinta?":
    "The price of a product increased by 20%, an increase of EUR 64. What was the original price?",
  "4+4\u22c510+10":
    "Calculate: 4 + 4 \u00d7 10 + 10.",
  "500 gramman karkkipussi maksaa 6,50 euroa. Kuinka paljon maksaa kilo karkkia?":
    "A 500-gram bag of sweets costs EUR 6.50. How much does one kilogram of sweets cost?",
  "1/3 + 1/3":
    "Calculate: 1/3 + 1/3.",
  "Mist\u00e4 luvusta 120 % on 60?":
    "120% of what number is 60?",
  "4\u00d7(2+2)\u00f74":
    "Calculate: 4 \u00d7 (2 + 2) \u00f7 4.",
  "Elsa ostaa 12 kappaletta haarukoita. 20 haarukkaa maksaa 40 euroa. Paljonko Elsan ostokset maksavat?":
    "Elsa buys 12 forks. Twenty forks cost EUR 40. How much do Elsa's forks cost?",
  "2\u20443 + 2\u20446":
    "Calculate: 2/3 + 2/6.",
  "Mist\u00e4 luvusta 10 % on luku 7?":
    "10% of what number is 7?",
  "2+6*(7+4)":
    "Calculate: 2 + 6 \u00d7 (7 + 4).",
  "Viisi kahvipakettia maksaa 20,25 euroa. Kuinka paljon maksaa yksi kahvipaketti?":
    "Five packets of coffee cost EUR 20.25. How much does one packet cost?",
  "3/4 + 1/4":
    "Calculate: 3/4 + 1/4.",
  "Mist\u00e4 luvusta 25 % on 5?":
    "25% of what number is 5?",
  "7+22+3*5":
    "Calculate: 7 + 22 + 3 \u00d7 5.",
  "2,5 desilitran limut\u00f6lkki maksaa 2,65 euroa. Mik\u00e4 on limun litrahinta?":
    "A 2.5-decilitre can of soft drink costs EUR 2.65. What is its price per litre?",
  "2\u20443 \u00b7 10\u20448":
    "Calculate: 2/3 \u00d7 10/8.",
  "Mist\u00e4 raham\u00e4\u00e4r\u00e4st\u00e4 95 % on 760 \u20ac?":
    "95% of what amount of money is EUR 760?",
  "110 : 10":
    "Calculate: 110 \u00f7 10.",
  "2 litraa limua maksaa 3 euroa. Puolen litran pullo samaa limua maksaa 1,50 euroa. Kuinka paljon kalliimpi litrahinta on puolen litran pullossa kuin kahden litra pullossa?":
    "Two litres of soft drink cost EUR 3. A half-litre bottle of the same drink costs EUR 1.50. How much higher is the price per litre in the half-litre bottle than in the two-litre bottle?",
  "1\u20442 + 1\u20444":
    "Calculate: 1/2 + 1/4.",
  "Montako prosenttia luku 4 on luvusta 200?":
    "What percentage of 200 is 4?",
  "Norjalaiset lohik\u00e4\u00e4rmeet harrastavat silmukoiden tekemist\u00e4 ilmassa. Nuoremmat lohik\u00e4\u00e4rmeet osaavat tehd\u00e4 per\u00e4kk\u00e4isi\u00e4 silmukoita luonnollisesti vanhempia enemm\u00e4n. Suurin per\u00e4kk\u00e4isten silmukoiden m\u00e4\u00e4r\u00e4 on 28. Vanhimmat lohik\u00e4\u00e4rmeet tekev\u00e4t silmukoita parhaimmillaan vain nelj\u00e4sosan parhaasta tuloksesta. Kuinka monta silmukkaa vanhat lohik\u00e4\u00e4rmeet tekev\u00e4t per\u00e4kk\u00e4in parhaimmillaan?":
    "Norwegian dragons practise making loops in the air. Younger dragons can naturally make more consecutive loops than older dragons. The highest number of consecutive loops is 28. At their best, the oldest dragons make only one quarter of that record. What is the greatest number of consecutive loops the old dragons can make?",
});

/* Presentation only: some frozen source prompts carry literal backslash-n
   sequences; fold them (and real whitespace) away before display. Does not
   decode other escapes or alter stored data. */
function normalizeDisplayText(value) {
  return String(value ?? "")
    .replace(/\\r\\n|\\r|\\n/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function englishSkillName(name) {
  return Object.prototype.hasOwnProperty.call(ENGLISH_SKILL_NAMES, name)
    ? ENGLISH_SKILL_NAMES[name] : name;
}

const ENGLISH_MIDPOINT_PATTERN = new RegExp(
  Object.keys(ENGLISH_SKILL_NAMES)
    .sort((a, b) => b.length - a.length)
    .map((key) => key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join("|"), "g");

function englishMidpointText(value) {
  return String(value ?? "").replace(
    ENGLISH_MIDPOINT_PATTERN, (match) => ENGLISH_SKILL_NAMES[match]);
}

const ENGLISH_MIDPOINT_TOPIC_DESCRIPTIONS = Object.freeze({
  "Divisibility, factors and primes":
    "Number factors, divisibility and prime numbers",
  "Fraction multiplication and division":
    "Multiplying and dividing fractions",
  "Percentages": "Working with percentages and amounts",
  "Combining like terms":
    "Simplifying algebraic expressions by combining matching terms",
  "Arithmetic": "Adding, subtracting, multiplying and dividing numbers",
  "Prices": "Working with prices and amounts of money",
  "Fractions": "Working with fractions",
});

function englishMidpointTopicDescription(name) {
  const english = englishSkillName(name);
  return Object.prototype.hasOwnProperty.call(
    ENGLISH_MIDPOINT_TOPIC_DESCRIPTIONS, english)
    ? ENGLISH_MIDPOINT_TOPIC_DESCRIPTIONS[english]
    : "Questions about this topic.";
}

function renderAssessmentResults(container, message, options) {
  if (!message.total || !Array.isArray(message.skills)) return;
  const opts = options || {};
  const box = el("div", null, opts.summaryClass || "midpoint-summary");
  box.appendChild(el(opts.headingTag || "h4", opts.title));
  box.appendChild(el("p",
    `Overall: ${message.total.correct} of ${message.total.out_of} ` +
    opts.totalSuffix, opts.totalClass || "midpoint-total"));
  const table = el("table", null, opts.tableClass);
  table.appendChild(el("caption", opts.caption));
  const head = el("thead");
  const headRow = el("tr");
  (opts.headers || ["Topic", "First-half result",
                    "What this topic covers"])
    .forEach((label) => {
      const th = el("th", label);
      th.scope = "col";
      headRow.appendChild(th);
    });
  head.appendChild(headRow);
  table.appendChild(head);
  const body = el("tbody");
  message.skills.forEach((skill) => {
    const row = el("tr");
    const name = el("th", skill.teacher_edited
      ? normalizeDisplayText(skill.skill_name)
      : englishSkillName(skill.skill_name));
    name.scope = "row";
    row.appendChild(name);
    row.appendChild(el("td",
      `${skill.correct} of ${skill.out_of} correct`));
    row.appendChild(el("td", skill.teacher_edited
      ? "Questions about this topic."
      : englishMidpointTopicDescription(skill.skill_name)));
    body.appendChild(row);
  });
  table.appendChild(body);
  box.appendChild(table);
  container.appendChild(box);
}

function renderMidpointSummary(container, message) {
  renderAssessmentResults(container, message, {
    title: "Your first 20 answers",
    totalSuffix: "correct in this first set.",
    totalClass: "midpoint-total",
    caption: "Topic results from the first 20 answers",
    headers: ["Topic", "First-half result", "What this topic covers"],
    summaryClass: "midpoint-summary",
    tableClass: "grid midpoint-topics"});
}

function englishQuestionText(question) {
  const text = normalizeDisplayText(question && question.text);
  if (Object.prototype.hasOwnProperty.call(ENGLISH_QUESTION_TEXT, text)) {
    return ENGLISH_QUESTION_TEXT[text];
  }
  const percentage = text.match(/^(\d+) % luvusta (\d+)$/);
  if (percentage) return `What is ${percentage[1]}% of ${percentage[2]}?`;
  const divisor = text.match(/^(\d+) on jaollinen luvulla\.\.\.$/);
  if (divisor) return `${divisor[1]} is divisible by…`;
  if (text === "Alkuluku?") return "Which is a prime number?";
  if (/^Sievenn[äa]/.test(text)) return text.replace(/^Sievenn[äa]( lauseke:)?\s*/, "Simplify: ");
  return text;
}

function englishOptionText(value) {
  return normalizeDisplayText(value)
    .replace(/(\d),(\d)/g, "$1.$2")
    .replace(/€/g, "EUR");
}

const $ = (sel) => document.querySelector(sel);
const PAGE = document.body.dataset.page;
const POLL_MS = 2000;
const MAX_BACKOFF_MS = 15000;
const SVGNS = "http://www.w3.org/2000/svg";
const statusLabel = (value) => ({not_requested: "Not started", pending: "Processing",
  ready: "Ready", fallback: "Baseline fallback", unavailable: "Unavailable",
  waiting_for_practice: "Waiting for practice selection",
  waiting_for_diagnostics: "Waiting for model diagnostics",
  in_progress: "In progress", complete: "Complete", queued: "Queued", running: "Running",
  cancelling: "Finishing current case", cancelled: "Stopped", interrupted: "Interrupted",
  failed: "Failed", complete_with_errors: "Finished with errors"}[value] || String(value || "Unknown").replaceAll("_", " "));

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

async function api(path, options = {}) {
  const init = {method: options.method || "GET", headers: {}};
  if (options.body !== undefined) {
    init.method = "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  if (options.token) init.headers["X-Demo-Session-Token"] = options.token;
  const response = await fetch(path, init);
  let payload = null;
  try { payload = await response.json(); } catch (err) { payload = null; }
  if (!response.ok) {
    const error = new Error((payload && payload.error) || "request failed");
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function showPanel(ids, activeId) {
  ids.forEach((id) => { $(id).hidden = id !== activeId; });
}

/* ========== SELECTION DECISION PANEL (shared, module level) ======= */

const NULL_FOCUS_LABELS = {
  neutral: "Neutral checkpoint feedback",
  optional_consolidation: "Optional consolidation"};

const ERROR_PATTERN_LABELS = {
  isolated_incorrect_answer: "Isolated incorrect answer",
  multiple_incorrect_answers: "Multiple incorrect answers"};

const COVERAGE_LABELS = {
  single_item: "Single assessed item",
  multiple_items: "Multiple assessed items"};

function candidatePattern(planning) {
  if (!planning) return "—";
  const pattern = ERROR_PATTERN_LABELS[planning.error_pattern] ||
    planning.error_pattern || "—";
  const coverage = planning.assessment_coverage === "single_item"
    ? "single item"
    : planning.assessment_coverage === "multiple_items"
      ? "multiple items" : (planning.assessment_coverage || "—");
  return `${pattern} · ${coverage}`;
}

function focusLabel(candidate, id) {
  if (!candidate) return "Recorded candidate not found";
  const focus = candidate.focus || null;
  if (focus) {
    return focus.subtopic_name
      ? `${focus.subtopic_name} (${englishSkillName(focus.skill_name) || "—"})`
      : (englishSkillName(focus.skill_name) || id || "—");
  }
  return NULL_FOCUS_LABELS[id] || id || "—";
}

function hostedConfirmed(exec, prefix) {
  return typeof exec.capture_file === "string"
    && exec.capture_file.startsWith(prefix)
    && (exec.metadata || {}).status === "completed";
}

const FAILURE_KIND_LABELS = new Set(
  ["timeout", "http_error", "network_error", "transport_error"]);
const FAILURE_STAGE_LABELS = new Set([
  "open_failed", "body_read_failed", "close_failed", "not_called",
  "open_started", "response_received", "body_read_started",
  "body_received", "response_contract_or_full_feedback_validation",
  "response_contract_or_opening_validation"]);

function captureProvenanceKind(execution, failed = false) {
  const exec = execution || {};
  if (exec.reused === true) {
    return failed ? "cached failure" : "cached hosted response";
  }
  if (exec.retry_of_failed_capture === true) {
    return failed ? "fresh hosted retry failed" : "fresh hosted retry";
  }
  if (failed) return "fresh hosted call failed";
  if (exec.reused === false) return "fresh hosted response";
  return "hosted response (reuse status unavailable)";
}

function feedbackVariantLabel(variant) {
  if (variant === "question_first") {
    return "Question-first / current version";
  }
  if (variant === "worked_first") {
    return "Worked-first / full Aitta narrative";
  }
  return "Historical / unspecified";
}

const TEMPLATE_VERSION_LABELS = {
  "phase3_student_feedback_worked_first_full_v1": "Worked-first",
  "phase3_student_feedback_template_v9": "Question-first",
  "phase3_student_feedback_template_v8": "Assessment review (concise)",
  "phase3_student_feedback_template_v7": "Assessment worked review",
  "phase3_student_feedback_template_v5": "Two-topic feedback",
  "phase3_student_feedback_template_v3":
    "Single-topic feedback with practice",
  "phase3_student_feedback_template_v1": "Single-topic feedback"};

function feedbackTemplateLabel(preview, variant) {
  const version = preview && preview.template_version;
  if (version) {
    return TEMPLATE_VERSION_LABELS[version]
      || "Recorded template (unrecognized version)";
  }
  return feedbackVariantLabel(variant).replace(
    / \/ (current version|full Aitta narrative)$/, "");
}

function selectionProvenance(review, job, selExec) {
  if (!review) {
    return (job && job.status === "pending")
      ? "Pending - deterministic baseline displayed"
      : "No provider selection recorded - deterministic baseline " +
        "displayed";
  }
  const trace = review.trace || {};
  const source = trace.selection_source;
  if (source === "rules_single_candidate") {
    return "Local policy - only one permitted option; " +
      "deterministic rules, no provider selection";
  }
  if (source === "rules") {
    let text = "Deterministic rules selection";
    if (trace.fallback_reason) text += ` - ${trace.fallback_reason}`;
    return text;
  }
  if (source === "injected_selector") {
    if (hostedConfirmed(selExec, "jev_")) {
      return `Jev - ${captureProvenanceKind(selExec)}`;
    }
    return "Injected selector - hosted provenance unavailable";
  }
  return "Selection provenance unavailable";
}

function aittaProvenance(review, genExec) {
  if (!review) return null;
  const trace = review.trace || {};
  const exec = genExec || {};
  const metadata = exec.metadata || {};
  const transport = metadata.transport || {};
  if (metadata.status === "failed"
      && typeof exec.capture_file === "string"
      && exec.capture_file.startsWith("aitta_")) {
    const detail = [
      FAILURE_KIND_LABELS.has(transport.failure_kind)
        ? transport.failure_kind : null,
      FAILURE_STAGE_LABELS.has(metadata.failure_stage)
        ? metadata.failure_stage : null]
      .filter(Boolean).join(", ");
    return `Aitta - ${captureProvenanceKind(exec, true)}` +
      `${detail ? ` (${detail})` : ""} - deterministic feedback used`;
  }
  if (String(trace.guardrails_version || "")
      .startsWith("provider_full_narrative_shape_checked_")) {
    const label = " (four narrative sections; shape-checked draft, " +
      "educator review required)";
    if (hostedConfirmed(exec, "aitta_")) {
      return `Aitta - ${captureProvenanceKind(exec)}` + label;
    }
    return "Injected full-feedback generator - hosted provenance " +
      "unavailable" + label;
  }
  if (trace.phrasing_source === "injected_generator_full_sections") {
    const guarded = trace.guardrails_version
      && (trace.section_sources || {}).assessment_summary === "provider";
    const label = guarded
      ? " (completion wording; factual sections application-owned)"
      : " (full structured feedback)";
    if (hostedConfirmed(exec, "aitta_")) {
      return `Aitta - ${captureProvenanceKind(exec)}` + label;
    }
    return "Injected full-feedback generator - hosted provenance " +
      "unavailable" + (guarded ? label : "");
  }
  if (trace.phrasing_source === "injected_generator_opening_only") {
    if (hostedConfirmed(exec, "aitta_")) {
      return `Aitta - ${captureProvenanceKind(exec)} (opening only)`;
    }
    return "Injected opening generator - hosted provenance unavailable";
  }
  return "Deterministic feedback";
}

/* Canonical serialization for feedback document blocks; must match
   the backend `_document_blocks_text` exactly. */
function serializeDocumentBlocks(blocks) {
  return (blocks || []).map((block) =>
    block.kind === "unordered_list"
      ? block.items.map((t) => `- ${t}`).join("\n")
      : block.kind === "ordered_list"
        ? block.items.map((t, i) => `${i + 1}. ${t}`).join("\n")
        : block.text).join("\n\n");
}

function isDocumentBlock(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  if (value.kind === "heading" || value.kind === "paragraph") {
    return typeof value.text === "string"
      && Object.keys(value).length === 2;
  }
  if (value.kind === "unordered_list"
      || value.kind === "ordered_list") {
    return Array.isArray(value.items)
      && value.items.every((t) => typeof t === "string")
      && Object.keys(value).length === 2;
  }
  return false;
}

const DOCUMENT_HEADING_RE =
  /^Worked review of your assessment answer|^Practice for |^Related practice|^Separate practice/;

/* Parses stored plaintext document_text into safe display blocks. */
function parseDocumentTextBlocks(payload, text) {
  const titles = new Set(((payload && payload.sections) || [])
    .map((s) => s && s.title).filter(Boolean));
  const blocks = [];
  for (const chunk of String(text).split(/\n\s*\n/)) {
    const lines = chunk.split("\n");
    if (lines.every((line) => line.startsWith("- "))) {
      const prev = blocks[blocks.length - 1];
      const items = lines.map((l) => l.slice(2));
      if (prev && prev.kind === "unordered_list") {
        prev.items.push(...items);
      } else {
        blocks.push({kind: "unordered_list", items});
      }
      continue;
    }
    if (lines.every((line) => /^\d+\. /.test(line))) {
      const prev = blocks[blocks.length - 1];
      const items = lines.map((l) => l.replace(/^\d+\. /, ""));
      if (prev && prev.kind === "ordered_list") {
        prev.items.push(...items);
      } else {
        blocks.push({kind: "ordered_list", items});
      }
      continue;
    }
    const trimmed = chunk.trim();
    if (lines.length === 1
        && (titles.has(trimmed) || DOCUMENT_HEADING_RE.test(trimmed))) {
      blocks.push({kind: "heading", text: trimmed});
      continue;
    }
    blocks.push({kind: "paragraph", text: chunk});
  }
  return blocks;
}

/* Derives the editable document blocks for a preview payload: stored
   blocks when present, parsed stored plaintext next, otherwise a walk
   of the rendered preview keeping only displayed semantic content
   (no scores, provenance, audit details or controls). */
function feedbackDocumentBlocks(payload) {
  if (payload && Array.isArray(payload.document_blocks)
      && payload.document_blocks.length
      && payload.document_blocks.every(isDocumentBlock)) {
    return JSON.parse(JSON.stringify(payload.document_blocks));
  }
  if (payload && typeof payload.document_text === "string") {
    return parseDocumentTextBlocks(payload, payload.document_text);
  }
  const host = document.createElement("div");
  renderStructuredFeedback(host, payload);
  const blocks = [];
  const SKIP_CLASSES = ["meta", "warn", "boundary-note", "mono",
                        "assessment-results", "score"];
  const HEADINGS = new Set(["H1", "H2", "H3", "H4", "H5", "H6"]);
  const listItems = (node) => [...node.children]
    .filter((li) => li.tagName === "LI")
    .map((li) => li.textContent.trim()).filter(Boolean);
  const walk = (node) => {
    for (const child of node.children) {
      if (child.tagName === "SUMMARY"
          || SKIP_CLASSES.some((c) => child.classList.contains(c))) {
        continue;
      }
      if (child.tagName === "DETAILS") {
        if (child.classList.contains("worked-hint")
            || child.classList.contains("worked-solution")) {
          walk(child);
        }
        continue;
      }
      const text = child.textContent.trim();
      if (HEADINGS.has(child.tagName)) {
        if (text) blocks.push({kind: "heading", text});
        continue;
      }
      if (child.tagName === "UL") {
        const items = listItems(child);
        if (items.length) {
          blocks.push({kind: "unordered_list", items});
        }
        continue;
      }
      if (child.tagName === "OL") {
        const items = listItems(child);
        if (items.length) {
          blocks.push({kind: "ordered_list", items});
        }
        continue;
      }
      if (child.tagName === "LI") {
        if (text) {
          blocks.push({kind: "unordered_list", items: [text]});
        }
        continue;
      }
      if (["P", "DT", "DD"].includes(child.tagName)) {
        if (text) blocks.push({kind: "paragraph", text});
        continue;
      }
      walk(child);
    }
  };
  walk(host);
  return blocks;
}

function feedbackDocumentText(payload) {
  if (payload && typeof payload.document_text === "string") {
    return payload.document_text;
  }
  return serializeDocumentBlocks(feedbackDocumentBlocks(payload));
}

/* Renders document blocks using only known elements and textContent;
   never interprets markup. */
function renderFeedbackDocument(host, blocks) {
  for (const block of blocks || []) {
    if (block.kind === "heading") {
      const h = el("h3", null, "feedback-document-heading");
      h.textContent = block.text;
      host.appendChild(h);
    } else if (block.kind === "unordered_list"
               || block.kind === "ordered_list") {
      const list = document.createElement(
        block.kind === "ordered_list" ? "ol" : "ul");
      for (const item of block.items) {
        const li = document.createElement("li");
        li.textContent = item;
        list.appendChild(li);
      }
      host.appendChild(list);
    } else {
      const p = document.createElement("p");
      p.className = "feedback-document-para";
      p.textContent = block.text;
      host.appendChild(p);
    }
  }
}

/* Parses the rich editor DOM back into allowlisted blocks. BR becomes
   a newline inside block text, nested block containers keep their own
   boundaries, inline markup flattens to text, and ignored or control
   nodes are dropped at any depth. Whitespace inside block text is
   preserved; only block boundaries are normalized. */
const DOM_IGNORE_TAGS = new Set(["SCRIPT", "STYLE", "IMG", "IFRAME",
                                 "BUTTON", "INPUT", "SELECT", "TEXTAREA",
                                 "DETAILS", "SUMMARY", "TEMPLATE",
                                 "OBJECT", "EMBED", "VIDEO", "AUDIO",
                                 "CANVAS", "SVG", "LINK", "META"]);
const DOM_HEADING_TAGS = new Set(
  ["H1", "H2", "H3", "H4", "H5", "H6"]);
const DOM_INLINE_TAGS = new Set(
  ["SPAN", "B", "I", "EM", "STRONG", "A", "SMALL", "CODE", "MARK",
   "SUB", "SUP", "U", "S", "Q", "CITE", "TIME", "KBD", "SAMP", "VAR",
   "ABBR", "FONT", "WBR"]);

function domInlineText(node) {
  let text = "";
  for (const child of node.childNodes) {
    if (child.nodeType === Node.TEXT_NODE) {
      text += child.textContent;
    } else if (child.nodeType === Node.ELEMENT_NODE) {
      if (DOM_IGNORE_TAGS.has(child.tagName)) continue;
      if (child.tagName === "BR") {
        text += "\n";
      } else {
        text += domInlineText(child);
      }
    }
  }
  return text;
}

function documentBlocksFromDom(root) {
  const blocks = [];
  const push = (block) => {
    if (block.kind === "paragraph" && !block.text.trim()
        && !blocks.length) {
      return;  // leading blank paragraph carries no content
    }
    blocks.push(block);
  };
  const visit = (node) => {
    for (const child of node.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        push({kind: "paragraph", text: child.textContent});
        continue;
      }
      if (child.nodeType !== Node.ELEMENT_NODE
          || DOM_IGNORE_TAGS.has(child.tagName)) {
        continue;
      }
      const text = domInlineText(child);
      if (DOM_HEADING_TAGS.has(child.tagName)) {
        if (text.trim()) push({kind: "heading", text});
      } else if (child.tagName === "UL" || child.tagName === "OL") {
        const items = [...child.children]
          .filter((li) => li.tagName === "LI")
          .map((li) => domInlineText(li))
          .filter((item) => item.trim());
        if (items.length) {
          push({kind: child.tagName === "OL"
                ? "ordered_list" : "unordered_list",
                items});
        }
      } else if (child.tagName === "LI") {
        if (text.trim()) {
          push({kind: "unordered_list", items: [text]});
        }
      } else if (child.tagName === "BR") {
        push({kind: "paragraph", text: ""});
      } else if (child.tagName === "P" || child.tagName === "DIV") {
        // Inline runs accumulate into one paragraph; nested block
        // children (e.g. Enter-split <div> inside <p>) keep their own
        // boundaries instead of being concatenated.
        let pending = "";
        const flush = () => {
          if (pending !== "") {
            push({kind: "paragraph", text: pending});
            pending = "";
          }
        };
        for (const part of child.childNodes) {
          if (part.nodeType === Node.TEXT_NODE) {
            pending += part.textContent;
          } else if (part.nodeType === Node.ELEMENT_NODE
                     && !DOM_IGNORE_TAGS.has(part.tagName)) {
            if (part.tagName === "BR") {
              pending += "\n";
            } else if (DOM_INLINE_TAGS.has(part.tagName)) {
              pending += domInlineText(part);
            } else {
              flush();
              visit({childNodes: [part]});
            }
          }
        }
        flush();
        if (!pending && !child.childNodes.length) {
          push({kind: "paragraph", text: ""});
        }
      } else if (DOM_INLINE_TAGS.has(child.tagName)) {
        push({kind: "paragraph", text});
      } else {
        // Nested block container: keep each child's own boundary.
        visit(child);
      }
    }
  };
  visit(root);
  return blocks;
}

function setFeedbackEditorEnabled(field, enabled) {
  field.contentEditable = enabled ? "true" : "false";
  field.setAttribute("aria-disabled", enabled ? "false" : "true");
  field.classList.toggle("edit-disabled", !enabled);
}

/* Text-offset bookmark for the contenteditable editor: captures anchor
   and focus as character offsets over the editor's rendered text so a
   selected range (including backward selections) survives a redraw.
   Element boundaries are handled via Range.toString, so caret on a
   fresh empty paragraph still resolves. */
function domPathTo(root, node) {
  const path = [];
  let n = node;
  while (n && n !== root) {
    path.unshift(
      [...n.parentNode.childNodes].indexOf(n));
    n = n.parentNode;
  }
  return n === root ? path : null;
}

function domNodeAtPath(root, path) {
  let n = root;
  for (const index of path || []) {
    if (!n || index >= n.childNodes.length) return null;
    n = n.childNodes[index];
  }
  return n;
}

function documentSelectionOffsets(root) {
  const sel = window.getSelection();
  if (!sel.rangeCount
      || !root.contains(sel.anchorNode)
      || !root.contains(sel.focusNode)) {
    return null;
  }
  const endpoint = (node, nodeOffset) => ({
    offset: (() => {
      const range = document.createRange();
      range.setStart(root, 0);
      range.setEnd(node, nodeOffset);
      return range.toString().length;
    })(),
    path: domPathTo(root, node),
    domOffset: nodeOffset,
    nodeType: node.nodeType});
  return {anchor: endpoint(sel.anchorNode, sel.anchorOffset),
          focus: endpoint(sel.focusNode, sel.focusOffset)};
}

function documentTextPoint(root, offset) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let remaining = offset, node = walker.nextNode(), last = null;
  while (node) {
    last = node;
    if (remaining <= node.textContent.length) break;
    remaining -= node.textContent.length;
    node = walker.nextNode();
  }
  if (!last) return null;
  return {node: last,
          offset: Math.min(remaining, last.textContent.length)};
}

function restoreDocumentSelection(root, bookmark) {
  const point = (endpoint) => {
    // Prefer the recorded DOM path when the same structure exists —
    // a text offset cannot distinguish an empty paragraph boundary
    // from the end of the previous block.
    const viaPath = domNodeAtPath(root, endpoint.path);
    if (viaPath && viaPath.nodeType === endpoint.nodeType) {
      const max = endpoint.nodeType === Node.TEXT_NODE
        ? viaPath.textContent.length : viaPath.childNodes.length;
      return {node: viaPath,
              offset: Math.min(endpoint.domOffset, max)};
    }
    return documentTextPoint(root, endpoint.offset);
  };
  const anchor = point(bookmark.anchor);
  const focus = point(bookmark.focus);
  if (!anchor || !focus) return;
  const sel = window.getSelection();
  if (sel.setBaseAndExtent) {
    sel.setBaseAndExtent(anchor.node, anchor.offset,
                         focus.node, focus.offset);
    return;
  }
  const range = document.createRange();
  range.setStart(anchor.node, anchor.offset);
  range.setEnd(focus.node, focus.offset);
  sel.removeAllRanges();
  sel.addRange(range);
}

function endFeedbackSource(review, delivery, execution) {
  const trace = (review && review.trace) || {};
  const source = trace.phrasing_source;
  const sectionSources = Object.values(trace.section_sources || {});
  const providerAuthored =
    source === "injected_generator_full_sections"
    || source === "injected_generator_opening_only"
    || String(trace.guardrails_version || "")
       .startsWith("provider_full_narrative_shape_checked_")
    || sectionSources.includes("provider");
  const deterministic = source === "deterministic"
    || (delivery && delivery.preview_source === "deterministic");
  let base = null;
  if (providerAuthored) {
    base = hostedConfirmed(execution || {}, "aitta_")
      ? "Aitta-generated" : "AI-generated";
  } else if (deterministic) {
    base = "Deterministic";
  }
  const partial = providerAuthored
    && (source === "injected_generator_opening_only"
        || (sectionSources.includes("provider")
            && sectionSources.some((v) => v !== "provider")));
  if (delivery && delivery.preview_source === "teacher_edited") {
    return {
      label: base
        ? `${base}, then teacher-edited`
        : "Teacher-edited (original source unavailable)",
      partial};
  }
  return {label: base || "Recorded wording", partial};
}

function renderSelectionDecision(audience, review, baseline,
                                 executions, job) {
  const multi = (review && review.selected_candidates)
    || (baseline && baseline.selected_candidates)
    || ((((review || baseline) || {}).candidates || [])[0] || {})
      .review_candidates;
  if (multi) {
    return renderMultiSelectionDecision(
      audience, review, baseline, executions, job);
  }
  const card = el("div", null, "card-inner decision");
  card.appendChild(el("h4", "Review-focus selection"));
  const trace = (review && review.trace) || {};
  const execs = (executions && executions[audience]) || {};
  const selExec = execs.selector || {};
  const genExec = execs.generator || {};
  const candidates =
    (review && review.candidates) ||
    (baseline && baseline.candidates) || [];
  const appliedId = review && review.selected_candidate_id;
  const baselineId = baseline && baseline.selected_candidate_id;
  const applied =
    candidates.find((c) => c.candidate_id === appliedId) || null;
  const base =
    candidates.find((c) => c.candidate_id === baselineId) || null;

  const rows = el("dl", null, "decision-kv");
  const put = (key, value) => {
    rows.appendChild(el("dt", key));
    rows.appendChild(el("dd", value));};
  if (review) {
    put("Applied focus", applied
        ? focusLabel(applied, appliedId)
        : "Recorded candidate not found");
    put("Candidate ID", appliedId || "—");
    const focus = applied && applied.focus;
    put("Observed evidence", focus
        ? `${focus.incorrect} incorrect of ${focus.out_of} assessed`
        : "—");
    put("Baseline focus", base
        ? focusLabel(base, baselineId)
        : "Recorded candidate not found");
    put("Comparison",
        appliedId === baselineId
          ? "matches the rules baseline"
          : "differs from the rules baseline");
  } else {
    put("Baseline focus", base ? focusLabel(base, baselineId) : "—");
  }
  put("Selection provenance",
      selectionProvenance(review, job, selExec));
  const aitta = aittaProvenance(review, genExec);
  if (aitta) put("Aitta provenance", aitta);
  if (trace.selection_source === "injected_selector" &&
      hostedConfirmed(selExec, "jev_")) {
    // Only a confirmed injected selection may carry a model label; a
    // rules fallback after a completed selector made no Jev choice.
    const version = selExec.model_version ||
      (selExec.metadata || {}).model_version;
    if (version) put("Selector model", String(version));
  }
  if (review && trace.fallback_reason &&
      trace.selection_source !== "rules") {
    // For injected selections the reason is a separate fact; for the
    // rules path it is already part of the provenance line above.
    put("Fallback reason", String(trace.fallback_reason));
  }
  card.appendChild(rows);

  const plan = review && review.feedback_plan;
  if (plan) {
    card.appendChild(el("h5",
      "Applied feedback plan — recorded before generation",
      "plan-heading"));
    const planRows = el("dl", null, "decision-kv feedback-plan");
    const pput = (key, value) => {
      planRows.appendChild(el("dt", key));
      planRows.appendChild(el("dd", value));};
    const planning = plan.planning || null;
    pput("Error pattern", planning
        ? (ERROR_PATTERN_LABELS[planning.error_pattern] ||
           String(planning.error_pattern))
        : "—");
    pput("Assessment coverage", planning
        ? (COVERAGE_LABELS[planning.assessment_coverage] ||
           String(planning.assessment_coverage))
        : "—");
    const parent = planning && planning.parent_counts;
    pput("Parent topic", parent
        ? `${englishSkillName(plan.focus && plan.focus.skill_name) || "—"} — ` +
          `${parent.correct} correct, ${parent.incorrect} ` +
          `incorrect of ${parent.out_of}`
        : "—");
    const groups = new Set(candidates
        .map((c) => c.planning && c.planning.priority_group)
        .filter((v) => v !== null && v !== undefined));
    pput("Priority group", planning
        ? `${planning.priority_group} of ${groups.size || "?"}`
        : "—");
    const ties = (planning && planning.tied_candidate_ids) || [];
    pput("Tied candidates",
        planning ? (ties.join(", ") || "None") : "—");
    pput("Strategy", plan.strategy || "—");
    pput("KT used for review-focus selection", plan.kt_used ? "Yes" : "No");
    card.appendChild(planRows);
    card.appendChild(el("p",
      "The application records this plan before any generated opening. " +
      "It is not a diagnosis and does not explain why answers were " +
      "incorrect.", "meta small"));
  }
  card.appendChild(el("p",
    "These are review options for this session, not simulation " +
    "profiles. Counts support offering an option; no validated " +
    "explanation of a provider's preference is recorded. KT estimates do " +
    "not determine this selection.", "meta"));

  const table = el("table", null, "grid decision-table");
  table.appendChild(el("caption",
    "Review options — applied selection vs rules baseline"));
  const head = el("tr");
  ["Review option", "Incorrect / assessed", "Observed pattern",
   "Priority", "Applied selection", "Rules baseline"]
    .forEach((h) => head.appendChild(el("th", h)));
  table.appendChild(head);
  candidates.forEach((c) => {
    const tr = el("tr");
    const focus = c.focus || null;
    tr.appendChild(el("td", focusLabel(c, c.candidate_id)));
    tr.appendChild(el("td", focus
        ? `${focus.incorrect} / ${focus.out_of}` : "—"));
    tr.appendChild(el("td", candidatePattern(c.planning)));
    tr.appendChild(el("td",
        c.planning ? String(c.planning.priority_group) : "—"));
    tr.appendChild(el("td",
        c.candidate_id === appliedId ? "Selected" : ""));
    tr.appendChild(el("td",
        c.candidate_id === baselineId ? "Baseline" : ""));
    table.appendChild(tr);
  });
  card.appendChild(table);
  return card;
}

function renderMultiSelectionDecision(audience, review, baseline,
                                      executions, job) {
  const card = el("div", null, "card-inner decision");
  card.appendChild(el("h4", "Review-focus selection"));
  const trace = (review && review.trace) || {};
  const execs = (executions && executions[audience]) || {};
  const selExec = execs.selector || {};
  const genExec = execs.generator || {};
  const candidates =
    (review && review.candidates) ||
    (baseline && baseline.candidates) || [];
  const appliedId = review && review.selected_candidate_id;
  const baselineId = baseline && baseline.selected_candidate_id;
  const applied =
    candidates.find((c) => c.candidate_id === appliedId) || null;
  const base =
    candidates.find((c) => c.candidate_id === baselineId) || null;
  const originals = (applied && applied.review_candidates)
    || (review && review.selected_candidates) || [];
  const bundleLabel = (bundle) => {
    const parts = ((bundle && bundle.review_candidates) || [])
      .map((c) => focusLabel(c, c.candidate_id));
    return parts.length ? parts.join(" + ") : "—";
  };
  const rows = el("dl", null, "decision-kv");
  const put = (key, value) => {
    rows.appendChild(el("dt", key));
    rows.appendChild(el("dd", value));};
  if (review) {
    put("Applied focus", originals.length
        ? originals.map((c, i) =>
            `${i + 1}. ${focusLabel(c, c.candidate_id)}`).join("; ")
        : "Recorded candidate not found");
    put("Candidate ID", appliedId || "—");
    put("Observed evidence",
        originals.filter((c) => c.focus).map((c) =>
          `${c.focus.subtopic_name}: ${c.focus.incorrect} incorrect of ` +
          `${c.focus.out_of} assessed`).join("; ") || "—");
    put("Baseline focus", base
        ? bundleLabel(base) : "Recorded candidate not found");
    put("Comparison",
        appliedId === baselineId
          ? "matches the rules baseline"
          : "differs from the rules baseline");
  } else {
    put("Baseline focus", base ? bundleLabel(base) : "—");
  }
  put("Selection provenance",
      selectionProvenance(review, job, selExec));
  const aitta = aittaProvenance(review, genExec);
  if (aitta) put("Aitta provenance", aitta);
  if (trace.selection_source === "injected_selector" &&
      hostedConfirmed(selExec, "jev_")) {
    const version = selExec.model_version ||
      (selExec.metadata || {}).model_version;
    if (version) put("Selector model", String(version));
  }
  if (review && trace.fallback_reason &&
      trace.selection_source !== "rules") {
    put("Fallback reason", String(trace.fallback_reason));
  }
  card.appendChild(rows);

  const plans = (review && review.feedback_plans) ||
    ((review && review.feedback_plan) ? [review.feedback_plan] : []);
  plans.forEach((plan, index) => {
    const label = plan.focus && plan.focus.subtopic_name
      ? ` — ${focusLabel(plan, plan.candidate_id)}` : "";
    card.appendChild(el("h5",
      `Applied feedback plan ${index + 1}${label} — recorded before ` +
      "generation", "plan-heading"));
    const planRows = el("dl", null, "decision-kv feedback-plan");
    const pput = (key, value) => {
      planRows.appendChild(el("dt", key));
      planRows.appendChild(el("dd", value));};
    const planning = plan.planning || null;
    pput("Error pattern", planning
        ? (ERROR_PATTERN_LABELS[planning.error_pattern] ||
           String(planning.error_pattern))
        : "—");
    pput("Assessment coverage", planning
        ? (COVERAGE_LABELS[planning.assessment_coverage] ||
           String(planning.assessment_coverage))
        : "—");
    const parent = planning && planning.parent_counts;
    pput("Parent topic", parent
        ? `${englishSkillName(plan.focus && plan.focus.skill_name) || "—"} — ` +
          `${parent.correct} correct, ${parent.incorrect} ` +
          `incorrect of ${parent.out_of}`
        : "—");
    pput("Priority group", planning
        ? String(planning.priority_group)
        : "—");
    const ties = (planning && planning.tied_candidate_ids) || [];
    pput("Tied candidates",
        planning ? (ties.join(", ") || "None") : "—");
    pput("Strategy", plan.strategy || "—");
    pput("KT used for review-focus selection", plan.kt_used ? "Yes" : "No");
    card.appendChild(planRows);
  });
  if (plans.length) {
    card.appendChild(el("p",
      "The application records each plan before any generated opening. " +
      "It is not a diagnosis and does not explain why answers were " +
      "incorrect.", "meta small"));
  }
  card.appendChild(el("p",
    "These are review options for this session, not simulation " +
    "profiles. Counts support offering an option; no validated " +
    "explanation of a provider's preference is recorded. KT estimates do " +
    "not determine this selection. Practice follows selected " +
    "review topics.", "meta"));

  const table = el("table", null, "grid decision-table");
  table.appendChild(el("caption",
    "Review bundles — applied selection vs rules baseline"));
  const head = el("tr");
  ["Review option", "Incorrect / assessed", "Applied selection",
   "Rules baseline"]
    .forEach((h) => head.appendChild(el("th", h)));
  table.appendChild(head);
  candidates.forEach((c) => {
    const tr = el("tr");
    const focuses = (c.review_candidates || [])
      .map((o) => o.focus).filter(Boolean);
    tr.appendChild(el("td", bundleLabel(c)));
    tr.appendChild(el("td", focuses.length
        ? focuses.map((f) => `${f.incorrect} / ${f.out_of}`).join(" + ")
        : "—"));
    tr.appendChild(el("td",
        c.candidate_id === appliedId ? "Selected" : ""));
    tr.appendChild(el("td",
        c.candidate_id === baselineId ? "Baseline" : ""));
    table.appendChild(tr);
  });
  card.appendChild(table);
  return card;
}

function teacherOriginalDetails(card, key) {
  const original = card && card.original_content;
  if (!original) return null;
  const details = el("details", null, "raw");
  details.dataset.disclosureKey = key;
  details.appendChild(el("summary",
    "Original content before teacher edits"));
  const put = (text, className) => {
    if (text !== undefined && text !== null && text !== "") {
      details.appendChild(el("p", String(text), className || "sec"));
    }
  };
  if (original.title) details.appendChild(el("h4", original.title));
  put(normalizeDisplayText(original.for_subtopic_name), "meta");
  put(normalizeDisplayText(original.topic_label), "meta");
  if (original.relationship) put(original.relationship.message);
  put(original.message);
  put(normalizeDisplayText(original.prompt));
  const question = original.question;
  if (question) {
    if (question.skill_name || question.concept_name) {
      put(`${normalizeDisplayText(question.skill_name)} · ` +
        `${normalizeDisplayText(question.concept_name)}`, "meta");
    }
    put(normalizeDisplayText(question.text));
    (question.options || []).forEach((option, i) => {
      if (i === 0) details.appendChild(el("ul", null,
        "practice-options"));
      details.lastChild.appendChild(
        el("li", normalizeDisplayText(option)));
    });
  }
  if (Array.isArray(original.options) && original.options.length) {
    const list = el("ul", null, "practice-options");
    original.options.forEach((option) => {
      list.appendChild(el("li", normalizeDisplayText(option)));
    });
    details.appendChild(list);
  }
  if (original.selected_answer !== undefined
      && original.selected_answer !== null) {
    put(`Your selected answer: ` +
      `${normalizeDisplayText(original.selected_answer)}`);
  }
  if (original.correct_answer !== undefined
      && original.correct_answer !== null) {
    put(`Correct answer: ` +
      `${normalizeDisplayText(original.correct_answer)}`);
  }
  if (Array.isArray(original.steps) && original.steps.length) {
    const steps = el("ol", null, "worked-steps");
    original.steps.forEach((step) => {
      steps.appendChild(el("li", step));
    });
    details.appendChild(steps);
  }
  if (original.reasoning_question) {
    put(`Explain your reasoning: ${original.reasoning_question}`);
  }
  if (original.socratic_prompt) {
    put(`Think first: ${original.socratic_prompt}`);
  }
  if (original.option_contrast) {
    put(`Point to consider: ${original.option_contrast}`);
  }
  if (Array.isArray(original.progressive_hints)
      && original.progressive_hints.length) {
    const hints = el("ul", null, "practice-options");
    original.progressive_hints.forEach((hint) => {
      hints.appendChild(el("li", hint));
    });
    details.appendChild(hints);
  }
  put(original.instruction);
  put(original.explanation);
  put(original.notice, "meta");
  return details;
}

const QUESTION_FIRST_VERSION = "question_first_worked_review_v1";
const EDIT_REVIEW_FIELDS = ["title", "for_subtopic_name", "prompt",
  "options", "selected_answer", "correct_answer", "steps",
  "explanation", "reasoning_question", "notice", "socratic_prompt",
  "option_contrast", "progressive_hints"];
const EDIT_PRACTICE_FIELDS = ["topic_label", "message", "notice",
  "instruction"];
const EDIT_QUESTION_FIELDS = ["skill_name", "concept_name", "text",
  "options"];
const EDIT_FIELD_LABELS = {title: "Card title",
  for_subtopic_name: "Subtopic label", prompt: "Question wording",
  options: "Options", selected_answer: "Selected answer display wording",
  correct_answer: "Correct answer",
  steps: "Worked steps (one step per line)",
  explanation: "Additional explanation",
  reasoning_question: "Reasoning question",
  notice: "Notice", topic_label: "Topic label", message: "Message",
  instruction: "Instruction", skill_name: "Skill label",
  concept_name: "Concept label", text: "Practice question",
  socratic_prompt: "Before-reveal reasoning question",
  option_contrast: "Option observation (optional)",
  progressive_hints: "Progressive hints (one hint per line)"};

function contentProjection() {
  return {};
}

function ensureContentPath(content, path) {
  const parts = path.split(".");
  let node = content;
  for (let i = 0; i < parts.length - 1; i += 1) {
    const key = parts[i];
    if (node[key] === undefined) {
      node[key] = /^\d+$/.test(parts[i + 1]) ? [] : {};
    }
    node = node[key];
  }
  return [node, parts[parts.length - 1]];
}

function contentPathGet(content, path) {
  const parts = path.split(".");
  let node = content;
  for (let i = 0; i < parts.length; i += 1) {
    if (node === null || node === undefined) return undefined;
    node = node[parts[i]];
  }
  return node;
}

function contentPathSet(content, path, value) {
  const [node, last] = ensureContentPath(content, path);
  node[last] = value;
}

function buildContentPayload(preview, content) {
  if (!content || !Object.keys(content).length) return undefined;
  const payload = {};
  if (content.section_titles
      && Object.keys(content.section_titles).length) {
    payload.section_titles = {...content.section_titles};
  }
  if (content.skill_labels && Array.isArray(preview.skills)) {
    payload.skill_labels = preview.skills.map((skill, i) =>
      Object.hasOwn(content.skill_labels, i)
        ? content.skill_labels[i]
        : (skill.teacher_edited
           ? normalizeDisplayText(skill.skill_name)
           : englishSkillName(skill.skill_name)));
  }
  if (Object.hasOwn(content, "worked_review_notice")) {
    payload.worked_review_notice = content.worked_review_notice;
  }
  const examples = preview.review_examples ||
    (preview.review_example ? [preview.review_example] : null);
  if (content.review_examples && examples) {
    payload.review_examples = examples.map((card, i) => {
      const src = content.review_examples[i] || {};
      const patch = {};
      EDIT_REVIEW_FIELDS.forEach((field) => {
        if (!Object.hasOwn(src, field)) return;
        if (field === "options" && Array.isArray(card.options)) {
          patch.options = card.options.map((opt, j) =>
            Object.hasOwn(src.options, j)
              ? src.options[j] : englishOptionText(opt));
        } else if (field === "steps") {
          patch.steps = Array.isArray(src.steps)
            ? src.steps.map((step, j) =>
                src.steps[j] !== undefined
                  ? src.steps[j] : (card.steps || [])[j] || "")
            : String(src.steps || "").split(/\r?\n/)
                .map((s) => s.trim()).filter(Boolean);
        } else if (field === "progressive_hints") {
          patch.progressive_hints =
            Array.isArray(src.progressive_hints)
              ? src.progressive_hints.map((hint, j) =>
                  src.progressive_hints[j] !== undefined
                    ? src.progressive_hints[j]
                    : (card.progressive_hints || [])[j] || "")
              : String(src.progressive_hints || "")
                  .split(/\r?\n/)
                  .map((s) => s.trim()).filter(Boolean);
        } else {
          patch[field] = src[field];
        }
      });
      return patch;
    });
  }
  const activities = preview.practice_activities ||
    (preview.practice ? [preview.practice] : null);
  if (content.practice_activities && activities) {
    payload.practice_activities = activities.map((card, i) => {
      const src = content.practice_activities[i] || {};
      const patch = {};
      EDIT_PRACTICE_FIELDS.forEach((field) => {
        if (Object.hasOwn(src, field)) patch[field] = src[field];
      });
      if (src.question && card.question) {
        const qpatch = {};
        EDIT_QUESTION_FIELDS.forEach((field) => {
          if (!Object.hasOwn(src.question, field)) return;
          if (field === "options"
              && Array.isArray(card.question.options)) {
            qpatch.options = card.question.options.map((opt, j) =>
              Object.hasOwn(src.question.options, j)
                ? src.question.options[j] : englishOptionText(opt));
          } else {
            qpatch[field] = src.question[field];
          }
        });
        if (Object.keys(qpatch).length) patch.question = qpatch;
      }
      if (src.relationship
          && Object.hasOwn(src.relationship, "message")) {
        patch.relationship = {message: src.relationship.message};
      }
      return patch;
    });
  }
  return Object.keys(payload).length ? payload : undefined;
}

function renderStructuredFeedback(container, payload) {
  if (!payload) return;
  if (payload.total && Array.isArray(payload.skills)
      && payload.skills.length) {
    renderAssessmentResults(container, payload, {
      title: "Your assessment results",
      totalSuffix: "correct on this assessment.",
      totalClass: "score",
      caption: "Topic results for this assessment",
      headers: ["Topic", "Result", "What this topic covers"],
      summaryClass: "assessment-results",
      tableClass: "grid assessment-topics"});
  } else if (payload.total) {
    container.appendChild(el("p",
      `Overall: ${payload.total.correct} of ` +
      `${payload.total.out_of} correct on this assessment.`,
      "score"));
  }
  if (typeof payload.document_text === "string") {
    const doc = el("div", null, "feedback-document");
    renderFeedbackDocument(doc, feedbackDocumentBlocks(payload));
    container.appendChild(doc);
    return;
  }
  (payload.sections || []).forEach((section) => {
    const box = el("div", null, "feedback-section");
    if (section.title) box.appendChild(el("h3", section.title));
    String(section.text || "").split("\n\n").forEach((part) => {
      if (part) box.appendChild(el("p", part, "sec"));
    });
    container.appendChild(box);
  });
  const examples = payload.review_examples ||
    (payload.review_example ? [payload.review_example] : []);
  const exampleNoticeCounts = new Map();
  examples.forEach((example) => {
    if (example.notice) {
      exampleNoticeCounts.set(example.notice,
        (exampleNoticeCounts.get(example.notice) || 0) + 1);
    }
  });
  examples.forEach((example, exampleIndex) => {
    const box = el("div", null, "feedback-section review-example");
    const original = example.original_content || null;
    const changed = (field) => example.teacher_edited && (!original
      || JSON.stringify(example[field])
         !== JSON.stringify(original[field]));
    const heading = example.for_subtopic_name
      ? `${example.title || "Worked review example"} — ` +
        `${changed("for_subtopic_name")
          ? normalizeDisplayText(example.for_subtopic_name)
          : englishSkillName(example.for_subtopic_name)}`
      : (example.title || "Worked review example");
    box.appendChild(el("h3", heading));
    if (example.teacher_edited) {
      box.appendChild(el("p",
        payload.requires_educator_review
          ? "Teacher-edited worked review — verify before release"
          : "Teacher-edited worked review",
        "warn"));
    }
    if (example.explanation_source === "provider") {
      box.appendChild(el("p",
        example.teacher_edited
          ? "Teacher-edited AI-drafted explanation — teacher " +
            "wording, review before release."
          : "AI-drafted explanation based on checked mathematics; " +
            "teacher review required.",
        "meta"));
    }
    let solutionHost = null;
    if (example.source === "incorrect_assessment_response") {
      const questionFirst = example.presentation_version
        === QUESTION_FIRST_VERSION;
      const displayPrompt = changed("prompt")
        ? normalizeDisplayText(example.prompt)
        : englishQuestionText({text: example.prompt});
      const displayOptions = (example.options || []).map(
        changed("options") ? normalizeDisplayText : englishOptionText);
      const selectedText = changed("selected_answer")
        ? normalizeDisplayText : englishOptionText;
      const correctText = changed("correct_answer")
        ? normalizeDisplayText : englishOptionText;
      box.appendChild(el("p", displayPrompt, "sec"));
      const list = el("ul", null, "practice-options");
      displayOptions.forEach((option) => {
        list.appendChild(el("li", option));
      });
      box.appendChild(list);
      box.appendChild(el("p",
        `Your selected answer: ${selectedText(
          example.selected_answer)}`, "sec"));
      const solutionContent = (target) => {
        target.appendChild(el("p",
          `Correct answer: ${correctText(
            example.correct_answer)}`, "sec"));
        if (Array.isArray(example.steps) && example.steps.length) {
          const steps = el("ol", null, "worked-steps");
          example.steps.forEach((step) => {
            steps.appendChild(el("li", step));
          });
          target.appendChild(steps);
        }
        if (example.reasoning_question) {
          target.appendChild(el("p",
            `Explain your reasoning: ${example.reasoning_question}`,
            "sec"));
        }
        if (example.explanation
            && example.explanation
               !== (example.steps || []).join("\n")) {
          target.appendChild(el("p", example.explanation, "sec"));
        }
      };
      if (questionFirst) {
        const cardKey = JSON.stringify({
          qid: example.question_id,
          version: example.presentation_version,
          prompt: example.prompt,
          options: example.options,
          selected: example.selected_answer,
          correct: example.correct_answer,
          steps: example.steps,
          socratic: example.socratic_prompt,
          contrast: example.option_contrast,
          hints: example.progressive_hints,
          explanation: example.explanation});
        const think = el("div", null, "think-first");
        think.appendChild(el("h4", "Think first"));
        think.appendChild(el("p",
          example.socratic_prompt || "", "sec"));
        box.appendChild(think);
        if (example.option_contrast) {
          const contrast = el("div", null, "option-contrast");
          contrast.appendChild(el("h4", "Point to consider"));
          contrast.appendChild(el("p", example.option_contrast,
            "sec"));
          box.appendChild(contrast);
        }
        const hints = Array.isArray(example.progressive_hints)
          ? example.progressive_hints : [];
        let hintHost = box;
        hints.forEach((hint, hintIndex) => {
          const hintDetails = el("details", null,
            "raw worked-hint");
          hintDetails.dataset.disclosureKey =
            `worked-hint:${hintIndex}:${cardKey}`;
          hintDetails.appendChild(el("summary",
            `Show hint ${hintIndex + 1}`));
          hintDetails.appendChild(el("p", hint, "sec"));
          hintHost.appendChild(hintDetails);
          hintHost = hintDetails;
        });
        const solution = el("details", null, "raw worked-solution");
        solution.dataset.disclosureKey = `worked-solution:${cardKey}`;
        solution.appendChild(el("summary",
          example.status === "ready"
            ? (example.teacher_edited
               ? "Reveal answer and worked steps"
               : "Reveal checked worked steps")
            : "Reveal recorded correct answer"));
        solutionContent(solution);
        box.appendChild(solution);
        solutionHost = solution;
      } else {
        solutionContent(box);
      }
    } else {
      if (example.prompt) box.appendChild(el("p", example.prompt, "sec"));
      if (example.explanation) {
        box.appendChild(el("p", example.explanation, "sec"));
      }
    }
    const originalCard = teacherOriginalDetails(
      example, `worked-review-original-content:${exampleIndex}`);
    if (originalCard) {
      (solutionHost || box).appendChild(originalCard);
    }
    if (example.notice
        && exampleNoticeCounts.get(example.notice) === 1) {
      box.appendChild(el("p", example.notice, "meta"));
    }
    container.appendChild(box);
  });
  exampleNoticeCounts.forEach((count, notice) => {
    if (count > 1) {
      container.appendChild(el("p", notice, "meta"));
    }
  });
  if (payload.worked_review_notice) {
    container.appendChild(el("p", payload.worked_review_notice, "meta"));
  }
  const activities = payload.practice_activities ||
    (payload.practice ? [payload.practice] : []);
  const practiceNoticeCounts = new Map();
  activities.forEach((practice) => {
    if (practice.notice) {
      practiceNoticeCounts.set(practice.notice,
        (practiceNoticeCounts.get(practice.notice) || 0) + 1);
    }
  });
  activities.forEach((practice, index) => {
    renderPracticeCard(container, practice, index,
      !practice.notice
      || practiceNoticeCounts.get(practice.notice) === 1,
      payload.requires_educator_review);
  });
  practiceNoticeCounts.forEach((count, notice) => {
    if (count > 1) {
      container.appendChild(el("p", notice, "meta"));
    }
  });
  if (container.querySelector("details.raw")) {
    container.appendChild(el("p",
      "English display wording is a draft translation; original " +
      "text is preserved.", "meta small"));
  }
}

function renderPracticeCard(container, practice, index, showNotice,
                            needsReview) {
  const relationship = practice.relationship || null;
  const box = el("div", null, "feedback-section practice-activity");
  const original = practice.original_content || null;
  const originalQuestion = (original || {}).question || null;
  const changed = (field) => practice.teacher_edited && (!original
    || JSON.stringify(practice[field])
       !== JSON.stringify(original[field]));
  const questionChanged = (field) => practice.teacher_edited
    && (!originalQuestion
        || JSON.stringify((practice.question || {})[field])
           !== JSON.stringify(originalQuestion[field]));
  const nameFor = (value, isChanged) =>
    (practice.teacher_edited && isChanged)
      ? normalizeDisplayText(value) : englishSkillName(value);
  let heading;
  if (relationship && relationship.kind === "same_topic_different_content") {
    heading = `Related practice — ${nameFor(
      practice.question?.concept_name || practice.topic_label
      || "related content",
      questionChanged("concept_name") || changed("topic_label"))}`;
  } else if (relationship && relationship.kind === "different_content") {
    heading = `Separate practice — ${nameFor(
      practice.question?.concept_name || "other content",
      questionChanged("concept_name"))}`;
  } else {
    heading = practice.topic_label
      ? `Practice for ${nameFor(practice.topic_label,
          changed("topic_label"))}`
      : "Practice activity";
  }
  box.appendChild(el("h3", heading));
  if (practice.teacher_edited) {
    box.appendChild(el("p",
      needsReview
        ? "Teacher-edited practice — verify before release"
        : "Teacher-edited practice", "warn"));
  }
  if (relationship && relationship.message) {
    box.appendChild(el("p", relationship.message, "sec"));
  }
  if (practice.message
      && (!(practice.status === "selected"
            && relationship && relationship.message)
          || changed("message"))) {
    box.appendChild(el("p", practice.message, "sec"));
  }
  const question = practice.question;
  if (question) {
    const displayPrompt = questionChanged("text")
      ? normalizeDisplayText(question.text)
      : englishQuestionText(question);
    const displayOptions = (question.options || []).map(
      questionChanged("options") ? normalizeDisplayText
                                 : englishOptionText);
    box.appendChild(el("p",
      practice.teacher_edited
        ? `${nameFor(question.skill_name, questionChanged("skill_name"))} · ` +
          `${nameFor(question.concept_name, questionChanged("concept_name"))}`
        : englishSkillName(question.skill_name),
      "meta"));
    box.appendChild(el("p", displayPrompt, "sec"));
    const list = el("ul", null, "practice-options");
    displayOptions.forEach((option) => {
      list.appendChild(el("li", option));
    });
    box.appendChild(list);
    if (practice.instruction) {
      box.appendChild(el("p", practice.instruction, "sec"));
    }
  }
  const originalCard = teacherOriginalDetails(
    practice, `practice-original-content:${index}`);
  if (originalCard) box.appendChild(originalCard);
  if (practice.notice && showNotice !== false) {
    box.appendChild(el("p", practice.notice, "meta"));
  }
  container.appendChild(box);
}

/* ============================ STUDENT ============================ */

const RELEASED_SNAPSHOT_KEY = "demoReleasedSnapshot";

function buildStudentSnapshotFromDetail(detail, releasedFeedback) {
  if (!detail) return null;
  const delivery = detail.feedback_delivery || {};
  const endFeedback = releasedFeedback || delivery.released_feedback;
  if (!endFeedback) return null;
  return {
    session_id: detail.session_id,
    synthetic: true,
    bank_mode: detail.bank_mode || "demo",
    bank_label: detail.bank_label || "Demo bank",
    status: "complete",
    answered_count: detail.answered_count || 40,
    assessment_total: detail.observed_total || null,
    current_question: null,
    feedback: {
      midpoint: null,
      end: endFeedback,
    },
    feedback_delivery: {
      status: "released",
      released_at: delivery.released_at || new Date().toISOString(),
    },
    provider_job: {
      status: (detail.provider_job || {}).status || "ready",
      provider_mode: (detail.provider_job || {}).provider_mode || "rules",
    },
  };
}

function saveReleasedStudentSnapshot(detail, releasedFeedback, clearActiveCreds = false) {
  const snap = buildStudentSnapshotFromDetail(detail, releasedFeedback);
  if (!snap) return;
  try {
    localStorage.setItem(RELEASED_SNAPSHOT_KEY, JSON.stringify(snap));
    if (clearActiveCreds) {
      const raw = localStorage.getItem("demoSession");
      const current = raw ? JSON.parse(raw) : null;
      if (!current || current.sid !== detail.session_id) {
        localStorage.removeItem("demoSession");
      }
    }
  } catch (err) {}
}

function studentPage() {
  const PANELS = ["#start-panel", "#question-panel", "#pause-panel",
                  "#end-panel", "#load-error"];
  const credKey = "demoSession";
  const ackKey = (sid) => `demoAck:${sid}`;
  let creds = null;
  let pollTimer = null;
  let submitting = false;
  let starting = false;
  let backoff = POLL_MS;
  let currentQuestionToken = null;

  function saveCreds(value) {
    creds = value;
    if (value) localStorage.setItem(credKey, JSON.stringify(value));
    else localStorage.removeItem(credKey);
  }

  function loadReleasedSnapshot() {
    try {
      const raw = localStorage.getItem(RELEASED_SNAPSHOT_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (err) {
      return null;
    }
  }

  function credsFromUrl() {
    const params = new URLSearchParams(location.search);
    const match = /(?:^|#|&)token=([^&]+)/.exec(location.hash);
    if (params.get("session") && match) {
      return {sid: params.get("session"), token: match[1]};
    }
    return null;
  }

  function stripHash() {
    history.replaceState(null, "", location.pathname + location.search);
  }

  function netError(message) {
    const box = $("#student-error");
    if (!message) { box.hidden = true; box.textContent = ""; return; }
    box.textContent = message;
    box.hidden = false;
  }

  function schedule(delay) {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(refresh, delay);
  }

  async function refresh() {
    if (!creds) {
      const releasedSnap = loadReleasedSnapshot();
      render(releasedSnap);
      return;
    }
    try {
      const snap = await api(`/api/sessions/${creds.sid}`,
                             {token: creds.token});
      backoff = POLL_MS;
      netError(null);
      render(snap);
    } catch (err) {
      if (err.status === 400 || err.status === 403 || err.status === 404) {
        // Bad or stale saved credentials are unrecoverable for this
        // session; only transport failures and 5xx retry.
        saveCreds(null);
        showPanel(PANELS, "#load-error");
        return;
      }
      netError("This page could not reach the demo server; retrying.");
      backoff = Math.min(backoff * 1.6, MAX_BACKOFF_MS);
      schedule(backoff);
    }
  }

  function renderMessage(container, message) {
    container.replaceChildren();
    if (!message) return;
    (message.sections || []).forEach((section) => {
      if (section.title) {
        const box = el("section", null, "sec");
        box.appendChild(el("h3", section.title));
        box.appendChild(el("p", englishMidpointText(section.text)));
        container.appendChild(box);
        if (section.kind === "encouragement") {
          renderMidpointSummary(container, message);
        }
        return;
      }
      const p = el("p", englishMidpointText(section.text), "sec");
      const kind = el("span", section.kind || "", "kind");
      p.prepend(kind, " ");
      container.appendChild(p);
      if (section.kind === "encouragement") {
        renderMidpointSummary(container, message);
      }
    });
    if (!message.sections || !message.sections.length) {
      renderMidpointSummary(container, message);
      container.appendChild(el("p", englishMidpointText(message.text || "")));
    }
  }

  function render(snapshot) {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    if (!snapshot) {
      showPanel(PANELS, "#start-panel");
      return;
    }
    const answered = snapshot.answered_count;
    $("#session-bank").textContent = snapshot.bank_label;
    const mid = snapshot.feedback && snapshot.feedback.midpoint;
    const acked =
      localStorage.getItem(ackKey(snapshot.session_id)) === "1";
    if (snapshot.status === "complete") {
      renderEnd(snapshot);
      showPanel(PANELS, "#end-panel");
      return;
    }
    if (answered === 20 && mid && !acked) {
      const entering = $("#pause-panel").hidden;
      renderMessage($("#pause-message"), mid);
      showPanel(PANELS, "#pause-panel");
      // Focus the heading only when entering the pause; the poll must
      // not steal focus back from the Continue button every cycle.
      if (entering) $("#pause-heading").focus({preventScroll: true});
      // Keep polling while paused so a ready Aitta midpoint wording
      // replaces the deterministic fallback without a reload.
      schedule(POLL_MS);
      return;
    }
    const question = snapshot.current_question;
    if (!question) { showPanel(PANELS, "#start-panel"); return; }
    currentQuestionToken = question.question_token;
    submitting = false;
    $("#q-pos").textContent = question.position;
    $("#q-progress").value = answered;
    const finnish = $("#display-language").value === "fi";
    $("#q-text").textContent = finnish ? normalizeDisplayText(question.text) : englishQuestionText(question);
    $("#q-skill").textContent = finnish ? question.skill_name : englishSkillName(question.skill_name);
    $("#language-note").textContent = finnish
      ? "Original source wording."
      : "Draft English display; untranslated prompts retain their original Finnish wording.";
    const box = $("#q-options");
    box.replaceChildren(el("legend", "Choose one answer", "visually-hidden"));
    question.options.forEach((option, index) => {
      const label = el("label", null, "option");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "answer";
      radio.value = String(index);
      label.appendChild(radio);
      label.appendChild(el("span", `${index + 1}.`, "opt-num"));
      label.appendChild(el("span", finnish ? normalizeDisplayText(option) : englishOptionText(option)));
      box.appendChild(label);
    });
    $("#submit-answer").disabled = true;
    $("#q-error").hidden = true;
    showPanel(PANELS, "#question-panel");
    $("#q-text").focus({preventScroll: true});
  }

  function renderEnd(snapshot) {
    const delivery = snapshot.feedback_delivery || {};
    $("#job-status").textContent =
      delivery.status === "released" ? "Released by your teacher"
      : delivery.status === "awaiting_teacher_review"
        ? "Awaiting teacher review"
        : statusLabel(snapshot.provider_job.status);
    const end = snapshot.feedback && snapshot.feedback.end;
    const summary = $("#end-summary");
    summary.replaceChildren();
    $("#end-sections").replaceChildren();
    if (end) {
      summary.appendChild(el("p",
        "Your teacher has reviewed and released this feedback.",
        "boundary-note"));
      renderStructuredFeedback(summary, end);
    } else {
      const total = snapshot.assessment_total;
      if (total) {
        summary.appendChild(el("p",
          `You answered ${total.correct} of ${total.out_of} ` +
          `questions correctly.`, "score"));
      }
      summary.appendChild(el("p",
        "Assessment complete—your teacher is reviewing your feedback.",
        "boundary-note"));
      schedule(POLL_MS);
    }
  }

  async function startSession() {
    if (starting || !$("#synthetic-consent").checked) return;
    starting = true;
    $("#start-button").disabled = true;
    $("#fresh-start").disabled = true;
    try {
      const payload = await api("/api/sessions", {
        body: {synthetic: true, bank_mode: $("#bank-mode").value}});
      saveCreds({sid: payload.session_id, token: payload.student_token});
      localStorage.removeItem(RELEASED_SNAPSHOT_KEY);
      localStorage.removeItem(ackKey(payload.session_id));
      netError(null);
      render(payload.snapshot);
    } catch (err) {
      netError("Could not start a session; check the demo server.");
      showPanel(PANELS, "#load-error");
    } finally {
      starting = false;
      $("#start-button").disabled = false;
      $("#fresh-start").disabled = false;
    }
  }

  $("#synthetic-consent").addEventListener("change", () => {
    $("#start-button").disabled = starting || !$("#synthetic-consent").checked;
  });
  $("#start-button").addEventListener("click", startSession);
  $("#display-language").addEventListener("change", refresh);
  function returnToSetup() {
    saveCreds(null);
    localStorage.removeItem(RELEASED_SNAPSHOT_KEY);
    $("#synthetic-consent").checked = false;
    $("#start-button").disabled = true;
    render(null);
    $("#synthetic-consent").focus();
  }
  $("#fresh-start").addEventListener("click", returnToSetup);
  $("#new-session").addEventListener("click", returnToSetup);
  $("#q-options").addEventListener("change", () => {
    $("#submit-answer").disabled = submitting;
  });
  $("#submit-answer").addEventListener("click", async () => {
    if (submitting || !creds || !currentQuestionToken) return;
    const chosen = document.querySelector("input[name=answer]:checked");
    if (!chosen) return;
    submitting = true;
    $("#submit-answer").disabled = true;
    try {
      const snap = await api(`/api/sessions/${creds.sid}/responses`, {
        token: creds.token,
        body: {question_token: currentQuestionToken,
               selected_index: Number(chosen.value)}});
      render(snap);
    } catch (err) {
      const error = $("#q-error");
      error.textContent = "Answer was not accepted; try again.";
      error.hidden = false;
      submitting = false;
      $("#submit-answer").disabled =
        !document.querySelector("input[name=answer]:checked");
    }
  });
  $("#continue-button").addEventListener("click", () => {
    if (creds) localStorage.setItem(ackKey(creds.sid), "1");
    refresh();
  });

  // Resume: URL token (from teacher simulation link) wins, then storage.
  const fromUrl = credsFromUrl();
  if (fromUrl) {
    localStorage.removeItem(RELEASED_SNAPSHOT_KEY);
    saveCreds(fromUrl);
    stripHash();
  } else {
    try { creds = JSON.parse(localStorage.getItem(credKey)); }
    catch (err) { creds = null; }
  }
  refresh();
}

/* ============================ TEACHER ============================ */

function teacherPage() {
  let config = null;
  let sessions = [];
  let detail = null;
  let selected = null;
  let unlocked = false;
  let pollInFlight = false;
  let simRunning = false;
  const simLinks = new Map(); // sid -> student URL with token
  const detailTemplate = $("#ws-detail").cloneNode(true);
  const liveView = createDetailView($("#ws-detail"), {
    refresh: pollOnce, onShow: () => { $("#ws-empty").hidden = true; },
    studentLink: (sid) => simLinks.get(sid)});

  function mountDetail(container, options) {
    const root = detailTemplate.cloneNode(true);
    const prefix = "scenario-";
    root.id = prefix + root.id;
    root.querySelectorAll("[id]").forEach((node) => { node.id = prefix + node.id; });
    container.append(root);
    return createDetailView(root, {...options, prefix});
  }

  function renderDetail() {
    if (detail) liveView.update(detail);
  }

  function teacherError(message) {
    const box = $("#teacher-error");
    if (!message) { box.hidden = true; box.textContent = ""; return; }
    box.textContent = message;
    box.hidden = false;
  }

  /* ---------- boot / auth ---------- */

  async function boot() {
    try {
      config = await api("/api/teacher/config");
      enterApp();
    } catch (err) {
      if (err.status === 403) {
        $("#unlock-panel").hidden = false;
        return;
      }
      teacherError("Could not reach the demo server.");
    }
  }

  function enterApp() {
    unlocked = true;
    $("#unlock-panel").hidden = true;
    $("#teacher-app").hidden = false;
    renderConfigStrip();
    initResearchWorkspace({mountDetail, getConfig: () => config});
    $("#pin-input").value = "";
    renderList();
    const select = $("#sim-profile");
    select.replaceChildren();
    config.simulation_profiles.forEach((p) => {
        const option = el("option", p.display_name);
        option.value = p.id;
        select.appendChild(option);
      });
    pollOnce();
    pollLoop();
  }

  function renderConfigStrip() {
    const strip = $("#config-strip");
    strip.replaceChildren();
    strip.hidden = false;
    const budget = config.call_budget || {};
    const diag = config.diagnostics || {};
    const shadow = config.shadow_practice || {};
    [
      `providers: ${config.provider_mode}`,
      "KT practice: " + (shadow.enabled
        ? `enabled · ${shadow.pool_question_count ?? "?"} questions` +
          (shadow.target_band ? ` · band ${shadow.target_band.join("–")}` : "")
        : "disabled"),
      `New provider calls this server run: ` +
      `${budget.used ?? 0} / ${budget.limit ?? "unlimited"}`,
      `diagnostics: ${diag.status || "unknown"}`,
      `review: ${config.review_mode || "legacy_single"} · up to ` +
        `${config.review_topic_limit ?? 1} topics / ` +
        `${config.practice_question_limit ?? 1} practice` +
        (config.practice_alignment ? " · aligned" : ""),
      `policy ${config.policy_version}`,
      "synthetic only — drafts require educator review",
    ].forEach((text, i, arr) => {
      strip.appendChild(el("span", text,
        i === arr.length - 2 ? "chip strong" : "chip"));
    });
  }

  async function unlock(event) {
    event.preventDefault();
    const pin = $("#pin-input").value.trim();
    try {
      await api("/api/teacher/unlock", {body: {pin}});
      config = await api("/api/teacher/config");
      enterApp();
    } catch (err) {
      const box = $("#unlock-error");
      box.textContent = err.status === 429
        ? "Too many attempts; wait a minute."
        : "Unlock failed.";
      box.hidden = false;
    }
  }

  /* ---------- single always-on poll loop ---------- */

  function pollLoop() {
    if (!unlocked) return;
    setTimeout(async () => {
      if (unlocked) await pollOnce();
      pollLoop();
    }, POLL_MS);
  }

  async function pollOnce() {
    if (pollInFlight) return;
    pollInFlight = true;
    try {
      const payload = await api("/api/teacher/sessions");
      const incoming = payload.sessions || [];
      if (JSON.stringify(incoming) !== JSON.stringify(sessions)) {
        sessions = incoming;
        renderList();
      }
      teacherError(null);
      const sid = selected; // capture: user may switch mid-fetch
      if (sid) {
        const view = await api(`/api/teacher/sessions/${sid}`);
        if (sid !== selected) return; // stale fetch; discard
        // Skip the redraw entirely when the fetched view is identical;
        // detail is null after a session switch, which always renders.
        const changed =
          detail === null ||
          JSON.stringify(view) !== JSON.stringify(detail);
        detail = view;
        if (changed) renderDetail();
      }
      // refresh call-budget usage and model state
      const cfg = await api("/api/teacher/config");
      config = cfg;
      renderConfigStrip();
    } catch (err) {
      if (err.status === 403) {
        unlocked = false;
        $("#teacher-app").hidden = true;
        $("#config-strip").hidden = true;
        $("#unlock-panel").hidden = false;
        return;
      }
      teacherError("Refresh failed; retrying automatically.");
    } finally {
      pollInFlight = false;
    }
  }

  /* ---------- session list ---------- */

  function renderList() {
    const list = $("#session-list");
    list.replaceChildren();
    const query = $("#session-search").value.trim().toLowerCase();
    const visible = sessions.filter((s) => `${s.display_name} ${s.label} ${s.status}`.toLowerCase().includes(query));
    if (!visible.length) list.appendChild(el("li", "No matching sessions. Create a synthetic example above.", "meta small"));
    visible.forEach((s) => {
      const wrapper = el("li");
      const li = el("button", null,
        "session-row" + (s.session_id === selected ? " active" : ""));
      li.type = "button";
      li.setAttribute("aria-pressed", String(s.session_id === selected));
      li.appendChild(el("span", s.display_name || s.label, "label"));
      li.appendChild(el("span", new Date(s.created_at).toLocaleString(), "meta"));
      li.appendChild(el("span",
        `${s.answered_count}/40 answered · ${statusLabel(s.status)} · ` +
        `Feedback: ${statusLabel(s.provider_job.status)}`, "meta"));
      const diag = s.diagnostics || {};
      li.appendChild(el("span",
        `Diagnostics: ${statusLabel(diag.end)} · ${s.review_count} review notes`, "meta"));
      li.addEventListener("click", () => {
        if (selected === s.session_id) return;
        selected = s.session_id;
        detail = null;
        $("#ws-detail").hidden = true;
        $("#ws-empty").hidden = false;
        renderList();
        pollOnce(); // immediate fetch for the newly selected session
      });
      wrapper.appendChild(li);
      list.appendChild(wrapper);
    });
  }

  /* ---------- events ---------- */

  $("#session-search").addEventListener("input", renderList);
  $("#unlock-form").addEventListener("submit", unlock);
  $("#logout").addEventListener("click", async () => {
    try { await api("/api/teacher/logout", {body: {}}); } catch (e) {}
    unlocked = false;
    location.reload();
  });
  $("#sim-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (simRunning) return;
    const seed = Number($("#sim-seed").value);
    if (!Number.isInteger(seed)) {
      const err = $("#sim-error");
      err.textContent = "Seed must be an integer.";
      err.hidden = false;
      return;
    }
    simRunning = true;
    $("#sim-run").disabled = true;
    $("#sim-error").hidden = true;
    try {
      const result = await api("/api/teacher/simulations", {
        body: {profile: $("#sim-profile").value, seed, bank_mode: $("#sim-bank").value}});
      simLinks.set(result.session_id, result.student_url);
      selected = result.session_id;
      detail = null;
      renderList();
      await pollOnce();
      if (selected === result.session_id) renderDetail();
    } catch (err) {
      const box = $("#sim-error");
      box.textContent = "Synthetic example could not be created.";
      box.hidden = false;
    } finally {
      simRunning = false;
      $("#sim-run").disabled = false;
    }
  });
  boot();
}

function createDetailView(root, options = {}) {
  const prefix = options.prefix || "";
  const $ = (selector) => root.querySelector(selector.replace(/#([\w-]+)/g, (_, id) => `#${prefix}${id}`));
  const TABS = ["overview", "evidence", "graph", "research", "feedback", "review"];
  const STALE_NOTE = "The feedback draft changed. Re-read it before reviewing the new version.";
  let detail = null, selected = null, tab = "overview", researchCp = "end";
  let contract = null;
  const drafts = new Map();   // "sid:audience" -> draft state
  const releaseDrafts = new Map();
  let reviewRenderKey = null; // rebuild review form only when this changes
  // scopeKey -> Map(disclosureKey -> open). Scope is derived from the
  // pane's last rendered scope so a session/checkpoint switch never
  // transfers another context's expand/collapse choices.
  const disclosureScopes = new Map();
  const pollOnce = () => options.refresh?.();

  /* ---------- shared render helpers ---------- */

  function kv(label, value) {
    const row = el("div", null, "kv");
    row.appendChild(el("span", label, "k"));
    row.appendChild(el("span",
      value === null || value === undefined ? "—" : String(value), "v"));
    return row;
  }

  function jsonDetails(title, value, key) {
    const d = el("details", null, "raw");
    if (key) d.dataset.disclosureKey = key;
    d.appendChild(el("summary", title));
    d.appendChild(el("p",
      "Original source JSON (untranslated)", "meta small"));
    d.appendChild(el("pre", JSON.stringify(value, null, 2), "mono"));
    return d;
  }

  /* Teacher-only view of Aitta wording that failed validation. The
     content is rendered exclusively through textContent inside a <pre>;
     it is never used for the Current preview, student snapshot, or any
     release action. Older captures may record the rejection without
     retaining the wording. */
  function rejectedDraftDetails(job) {
    const gen = ((((job || {}).executions || {}).student || {})
                 .generator) || {};
    const draft = gen.rejected_draft || null;
    const code = ((gen.metadata || {}).rejection_code)
      || gen.rejection_code || null;
    if (!draft && !code) return null;
    const box = el("details", null, "raw rejected-draft");
    box.dataset.disclosureKey = "rejected-aitta-draft";
    box.appendChild(el("summary", "Rejected Aitta draft (unvalidated)"));
    const inner = el("div");
    if (code) {
      inner.appendChild(kv("Rejection reason", String(code)));
    }
    inner.appendChild(el("p",
      "This draft failed validation and is not used in the Current " +
      "preview or any automatic release. The currently displayed " +
      "draft remains the fallback.", "warn"));
    if (draft) {
      if (draft.truncated) {
        inner.appendChild(el("p",
          "The response was incomplete or the retained wording " +
          "reached the 32768-character limit.", "meta small"));
      }
      const pre = el("pre", null, "mono");
      pre.textContent = draft.content || "";
      inner.appendChild(pre);
    } else {
      inner.appendChild(el("p",
        "Rejected wording is unavailable in this capture.", "meta"));
    }
    box.appendChild(inner);
    return box;
  }

  /* Disclosure preservation: before a pane is redrawn, the open state
     of every keyed <details> in the live DOM is recorded under the scope
     that produced that DOM (pane.dataset.renderedScope). After the
     renderer finishes — including early-return paths — the new DOM's
     keyed disclosures are set from the incoming scope's saved map. */

  function renderPane(name, renderer) {
    const pane = $(`#tab-${name}`);
    const scope = `${selected}|${name}` +
      (name === "research" ? `|${researchCp}` : "");
    const prevScope = pane.dataset.renderedScope;
    if (prevScope) {
      const saved = disclosureScopes.get(prevScope) || new Map();
      pane.querySelectorAll("details[data-disclosure-key]")
        .forEach((d) => saved.set(d.dataset.disclosureKey, d.open));
      disclosureScopes.set(prevScope, saved);
    }
    try {
      renderer();
    } finally {
      const saved = disclosureScopes.get(scope);
      if (saved) {
        pane.querySelectorAll("details[data-disclosure-key]")
          .forEach((d) => {
            if (saved.has(d.dataset.disclosureKey)) {
              d.open = saved.get(d.dataset.disclosureKey);
            }
          });
      }
      pane.dataset.renderedScope = scope;
    }
  }

  function renderDetail() {
    if (!detail) return;
    options.onShow?.();
    root.hidden = false;
    $("#ws-title").textContent =
      `${detail.display_name || detail.label} — ${detail.answered_count}/40 answered`;
    $("#ws-export").href = detail.export_url || (detail.read_only ? `/api/teacher/scenarios/${selected}/export` : `/api/teacher/sessions/${selected}/export`);
    const linkBox = $("#ws-student-link");
    linkBox.replaceChildren();
    const link = options.studentLink?.(selected);
    if (link) {
      const a = el("a", "Open student view", "ghost small");
      a.href = link;
      a.target = "_blank";
      a.rel = "noopener";
      linkBox.appendChild(a);
    } else if (detail.feedback_delivery && detail.feedback_delivery.released_feedback) {
      saveReleasedStudentSnapshot(detail, detail.feedback_delivery.released_feedback, false);
      const a = el("a", "Open student view", "ghost small");
      a.href = "/";
      a.addEventListener("click", () => {
        saveReleasedStudentSnapshot(detail, detail.feedback_delivery.released_feedback, true);
      });
      linkBox.appendChild(a);
    }
    renderPane("overview", renderOverviewContent);
    renderPane("evidence", renderEvidenceContent);
    renderPane("graph", renderGraphContent);
    renderPane("research", renderResearchContent);
    renderPane("feedback", renderFeedbackContent);
    renderPane("review", renderReviewContent);
  }

  /* ---------- tabs ---------- */

  function renderOverviewContent() {
    const pane = $("#tab-overview");
    pane.replaceChildren();
    const card = el("div", null, "card-inner");
    card.appendChild(el("p", detail.read_only ? detail.read_only_reason || "Retained original results — no computation was run when opening this case." :
      detail.replay ? "New replay — the original answers were resubmitted through the running pipeline." :
      "Live synthetic session.", "boundary-note"));
    card.appendChild(kv("Status", statusLabel(detail.status)));
    card.appendChild(kv("Answered", `${detail.answered_count}/40`));
    card.appendChild(kv("Observed total",
      `${detail.observed_total.correct} / ${detail.observed_total.out_of}`));
    card.appendChild(kv("Provider job",
      `${detail.provider_job.status} (${detail.provider_job.provider_mode})`));
    ["midpoint", "end"].forEach((cp) => {
      const block = detail.checkpoints[cp] || {};
      card.appendChild(kv(`${cp} diagnostics`,
        (block.diagnostics_job || {}).status || "not_requested"));
    });
    if (detail.description) card.appendChild(el("p", detail.description, "meta"));
    if (detail.replay_changes) {
      const changes = detail.replay_changes;
      card.appendChild(el("h3", "What changed from the retained original?"));
      card.appendChild(kv("Observed correct-answer change", changes.observed_correct_delta));
      card.appendChild(kv("Largest KT probability change", changes.kt_max_absolute_delta ?? "Not comparable / unavailable"));
      changes.feedback.forEach((row) => {
        card.appendChild(kv(`${row.audience === "student" ? "Student" : "Educator"} draft`,
          `${row.text_changed ? "Text changed" : "Same text"}; ${row.candidate_changed ? "different focus" : "same focus"}`));
        const disclosure = el("details", null, "raw");
        disclosure.dataset.disclosureKey = `replay-diff:${row.audience}`;
        disclosure.append(el("summary", `Compare ${row.audience} wording with the original`));
        const pair = el("div", null, "compare");
        [["Retained original", row.previous_text], ["This replay", row.current_text]].forEach(([title, text]) => {
          const col = el("div", null, "col");
          col.append(el("h4", title), el("p", text, "draft-text")); pair.append(col);
        });
        disclosure.append(pair); card.append(disclosure);
      });
      card.appendChild(el("p", changes.interpretation, "meta"));
    }
    card.appendChild(jsonDetails("Technical identity and provenance", {...detail.provenance,
      technical_label: detail.label, session_id: detail.session_id, simulation: detail.simulated, replay: detail.replay},
      "provenance"));
    pane.appendChild(card);
  }

  function renderEvidenceContent() {
    const pane = $("#tab-evidence");
    pane.replaceChildren();
    const card = el("div", null, "card-inner");
    const evidence = (detail.checkpoints.end || {}).evidence;
    if (evidence) {
      card.appendChild(el("h3",
        `Observed totals: ${evidence.total.correct} correct / ` +
        `${evidence.total.incorrect} incorrect of ${evidence.total.out_of}`));
      const table = el("table", null, "grid");
      table.appendChild(el("caption", "Skill and subtopic observed counts"));
      const head = el("tr");
      ["Skill", "Subtopic", "Correct", "Incorrect", "Out of"]
        .forEach((h) => head.appendChild(el("th", h)));
      table.appendChild(head);
      evidence.subtopics.forEach((row) => {
        const tr = el("tr");
        [englishSkillName(row.skill_name), row.subtopic_name,
         row.correct, row.incorrect, row.out_of]
          .forEach((v) => tr.appendChild(el("td", String(v))));
        table.appendChild(tr);
      });
      card.appendChild(table);
    } else {
      card.appendChild(el("p",
        "End evidence appears once all 40 answers are in."));
    }
    if (detail.response_notice) card.appendChild(el("p", detail.response_notice, "meta"));
    if (detail.question_responses.length) {
      card.appendChild(el("h3", "Per-question record (private)"));
      detail.question_responses.forEach((row) => {
        const d = el("details", null, "qrow");
        d.dataset.disclosureKey = `q:${row.question_id}`;
        d.appendChild(el("summary",
          `#${row.position} · ${skillNameForId(row.skill_id)} · ` +
          `${row.correct ? "Correct" : "Incorrect"} · ${row.subtopic_name}`));
        const inner = el("div", null, "qrow-body");
        inner.appendChild(kv("Question", englishQuestionText(row)));
        inner.appendChild(kv("Subtopic",
          `${row.subtopic_name} (${row.subtopic_id})`));
        inner.appendChild(kv("Chosen",
          englishOptionText(row.options[row.selected_index])));
        inner.appendChild(kv("Correct option",
          englishOptionText(row.options[row.answer_index])));
        inner.appendChild(kv("Answered at", row.answered_at || "Not recorded in the original scenario"));
        inner.appendChild(kv("Question reference", row.question_id));
        d.appendChild(inner);
        card.appendChild(d);
      });
    }
    pane.appendChild(card);
  }

  function graphFor() {
    // Prefer the observed graph stored at admission; fall back to the
    // diagnostics payload for sessions recorded before graph storage.
    const end = detail.checkpoints.end || {};
    const mid = detail.checkpoints.midpoint || {};
    return end.graph || (end.diagnostics || {}).graph
        || mid.graph || (mid.diagnostics || {}).graph || null;
  }

  // Display label for a skill_id: evidence skills first, then graph
  // topics; unknown ids keep the stable technical id.
  function skillNameForId(skillId) {
    const end = detail.checkpoints.end || {};
    const skills = (end.evidence || {}).skills || [];
    const topics = (graphFor() || {}).topics || [];
    const hit = skills.concat(topics).find((s) => s.skill_id === skillId);
    return hit ? englishSkillName(hit.skill_name) : skillId;
  }

  function renderGraphContent() {
    const pane = $("#tab-graph");
    pane.replaceChildren();
    const context = detail.content_context;
    if (context && context.exercises) {
      const content = el("div", null, "card-inner");
      content.appendChild(el("h3", `${detail.bank_label || "Assessment"} · descriptive content and practice`));
      content.appendChild(el("p",
        `Graph v3 · ${context.practice_question_count} practice drafts · pending formal educator review. ` +
        "Review does not approve learner delivery. These descriptions do not explain incorrect answers.", "warn"));
      for (const role of ["assessment", "practice"]) {
        const rows = context.exercises.filter((row) => row.role === role);
        if (!rows.length) continue;
        const section = el("details");
        section.appendChild(el("summary", `${role === "practice" ? "Practice drafts" : "Assessed tasks"} (${rows.length})`));
        rows.forEach((row) => {
          const item = el("div", null, "topic");
          item.appendChild(el("h4", englishQuestionText(row)));
          const options = el("ol");
          row.options.forEach((option) => options.appendChild(el("li", englishOptionText(option))));
          item.appendChild(options);
          item.appendChild(el("p", `${row.concept_label}: ${row.mathematical_task}`));
          item.appendChild(el("p", row.interpretation_boundary, "meta small"));
          section.appendChild(item);
        });
        content.appendChild(section);
      }
      pane.appendChild(content);
    }
    const card = el("div", null, "card-inner");
    card.appendChild(el("p",
      "Assessment structure only: subtopics are part-of groupings, " +
      "not prerequisites.", "meta"));
    const graph = graphFor();
    if (!graph) {
      card.appendChild(el("p",
        "Graph appears once a checkpoint is reached."));
      pane.appendChild(card);
      return;
    }
    card.appendChild(el("p",
      `Observed counts at ${graph.checkpoint} checkpoint.`, "meta"));
    (graph.topics || []).forEach((topic) => {
      const box = el("div", null, "topic");
      const heading = topic.out_of === 0
        ? `${englishSkillName(topic.skill_name)} — not yet assessed`
        : `${englishSkillName(topic.skill_name)} — ${topic.correct}/${topic.out_of} observed correct`;
      box.appendChild(el("h4", heading));
      const list = el("ul");
      (topic.subtopics || []).forEach((leaf) => {
        list.appendChild(el("li",
          leaf.out_of === 0
            ? `${leaf.subtopic_name}: not yet assessed`
            : `${leaf.subtopic_name}: ${leaf.correct} correct, ` +
              `${leaf.incorrect} incorrect of ${leaf.out_of}`));
      });
      box.appendChild(list);
      card.appendChild(box);
    });
    pane.appendChild(card);
  }

  function ktChart(diag) {
    const probs = diag.kt.p_correct_before_each_answer || [];
    if (!probs.length) return null;
    const w = 680, h = 220, padX = 44, padY = 26;
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    svg.setAttribute("class", "ktchart");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Research-only predicted probability before each answer, from zero to one.");
    const xFor = (i) =>
      padX + i * (w - 2 * padX) / Math.max(probs.length - 1, 1);
    const yFor = (p) => h - padY - p * (h - 2 * padY);
    // Axes: probability 0..1 on y, response position on x.
    [0, 0.25, 0.5, 0.75, 1].forEach((tick) => {
      const y = yFor(tick);
      const grid = document.createElementNS(SVGNS, "line");
      grid.setAttribute("x1", padX); grid.setAttribute("x2", w - padX);
      grid.setAttribute("y1", y); grid.setAttribute("y2", y);
      grid.setAttribute("class", "axis-grid");
      svg.appendChild(grid);
      const label = document.createElementNS(SVGNS, "text");
      label.setAttribute("x", 6); label.setAttribute("y", y + 4);
      label.setAttribute("class", "axis-label");
      label.textContent = Number(tick.toFixed(2)).toString();
      svg.appendChild(label);
    });
    const step = probs.length > 20 ? 10 : 5;
    for (let i = 0; i < probs.length; i += step) {
      const label = document.createElementNS(SVGNS, "text");
      label.setAttribute("x", xFor(i)); label.setAttribute("y", h - 8);
      label.setAttribute("class", "axis-label");
      label.setAttribute("text-anchor", "middle");
      label.textContent = String(i + 1);
      svg.appendChild(label);
    }
    const axisX = document.createElementNS(SVGNS, "text");
    axisX.setAttribute("x", w / 2); axisX.setAttribute("y", h - 2);
    axisX.setAttribute("class", "axis-label");
    axisX.setAttribute("text-anchor", "middle");
    axisX.textContent = "response position";
    svg.appendChild(axisX);
    const line = document.createElementNS(SVGNS, "polyline");
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", "#21615B");
    line.setAttribute("stroke-width", "2");
    line.setAttribute("points",
      probs.map((p, i) => `${xFor(i)},${yFor(p)}`).join(" "));
    svg.appendChild(line);
    probs.forEach((p, i) => {
      const dot = document.createElementNS(SVGNS, "circle");
      dot.setAttribute("cx", xFor(i));
      dot.setAttribute("cy", yFor(p));
      dot.setAttribute("r", "4");
      const observed = detail.question_responses[i];
      dot.setAttribute("fill",
        !observed ? "#566973" : observed.correct ? "#21615B" : "#C5603F");
      svg.appendChild(dot);
    });
    return svg;
  }

  function renderResearchContent() {
    const pane = $("#tab-research");
    pane.replaceChildren();
    const card = el("div", null, "card-inner");
    card.appendChild(el("p",
      "Private research diagnostics — frozen-model outputs, not mastery " +
      "evidence and not shown to students.", "warn"));
    card.appendChild(el("p",
      "KT was trained and predictively evaluated on real ViLLE data. It " +
      "estimates answer correctness, not mastery. The 40-question bank is " +
      "source-derived, but the simulated sessions are not additional " +
      "real-learner validation."));
    card.appendChild(el("p",
      "Why still research-only: predictive performance needs assessment-specific evaluation. These " +
      "estimates do not establish mastery, difficulty or learning benefit " +
      "and do not determine the feedback focus."));
    const picker = el("div", null, "cp-picker");
    [["midpoint", "Midpoint — first 20 answers"],
     ["end", "End — all 40 answers"]].forEach(([cp, label]) => {
      const b = el("button", label,
        "ghost small" + (researchCp === cp ? " active" : ""));
      b.type = "button";
      b.addEventListener("click", () => {
        researchCp = cp;
        renderPane("research", renderResearchContent);
      });
      picker.appendChild(b);
    });
    card.appendChild(picker);
    const block = detail.checkpoints[researchCp] || {};
    const job = block.diagnostics_job || {};
    const diag = block.diagnostics;
    if (!diag || diag.status !== "ready") {
      card.appendChild(el("p",
        `${researchCp} diagnostics: ${job.status || "not_requested"}` +
        (job.reason ? ` (${job.reason})` : "")));
      pane.appendChild(card);
      return;
    }
    card.appendChild(el("p", diag.mode === "frozen_replay_no_model_rerun" ?
      "Saved historical inference — not recomputed." : "Fresh inference using the frozen model; the model was not retrained.", "boundary-note"));
    card.appendChild(el("h3",
      `${researchCp === "end" ? "Final" : "Halfway"} checkpoint · ${diag.answer_count} answers`));
    card.appendChild(el("p",
      "Frozen KT pre-answer probability per response position " +
      "(axis 0–1); dots mark observed correctness " +
      "(teal=correct, rust=incorrect, grey=answer record unavailable).", "meta"));
    const chart = ktChart(diag);
    if (chart) card.appendChild(chart);
    card.appendChild(el("p", diag.scope_warning || "", "meta"));
    card.appendChild(jsonDetails("Full KT trace (raw JSON)", diag.kt,
      "kt-trace"));
    card.appendChild(jsonDetails("Provenance",
      (diag.kt || {}).provenance || {}, "provenance"));
    pane.appendChild(card);
  }

  const editDrafts = new Map();

  function releaseDraftFor(sid) {
    if (!releaseDrafts.has(sid)) {
      releaseDrafts.set(sid, {label: "", sending: false, note: null});
    }
    return releaseDrafts.get(sid);
  }

  function editDraftFor(sid, delivery) {
    if (!editDrafts.has(sid)) {
      editDrafts.set(sid, {hash: null, texts: {}, content: null,
                           document: null, document_blocks: null,
                           mode: "document",
                           dirty: false,
                           saving: false, note: null, stale: false,
                           awaitingPreview: false, savedAt: null});
    }
    const draft = editDrafts.get(sid);
    const preview = delivery.preview;
    if (preview) {
      const current = {};
      (preview.sections || []).forEach((s) => { current[s.kind] = s.text; });
      if (draft.awaitingPreview) {
        if (delivery.preview_sha256 === draft.hash) {
          draft.awaitingPreview = false;
          draft.texts = current;
          draft.content = contentProjection(preview);
          draft.document_blocks = feedbackDocumentBlocks(preview);
          draft.document = serializeDocumentBlocks(
            draft.document_blocks);
          draft.note = `Edited draft saved (${draft.savedAt}).`;
        } else if (delivery.edited_at && draft.savedAt
                   && delivery.edited_at > draft.savedAt) {
          draft.awaitingPreview = false;
          draft.dirty = true;
          draft.stale = true;
        }
      } else if (!draft.dirty || !draft.hash) {
        draft.hash = delivery.preview_sha256;
        draft.texts = current;
        draft.content = contentProjection(preview);
        draft.document_blocks = feedbackDocumentBlocks(preview);
        draft.document = serializeDocumentBlocks(
          draft.document_blocks);
      } else if (delivery.preview_sha256
                 && draft.hash !== delivery.preview_sha256) {
        draft.stale = true;
      }
    }
    return draft;
  }

  function renderReleaseCard(pane) {
    const delivery = detail.feedback_delivery;
    const card = el("div", null, "card-inner release-card");
    card.appendChild(el("h3", "Shared feedback — teacher preview and release"));
    card.appendChild(el("p",
      "This approval releases feedback and practice only within this " +
      "synthetic demo session. It does not certify the research bank or " +
      "authorize real-learner delivery.",
      "warn"));
    const statusText = {
      not_ready: "Not ready — the assessment is incomplete",
      awaiting_teacher_review:
        "Awaiting educator review — hidden from the student",
      released: "Released to the student"}[delivery.status]
      || delivery.status;
    card.appendChild(kv("Release status", statusText));
    const released = delivery.released_feedback;
    if (released) {
      saveReleasedStudentSnapshot(detail, released, Boolean(detail.replay));
      card.appendChild(kv("Released at", delivery.released_at));
      card.appendChild(kv("Reviewer", delivery.reviewer_label));
      const box = el("details", null, "raw");
      box.dataset.disclosureKey = "released-feedback";
      box.appendChild(el("summary",
        "Released snapshot — exactly what the student sees"));
      const inner = el("div");
      renderStructuredFeedback(inner, released);
      box.appendChild(inner);
      card.appendChild(box);
      if (delivery.preview_sha256 &&
          delivery.released_preview_sha256 !== delivery.preview_sha256) {
        card.appendChild(el("p",
          "The current draft differs from the released version; the " +
          "student still sees the released version above until a new " +
          "release is approved.", "meta"));
      }
    }
    const preview = delivery.preview;
    const end = detail.checkpoints.end || {};
    const job = detail.provider_job;
    const selReview = end.student_review || end.baseline_student;
    const strip = el("div", null, "release-status");
    const genExec = (((job || {}).executions || {}).student || {})
      .generator || {};
    const wording = endFeedbackSource(selReview, delivery, genExec);
    const srcRow = kv("Wording source", wording.label);
    srcRow.classList.add("end-wording-source");
    strip.appendChild(srcRow);
    if (wording.partial) {
      strip.appendChild(el("p",
        "AI authored selected wording; remaining text is " +
        "application-owned.", "meta"));
    }
    strip.appendChild(kv("Feedback generation", statusLabel(job.status)));
    const templateRow = kv("Base feedback template",
      feedbackTemplateLabel(preview, detail.feedback_variant));
    templateRow.classList.add("end-feedback-template");
    strip.appendChild(templateRow);
    strip.appendChild(kv("Current presentation",
      preview && preview.document_blocks
        ? "Teacher-edited formatted document"
        : preview && typeof preview.document_text === "string"
          ? "Teacher-edited document"
          : "Structured feedback cards"));
    const workedExamples = (preview && preview.review_examples) || [];
    if (preview && typeof preview.document_text === "string") {
      strip.appendChild(kv("Worked explanations",
        "Included in teacher-edited document"));
    } else if (workedExamples.length) {
      const providerCount = workedExamples.filter(
        (e) => e.explanation_source === "provider").length;
      const anyEdited = workedExamples.some((e) => e.teacher_edited);
      let expl;
      if (providerCount === workedExamples.length) {
        expl = hostedConfirmed(genExec, "aitta_")
          ? "Aitta-generated" : "AI-generated";
      } else if (providerCount) {
        expl = "Mixed AI and application explanations";
      } else {
        expl = "Application-owned";
      }
      if (anyEdited) expl += " with teacher edits";
      strip.appendChild(kv("Worked explanations", expl));
    }
    const activities = preview && preview.practice_activities;
    if (activities && activities.length) {
      activities.forEach((activity, i) => {
        const label = activity.topic_label
          ? `Practice ${i + 1} (${englishSkillName(activity.topic_label)})`
          : `Practice ${i + 1}`;
        strip.appendChild(kv(label, statusLabel(activity.status)));
      });
    } else {
      const practiceStatus = preview && preview.practice
        ? statusLabel(preview.practice.status)
        : (delivery.practice_status
           ? statusLabel(delivery.practice_status)
           : ((end.recommendation_job || {}).status
              ? statusLabel(end.recommendation_job.status) : "—"));
      strip.appendChild(kv("Practice selection", practiceStatus));
    }
    if (selReview) {
      const selectedOriginals = selReview.selected_candidates || [];
      if (selectedOriginals.length) {
        selectedOriginals.forEach((original, i) => {
          const label = selectedOriginals.length > 1
            ? `Review focus ${i + 1}` : "Review focus";
          strip.appendChild(kv(label,
            focusLabel(original, original.candidate_id)));
        });
      } else {
        const applied = (selReview.candidates || []).find(
          (c) => c.candidate_id === selReview.selected_candidate_id);
        strip.appendChild(kv("Review focus", applied
          ? focusLabel(applied, selReview.selected_candidate_id)
          : "Recorded candidate not found"));
      }
      if ((selReview.trace || {}).guardrails_version
          && delivery.preview_source !== "teacher_edited") {
        strip.appendChild(kv("Feedback safeguards",
          String((selReview.trace || {}).guardrails_version || "")
            .startsWith("provider_full_narrative_shape_checked_")
            ? "All four narrative sections are provider-drafted and " +
              "shape-checked, not fact-verified. Counts, selected " +
              "topics, answer keys, checked maths and practice cards " +
              "remain application-owned."
            : "Strengths, review focus and next steps are " +
              "application-owned."));
      }
    }
    if (detail.feedback_variant === "worked_first" && preview
        && String((selReview && (selReview.trace || {})
                  .guardrails_version) || "")
           .startsWith("provider_full_narrative_shape_checked_")) {
      strip.appendChild(el("p",
        "Aitta narrative is shape-checked, not fact-verified; " +
        "educator review is required.", "warn"));
    }
    const fallback = (selReview && (selReview.trace || {})
                      .fallback_reason) || null;
    if (fallback) {
      strip.appendChild(el("p",
        `A provider fallback was recorded ` +
        `(${String(fallback).replaceAll("_", " ")}). ` +
        "Review the current preview before approval.", "warn"));
    }
    if (!end.student_review && job.status === "pending"
        && job.provider_mode === "hosted") {
      strip.appendChild(el("p",
        "Hosted feedback is still processing. The current preview " +
        "uses the deterministic baseline unless you have saved " +
        "teacher edits.", "warn"));
    }
    const diagnosticsPending = Object.values(detail.checkpoints || {})
      .some((checkpoint) =>
        checkpoint && checkpoint.diagnostics_job
        && checkpoint.diagnostics_job.status === "pending");
    if ((job.status === "ready" || job.status === "fallback")
        && diagnosticsPending) {
      strip.appendChild(kv("Model diagnostics", "Still running"));
      strip.appendChild(el("p",
        "Feedback is ready; independent model diagnostics are still " +
        "running.", "boundary-note"));
    }
    card.appendChild(strip);
    if (!preview) {
      const waiting = ["not_requested", "pending",
                       "waiting_for_practice",
                       "waiting_for_diagnostics"].includes(job.status);
      card.appendChild(el("p",
        delivery.practice_status_message ||
        (waiting
          ? "Feedback is still being prepared on the server; " +
            "no preview is available yet."
          : "No final feedback preview yet. A preview appears once " +
            "the assessment is complete.")));
      pane.appendChild(card);
      return;
    }
    card.appendChild(el("h4",
      "Current preview — exactly what approval would release"));
    const shown = el("div", null, "feedback-preview");
    renderStructuredFeedback(shown, preview);
    card.appendChild(shown);
    if (delivery.deterministic_preview) {
      const compare = el("details", null,
        "raw end-deterministic-comparison");
      compare.dataset.disclosureKey = "end-deterministic-comparison";
      compare.appendChild(el("summary",
        "Deterministic end feedback - comparison"));
      const inner = el("div");
      inner.appendChild(el("p",
        "Deterministic feedback is a reference, not a hallucination " +
        "detector. Compare claims against the recorded evidence. " +
        "This comparison is not the preview being approved.", "meta"));
      if (delivery.deterministic_preview.template_version) {
        inner.appendChild(kv("Deterministic template version",
          delivery.deterministic_preview.template_version));
      }
      renderStructuredFeedback(inner, delivery.deterministic_preview);
      compare.appendChild(inner);
      card.appendChild(compare);
    }
    if (preview.practice && preview.practice.status === "disabled"
        && delivery.practice_hint) {
      card.appendChild(el("p", delivery.practice_hint, "meta"));
    }
    const draft = releaseDraftFor(selected);
    const edit = editDraftFor(selected, delivery);
    if (delivery.preview_source === "teacher_edited") {
      card.appendChild(kv("Last edited",
        `${delivery.edited_at || "—"} by ` +
        `${delivery.edit_reviewer_label || "anonymous"}`));
    }
    const editStatus = el("p", edit.note || null, "meta");
    const sectionInputs = {};
    const contentInputs = [];
    card.appendChild(el("p",
      "Scores, answer keys and practice tasks are application-owned. " +
      "End feedback still requires educator approval.",
      "boundary-note"));
    if (edit.dirty && edit.stale) {
      card.appendChild(el("p",
        "The feedback draft changed on the server since these edits " +
        "were started. Review the latest preview, then re-apply your " +
        "edits to it with the button below.", "warn"));
    }
    const editBox = el("details", null, "raw");
    editBox.dataset.disclosureKey = "feedback-editing";
    editBox.appendChild(el("summary", "Edit full feedback package"));
    const docWrap = el("span", "Edit full feedback");
    docWrap.id = `${prefix}edit-document-label`;
    const docField = document.createElement("div");
    docField.id = `${prefix}edit-document`;
    docField.className = "edit-document";
    docField.contentEditable = "true";
    docField.setAttribute("role", "textbox");
    docField.setAttribute("aria-multiline", "true");
    docField.setAttribute("aria-labelledby", docWrap.id);
    docField.tabIndex = 0;
    setFeedbackEditorEnabled(docField,
      !(edit.saving || edit.awaitingPreview
        || edit.mode === "structured"));
    if (edit.document_blocks === null
        || edit.document_blocks === undefined) {
      edit.document_blocks = feedbackDocumentBlocks(preview);
      edit.document = serializeDocumentBlocks(edit.document_blocks);
    }
    renderFeedbackDocument(docField, edit.document_blocks);
    docWrap.htmlFor = docField.id;
    const syncDocument = () => {
      const blocks = documentBlocksFromDom(docField);
      edit.document_blocks = blocks;
      edit.document = serializeDocumentBlocks(blocks);
      edit.dirty = true;
      button.disabled = true;
      if (edit.document.length > 32768) {
        editStatus.textContent =
          `Document exceeds 32768 characters ` +
          `(${edit.document.length}); shorten it before saving.`;
      } else if (!edit.stale && !edit.note) {
        editStatus.textContent =
          "Unsaved edits — save the edited draft before releasing.";
      }
    };
    docField.addEventListener("beforeinput", (ev) => {
      if (ev.inputType && !/^(insertText|insertParagraph|insertLineBreak|deleteContent|deleteByCut|deleteByDrag|insertFromPaste|insertFromDrop|historyUndo|historyRedo|insertCompositionText|deleteCompositionText|insertReplacementText)/.test(ev.inputType)) {
        ev.preventDefault();
      }
    });
    docField.addEventListener("paste", (ev) => {
      ev.preventDefault();
      const text = (ev.clipboardData
                    || window.clipboardData).getData("text/plain");
      if (text) {
        document.execCommand("insertText", false, text);
      }
    });
    docField.addEventListener("drop", (ev) => {
      ev.preventDefault();
      const text = ev.dataTransfer
        && ev.dataTransfer.getData("text/plain");
      if (text) {
        document.execCommand("insertText", false, text);
      }
    });
    docField.addEventListener("compositionstart", () => {
      docField.dataset.composing = "1";
    });
    docField.addEventListener("compositionend", () => {
      delete docField.dataset.composing;
    });
    docField.addEventListener("input", syncDocument);
    editBox.appendChild(docWrap);
    editBox.appendChild(docField);
    editBox.appendChild(el("p",
      "You are editing all displayed feedback wording, including " +
      "mathematical explanations; check it before approval. Scores " +
      "remain locked.", "meta"));
    const advanced = el("details", null, "raw feedback-editor-advanced");
    advanced.dataset.disclosureKey = "feedback-editing-advanced";
    advanced.appendChild(
      el("summary", "Advanced field-by-field editor"));
    advanced.appendChild(el("p",
      "Advanced edits use the structured original, not the " +
      "free-form document.", "meta"));
    (preview.sections || []).forEach((section) => {
      const field = document.createElement("textarea");
      field.id = `${prefix}edit-${section.kind}`;
      field.className = "edit-section";
      field.dataset.section = section.kind;
      field.maxLength = 2000;
      field.disabled = edit.saving || edit.awaitingPreview;
      field.rows = Math.max(3, Math.min(10,
        Math.ceil((section.text || "").length / 80)));
      field.value = Object.hasOwn(edit.texts, section.kind)
        ? edit.texts[section.kind] : (section.text || "");
      const wrap = el("label",
        `${section.title} (${edit.dirty ? "unsaved edits" : "saved draft"})`);
      wrap.htmlFor = field.id;
      field.addEventListener("input", () => {
        edit.texts[section.kind] = field.value;
        edit.dirty = true;
        edit.mode = "structured";
        setFeedbackEditorEnabled(docField, false);
        modeNote.textContent =
          "Saving the advanced structured fields on next save; the " +
          "document stays unchanged until you use it.";
        button.disabled = true;
        wrap.textContent = `${section.title} (unsaved edits)`;
        if (!edit.stale && !edit.note) {
          editStatus.textContent =
            "Unsaved edits — save the edited draft before releasing.";
        }
      });
      advanced.appendChild(wrap);
      advanced.appendChild(field);
      sectionInputs[section.kind] = field;
    });
    const contentField = (target, path, labelText, fallbackValue, rows) => {
      const current = contentPathGet(edit.content, path);
      const value = current !== undefined
        ? (Array.isArray(current) ? current.join("\n") : current)
        : fallbackValue;
      const field = document.createElement("textarea");
      field.id = `${prefix}edit-content-${path}`;
      field.className = "edit-content";
      field.dataset.contentPath = path;
      field.maxLength = 2000;
      field.disabled = edit.saving || edit.awaitingPreview;
      field.rows = rows || Math.max(2, Math.min(8,
        Math.ceil(String(value || "").length / 80)));
      field.value = value === null || value === undefined
        ? "" : String(value);
      const wrap = el("label", labelText);
      wrap.htmlFor = field.id;
      field.addEventListener("input", () => {
        contentPathSet(edit.content, path, field.value);
        edit.dirty = true;
        edit.mode = "structured";
        setFeedbackEditorEnabled(docField, false);
        modeNote.textContent =
          "Saving the advanced structured fields on next save; the " +
          "document stays unchanged until you use it.";
        button.disabled = true;
        if (!edit.stale && !edit.note) {
          editStatus.textContent =
            "Unsaved edits — save the edited draft before releasing.";
        }
      });
      target.appendChild(wrap);
      target.appendChild(field);
      contentInputs.push(field);
    };
    const subSection = (parent, key, titleText) => {
      const box = el("details", null,
        "raw feedback-editor-section"
        + (parent.classList.contains("feedback-editor-section")
           ? " feedback-editor-subsection" : ""));
      box.dataset.disclosureKey = key;
      box.appendChild(el("summary", titleText));
      parent.appendChild(box);
      return box;
    };
    const reviewCards = preview.review_examples ||
      (preview.review_example ? [preview.review_example] : []);
    reviewCards.forEach((reviewCard, i) => {
      const orig = reviewCard.original_content || null;
      const changed = (f) => reviewCard.teacher_edited && (!orig
        || JSON.stringify(reviewCard[f]) !== JSON.stringify(orig[f]));
      const subtopic = reviewCard.for_subtopic_name
        ? (changed("for_subtopic_name")
           ? normalizeDisplayText(reviewCard.for_subtopic_name)
           : englishSkillName(reviewCard.for_subtopic_name))
        : "";
      const cardBox = subSection(advanced, `edit-review-card:${i}`,
        `Worked review card ${i + 1}${subtopic ? ` — ${subtopic}` : ""}`);
      if (Array.isArray(reviewCard.steps) && reviewCard.steps.length) {
        contentField(cardBox, `review_examples.${i}.steps`,
          EDIT_FIELD_LABELS.steps, reviewCard.steps.join("\n"),
          Math.max(3, reviewCard.steps.length + 1));
      } else if (Object.hasOwn(reviewCard, "explanation")) {
        contentField(cardBox, `review_examples.${i}.explanation`,
          "Explanation", reviewCard.explanation || "");
      }
      if (Object.hasOwn(reviewCard, "reasoning_question")) {
        contentField(cardBox, `review_examples.${i}.reasoning_question`,
          EDIT_FIELD_LABELS.reasoning_question,
          reviewCard.reasoning_question);
      }
      if (Object.hasOwn(reviewCard, "socratic_prompt")) {
        contentField(cardBox,
          `review_examples.${i}.socratic_prompt`,
          EDIT_FIELD_LABELS.socratic_prompt,
          reviewCard.socratic_prompt);
      }
      if (Object.hasOwn(reviewCard, "option_contrast")) {
        contentField(cardBox,
          `review_examples.${i}.option_contrast`,
          EDIT_FIELD_LABELS.option_contrast,
          reviewCard.option_contrast || "", 2);
      }
      if (Array.isArray(reviewCard.progressive_hints)
          && reviewCard.progressive_hints.length) {
        contentField(cardBox,
          `review_examples.${i}.progressive_hints`,
          EDIT_FIELD_LABELS.progressive_hints,
          reviewCard.progressive_hints.join("\n"),
          reviewCard.progressive_hints.length);
      }
      if (Object.hasOwn(reviewCard, "prompt")) {
        const displayPrompt = reviewCard.source
          === "incorrect_assessment_response"
          ? (changed("prompt")
             ? normalizeDisplayText(reviewCard.prompt)
             : englishQuestionText({text: reviewCard.prompt}))
          : reviewCard.prompt;
        contentField(cardBox, `review_examples.${i}.prompt`,
          EDIT_FIELD_LABELS.prompt, displayPrompt);
      }
      if (reviewCard.explanation
          && Array.isArray(reviewCard.steps)
          && reviewCard.steps.length
          && reviewCard.explanation !== reviewCard.steps.join("\n")) {
        contentField(cardBox, `review_examples.${i}.explanation`,
          EDIT_FIELD_LABELS.explanation, reviewCard.explanation);
      }
      const adv = subSection(cardBox, `edit-review-adv:${i}`,
        "Card options, labels & notice");
      if (Object.hasOwn(reviewCard, "title")) {
        contentField(adv, `review_examples.${i}.title`,
          EDIT_FIELD_LABELS.title, reviewCard.title, 1);
      }
      if (Object.hasOwn(reviewCard, "for_subtopic_name")) {
        contentField(adv, `review_examples.${i}.for_subtopic_name`,
          EDIT_FIELD_LABELS.for_subtopic_name, subtopic, 1);
      }
      if (Array.isArray(reviewCard.options)) {
        reviewCard.options.forEach((item, j) => {
          const optText = changed("options")
            ? normalizeDisplayText(item) : englishOptionText(item);
          contentField(adv, `review_examples.${i}.options.${j}`,
            `Option ${j + 1}`, optText, 1);
        });
      }
      if (Object.hasOwn(reviewCard, "selected_answer")) {
        const selText = changed("selected_answer")
          ? normalizeDisplayText(reviewCard.selected_answer)
          : englishOptionText(reviewCard.selected_answer);
        contentField(adv, `review_examples.${i}.selected_answer`,
          EDIT_FIELD_LABELS.selected_answer, selText, 1);
      }
      if (Object.hasOwn(reviewCard, "correct_answer")) {
        const corText = changed("correct_answer")
          ? normalizeDisplayText(reviewCard.correct_answer)
          : englishOptionText(reviewCard.correct_answer);
        contentField(adv, `review_examples.${i}.correct_answer`,
          EDIT_FIELD_LABELS.correct_answer, corText, 1);
      }
      if (Object.hasOwn(reviewCard, "notice")) {
        contentField(adv, `review_examples.${i}.notice`,
          EDIT_FIELD_LABELS.notice, reviewCard.notice);
      }
    });
    const practiceCards = preview.practice_activities ||
      (preview.practice ? [preview.practice] : []);
    practiceCards.forEach((practiceCard, i) => {
      const orig = practiceCard.original_content || null;
      const origQ = (orig || {}).question || null;
      const changed = (f) => practiceCard.teacher_edited && (!orig
        || JSON.stringify(practiceCard[f]) !== JSON.stringify(orig[f]));
      const qChanged = (f) => practiceCard.teacher_edited && (!origQ
        || JSON.stringify((practiceCard.question || {})[f])
           !== JSON.stringify(origQ[f]));
      const topic = practiceCard.topic_label
        ? (changed("topic_label")
           ? normalizeDisplayText(practiceCard.topic_label)
           : englishSkillName(practiceCard.topic_label))
        : "";
      const cardBox = subSection(advanced, `edit-practice-card:${i}`,
        `Practice card ${i + 1}${topic ? ` — ${topic}` : ""}`);
      if (practiceCard.relationship
          && Object.hasOwn(practiceCard.relationship, "message")) {
        contentField(cardBox,
          `practice_activities.${i}.relationship.message`,
          "Relationship note", practiceCard.relationship.message);
      }
      if (Object.hasOwn(practiceCard, "instruction")) {
        contentField(cardBox, `practice_activities.${i}.instruction`,
          EDIT_FIELD_LABELS.instruction, practiceCard.instruction);
      }
      const q = practiceCard.question;
      if (q && Object.hasOwn(q, "text")) {
        const qText = qChanged("text")
          ? normalizeDisplayText(q.text) : englishQuestionText(q);
        contentField(cardBox, `practice_activities.${i}.question.text`,
          EDIT_FIELD_LABELS.text, qText);
      }
      if (Object.hasOwn(practiceCard, "message")
          && (!practiceCard.relationship || changed("message")
              || practiceCard.status !== "selected")) {
        contentField(cardBox, `practice_activities.${i}.message`,
          EDIT_FIELD_LABELS.message, practiceCard.message);
      }
      const adv = subSection(cardBox, `edit-practice-adv:${i}`,
        "Practice options, labels & notice");
      if (Object.hasOwn(practiceCard, "topic_label")) {
        contentField(adv, `practice_activities.${i}.topic_label`,
          EDIT_FIELD_LABELS.topic_label, topic, 1);
      }
      if (Object.hasOwn(practiceCard, "message")
          && practiceCard.relationship && !changed("message")
          && practiceCard.status === "selected") {
        contentField(adv, `practice_activities.${i}.message`,
          EDIT_FIELD_LABELS.message, practiceCard.message);
      }
      if (q) {
        if (Object.hasOwn(q, "skill_name")) {
          const sName = qChanged("skill_name")
            ? normalizeDisplayText(q.skill_name)
            : englishSkillName(q.skill_name);
          contentField(adv,
            `practice_activities.${i}.question.skill_name`,
            EDIT_FIELD_LABELS.skill_name, sName, 1);
        }
        if (Object.hasOwn(q, "concept_name")) {
          const cName = qChanged("concept_name")
            ? normalizeDisplayText(q.concept_name)
            : englishSkillName(q.concept_name);
          contentField(adv,
            `practice_activities.${i}.question.concept_name`,
            EDIT_FIELD_LABELS.concept_name, cName, 1);
        }
        if (Array.isArray(q.options)) {
          q.options.forEach((item, j) => {
            const optText = qChanged("options")
              ? normalizeDisplayText(item) : englishOptionText(item);
            contentField(adv,
              `practice_activities.${i}.question.options.${j}`,
              `Practice option ${j + 1}`, optText, 1);
          });
        }
      }
      if (Object.hasOwn(practiceCard, "notice")) {
        contentField(adv, `practice_activities.${i}.notice`,
          EDIT_FIELD_LABELS.notice, practiceCard.notice);
      }
    });
    const advGlobal = subSection(advanced, "edit-advanced-labels",
      "Advanced headings, skill labels & notices");
    (preview.sections || []).forEach((section) => {
      contentField(advGlobal, `section_titles.${section.kind}`,
        `Heading for ${section.kind.replaceAll("_", " ")}`,
        section.title || "", 1);
    });
    (preview.skills || []).forEach((skill, i) => {
      const label = skill.teacher_edited
        ? normalizeDisplayText(skill.skill_name)
        : englishSkillName(skill.skill_name);
      contentField(advGlobal, `skill_labels.${i}`,
        `Skill ${i + 1} label`, label, 1);
    });
    if (Object.hasOwn(preview, "worked_review_notice")) {
      contentField(advGlobal, "worked_review_notice",
        "Worked review notice", preview.worked_review_notice || "");
    }
    const useDoc = el("button", "Use single editor", "ghost");
    useDoc.type = "button";
    useDoc.addEventListener("click", () => {
      edit.mode = "document";
      setFeedbackEditorEnabled(docField,
        !(edit.saving || edit.awaitingPreview));
      modeNote.textContent =
        "Saving the single document below on next save.";
      advanced.open = false;
    });
    advanced.appendChild(useDoc);
    editBox.appendChild(advanced);
    const modeNote = el("p",
      edit.mode === "document"
        ? "Saving the single document below on next save."
        : "Saving the advanced structured fields on next save; " +
          "the document stays unchanged until you use it.",
      "meta feedback-editor-mode");
    editBox.appendChild(modeNote);
    card.appendChild(editBox);
    const rebindBtn = el("button",
      "Use latest revision with my edits", "secondary");
    rebindBtn.type = "button";
    rebindBtn.hidden = !(edit.dirty && edit.stale);
    rebindBtn.addEventListener("click", () => {
      edit.hash = delivery.preview_sha256;
      edit.stale = false;
      rebindBtn.hidden = true;
      saveBtn.disabled = false;
      edit.note = null;
      editStatus.textContent =
        "Edits kept; the next save targets the latest revision.";
    });
    card.appendChild(rebindBtn);
    const saveBtn = el("button", "Save edited draft", "secondary");
    saveBtn.type = "button";
    saveBtn.disabled = edit.saving || edit.stale || edit.awaitingPreview;
    saveBtn.addEventListener("click", async () => {
      const sid = selected;
      const baseHash = edit.hash;
      if (edit.saving || edit.stale || edit.awaitingPreview || !baseHash) {
        return;
      }
      const sections = {};
      (preview.sections || []).forEach((s) => {
        sections[s.kind] = sectionInputs[s.kind].value;
      });
      const contentPayload = buildContentPayload(preview, edit.content);
      const isDocument = edit.mode === "document";
      edit.texts = sections;
      if (isDocument) {
        edit.document_blocks = documentBlocksFromDom(docField);
        edit.document =
          serializeDocumentBlocks(edit.document_blocks);
        if (!edit.document.trim()) {
          editStatus.textContent =
            "The document is empty; add feedback text before saving.";
          return;
        }
      }
      edit.dirty = true;
      edit.saving = true;
      saveBtn.disabled = true;
      button.disabled = true;
      setFeedbackEditorEnabled(docField, false);
      Object.values(sectionInputs).forEach((f) => { f.disabled = true; });
      contentInputs.forEach((f) => { f.disabled = true; });
      let failed = false;
      try {
        const body = isDocument
          ? {message_sha256: baseHash,
             document_text: edit.document,
             document_blocks: edit.document_blocks,
             reviewer_label: draft.label || "anonymous"}
          : {message_sha256: baseHash, sections: sections,
             reviewer_label: draft.label || "anonymous"};
        if (!isDocument && contentPayload) {
          body.content = contentPayload;
        }
        const res = await api(
          `/api/teacher/sessions/${sid}/feedback-edits`, {body});
        edit.hash = res.preview_sha256;
        edit.savedAt = res.saved_at;
        edit.awaitingPreview = true;
        edit.dirty = false;
        edit.stale = false;
        edit.note = "Edited draft saved; waiting for the saved " +
          "preview before approval.";
        pollOnce();
      } catch (err) {
        failed = true;
        if (err.status === 409) {
          edit.stale = true;
          edit.note = "The displayed feedback changed before saving. " +
            "Review the latest revision, then use it with your edits.";
          pollOnce();
        } else {
          edit.note = "The edited draft was not saved.";
        }
      } finally {
        edit.saving = false;
        saveBtn.disabled = edit.saving || edit.stale
          || edit.awaitingPreview;
        button.disabled = edit.dirty || edit.saving || edit.awaitingPreview
          || draft.sending || currentReleased;
        editStatus.textContent = edit.note;
        if (failed && selected === sid) {
          renderPane("feedback", renderFeedbackContent);
        }
      }
    });
    card.appendChild(saveBtn);
    card.appendChild(editStatus);
    const labelInput = document.createElement("input");
    labelInput.id = `${prefix}release-reviewer`;
    labelInput.maxLength = 80;
    labelInput.value = draft.label;
    labelInput.addEventListener("input", () => {
      draft.label = labelInput.value;
    });
    const labelWrap = el("label", "Reviewer label (optional)");
    labelWrap.htmlFor = labelInput.id;
    card.appendChild(labelWrap);
    card.appendChild(labelInput);
    const status = el("p", draft.note || null, "meta");
    const currentReleased = delivery.released_preview_sha256 &&
      delivery.released_preview_sha256 === delivery.preview_sha256;
    const multi = delivery.review_mode === "aligned_two_topic";
    if (delivery.integrated && !currentReleased) {
      card.appendChild(el("p",
        multi
          ? "One approval releases this feedback and all activities " +
            "shown above."
          : "One approval releases this feedback and the practice " +
            "activity shown above.", "meta"));
    }
    const button = el("button",
      currentReleased ? "Already released"
                      : (delivery.integrated
                         ? (multi
                            ? "Approve feedback and activities"
                            : "Approve feedback and practice")
                         : "Approve and release to student"),
      "primary");
    button.type = "button";
    button.disabled =
      !delivery.preview_sha256 || draft.sending || currentReleased ||
      edit.dirty || edit.saving || edit.awaitingPreview;
    button.addEventListener("click", async () => {
      const sid = selected;
      if (draft.sending || currentReleased || edit.dirty || edit.saving
          || edit.awaitingPreview) {
        return;
      }
      draft.sending = true;
      button.disabled = true;
      status.textContent = "Releasing…";
      try {
        await api(`/api/teacher/sessions/${sid}/release`, {
          body: {message_sha256: delivery.preview_sha256,
                 reviewer_label: draft.label || "anonymous"}});
        saveReleasedStudentSnapshot(detail, delivery.preview, true);
        draft.note = "Released to the student.";
        pollOnce();
      } catch (err) {
        if (err.status === 409) {
          draft.note = "The feedback changed before release. Re-read " +
            "the updated preview, then approve again.";
          pollOnce();
        } else {
          draft.note = "Release was not accepted.";
        }
      } finally {
        draft.sending = false;
        button.disabled = currentReleased || edit.dirty || edit.saving
          || edit.awaitingPreview;
      }
      status.textContent = draft.note;
    });
    card.appendChild(button);
    if (released) {
      const openLink = el("a", "Open in Assessment view", "ghost small");
      openLink.href = "/";
      openLink.addEventListener("click", () => {
        saveReleasedStudentSnapshot(detail, released, true);
      });
      card.appendChild(openLink);
    }
    card.appendChild(status);
    pane.appendChild(card);
  }

  function renderMidpointCard(pane) {
    const card = el("div", null, "card-inner midpoint-feedback-card");
    card.appendChild(el("h3", "Student midpoint check-in"));
    const midpoint = detail.checkpoints.midpoint || {};
    const job = midpoint.phrasing_job || null;
    const aittaActive = Boolean(
      job && job.status === "ready" && midpoint.provider_draft);
    const baseline = midpoint.student_message
      || (midpoint.baseline_student && midpoint.baseline_student.message);
    const message = aittaActive ? midpoint.provider_draft : baseline;
    if (!message) {
      card.appendChild(el("p",
        "No saved midpoint message is available in this result."));
    } else {
      card.appendChild(el("p",
        "Current student-facing wording from the 20-question " +
        "checkpoint, displayed with English topic labels. Aitta " +
        "wording is used automatically when the hosted draft is ready; " +
        "otherwise the deterministic check-in is shown. This record " +
        "does not confirm that the student opened or read it.",
        "meta"));
      const box = el("div", null, "midpoint-feedback-message");
      const sections = message.sections || [];
      if (sections.length) {
        sections.forEach((section) => {
          if (section.title) box.appendChild(el("h4", section.title));
          box.appendChild(el("p", englishMidpointText(section.text)));
          if (section.kind === "encouragement") {
            renderMidpointSummary(box, message);
          }
        });
      } else {
        renderMidpointSummary(box, message);
        box.appendChild(el("p", englishMidpointText(message.text || "")));
      }
      card.appendChild(box);
      card.appendChild(kv("Wording source",
        aittaActive ? "Aitta" : "Deterministic fallback"));
      if (aittaActive && baseline) {
        const compare = el("details", null,
          "raw midpoint-deterministic-comparison");
        compare.dataset.disclosureKey =
          "midpoint-deterministic-comparison";
        compare.appendChild(el("summary",
          "Deterministic midpoint - comparison"));
        const inner = el("div");
        if (midpoint.student_message_version) {
          inner.appendChild(kv("Deterministic template version",
            midpoint.student_message_version));
        }
        renderMidpointDraftMessage(inner, baseline, null);
        compare.appendChild(inner);
        card.appendChild(compare);
      }
    }
    renderMidpointProviderDraft(card, midpoint, aittaActive);
    pane.appendChild(card);
  }

  /* Midpoint generation details: Aitta wording is shown to the student
     automatically when ready; the deterministic check-in is the
     fallback. Rejected wording and execution records stay
     teacher-facing only. */
  function renderMidpointProviderDraft(card, midpoint, aittaActive) {
    const job = midpoint.phrasing_job || null;
    const draft = midpoint.provider_draft || null;
    if (!job && !draft) return;
    const box = el("details", null, "raw midpoint-provider-draft");
    box.dataset.disclosureKey = "midpoint-provider-draft";
    box.appendChild(el("summary", "Midpoint generation details"));
    const inner = el("div");
    inner.appendChild(el("p",
      "Aitta wording is shown automatically when ready. Counts and " +
      "the topic table are application-owned; wording is " +
      "shape-checked, not fact-verified.", "warn"));
    renderMidpointDraftJob(inner, job);
    renderMidpointVersionDetails(inner, midpoint, job, aittaActive);
    if (!aittaActive) {
      renderMidpointDraftMessage(inner, draft, job);
    }
    const rejected = (job && job.execution
      && job.execution.rejected_draft) || null;
    if (rejected) {
      const rej = el("details", null, "raw");
      rej.dataset.disclosureKey = "midpoint-rejected-wording";
      rej.appendChild(el("summary",
        "Rejected provider wording (unvalidated)"));
      const pre = el("pre", null, "mono");
      pre.textContent = rejected.content || "";
      rej.appendChild(pre);
      inner.appendChild(rej);
    }
    box.appendChild(inner);
    card.appendChild(box);
  }

  const MIDPOINT_JOB_STATUS = {
    ready: "Ready", pending: "Queued or in progress",
    fallback: "Deterministic fallback", unavailable: "Unavailable",
    not_requested: "Not requested"};
  const MIDPOINT_REASON_LABELS = {
    provider_error: "Provider request failed",
    demo_call_budget_exhausted: "Call budget exhausted",
    demo_provider_queue_capacity: "Provider queue full",
    interrupted_by_restart: "Interrupted by server restart",
    rules_mode_no_providers: "Rules-only session",
    midpoint_provider_not_configured: "Midpoint provider not " +
      "configured",
    service_closing: "Server shutting down"};
  const MIDPOINT_RETRY_STOP = {
    attempts_exhausted: "Attempts exhausted",
    budget_exhausted: "Call budget exhausted",
    non_retryable: "Non-retryable failure",
    retry_after_too_long: "Requested wait too long",
    shutdown: "Stopped during shutdown"};

  /* Optional collapsed version identifiers from saved records only —
     never invents or hardcodes prompt/message version constants. */
  /* Recorded version identifiers inline in the generation details —
     saved records only, no invented constants. The deterministic
     reference version lives inside its own comparison dropdown. */
  function renderMidpointVersionDetails(inner, midpoint, job,
                                        aittaActive) {
    const selected = aittaActive
      ? midpoint.provider_draft_version
      : midpoint.student_message_version;
    if (selected) {
      inner.appendChild(kv("Template version", selected));
    }
    const prompt = (job && job.execution
                    && job.execution.metadata
                    && job.execution.metadata.prompt_version)
      || (job && job.prompt_version) || null;
    if (prompt) inner.appendChild(kv("Prompt version", prompt));
  }

  function renderMidpointDraftJob(inner, job) {
    if (!job) return;
    inner.appendChild(kv("Generation status",
      MIDPOINT_JOB_STATUS[job.status] || String(job.status)));
    const exec = job.execution || {};
    const gmeta = exec.metadata || {};
    const transport = gmeta.transport || {};
    const model = exec.model_version || gmeta.model_version;
    if (model) inner.appendChild(kv("Model", model));
    if (exec.reused === true || exec.reused === false) {
      inner.appendChild(kv("Generation source",
        exec.reused ? "Reused saved response" : "New provider call"));
    }
    if (Number.isFinite(exec.original_call_latency_ms)) {
      inner.appendChild(kv("Call duration",
        `${(exec.original_call_latency_ms / 1000).toFixed(2)} ` +
        "seconds"));
    }
    const retry = exec.automatic_retry;
    if (retry && Number.isFinite(retry.attempts)) {
      inner.appendChild(kv("Attempts in this run",
        `${retry.attempts} of ${retry.max_attempts || 3}`));
      if (retry.stop_reason && retry.stop_reason !== "success") {
        inner.appendChild(kv("Retry outcome",
          MIDPOINT_RETRY_STOP[retry.stop_reason]
          || String(retry.stop_reason)));
      }
    }
    const usage = gmeta.usage || {};
    if (Number.isFinite(usage.total_tokens)) {
      inner.appendChild(kv("Tokens used", usage.total_tokens));
    }
    const failure = (() => {
      if (transport.failure_kind === "timeout") {
        return "Provider request timed out";
      }
      const parts = [
        FAILURE_KIND_LABELS.has(transport.failure_kind)
          ? String(transport.failure_kind).replaceAll("_", " ")
          : null,
        Number.isFinite(transport.http_status)
          ? `HTTP ${transport.http_status}` : null]
        .filter(Boolean);
      if (parts.length) return parts.join(", ");
      if (gmeta.rejection_code
          && gmeta.rejection_code !== "transport_failure") {
        return "Response rejected by validation";
      }
      if (job.reason) {
        return MIDPOINT_REASON_LABELS[job.reason]
          || "Generation unavailable";
      }
      return null;
    })();
    if (failure) inner.appendChild(kv("Failure", failure));
  }

  function renderMidpointDraftMessage(inner, draft, job) {
    if (draft && Array.isArray(draft.sections) && draft.sections.length) {
      const text = el("div", null, "midpoint-feedback-message");
      draft.sections.forEach((section) => {
        if (section.title) text.appendChild(el("h4", section.title));
        text.appendChild(el("p", englishMidpointText(
          section.text || "")));
        if (section.kind === "encouragement") {
          renderMidpointSummary(text, draft);
        }
      });
      inner.appendChild(text);
      return;
    }
    if (draft) {
      renderMidpointSummary(inner, draft);
      inner.appendChild(el("p", englishMidpointText(
        draft.text || "")));
      return;
    }
    if (job && job.status === "pending") {
      inner.appendChild(el("p",
        "Aitta midpoint drafting is queued or in progress. The " +
        "deterministic student check-in above remains unchanged.",
        "meta"));
      return;
    }
    const transport = (job && job.execution && job.execution.metadata
      && job.execution.metadata.transport) || null;
    if (transport && transport.failure_kind === "timeout") {
      inner.appendChild(el("p",
        "The Aitta request timed out before a draft was received. " +
        "The deterministic student check-in above remains unchanged.",
        "meta"));
      return;
    }
    inner.appendChild(el("p",
      "No Aitta draft was produced; the deterministic student " +
      "check-in above remains unchanged.", "meta"));
  }

  function renderFeedbackContent() {
    const pane = $("#tab-feedback");
    const active = document.activeElement;
    const composing = active && pane.contains(active)
      && active.classList.contains("edit-document")
      && active.dataset.composing === "1";
    if (composing) {
      return;  // never tear down the editor mid-IME composition
    }
    const focusedId = (active && pane.contains(active) && active.id
      && (active.id === `${prefix}release-reviewer`
          || active.classList.contains("edit-section")
          || active.classList.contains("edit-content")
          || active.classList.contains("edit-document")))
      ? active.id : null;
    const richCaret = (focusedId
        && active.classList.contains("edit-document"))
      ? documentSelectionOffsets(active) : null;
    const caret = (focusedId && richCaret === null
        && typeof active.setSelectionRange === "function")
      ? {start: active.selectionStart, end: active.selectionEnd}
      : null;
    pane.replaceChildren();
    renderMidpointCard(pane);
    if (!detail.read_only && detail.feedback_delivery) {
      renderReleaseCard(pane);
    } else if (detail.read_only) {
      const savedReview =
        (detail.checkpoints.end || {}).student_review;
      if (savedReview && savedReview.message) {
        const saved = el("div", null,
          "card-inner saved-end-feedback");
        saved.appendChild(el("h4", "Saved end feedback"));
        saved.appendChild(kv("Base feedback template",
          feedbackTemplateLabel(savedReview.message,
                                detail.feedback_variant)));
        const shown = el("div", null, "feedback-preview");
        renderStructuredFeedback(shown, {
          sections: savedReview.message.sections
            || (savedReview.message.text
                ? [{text: savedReview.message.text}] : []),
          review_examples: savedReview.review_examples});
        saved.appendChild(shown);
        const savedBaseline =
          (detail.checkpoints.end || {}).baseline_student;
        if (savedBaseline && savedBaseline.message) {
          const compare = el("details", null,
            "raw end-deterministic-comparison");
          compare.dataset.disclosureKey =
            "end-deterministic-comparison";
          compare.appendChild(el("summary",
            "Deterministic end feedback - comparison"));
          const inner = el("div");
          if (savedBaseline.message.template_version) {
            inner.appendChild(kv("Deterministic template version",
              savedBaseline.message.template_version));
          }
          renderStructuredFeedback(inner, {
            sections: savedBaseline.message.sections
              || (savedBaseline.message.text
                  ? [{text: savedBaseline.message.text}] : []),
            review_examples: savedBaseline.review_examples});
          compare.appendChild(inner);
          saved.appendChild(compare);
        }
        pane.appendChild(saved);
      }
    }
    if (focusedId) {
      requestAnimationFrame(() => {
        const input = document.getElementById(focusedId);
        if (!input) return;
        input.focus();
        if (richCaret !== null
            && input.classList.contains("edit-document")) {
          restoreDocumentSelection(input, richCaret);
        } else if (caret) {
          input.setSelectionRange(caret.start, caret.end);
        }
      });
    }
    const research = el("details", null, "raw");
    research.dataset.disclosureKey = "feedback-research";
    research.appendChild(el("summary", "End-point generation details"));
    const card = el("div", null, "card-inner");
    research.appendChild(card);
    const end = detail.checkpoints.end || {};
    card.appendChild(kv("Provider job",
      `${detail.provider_job.status} (${detail.provider_job.provider_mode})`));
    if (detail.provider_job.queued_at) {
      card.appendChild(kv("Queued", detail.provider_job.queued_at));
      card.appendChild(kv("Finished",
        detail.provider_job.finished_at || "—"));
    }
    const review = end.student_review;
    if (review || end.baseline_student) {
      const ev = el("div", null, "card-inner end-review-evidence");
      ev.appendChild(el("h4", "Review evidence"));
      const applied = ((review || end.baseline_student)
        .candidates || []).find(
        (c) => c.candidate_id
               === (review || end.baseline_student)
                  .selected_candidate_id);
      const originals = (review && review.selected_candidates)
        || (applied && applied.review_candidates)
        || (applied ? [applied] : []);
      originals.forEach((c) => {
        ev.appendChild(el("h5", focusLabel(c, c.candidate_id)));
        const focus = c.focus || null;
        if (focus) {
          ev.appendChild(kv("Observed",
            `${focus.correct === undefined ? "—" : focus.correct} ` +
            `correct, ${focus.incorrect} incorrect of ` +
            `${focus.out_of} assessed`));
        }
        const planning = c.planning || null;
        if (planning && planning.error_pattern) {
          ev.appendChild(kv("Error pattern",
            ERROR_PATTERN_LABELS[planning.error_pattern]
            || String(planning.error_pattern)));
        }
        if (planning && planning.assessment_coverage) {
          ev.appendChild(kv("Assessment coverage",
            COVERAGE_LABELS[planning.assessment_coverage]
            || String(planning.assessment_coverage)));
        }
      });
      const selSource = ((((review || end.baseline_student) || {})
        .trace) || {}).selection_source || null;
      ev.appendChild(kv("Review topics chosen by",
        selSource === "rules" || selSource === "rules_single_candidate"
          ? "Application rules"
          : (selSource === "injected_selector"
             ? "Provider selection (recorded)"
             : "Recorded selection (source unavailable)")));
      ev.appendChild(el("p",
        "Topic selection is separate from feedback wording."
        + ((selSource === "rules"
            || selSource === "rules_single_candidate")
           ? " Review topics are selected by deterministic rules; " +
             "KT estimates do not select review topics."
           : ""), "meta"));
      card.appendChild(ev);
    }
    const recommendation = end.recommendation;
    if (recommendation) {
      const evidenceBox = el("details", null, "raw");
      evidenceBox.dataset.disclosureKey = "practice-evidence";
      evidenceBox.appendChild(
        el("summary", "Practice selection evidence"));
      const inner = el("div");
      inner.appendChild(kv("Status",
        statusLabel(recommendation.status)));
      if (recommendation.reason) {
        inner.appendChild(kv("Reason", recommendation.reason));
      }
      if (recommendation.target_band) {
        inner.appendChild(kv(
          "Experimental probability band (not validated learning " +
          "difficulty)",
          recommendation.target_band.join("–")));
      }
      const predictions = recommendation.candidate_predictions || [];
      const selectedId = end.practice_context
        && end.practice_context.selected_question_id;
      const prediction = predictions
        .find((p) => p.question_id === selectedId);
      const p = prediction && prediction.p_correct;
      const practiceStatus = (((detail.feedback_delivery || {})
        .preview || {}).practice || {}).status;
      if (practiceStatus === "selected" && Number.isFinite(p)) {
        inner.appendChild(kv(
          "Estimated correctness on this question (not mastery)",
          p.toFixed(3)));
      }
      (recommendation.topic_selections || []).forEach((slot, i) => {
        const pred = predictions.find(
          (row) => row.question_id === slot.selected_question_id);
        inner.appendChild(kv(
          `Practice slot ${i + 1}`,
          `${statusLabel(slot.status)}` +
          (slot.status === "selected" && pred &&
           Number.isFinite(pred.p_correct)
             ? ` — estimated correctness ${pred.p_correct.toFixed(3)} ` +
               `(band ${recommendation.target_band.join("–")})`
             : "")));
      });
      evidenceBox.appendChild(inner);
      card.appendChild(evidenceBox);
    }
    const genExec = (((detail.provider_job.executions || {})
      .student) || {}).generator || {};
    const gmeta = genExec.metadata || {};
    const gen = el("div", null, "card-inner end-generation-details");
    gen.appendChild(el("h4", "Generation details"));
    const aitta = aittaProvenance(review, genExec);
    if (aitta) gen.appendChild(kv("Aitta provenance", aitta));
    const tpl = ((detail.feedback_delivery || {}).preview) || {};
    if (tpl.template_version) {
      gen.appendChild(kv("Template version", tpl.template_version));
    }
    if (gmeta.prompt_version) {
      gen.appendChild(kv("Prompt version", gmeta.prompt_version));
    }
    const model = genExec.model_version || gmeta.model_version;
    if (model) gen.appendChild(kv("Model", model));
    if (gmeta.status || typeof genExec.capture_file === "string") {
      gen.appendChild(kv("Capture",
        captureProvenanceKind(genExec, gmeta.status === "failed")));
    }
    if (Number.isFinite(genExec.original_call_latency_ms)) {
      gen.appendChild(kv("Call duration",
        `${genExec.original_call_latency_ms} ms`));
    }
    const retry = genExec.automatic_retry;
    if (retry && Number.isFinite(retry.attempts)) {
      gen.appendChild(kv("Attempts in this run",
        `${retry.attempts} of ${retry.max_attempts || 3} ` +
        `(${retry.stop_reason || "recorded"})`));
    }
    const usage = gmeta.usage || {};
    if (Number.isFinite(usage.total_tokens)) {
      gen.appendChild(kv("Tokens used", usage.total_tokens));
    }
    if (gmeta.status === "failed") {
      const transport = gmeta.transport || {};
      const detailText = [
        FAILURE_KIND_LABELS.has(transport.failure_kind)
          ? transport.failure_kind : null,
        FAILURE_STAGE_LABELS.has(gmeta.failure_stage)
          ? gmeta.failure_stage : null]
        .filter(Boolean).join(", ");
      gen.appendChild(kv("Failure",
        detailText || "provider call failed"));
    }
    if (review && (review.trace || {}).generation_context_version) {
      gen.appendChild(kv("Aitta input",
        "answered assessment, descriptive concept graph, checked " +
        "review cards and practice context; experimental KT " +
        "estimates may be included when available."));
      gen.appendChild(el("p",
        "Deterministic feedback is a reference, not a hallucination " +
        "detector. Check claims against the recorded evidence.",
        "meta"));
    }
    if (gen.childElementCount > 1) card.appendChild(gen);
    const rejected = rejectedDraftDetails(detail.provider_job);
    if (rejected) card.appendChild(rejected);
    // With a live release card, generation details belong with the
    // preview — directly after the deterministic comparison, before
    // the edit and approval controls. Otherwise append at the end.
    const releaseCard = pane.querySelector(".release-card");
    if (releaseCard && releaseCard.querySelector(".feedback-preview")) {
      const anchor = releaseCard.querySelector(
        ".end-deterministic-comparison")
        || releaseCard.querySelector(".feedback-preview");
      anchor.after(research);
    } else {
      pane.appendChild(research);
    }
  }

  /* ---------- review tab with per-session draft state ---------- */

  function draftKey(sid, audience) { return `${sid}:${audience}`; }

  function draftFor(sid, audience, currentHash) {
    const key = draftKey(sid, audience);
    if (!drafts.has(key)) {
      drafts.set(key, {
        judgments: {}, note: "", label: "",
        hash: currentHash, savedToast: null});
    }
    const draft = drafts.get(key);
    // First-ready bind: a draft opened while the provider job was still
    // pending holds hash null; adopt the first real hash. An already
    // bound non-null hash is never overwritten silently.
    if (draft.hash === null && currentHash) draft.hash = currentHash;
    return draft;
  }

  function renderReviewContent() {
    const pane = $("#tab-review");
    if (detail.read_only) {
      pane.replaceChildren(el("h3", "Saved result — read-only"), el("p",
        detail.read_only_reason || "Replay this scenario, then select the new result to record an educator review against its exact feedback version. Original reports and existing blind comparisons are never overwritten.", "boundary-note"));
      renderLedger(pane);
      return;
    }
    const hashes = detail.review_hashes || {};
    const audience = "student";
    const currentHash = hashes[audience] || null;
    const draft = draftFor(selected, audience, currentHash);
    const key = [selected, audience, currentHash,
                 (detail.reviews || []).length, draft.savedToast,
                 draft.stale].join("|");
    if (key === reviewRenderKey && pane.childNodes.length) {
      return; // poll left the form untouched; draft state is in DOM
    }
    reviewRenderKey = key;
    pane.replaceChildren();
    const card = el("div", null, "card-inner");
    card.appendChild(el("p", contract ? contract.notice : "", "warn"));

    card.appendChild(el("p", "Shared student-facing feedback", "meta"));

    if (!currentHash) {
      card.appendChild(el("p",
        "Reviews open once the end feedback draft exists."));
      renderLedger(card);
      pane.appendChild(card);
      return;
    }

    const stale = draft.hash !== currentHash || draft.stale;
    if (stale) {
      const warn = el("div", null, "warn");
      warn.appendChild(el("p", STALE_NOTE));
      const adopt = el("button", "Review current version", "primary small");
      adopt.type = "button";
      adopt.addEventListener("click", () => {
        draft.hash = currentHash;
        draft.stale = false;
        draft.judgments = {}; // choices must be re-checked
        draft.savedToast = null;
        reviewRenderKey = null;
        renderPane("review", renderReviewContent);
      });
      warn.appendChild(adopt);
      card.appendChild(warn);
    }
    card.appendChild(el("p",
      `Draft message bound: ${draft.hash.slice(0, 16)}…`,
      "mono small"));

    const form = el("form", null, "review-form");
    (contract.fields || []).forEach((field) => {
      const wrap = el("div", null, "field");
      const sel = el("select");
      sel.id = `${prefix}rev-field-${field.id}`;
      const fieldLabel = el("label", field.question);
      fieldLabel.htmlFor = sel.id;
      wrap.appendChild(fieldLabel);
      const blank = el("option", "— choose —");
      blank.value = "";
      sel.appendChild(blank);
      field.choices.forEach((c) => {
        const o = el("option", c);
        o.value = c;
        sel.appendChild(o);
      });
      sel.value = draft.judgments[field.id] || "";
      sel.addEventListener("change", () => {
        draft.judgments[field.id] = sel.value;
      });
      wrap.appendChild(sel);
      form.appendChild(wrap);
    });
    const note = document.createElement("textarea");
    note.id = `${prefix}rev-note`;
    note.maxLength = 3000;
    note.value = draft.note;
    note.addEventListener("input", () => { draft.note = note.value; });
    const label = document.createElement("input");
    label.id = `${prefix}rev-reviewer`;
    label.maxLength = 80;
    label.value = draft.label;
    label.addEventListener("input", () => { draft.label = label.value; });
    const noteLabel = el("label", "Note (≤3000 chars, plain text)");
    noteLabel.htmlFor = note.id;
    const reviewerLabel = el("label", "Reviewer label (optional)");
    reviewerLabel.htmlFor = label.id;
    form.appendChild(noteLabel);
    form.appendChild(note);
    form.appendChild(reviewerLabel);
    form.appendChild(label);
    const status = el("p", null, "meta");
    if (draft.savedToast) status.textContent = draft.savedToast;
    const save = el("button", "Save review note", "primary");
    save.type = "submit";
    form.appendChild(save);
    form.appendChild(status);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const judgments = {};
      let missing = false;
      (contract.fields || []).forEach((field) => {
        const v = draft.judgments[field.id] || "";
        if (!v) missing = true;
        judgments[field.id] = v;
      });
      if (missing) {
        status.textContent = "Answer all five review questions.";
        return;
      }
      save.disabled = true;
      status.textContent = "Saving…";
      try {
        await api(`/api/teacher/sessions/${selected}/reviews`, {
          body: {audience, message_sha256: draft.hash,
                 judgments, note: draft.note,
                 reviewer_label: draft.label || "anonymous"}});
        draft.savedToast =
          `Review note recorded — bound to ${draft.hash.slice(0, 16)}…`;
        draft.stale = false;
        status.textContent = draft.savedToast;
        reviewRenderKey = null;
        pollOnce(); // ledger refreshes separately
      } catch (err) {
        if (err.status === 409) {
          draft.stale = true;
          reviewRenderKey = null;
          renderPane("review", renderReviewContent);
        } else {
          status.textContent = "Review was not accepted.";
        }
      } finally {
        save.disabled = false;
      }
    });
    card.appendChild(form);
    renderLedger(card);
    pane.appendChild(card);
  }

  function renderLedger(card) {
    const ledger = el("div", null, "ledger");
    ledger.appendChild(el("h3", "Saved review notes"));
    (detail.reviews || []).forEach((entry) => {
      const row = el("div", null, "ledger-row");
      row.appendChild(el("p",
        `${entry.recorded_at} · ${entry.audience} · ` +
        `${entry.reviewer_label}`, "meta"));
      row.appendChild(el("p",
        `bound message ${entry.message_sha256.slice(0, 16)}… · ` +
        `candidate ${entry.selected_candidate_id}`, "mono small"));
      row.appendChild(jsonDetails("Judgments", entry.judgments,
        `judgments:${entry.audience}:${entry.message_sha256}:` +
        `${entry.recorded_at}`));
      if (entry.note) row.appendChild(el("p", entry.note, "note"));
      ledger.appendChild(row);
    });
    card.appendChild(ledger);
  }

  $("#ws-tabs").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-tab]");
    if (!button) return;
    tab = button.dataset.tab;
    root.querySelectorAll(".tab").forEach((b) => {
      b.classList.toggle("active", b === button);
      b.setAttribute("aria-pressed", String(b === button));
    });
    TABS.forEach((name) => { $(`#tab-${name}`).hidden = name !== tab; });
  });

  return {update(view) {
    if (JSON.stringify(view) === JSON.stringify(detail)) return;
    if (selected !== view.session_id) reviewRenderKey = null;
    detail = view;
    selected = view.session_id;
    contract = view.review_contract;
    renderDetail();
  }};
}

if (PAGE === "student") studentPage();
if (PAGE === "teacher") teacherPage();
