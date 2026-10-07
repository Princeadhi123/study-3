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
});

function englishSkillName(name) {
  return Object.prototype.hasOwnProperty.call(ENGLISH_SKILL_NAMES, name)
    ? ENGLISH_SKILL_NAMES[name] : name;
}

function englishQuestionText(question) {
  const text = (question && question.text) || "";
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
  return String(value)
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
    return "Local policy - only one permitted option; Jev not called";
  }
  if (source === "rules") {
    let text = "Deterministic policy (not a Jev selection)";
    if (trace.fallback_reason) text += ` - ${trace.fallback_reason}`;
    return text;
  }
  if (source === "injected_selector") {
    if (hostedConfirmed(selExec, "jev_")) {
      if (selExec.reused === true) return "Jev - cached hosted response";
      if (selExec.reused === false) return "Jev - fresh hosted response";
      return "Jev - hosted response (reuse status unavailable)";
    }
    return "Injected selector - hosted provenance unavailable";
  }
  return "Selection provenance unavailable";
}

function aittaProvenance(review, genExec) {
  if (!review) return null;
  const trace = review.trace || {};
  if (trace.phrasing_source === "injected_generator_opening_only") {
    if (hostedConfirmed(genExec, "aitta_")) {
      const kind = genExec.reused === true ? "cached hosted response"
        : genExec.reused === false ? "fresh hosted response"
        : "hosted response (reuse status unavailable)";
      return `Aitta - ${kind} (opening only)`;
    }
    return "Injected opening generator - hosted provenance unavailable";
  }
  return "Deterministic opening";
}

function renderSelectionDecision(audience, review, baseline,
                                 executions, job) {
  const card = el("div", null, "card-inner decision");
  card.appendChild(el("h4", `Selection decision — ${audience}`));
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
  card.appendChild(el("p",
    "These are review options for this session, not simulation " +
    "profiles. Counts support offering an option; no validated " +
    "explanation of Jev's preference is recorded. KT estimates do " +
    "not determine this selection.", "meta"));

  const table = el("table", null, "grid decision-table");
  table.appendChild(el("caption",
    "Review options — applied selection vs rules baseline"));
  const head = el("tr");
  ["Review option", "Incorrect / assessed",
   "Applied selection", "Rules baseline"]
    .forEach((h) => head.appendChild(el("th", h)));
  table.appendChild(head);
  candidates.forEach((c) => {
    const tr = el("tr");
    const focus = c.focus || null;
    tr.appendChild(el("td", focusLabel(c, c.candidate_id)));
    tr.appendChild(el("td", focus
        ? `${focus.incorrect} / ${focus.out_of}` : "—"));
    tr.appendChild(el("td",
        c.candidate_id === appliedId ? "Selected" : ""));
    tr.appendChild(el("td",
        c.candidate_id === baselineId ? "Baseline" : ""));
    table.appendChild(tr);
  });
  card.appendChild(table);
  return card;
}

