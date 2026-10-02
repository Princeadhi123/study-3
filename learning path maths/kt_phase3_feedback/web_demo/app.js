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
});

const ENGLISH_QUESTION_TEXT = Object.freeze({
  // Draft translations bound to the exact approved question IDs.
  "23127__p10__q07e4b362538179c3__o24d3b21657c80f9c":
    "Calculate: 6 - 0 \u00d7 (0 + 9).",
  "19062__p10__q6698d8d0febd22aa__o24cda435d536d718":
    "Calculate: EUR 10 - EUR 2.50.",
  "17639__p10__q1d8b22fe6257bc98__o04524bc505f1027b":
    "Which fraction is the largest?",
  "459786__p10__q13bf7029212b1aae__oadc720eb9a0c17de":
    "What is 20% of 65?",
  "23127__p10__q1c40f6abd4a51d34__o07d44b0b4e2e0d9b":
    "Calculate: 8 \u00d7 (7 + 1 \u00d7 1).",
  "79717__p1__qeffb016560021712__o01bae8ffd3d81fd4":
    "Ten litres of strawberries cost EUR 25. How much does one litre cost?",
  "39338__p10__q4bd6280b64454754__oa8ca4de49d22cf1c":
    "Calculate: 3/6 \u00d7 6/5.",
  "460002__p10__q05d66fa42a773bc2__o6b51258a178861dd":
    "What is 25% of 100?",
  "23127__p10__q26b7391305bb53e7__o0ec5d00e5175138a":
    "Calculate: 7 + 3 \u00d7 (6 + 7).",
  "653713__p4__q946d45957a2746a1__o7c1aa4ef2077bb15":
    "A 250-gram chocolate bar costs EUR 2.20. What is its price per kilogram?",
  "47506__p1__q09d1d120642811bf__o34a62e883346940a":
    "Calculate: 1/7 \u00d7 11.",
  "459972__p10__qbf74fc2d62d61d6f__oeaee88efbc731632":
    "140% of what amount of money is EUR 42?",
  "23127__p10__q29bb7de2f2bd739b__o1136761f07186ead":
    "Calculate: 7 \u00d7 13 + 1 \u00d7 3.",
  "79720__p2__qe840d7df5041cfb2__ocaf00104d3b8bea8":
    "Maria buys three litres of ice cream. Ten litres cost EUR 25. How much does Maria's ice cream cost?",
  "17754__p10__q3c501c431a5f75cb__oec36fda6540975c9":
    "Calculate: 2/4 + 2/4.",
  "460131__p10__q13a0e43cee444f8c__o3bd9f83da741f2fe":
    "Write 25% as a decimal multiplier.",
  "65204__p10__q317369c78834f798__o038732b487a43a51":
    "Calculate: 450 \u00f7 10.",
  "79717__p3__q68eddd414b1c3673__oceb5aee08c735a3d":
    "Eight litres of vanilla ice cream cost EUR 32. How much do nine litres cost?",
  "47506__p8__qcc51534be3a3e23a__o7197e2866f97b3d1":
    "Calculate: 4/10 \u00f7 2.",
  "459972__p1__q1fd796c3931c36f0__of2ba5e496304fbef":
    "The price of a product increased by 20%, an increase of EUR 64. What was the original price?",
  "23127__p10__q0ccbb3e88ed3b715__o4c7237e888269437":
    "Calculate: 4 + 4 \u00d7 10 + 10.",
  "653713__p3__q1cf64466888647cb__oa77a4bd3afab91f9":
    "A 500-gram bag of sweets costs EUR 6.50. How much does one kilogram of sweets cost?",
  "17754__p10__q02fc7651eeff3595__ofb00806eff226d69":
    "Calculate: 1/3 + 1/3.",
  "459972__p10__qaff17c40310ae601__o65a289bd8e6d5a65":
    "120% of what number is 60?",
  "23127__p10__q1d7ed4f6c655c203__o5ecd0eac0b919a05":
    "Calculate: 4 \u00d7 (2 + 2) \u00f7 4.",
  "79720__p1__q109c0d7c9ac0bee2__o6954d1d0ede8460d":
    "Elsa buys 12 forks. Twenty forks cost EUR 40. How much do Elsa's forks cost?",
  "42903__p10__q308ea024c44e5c4c__o7dcc2f928a816786":
    "Calculate: 2/3 + 2/6.",
  "460131__p10__q09ffa2c535cf3137__o7eb69369145914ef":
    "10% of what number is 7?",
  "23127__p10__q299a45545d6434fc__o01664e308966e120":
    "Calculate: 2 + 6 \u00d7 (7 + 4).",
  "79717__p2__q32d47b510b68f842__o4e7e6ec741b14058":
    "Five packets of coffee cost EUR 20.25. How much does one packet cost?",
  "64974__p10__q01d29c49363cca85__o415d3a4d9721886f":
    "Calculate: 3/4 + 1/4.",
  "460002__p10__q0aedf501f9cda8dc__o27611459b8dad17f":
    "25% of what number is 5?",
  "23127__p10__q2a52a7db5fbacc15__o035bf13fba7c78e1":
    "Calculate: 7 + 22 + 3 \u00d7 5.",
  "653713__p5__q0722e28763c17950__oe55ff617fd891802":
    "A 2.5-decilitre can of soft drink costs EUR 2.65. What is its price per litre?",
  "39338__p10__q5f1e4aabbc541a3b__o8f7f1e6ee61a5b94":
    "Calculate: 2/3 \u00d7 10/8.",
  "459972__p1__q971d7cb98273d764__o7c0a4c976e92f883":
    "95% of what amount of money is EUR 760?",
  "65204__p10__q3fe42eef29bab2d9__o11d688f5b86e5036":
    "Calculate: 110 \u00f7 10.",
  "653713__p6__qb73206ba0d284e0c__o627d28a41240218e":
    "Two litres of soft drink cost EUR 3. A half-litre bottle of the same drink costs EUR 1.50. How much higher is the price per litre in the half-litre bottle than in the two-litre bottle?",
  "42903__p10__qc6d6158988ac31d3__oaa6de5b5b60f3839":
    "Calculate: 1/2 + 1/4.",
  "460131__p10__q58804b37a5e9b683__o5e7af3576774b5e8":
    "What percentage of 200 is 4?",
});

