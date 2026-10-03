"use strict";

function initResearchWorkspace({mountDetail, getConfig}) {
  const nav = document.querySelector(".workspace-nav");
  if (nav.dataset.ready) return;
  nav.dataset.ready = "true";
  let library = null;
  let chosen = null;
  let caseRequest = 0;
  let comparisonRequest = 0;
  let currentStudy = null;
  let exposed = false;
  let runs = [], filteredIds = [], selectedRun = "", scenarioView = null;
  let viewHost = null, versionPicker = null, caseNotice = null, replaySelected = null;
  let runRequest = false, runTimer = null, latestRunPayload = "", displayedVersion = "";
  try { exposed = sessionStorage.getItem("replay-exposure") === "yes"; } catch (err) {}
  const libraryPane = $("#library-pane");
  const comparisonPane = $("#comparison-pane");

  function button(label, action, className = "ghost") {
    const node = el("button", label, className);
    node.type = "button";
    node.addEventListener("click", action);
    return node;
  }

  function heading(kicker, title, description) {
    const node = el("div", null, "section-heading");
    node.append(el("p", kicker, "eyebrow"), el("h2", title), el("p", description, "meta"));
    return node;
  }

  function notice(message, className = "boundary-note") {
    return el("p", message, className);
  }

  function failure(container, retry) {
    container.replaceChildren(heading("CONNECTION", "This view could not be loaded.",
      "Check the local server. If your access expired, reload and unlock the workspace."),
      button("Try again", retry, "primary"));
  }

  function field(labelText, input) {
    const label = el("label", null, "control-field");
    label.append(el("span", labelText), input);
    return label;
  }

  function select(options) {
    const node = el("select");
    options.forEach(([value, label]) => {
      const option = el("option", label);
      option.value = value;
      node.append(option);
    });
    return node;
  }

  function raw(title, value) {
    const details = el("details", null, "raw");
    details.append(el("summary", title), el("pre", JSON.stringify(value, null, 2)));
    return details;
  }

  function metric(value, label, note) {
    const card = el("div", null, "metric-card");
    card.append(el("span", label, "metric-label"), el("strong", String(value), "metric-value"));
    if (note) card.append(el("span", note, "meta small"));
    return card;
  }

  function evidenceView(evidence) {
    const block = el("section", null, "evidence-block");
    const total = evidence.total || {};
    block.append(el("h3", "Observed evidence"), el("p",
      `${total.correct ?? "—"} / ${total.out_of ?? "—"} correct on these assessment items. Not a mastery estimate.`, "meta"));
    const skills = el("div", null, "skill-bars");
    (evidence.skills || []).forEach((row) => {
      const line = el("div", null, "skill-bar");
      const label = englishSkillName(row.skill_name);
      const meter = el("progress");
      meter.max = row.out_of || 1;
      meter.value = row.correct;
      meter.setAttribute("aria-label", `${label}: ${row.correct} of ${row.out_of} correct`);
      line.append(el("span", label), el("strong", `${row.correct}/${row.out_of}`, "mono"), meter);
      skills.append(line);
    });
    block.append(skills);
    if (evidence.subtopics?.length) {
      const disclosure = el("details", null, "evidence-details");
      disclosure.append(el("summary", "Inspect subtopic counts"));
      const table = el("table", null, "grid");
      table.append(el("caption", "Descriptive subtopics — small counts are not diagnoses"));
      const head = el("thead");
      const hr = el("tr");
      ["Subtopic", "Skill", "Correct", "Incorrect", "Items"].forEach((name) => {
        const th = el("th", name); th.scope = "col"; hr.append(th);
      });
      head.append(hr);
      const body = el("tbody");
      evidence.subtopics.forEach((row) => {
        const tr = el("tr");
        [row.subtopic_name, englishSkillName(row.skill_name), row.correct, row.incorrect, row.out_of]
          .forEach((value) => tr.append(el("td", String(value))));
        body.append(tr);
      });
      table.append(head, body);
      disclosure.append(table);
      block.append(disclosure);
    }
    return block;
  }

  function draft(title, message, tag) {
    const card = el("article", null, "draft-card");
    if (tag) card.append(el("span", tag, "eyebrow"));
    card.append(el("h3", title), el("p", message, "draft-text"));
    return card;
  }

  nav.addEventListener("click", (event) => {
    const target = event.target.closest("button[data-workspace]");
    if (!target) return;
    const mode = target.dataset.workspace;
    document.body.dataset.workspace = mode;
    nav.querySelectorAll("[data-workspace]").forEach((node) => {
      const active = node === target;
      node.classList.toggle("active", active);
      node.setAttribute("aria-pressed", String(active));
    });
    ["live", "library", "comparison"].forEach((name) => {
      $(`#${name}-pane`).hidden = name !== mode;
    });
    if (mode === "library") {
      if (!library) loadLibrary();
      else refreshRuns();
    }
    if (mode === "comparison" && !comparisonPane.childElementCount) loadComparisons();
  });

  async function loadLibrary() {
    libraryPane.replaceChildren(notice("Loading the saved replay. No inference or provider calls are made."));
    try {
      library = await api("/api/teacher/scenarios");
      if (library.status !== "ready") {
        library = null;
        libraryPane.replaceChildren(heading("SAVED REPLAY", "No compatible replay is available.",
          "The configured report is missing or invalid. Live sessions still work. Set --replay-report to a retained integrated report."),
          button("Check again", loadLibrary));
        return;
      }
      renderLibrary();
      await refreshRuns();
      scheduleRuns();
    } catch (err) { library = null; failure(libraryPane, loadLibrary); }
  }

  function renderLibrary() {
    libraryPane.replaceChildren(heading("RETAINED EXPERIMENTS", "Scenario library",
      "Inspect the retained original or replay the same answers through the current pipeline. Every new run is saved separately."));
    libraryPane.append(notice("Planning a blind review? Complete it before opening scenario drafts. Prior exposure can bias preferences."));
    const summary = library.summary;
    const metrics = el("div", null, "metrics-grid");
    metrics.append(metric(summary.scenarios, "Saved scenarios", "Chosen stress-test cases"),
      metric(summary.end_packages, "End-stage drafts", "Student + teacher audiences"),
      metric(summary.selection_differences, "Different selections", `Of ${summary.end_packages} end packages; not a quality score`),
      metric(summary.fallback_packages, "Recorded fallbacks", "End packages in this replay"));
    libraryPane.append(metrics, el("p", "These summary counts describe the retained original report, not the most recent replay.", "meta small"));
    const replayToolbar = el("div", null, "replay-toolbar");
    const replayAll = button(`Replay all ${library.scenarios.length} scenarios`, () => confirmReplay(library.scenarios.map((row) => row.id)), "primary");
    replayAll.id = "replay-all";
    const replayFiltered = button("Replay filtered scenarios", () => confirmReplay(filteredIds));
    replayFiltered.id = "replay-filtered";
    replayToolbar.append(replayAll, replayFiltered, el("span", "Same answers · current scoring, feedback & model inference · no retraining", "meta small"));
    libraryPane.append(replayToolbar);
    const history = el("div"); history.id = "replay-history";
    libraryPane.append(history);
    const controls = el("div", null, "library-controls");
    const search = el("input");
    search.type = "search"; search.placeholder = "Search a scenario or skill…"; search.id = "scenario-search";
    const groups = [...new Set(library.scenarios.map((row) => row.group))].sort();
    const group = select([["", "All patterns"], ...groups.map((g) => [g, library.scenarios.find((row) => row.group === g).group_label])]);
    const difference = select([["", "All selections"], ["different", "At least one differs"], ["same", "Both match baseline"]]);
    const order = select([["name", "Name A–Z"], ["low", "Observed score: low first"], ["high", "Observed score: high first"]]);
    controls.append(field("Find a case", search), field("Scenario group", group),
      field("Selection comparison", difference), field("Sort by", order));
    libraryPane.append(controls);
    const layout = el("div", null, "library-layout");
    const side = el("div", null, "scenario-browser");
    const count = el("p", null, "meta small"); count.setAttribute("role", "status");
    const list = el("div", null, "scenario-list");
    list.setAttribute("aria-label", "Saved scenarios");
    const detail = el("div", null, "scenario-detail");
    detail.id = "scenario-detail";
    const intro = heading("EXPLORE THE EVIDENCE", "Choose a scenario.",
      "Use the same detailed views as live sessions. Replay a case to test your changes and record new educator review notes.");
    intro.id = "scenario-intro";
    detail.append(intro);
    const versionBar = el("div", null, "replay-toolbar");
    versionPicker = select([["", "Retained original — not recomputed"]]);
    versionPicker.id = "scenario-version";
    versionPicker.addEventListener("change", () => { selectedRun = versionPicker.value; loadCase(chosen); });
    replaySelected = button("Replay this scenario", () => confirmReplay([chosen]), "primary");
    replaySelected.id = "replay-selected"; replaySelected.disabled = true;
    versionBar.append(field("Result version", versionPicker), replaySelected);
    caseNotice = el("p", "Select a scenario from the list.", "boundary-note");
    caseNotice.setAttribute("role", "status");
    viewHost = el("div");
    detail.append(versionBar, caseNotice, viewHost);
    scenarioView = mountDetail(viewHost, {refresh: () => loadCase(chosen)});
    side.append(count, list); layout.append(side, detail); libraryPane.append(layout);
    libraryPane.append(raw("Replay identity and interpretation limits", {
      report_sha256: library.report_sha256, source: library.source, policy: library.policy,
      selector_execution: library.summary.selector_execution, limitation: library.limitation,
      cached_captures_are_independent_trials: false, educational_effectiveness_tested: false}));
    function renderCases() {
      const query = search.value.trim().toLowerCase();
      const rows = library.scenarios.filter((row) =>
        (!group.value || row.group === group.value) &&
        (!difference.value || (difference.value === "different") === (row.selection_differences > 0)) &&
        `${row.display_name} ${row.description} ${row.name} ${row.skills.map((s) => englishSkillName(s.skill_name)).join(" ")}`.toLowerCase().includes(query));
      rows.sort((a, b) => order.value === "name" ? a.display_name.localeCompare(b.display_name) :
        (order.value === "low" ? 1 : -1) * (a.total.correct - b.total.correct) || a.name.localeCompare(b.name));
      filteredIds = rows.map((row) => row.id);
      replayFiltered.disabled = !rows.length;
      count.textContent = `${rows.length} of ${library.scenarios.length} scenarios`;
      list.replaceChildren();
      if (!rows.length) list.append(notice("No cases match. Try a broader search or clear the filters."));
      rows.forEach((row) => {
        const item = button("", () => { chosen = row.id; renderCases(); updateVersions(); loadCase(row.id); }, "scenario-row");
        item.classList.toggle("active", chosen === row.id);
        item.setAttribute("aria-pressed", String(chosen === row.id));
        item.dataset.caseId = row.id;
        item.title = row.description;
        item.append(el("span", row.display_name, "label"),
          el("span", `${row.total.correct}/${row.total.out_of} correct · ${row.group_label}`, "meta small"),
          el("span", row.selection_differences ? `${row.selection_differences} end selection(s) differ` : "End selections match baseline", "case-tag"));
        list.append(item);
      });
    }
    [search, group, difference, order].forEach((node) => node.addEventListener("input", renderCases));
    renderCases();
  }

  function updateVersions() {
    if (!versionPicker) return;
    versionPicker.replaceChildren();
    const original = el("option", "Retained original — not recomputed"); original.value = "";
    versionPicker.append(original);
    runs.filter((run) => run.cases.some((c) => c.scenario_id === chosen)).forEach((run) => {
      const row = run.cases.find((c) => c.scenario_id === chosen);
      const option = el("option", `${new Date(run.created_at).toLocaleString()} · ${run.provider_mode === "rules" ? "Rules only" : "Configured providers"} · ${statusLabel(row.status)}`);
      option.value = run.id; versionPicker.append(option);
    });
    if (![...versionPicker.options].some((o) => o.value === selectedRun)) selectedRun = "";
    versionPicker.value = selectedRun;
    versionPicker.disabled = !chosen;
    replaySelected.disabled = !chosen;
  }

  async function loadCase(id) {
    if (!id || !scenarioView) return;
    const request = ++caseRequest;
    const version = selectedRun;
    $("#scenario-intro").hidden = true;
    if (displayedVersion !== `${id}|${version}`) viewHost.hidden = true;
    const run = runs.find((r) => r.id === version);
    const row = run?.cases.find((c) => c.scenario_id === id);
    caseNotice.textContent = version ? `Replay result · ${statusLabel(row?.status)}. Original results remain unchanged.` : "Retained original — read-only. Select a replay result to inspect current behavior and save review notes.";
    if (version && !row?.session_id) {
      viewHost.hidden = true;
      return;
    }
    try {
      const response = await api(version ? `/api/teacher/replays/${version}/cases/${id}` : `/api/teacher/scenarios/${id}`);
      if (request !== caseRequest || id !== chosen || version !== selectedRun) return;
      const view = version ? {...response, replay_changes: row.changes || null} : response.detail;
      exposed = true;
      try { sessionStorage.setItem("replay-exposure", "yes"); } catch (err) {}
      scenarioView.update(view);
      displayedVersion = `${id}|${version}`;
      viewHost.hidden = false;
    } catch (err) {
      if (request !== caseRequest) return;
      viewHost.hidden = true;
      caseNotice.replaceChildren(el("span", "Could not load this result. Check the server or unlock the workspace again. "),
        button("Retry", () => loadCase(id), "ghost small"));
    }
  }

  function scheduleRuns() {
    if (runTimer) clearTimeout(runTimer);
    runTimer = setTimeout(async () => {
      if (!libraryPane.hidden && !document.hidden && !$("#teacher-app").hidden) await refreshRuns();
      scheduleRuns();
    }, POLL_MS);
  }

  async function refreshRuns() {
    if (runRequest || !library) return;
    runRequest = true;
    try {
      const payload = await api("/api/teacher/replays");
      const serialized = JSON.stringify(payload);
      if (serialized !== latestRunPayload) {
        latestRunPayload = serialized;
        runs = payload.runs;
        renderRuns(payload);
        updateVersions();
        if (chosen && selectedRun) await loadCase(chosen);
      }
    } catch (err) {
      const node = $("#replay-history");
      if (node) {
        node.replaceChildren(notice("Replay progress could not be refreshed. Existing jobs continue on the server; do not resubmit until their status is known."), button("Refresh status", refreshRuns));
        latestRunPayload = "";
      }
    } finally { runRequest = false; }
  }

  function renderRuns(payload) {
    const container = $("#replay-history");
    if (!container) return;
    const open = new Map([...container.querySelectorAll("details[data-run-id]")].map((d) => [d.dataset.runId, d.open]));
    container.replaceChildren();
    if (payload.restart_required) container.append(notice("Python code changed since this server started. Restart the server before replaying so the run uses your changes.", "warn"));
    if (!runs.length) {
      container.append(el("p", "No new replay runs yet. Choose a scenario, or replay the full set. Browsing never starts a run.", "meta small"));
      return;
    }
    const history = el("details", null, "run-history");
    history.dataset.runId = "history";
    history.open = open.get("history") ?? runs.some((r) => ["running", "queued", "cancelling"].includes(r.status));
    history.append(el("summary", `Replay history (${runs.length}) · Latest: ${statusLabel(runs[0].status)} · ${runs[0].completed}/${runs[0].total} complete`));
    runs.forEach((run) => {
      const section = el("details", null, "run-record");
      section.dataset.runId = run.id;
      section.open = open.get(run.id) ?? ["running", "queued", "cancelling"].includes(run.status);
      section.append(el("summary", `${new Date(run.created_at).toLocaleString()} · ${run.provider_mode === "rules" ? "Rules only" : "Configured providers"} · ${statusLabel(run.status)} · ${run.completed}/${run.total}`));
      const progress = el("progress"); progress.max = run.total;
      progress.value = run.cases.filter((c) => !["running", "queued"].includes(c.status)).length;
      progress.setAttribute("aria-label", "Replay cases processed");
      section.append(progress);
      const actions = el("div", null, "replay-toolbar");
      const download = el("a", "Export run & changes", "ghost small");
      download.href = `/api/teacher/replays/${run.id}/export`; download.download = ""; actions.append(download);
      if (["queued", "running", "cancelling"].includes(run.status)) {
        const stop = button("Stop after current scenario", async () => {
          stop.disabled = true;
          try { await api(`/api/teacher/replays/${run.id}/cancel`, {body: {}}); await refreshRuns(); }
          catch (err) { stop.textContent = "Stop not confirmed — retry"; stop.disabled = false; }
        }, "ghost small");
        stop.disabled = run.status === "cancelling";
        actions.append(stop);
      }
      section.append(actions);
      const cases = el("div", null, "run-cases");
      run.cases.forEach((row) => {
        const item = button("", () => {
          chosen = row.scenario_id; selectedRun = run.id;
          updateVersions(); loadCase(chosen);
          libraryPane.querySelectorAll(".scenario-row").forEach((node) => {
            const active = node.dataset.caseId === chosen;
            node.classList.toggle("active", active); node.setAttribute("aria-pressed", String(active));
          });
        }, "run-case");
        const warnings = row.diagnostics && Object.values(row.diagnostics).some((s) => s !== "ready");
        item.append(el("span", row.display_name), el("span", `${statusLabel(row.status)}${warnings ? " · diagnostics unavailable" : ""}${row.provider_status === "fallback" ? " · baseline fallback" : ""}`, "meta small"));
        if (row.changes) item.append(el("span", `${row.changes.feedback.filter((f) => f.text_changed).length}/2 drafts changed from original`, "case-tag"));
        cases.append(item);
      });
      section.append(cases, raw("Run provenance and code fingerprints", {code_sha256: run.code_sha256, source: run.source,
        report_sha256: run.report_sha256, diagnostics_mode: run.diagnostics_mode}));
      history.append(section);
    });
    container.append(history);
  }

  function confirmReplay(ids) {
    if (!ids.length || ids.some((id) => !id)) return;
    const selectedIds = [...ids];
    const dialog = el("dialog", null, "replay-dialog");
    const form = el("form", null, "review-form");
    const title = el("h2", selectedIds.length === 1 ? "Replay this scenario?" : `Replay ${selectedIds.length} scenarios?`);
    title.id = "replay-dialog-title"; dialog.setAttribute("aria-labelledby", title.id);
    const mode = select([["rules", "Rules only — no hosted calls"], ["hosted", "Use configured hosted providers"]]);
    mode.id = "replay-provider-mode";
    mode.options[1].disabled = getConfig().provider_mode !== "hosted";
    const consent = el("input"); consent.type = "checkbox"; consent.id = "replay-hosted-consent";
    const approval = el("label", null, "attestation");
    approval.append(consent, el("span", "I approve hosted calls for this run within the server's remaining call budget. Matching requests may reuse captures."));
    approval.hidden = true;
    mode.addEventListener("change", () => { approval.hidden = mode.value !== "hosted"; consent.required = mode.value === "hosted"; });
    const budget = getConfig().call_budget;
    const explanation = notice(`This creates ${selectedIds.length} separate test results using the original answers. It recomputes scoring, skill maps and feedback, and requests fresh frozen-model diagnostics. No model retraining. Hosted budget used: ${budget.used}/${budget.limit}.`);
    const error = el("p", "", "error"); error.hidden = true; error.setAttribute("role", "alert");
    const start = el("button", "Start replay", "primary"); start.type = "submit"; start.id = "confirm-replay";
    const cancel = button("Cancel", () => dialog.close());
    const actions = el("div", null, "replay-toolbar"); actions.append(start, cancel);
    form.append(title, explanation, field("Feedback execution", mode), approval,
      notice("Restart the server after Python changes. Historical outputs and reviews are preserved. One replay batch runs at a time; stop prevents further cases after the current one finishes.", "meta small"), error, actions);
    dialog.append(form); document.body.append(dialog);
    dialog.addEventListener("close", () => dialog.remove());
    const requestId = [...crypto.getRandomValues(new Uint8Array(16))].map((b) => b.toString(16).padStart(2, "0")).join("");
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); if (start.disabled) return;
      start.disabled = true; cancel.disabled = true; error.hidden = true;
      try {
        const run = await api("/api/teacher/replays", {body: {request_id: requestId,
          report_sha256: library.report_sha256, scenario_ids: selectedIds, provider_mode: mode.value,
          allow_provider_calls: mode.value === "hosted" && consent.checked}});
        if (chosen && selectedIds.includes(chosen)) selectedRun = run.id;
        dialog.close();
        await refreshRuns();
      } catch (err) {
        error.textContent = err.status === 409 ? `Replay not started: ${err.payload?.reason || "another run is active or the source changed"}.` :
          "Replay was not confirmed. Check the matching original answer source (--replay-source), provider configuration and server connection. The same request can be retried without creating duplicate runs.";
        error.hidden = false;
      } finally { start.disabled = false; cancel.disabled = false; }
    });
    dialog.showModal();
  }

  async function loadComparisons() {
    currentStudy = null;
    const request = ++comparisonRequest;
    comparisonPane.replaceChildren(notice("Loading comparison records…"));
    try {
      const listing = await api("/api/teacher/comparisons");
      if (request !== comparisonRequest) return;
      currentStudy = null;
      comparisonPane.replaceChildren(heading("EDUCATOR REVIEW / SOURCE-MASKED", "Judge the draft, not the provider.",
        "Compare two drafts against the same observed evidence. Sources stay hidden until every judgment in this review is saved."));
      comparisonPane.append(notice("This is a pilot review protocol, not a validated benchmark. Case order is shuffled, A/B placement is balanced, and identical drafts are retained. Writing style or prior exposure may still reveal sources."));
      const layout = el("div", null, "comparison-setup");
      const form = el("form", null, "card review-form");
      form.append(el("h3", "Start a review"));
      const reviewer = el("input"); reviewer.required = true; reviewer.maxLength = 80;
      reviewer.placeholder = "e.g. reviewer-01"; reviewer.autocomplete = "off";
      const audience = select([["student", "Student-facing drafts"], ["teacher", "Teacher-facing drafts"]]);
      const exposure = select([["", "Choose an answer"], ["no", "No, I have not seen these drafts"], ["yes", "Yes, or I am unsure"]]);
      exposure.required = true;
      if (exposed) exposure.value = "yes";
      form.append(field("Reviewer code (no personal names)", reviewer), field("Audience for this review", audience),
        field("Have you seen these scenario drafts or their sources?", exposure),
        notice("Review all saved cases for one audience. You can leave and resume later. Saved judgments cannot be edited; sources are revealed only at completion.", "meta small"));
      const submit = el("button", "Begin source-masked review", "primary"); submit.type = "submit";
      const error = el("p", "", "error"); error.setAttribute("role", "alert"); error.hidden = true;
      form.append(submit, error);
      form.addEventListener("submit", async (event) => {
        event.preventDefault(); submit.disabled = true; error.hidden = true;
        try {
          const view = await api("/api/teacher/comparisons", {body: {reviewer_label: reviewer.value.trim(),
            audience: audience.value, prior_exposure: exposed || exposure.value === "yes"}});
          renderComparison(view);
        } catch (err) {
          error.textContent = "Review could not be started. Check that a compatible saved replay is available and the server is reachable.";
          error.hidden = false;
        } finally { submit.disabled = false; }
      });
      const ledger = el("div", null, "card");
      ledger.append(el("h3", "Resume a saved review"), el("p", "Local review records are separate from live-session reviews.", "meta"));
      if (!listing.comparisons.length) ledger.append(notice("No reviews yet. Your first review will appear here.", "empty-state"));
      listing.comparisons.forEach((row) => {
        const entry = button("", () => loadComparison(row.id), "saved-comparison");
        entry.append(el("strong", row.reviewer_label), el("span", `${row.completed}/${row.total} · ${row.audience} · ${row.status.replaceAll("_", " ")}`, "meta"),
          el("span", new Date(row.created_at).toLocaleString(), "meta small"));
        ledger.append(entry);
      });
      layout.append(form, ledger); comparisonPane.append(layout);
    } catch (err) { if (request === comparisonRequest) failure(comparisonPane, loadComparisons); }
  }

  async function loadComparison(id) {
    currentStudy = null;
    const request = ++comparisonRequest;
    comparisonPane.replaceChildren(notice("Resuming your saved review…"));
    try {
      const view = await api(`/api/teacher/comparisons/${id}`);
      if (request === comparisonRequest) renderComparison(view);
    } catch (err) { if (request === comparisonRequest) failure(comparisonPane, () => loadComparison(id)); }
  }

  function renderComparison(view) {
    currentStudy = view.id;
    comparisonPane.replaceChildren();
    const top = el("div", null, "ws-head");
    top.append(heading("SOURCE-MASKED REVIEW", `${view.reviewer_label} / ${view.audience} drafts`,
      `${view.completed} of ${view.total} judgments saved. ${view.prior_exposure ? "Prior exposure declared." : "No prior exposure declared."}`),
      button("All reviews", loadComparisons));
    comparisonPane.append(top);
    const progress = el("progress", null, "review-progress");
    progress.max = view.total; progress.value = view.completed;
    progress.setAttribute("aria-label", "Saved review progress");
    comparisonPane.append(progress);
    if (view.status === "source_changed") {
      comparisonPane.append(notice("The replay source changed or is unavailable. This review is paused to avoid mixing revisions. Restore the exact original report and restart the server, or begin a new review."));
      return;
    }
    if (view.status === "complete") {
      comparisonPane.append(heading("REVIEW COMPLETE", "Your judgments are saved.",
        "Sources can now be revealed. These are descriptive preferences, not evidence of educational effectiveness."));
      const metrics = el("div", null, "metrics-grid");
      Object.entries(view.results.preferences).forEach(([key, count]) => metrics.append(metric(count,
        {baseline: "Baseline preferred", selected: "Selected draft preferred", tie: "Ties", neither: "Neither acceptable"}[key])));
      comparisonPane.append(metrics, notice(view.results.limitation),
        notice(`${view.results.identical_pairs} identical-text pairs were included. “Selected” includes recorded local, cached, and fallback execution; it does not always mean a new provider call.`, "meta"));
      const link = el("a", "Download judgments, source mapping & provenance", "primary-link");
      link.href = `/api/teacher/comparisons/${view.id}/export`; link.download = "";
      comparisonPane.append(link);
      return;
    }
    const task = view.task;
    const title = el("h3", `Case ${task.ordinal} of ${view.total}`); title.tabIndex = -1;
    comparisonPane.append(title, evidenceView(task.evidence));
    const pair = el("div", null, "compare blind-pair");
    pair.append(draft("Draft A", task.drafts.A, "SOURCE HIDDEN"), draft("Draft B", task.drafts.B, "SOURCE HIDDEN"));
    comparisonPane.append(pair);
    const form = el("form", null, "card judgment-form");
    form.append(el("h3", "Record your judgment"), el("p",
      "Consider evidence support, usefulness, audience fit, and unsupported claims. A tie is valid; choose neither if both drafts are unsuitable.", "meta"));
    const choices = el("fieldset", null, "preference-options");
    choices.append(el("legend", "Which draft would you prefer for educator review?"));
    [["A", "Prefer A"], ["B", "Prefer B"], ["tie", "Tie / no preference"], ["neither", "Neither is acceptable"]].forEach(([value, label]) => {
      const input = el("input"); input.type = "radio"; input.name = "preference"; input.value = value; input.required = true;
      const option = el("label", null, "option"); option.append(input, el("span", label)); choices.append(option);
    });
    const fields = el("div", null, "judgment-fields");
    const supports = [["", "Choose a judgment"], ["supported", "Supported by the evidence"],
      ["unsupported", "Contains unsupported claims"], ["unsure", "Unsure / needs review"]];
    const supportA = select(supports); supportA.required = true;
    const supportB = select(supports); supportB.required = true;
    const confidence = select([["", "Choose confidence"], ["low", "Low"], ["moderate", "Moderate"], ["high", "High"]]); confidence.required = true;
    fields.append(field("Evidence support · A", supportA), field("Evidence support · B", supportB), field("Confidence in preference", confidence));
    const note = el("textarea"); note.rows = 3; note.maxLength = 2000;
    note.placeholder = "What supported your judgment? Do not enter personal or student information.";
    const submit = el("button", task.ordinal === view.total ? "Save final judgment & reveal results" : "Save judgment & next case", "primary"); submit.type = "submit";
    const error = el("p", "", "error"); error.hidden = true; error.setAttribute("role", "alert");
    form.append(choices, fields, field("Rationale (optional, up to 2,000 characters)", note),
      notice("Saved judgments are immutable. A retry of the same submission is recorded only once.", "meta small"), submit, error);
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); submit.disabled = true; error.hidden = true;
      try {
        const result = await api(`/api/teacher/comparisons/${view.id}/reviews`, {body: {
          task_id: task.task_id, preference: new FormData(form).get("preference"),
          support_a: supportA.value, support_b: supportB.value, confidence: confidence.value, note: note.value}});
        if (currentStudy !== view.id) return;
        renderComparison(result);
        comparisonPane.querySelector("h3, h2")?.focus();
      } catch (err) {
        error.textContent = err.status === 409 ?
          "This task or replay changed, or a judgment was already saved. Your entries are still here; use All reviews to resume the current state." :
          "Save was not confirmed. Your entries are preserved; retry the same judgment safely.";
        error.hidden = false;
      } finally { submit.disabled = false; }
    });
    comparisonPane.append(form);
  }
}