/* ============================ STUDENT ============================ */

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
    if (!creds) { render(null); return; }
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
      const p = el("p", section.text, "sec");
      const kind = el("span", section.kind || "", "kind");
      p.prepend(kind, " ");
      container.appendChild(p);
    });
    if (!message.sections || !message.sections.length) {
      container.appendChild(el("p", message.text || ""));
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
      renderMessage($("#pause-message"), mid);
      showPanel(PANELS, "#pause-panel");
      return;
    }
    const question = snapshot.current_question;
    if (!question) { showPanel(PANELS, "#start-panel"); return; }
    currentQuestionToken = question.question_token;
    submitting = false;
    $("#q-pos").textContent = question.position;
    $("#q-progress").value = answered;
    const finnish = $("#display-language").value === "fi";
    $("#q-text").textContent = finnish ? question.text : englishQuestionText(question);
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
      label.appendChild(el("span", finnish ? option : englishOptionText(option)));
      box.appendChild(label);
    });
    $("#submit-answer").disabled = true;
    $("#q-error").hidden = true;
    showPanel(PANELS, "#question-panel");
    $("#q-text").focus({preventScroll: true});
  }

  function renderEnd(snapshot) {
    $("#job-status").textContent =
      `${snapshot.provider_job.status} (${snapshot.provider_job.provider_mode})`;
    const end = snapshot.feedback && snapshot.feedback.end;
    const summary = $("#end-summary");
    summary.replaceChildren();
    if (end) {
      summary.appendChild(el("p",
        `Observed result: ${end.total.correct} of ${end.total.out_of} ` +
        `answers correct on this assessment.`, "score"));
      const list = el("ul", null, "skill-list");
      end.skills.forEach((skill) => {
        list.appendChild(el("li",
          `${englishSkillName(skill.skill_name)}: ${skill.correct} / ${skill.out_of} observed`));
      });
      summary.appendChild(list);
      renderMessage($("#end-sections"), end);
    }
    if (snapshot.provider_job.status === "pending") {
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
    [
      `providers: ${config.provider_mode}`,
      `New provider calls this server run: ` +
      `${budget.used ?? 0} / ${budget.limit ?? "—"}`,
      `diagnostics: ${diag.status || "unknown"}`,
      `policy ${config.policy_version}`,
      "synthetic only — drafts require educator review",
    ].forEach((text, i) => {
      strip.appendChild(el("span", text,
        i === 4 ? "chip strong" : "chip"));
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
  let detail = null, selected = null, tab = "overview", researchCp = "end", lastAudience = "student";
  let contract = null;
  const drafts = new Map();   // "sid:audience" -> draft state
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

  function renderFeedbackContent() {
    const pane = $("#tab-feedback");
    pane.replaceChildren();
    const card = el("div", null, "card-inner");
    const end = detail.checkpoints.end || {};
    card.appendChild(kv("Provider job",
      `${detail.provider_job.status} (${detail.provider_job.provider_mode})`));
    if (detail.provider_job.queued_at) {
      card.appendChild(kv("Queued", detail.provider_job.queued_at));
      card.appendChild(kv("Finished",
        detail.provider_job.finished_at || "—"));
    }
    ["student", "teacher"].forEach((audience) => {
      const review = end[`${audience}_review`];
      const baseline = end[`baseline_${audience}`];
      if (!baseline && !review) return;
      card.appendChild(el("h3",
        `${audience} end draft (baseline vs selected)`));
      card.appendChild(renderSelectionDecision(
        audience, review, baseline,
        detail.provider_job.executions, detail.provider_job));
      const wrap = el("div", null, "compare");
      [baseline, review].forEach((r, i) => {
        const col = el("div", null, "col");
        col.appendChild(el("h4",
          i === 0 ? "Deterministic baseline" : "Selected draft"));
        if (r) {
          const which = i === 0 ? "baseline" : "selected";
          col.appendChild(el("p",
            `candidate: ${r.selected_candidate_id}`, "mono small"));
          (r.message.sections || []).forEach((s) =>
            col.appendChild(el("p", s.text, "sec")));
          if (!r.message.sections?.length) col.appendChild(el("p", r.message.text, "draft-text"));
          col.appendChild(jsonDetails("Trace", r.trace,
            `${audience}:${which}:trace`));
          const cand = el("details", null, "raw");
          cand.dataset.disclosureKey =
            `${audience}:${which}:candidates`;
          cand.appendChild(el("summary",
            `All candidates (${(r.candidates || []).length})`));
          (r.candidates || []).forEach((c) => {
            cand.appendChild(el("pre",
              JSON.stringify(c, null, 2), "mono"));
          });
          col.appendChild(cand);
        }
        wrap.appendChild(col);
      });
      card.appendChild(wrap);
    });
    const halfway = detail.checkpoints.midpoint?.baseline_student;
    if (halfway) {
      const saved = el("details", null, "raw");
      saved.dataset.disclosureKey = "halfway-feedback";
      saved.append(el("summary", "Halfway student feedback in this result"), el("p", halfway.message.text, "draft-text"));
      card.append(saved);
    }
    if (detail.provider_job.executions) {
      card.appendChild(jsonDetails(
        "Provider executions (provenance, metadata, latency)",
        detail.provider_job.executions, "executions"));
    }
    pane.appendChild(card);
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
    const audienceEl = $("#tab-review select[data-audience]");
    const audience = (audienceEl && audienceEl.value) ||
      lastAudience || "student";
    lastAudience = audience;
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

    const audSel = el("select");
    audSel.dataset.audience = "1";
    audSel.id = `${prefix}rev-audience`;
    audSel.setAttribute("aria-label", "Review audience");
    ["student", "teacher"].forEach((a) => {
      const o = el("option", a);
      o.value = a;
      audSel.appendChild(o);
    });
    audSel.value = audience;
    audSel.addEventListener("change", () => {
      lastAudience = audSel.value;
      reviewRenderKey = null;
      renderPane("review", renderReviewContent);
    });
    const audLabel = el("label", "Audience draft");
    audLabel.htmlFor = audSel.id;
    card.appendChild(audLabel);
    card.appendChild(audSel);

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