function englishSkillName(name) {
  return Object.prototype.hasOwnProperty.call(ENGLISH_SKILL_NAMES, name)
    ? ENGLISH_SKILL_NAMES[name] : name;
}

function englishQuestionText(question) {
  const id = question && question.question_id;
  return Object.prototype.hasOwnProperty.call(ENGLISH_QUESTION_TEXT, id)
    ? ENGLISH_QUESTION_TEXT[id] : (question && question.text) || "";
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
    "explanation of Jev's preference is recorded. KT/conformal do " +
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
  let currentQuestionId = null;

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
    currentQuestionId = question.question_id;
    submitting = false;
    $("#q-pos").textContent = question.position;
    $("#q-progress").value = answered;
    $("#q-text").textContent = englishQuestionText(question);
    const box = $("#q-options");
    box.replaceChildren();
    question.options.forEach((option, index) => {
      const label = el("label", null, "option");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "answer";
      radio.value = String(index);
      label.appendChild(radio);
      label.appendChild(el("span", `${index + 1}.`, "opt-num"));
      label.appendChild(el("span", englishOptionText(option)));
      box.appendChild(label);
    });
    $("#submit-answer").disabled = true;
    $("#q-error").hidden = true;
    showPanel(PANELS, "#question-panel");
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
    if (starting) return;
    starting = true;
    $("#start-button").disabled = true;
    $("#fresh-start").disabled = true;
    try {
      const payload = await api("/api/sessions", {body: {synthetic: true}});
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

  $("#start-button").addEventListener("click", startSession);
  $("#fresh-start").addEventListener("click", () => {
    saveCreds(null);
    startSession();
  });
  $("#new-session").addEventListener("click", () => {
    saveCreds(null);
    startSession();
  });
  $("#q-options").addEventListener("change", () => {
    $("#submit-answer").disabled = submitting;
  });
  $("#submit-answer").addEventListener("click", async () => {
    if (submitting || !creds || !currentQuestionId) return;
    const chosen = document.querySelector("input[name=answer]:checked");
    if (!chosen) return;
    submitting = true;
    $("#submit-answer").disabled = true;
    try {
      const snap = await api(`/api/sessions/${creds.sid}/responses`, {
        token: creds.token,
        body: {question_id: currentQuestionId,
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
  const TABS = ["overview", "evidence", "graph", "research",
                "feedback", "review"];
  const STALE_NOTE =
    "The feedback draft changed. Re-read it before reviewing the new version.";
  let config = null;
  let contract = null;
  let sessions = [];
  let detail = null;
  let selected = null;
  let tab = "overview";
  let researchCp = "end";
  let lastAudience = "student";
  let unlocked = false;
  let pollInFlight = false;
  let simRunning = false;
  const drafts = new Map();   // "sid:audience" -> draft state
  const simLinks = new Map(); // sid -> student URL with token
  let reviewRenderKey = null; // rebuild review form only when this changes
  // scopeKey -> Map(disclosureKey -> open). Scope is derived from the
  // pane's last rendered scope so a session/checkpoint switch never
  // transfers another context's expand/collapse choices.
  const disclosureScopes = new Map();

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
    contract = config.review_contract;
    unlocked = true;
    $("#unlock-panel").hidden = true;
    $("#teacher-app").hidden = false;
    renderConfigStrip();
    const select = $("#sim-profile");
    select.replaceChildren();
    ["all_correct", "all_incorrect", "weak_fractions_only",
     "first_half_correct_second_half_wrong", "alternating"]
      .forEach((p) => select.appendChild(el("option", p)));
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
      sessions = payload.sessions || [];
      renderList();
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
    sessions.forEach((s) => {
      const li = el("li", null,
        "session-row" + (s.session_id === selected ? " active" : ""));
      li.appendChild(el("span", s.label, "label"));
      li.appendChild(el("span",
        `${s.answered_count}/40 · ${s.status} · ` +
        `providers ${s.provider_job.status}`, "meta"));
      const diag = s.diagnostics || {};
      li.appendChild(el("span",
        `mid ${diag.midpoint || "—"} · end ${diag.end || "—"} · ` +
        `reviews ${s.review_count}`, "meta"));
      li.addEventListener("click", () => {
        if (selected === s.session_id) return;
        selected = s.session_id;
        detail = null;
        reviewRenderKey = null;
        renderList();
        pollOnce(); // immediate fetch for the newly selected session
      });
      list.appendChild(li);
    });
  }

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
    $("#ws-empty").hidden = true;
    $("#ws-detail").hidden = false;
    $("#ws-title").textContent =
      `${detail.label} — ${detail.status} (${detail.answered_count}/40)`;
    $("#ws-export").href = `/api/teacher/sessions/${selected}/export`;
    const linkBox = $("#ws-student-link");
    linkBox.replaceChildren();
    const link = simLinks.get(selected);
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
    card.appendChild(kv("Status", detail.status));
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
    if (detail.simulated) {
      card.appendChild(el("p",
        `Simulated response pattern (${detail.simulated.profile}, ` +
        `seed ${detail.simulated.seed}) — not a diagnosis.`, "meta"));
    }
    card.appendChild(jsonDetails("Provenance", detail.provenance,
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
    if (detail.question_responses.length) {
      card.appendChild(el("h3", "Per-question record (private)"));
      detail.question_responses.forEach((row) => {
        const d = el("details", null, "qrow");
        d.dataset.disclosureKey = `q:${row.question_id}`;
        d.appendChild(el("summary",
          `#${row.position} · ${skillNameForId(row.skill_id)} · ` +
          `${row.correct ? "correct" : "incorrect"} · ${row.question_id}`));
        const inner = el("div", null, "qrow-body");
        inner.appendChild(kv("Question", englishQuestionText(row)));
        inner.appendChild(kv("Subtopic",
          `${row.subtopic_name} (${row.subtopic_id})`));
        inner.appendChild(kv("Chosen",
          englishOptionText(row.options[row.selected_index])));
        inner.appendChild(kv("Correct option",
          englishOptionText(row.options[row.answer_index])));
        inner.appendChild(kv("Answered at", row.answered_at));
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
        observed && observed.correct ? "#21615B" : "#C5603F");
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
    const picker = el("div", null, "cp-picker");
    [["midpoint", "Midpoint — first 20 answers (k=5)"],
     ["end", "End — all 40 answers (k=10)"]].forEach(([cp, label]) => {
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
    card.appendChild(el("h3",
      `${researchCp} checkpoint · ${diag.answer_count} answers`));
    card.appendChild(el("p",
      "Frozen KT pre-answer probability per response position " +
      "(axis 0–1); dots mark observed correctness " +
      "(teal=correct, rust=incorrect).", "meta"));
    const chart = ktChart(diag);
    if (chart) card.appendChild(chart);
    const conformal = diag.conformal || {};
    const skills = conformal.skills || {};
    if (Object.keys(skills).length) {
      const table = el("table", null, "grid");
      table.appendChild(el("caption",
        `Conformal checkpoint summary — calibrated k=${conformal.calibrated_k}; ` +
        `labels are raw historical gate labels, exploratory only`));
      const head = el("tr");
      ["Skill", "Point", "Lower", "Upper", "Items", "Regime",
       "Raw label"].forEach((h) => head.appendChild(el("th", h)));
      table.appendChild(head);
      Object.values(skills).forEach((row) => {
        const tr = el("tr");
        [englishSkillName(row.skill_name), row.point_estimate,
         row.lower, row.upper,
         row.n_items, row.regime, row.status]
          .forEach((v) => tr.appendChild(el("td", String(v))));
        table.appendChild(tr);
      });
      card.appendChild(table);
    }
    card.appendChild(el("p", diag.scope_warning || "", "meta"));
    card.appendChild(jsonDetails("Full KT trace (raw JSON)", diag.kt,
      "kt-trace"));
    card.appendChild(jsonDetails("Full conformal output (raw JSON)",
      conformal, "conformal"));
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
    const hashes = detail.review_hashes || {};
    const audienceEl = document.querySelector(
      "#tab-review select[data-audience]");
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
    audSel.id = "rev-audience";
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
    audLabel.htmlFor = "rev-audience";
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
      sel.id = `rev-field-${field.id}`;
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
    note.id = "rev-note";
    note.maxLength = 3000;
    note.value = draft.note;
    note.addEventListener("input", () => { draft.note = note.value; });
    const label = document.createElement("input");
    label.id = "rev-reviewer";
    label.maxLength = 80;
    label.value = draft.label;
    label.addEventListener("input", () => { draft.label = label.value; });
    const noteLabel = el("label", "Note (≤3000 chars, plain text)");
    noteLabel.htmlFor = "rev-note";
    const reviewerLabel = el("label", "Reviewer label (optional)");
    reviewerLabel.htmlFor = "rev-reviewer";
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

  /* ---------- events ---------- */

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
        body: {profile: $("#sim-profile").value, seed}});
      simLinks.set(result.session_id, result.student_url);
      selected = result.session_id;
      reviewRenderKey = null;
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
  $("#ws-tabs").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-tab]");
    if (!button) return;
    tab = button.dataset.tab;
    document.querySelectorAll(".tab").forEach((b) =>
      b.classList.toggle("active", b === button));
    TABS.forEach((name) => {
      $(`#tab-${name}`).hidden = name !== tab;
    });
  });

  boot();
}

if (PAGE === "student") studentPage();
if (PAGE === "teacher") teacherPage();
