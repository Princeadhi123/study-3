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
    const node = DemoUI.heading(title, description,
      {eyebrow: kicker, level: 2});
    node.classList.add("section-heading");
    return node;
  }

  function notice(message, className = "boundary-note") {
    return el("p", message, className);
  }

  function replayProviderLabel(run) {
    if (run.provider_mode === "rules" || run.provider_execution === "rules_only") return "Rules only";
    if (run.provider_execution === "fresh_hosted_calls") return "Configured providers · fresh hosted calls";
    return "Configured providers · historical execution";
  }

  function feedbackVariantLabel(variant) {
    if (variant === "question_first") return "Question-first";
    if (variant === "worked_first") return "Worked-first";
    return "Historical / unspecified";
  }

  function replayCaseNotice(run, row, response) {
    const base = `Replay result · ${feedbackVariantLabel(run?.feedback_variant)} · ${statusLabel(row?.status)}. Original results remain unchanged.`;
    if (!row || (row.status !== "queued" && row.status !== "running")) {
      if (row?.status === "complete") {
        return `${base} Replay is complete.`;
      }
      return base;
    }
    const provider = response?.provider_job?.status || "not_requested";
    const diagnostics = Object.values(response?.checkpoints || {})
      .map((checkpoint) => checkpoint?.diagnostics_job?.status)
      .filter(Boolean);
    const providerReady = provider === "ready" || provider === "fallback";
    const diagnosticsPending = diagnostics.some((s) => s === "pending");
    if (provider === "waiting_for_diagnostics") {
      return `${base} Waiting for model diagnostics before Aitta can draft the feedback. This replay is not complete.`;
    }
    if (!providerReady) {
      return `${base} Feedback is still being prepared; this replay is not complete.`;
    }
    if (diagnosticsPending) {
      let message = `${base} Feedback is ready; model diagnostics are still pending. This replay is not complete.`;
      const generator = response?.provider_job?.executions?.student?.generator;
      if (generator?.reused === true && generator?.metadata?.status === "completed") {
        message += " The Aitta response was reused from a successful saved capture.";
      }
      return message;
    }
    return `${base} Feedback and diagnostics are ready; saving the replay result.`;
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
    const bankMode = library.bank_mode || "demo";
    const bankLabel = library.bank_label || "Demo bank";
    libraryPane.append(notice(bankMode === "demo"
      ? `${bankLabel} · 40 questions per scenario.`
      : `${bankLabel} · 40 questions per scenario. Synthetic research bank pending educator review — not approved for learner delivery.`));
    libraryPane.append(notice("Planning a blind review? Complete it before opening scenario drafts. Prior exposure can bias preferences."));
    const replayToolbar = el("div", null, "replay-toolbar");
    const replayAll = button(`Replay all ${library.scenarios.length} scenarios`, () => confirmReplay(library.scenarios.map((row) => row.id)), "primary");
    replayAll.id = "replay-all";
    replayToolbar.append(replayAll, el("span", "Same answers · current scoring, feedback & model inference · no retraining", "meta small"));
    const history = el("div"); history.id = "replay-history";
    const replayTools = DemoUI.section("Replay tools",
      "Replay the same recorded answers through the current pipeline — browsing never starts a run.");
    replayTools.append(replayToolbar, history);
    libraryPane.append(replayTools);
    const controls = el("div", null, "library-controls");
    const search = el("input");
    search.type = "search"; search.placeholder = "Search a scenario or skill…"; search.id = "scenario-search";
    const groups = [...new Set(library.scenarios.map((row) => row.group))].sort();
    const group = select([["", "All patterns"], ...groups.map((g) => [g, library.scenarios.find((row) => row.group === g).group_label])]);
    const difference = select([["", "All selections"], ["different", "At least one differs"], ["same", "Both match baseline"]]);
    const order = select([["name", "Name A–Z"], ["low", "Observed score: low first"], ["high", "Observed score: high first"]]);
    const replayFiltered = button("", () => confirmReplay(filteredIds));
    replayFiltered.id = "replay-filtered";
    controls.append(field("Find a case", search), field("Scenario group", group),
      field("Selection comparison", difference), field("Sort by", order),
      field("Matching cases", replayFiltered));
    const layout = el("div", null, "library-layout");
    const side = el("div", null, "scenario-browser");
    const count = el("p", null, "meta small"); count.setAttribute("role", "status");
    const list = el("div", null, "scenario-list");
    list.setAttribute("aria-label", "Saved scenarios");
    const detail = el("div", null, "scenario-detail");
    detail.id = "scenario-detail";
    const intro = heading("EXPLORE THE EVIDENCE", "Choose a scenario.",
      "Use the same detailed views as live sessions. Replay a case to test your changes. Use 03 Blind comparison to compare feedback drafts.");
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
    scenarioView = mountDetail(viewHost, {refresh: () => loadCase(chosen),
      getConfig});
    side.append(count, list); layout.append(side, detail);
    const browse = DemoUI.section("Browse scenarios",
      "Inspect a retained original or a replay result. Scenario evidence stays read-only.");
    browse.append(controls, layout);
    libraryPane.append(browse);
    function renderCases() {
      const query = search.value.trim().toLowerCase();
      const rows = library.scenarios.filter((row) =>
        (!group.value || row.group === group.value) &&
        (!difference.value || (difference.value === "different") === (row.selection_differences > 0)) &&
        `${row.display_name} ${row.description} ${row.name} ${row.skills.map((s) => englishSkillName(s.skill_name)).join(" ")}`.toLowerCase().includes(query));
      rows.sort((a, b) => order.value === "name" ? a.display_name.localeCompare(b.display_name) :
        (order.value === "low" ? 1 : -1) * (a.total.correct - b.total.correct) || a.name.localeCompare(b.name));
      filteredIds = rows.map((row) => row.id);
      replayFiltered.textContent = `Replay ${rows.length} matching scenario` +
        (rows.length === 1 ? "" : "s");
      replayFiltered.disabled = !rows.length;
      count.textContent = `${rows.length} of ${library.scenarios.length} scenarios`;
      const scrollTop = list.scrollTop;
      list.replaceChildren();
      if (!rows.length) list.append(notice("No cases match. Try a broader search or clear the filters."));
      rows.forEach((row) => {
        const item = button("", () => { chosen = row.id; renderCases(); updateVersions(); loadCase(row.id); }, "scenario-row");
        item.classList.toggle("active", chosen === row.id);
        item.setAttribute("aria-pressed", String(chosen === row.id));
        item.dataset.caseId = row.id;
        item.title = row.description;
        item.append(el("span", row.display_name, "label"),
          el("span", `Original scenario: ${row.total.correct}/${row.total.out_of} correct · ${row.group_label}`, "meta small"));
        list.append(item);
      });
      list.scrollTop = scrollTop;
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
      const option = el("option", `${new Date(run.created_at).toLocaleString()} · ${feedbackVariantLabel(run.feedback_variant)} · ${replayProviderLabel(run)} · ${statusLabel(row.status)}`);
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
    const contextKey = `${id}|${version}`;
    const contextChanged = displayedVersion !== contextKey;
    $("#scenario-intro").hidden = true;
    if (contextChanged) viewHost.hidden = true;
    const run = runs.find((r) => r.id === version);
    const row = run?.cases.find((c) => c.scenario_id === id);
    const initialNotice = version ? `Replay result · ${feedbackVariantLabel(run?.feedback_variant)} · ${statusLabel(row?.status)}. Original results remain unchanged.` : "Retained original — read-only. Select a replay result to inspect current behavior.";
    // During refresh of the already-displayed context keep the confirmed
    // stage suffix; rewriting the generic prefix here would flicker on
    // every poll until the detail response returns.
    if (contextChanged && caseNotice.textContent !== initialNotice) {
      caseNotice.textContent = initialNotice;
    }
    if (version && !row?.session_id) {
      viewHost.hidden = true;
      return;
    }
    try {
      const response = await api(version ? `/api/teacher/replays/${version}/cases/${id}` : `/api/teacher/scenarios/${id}`);
      if (request !== caseRequest || id !== chosen || version !== selectedRun) return;
      const view = version ? response : response.detail;
      exposed = true;
      try { sessionStorage.setItem("replay-exposure", "yes"); } catch (err) {}
      scenarioView.update(view);
      const noticeText = version ? replayCaseNotice(run, row, response) : initialNotice;
      if (caseNotice.textContent !== noticeText) caseNotice.textContent = noticeText;
      displayedVersion = contextKey;
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
      }
      // A completed replay can still have teacher-triggered generation
      // running. Its live detail changes independently of the run list.
      if (chosen && selectedRun) await loadCase(chosen);
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
      const activeCount = run.cases.filter((c) => c.status === "running").length;
      const parallelNote = (run.max_parallel && run.max_parallel > 1) ? ` · ${run.max_parallel}x parallel` : "";
      const activeNote = activeCount > 0 ? ` (${activeCount} active)` : "";
      section.append(el("summary", `${new Date(run.created_at).toLocaleString()} · ${feedbackVariantLabel(run.feedback_variant)} · ${replayProviderLabel(run)}${parallelNote} · ${statusLabel(run.status)}${activeNote} · ${run.completed}/${run.total}`));
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
        cases.append(item);
      });
      section.append(cases, raw("Run provenance and code fingerprints", {code_sha256: run.code_sha256, source: run.source,
        report_sha256: run.report_sha256, diagnostics_mode: run.diagnostics_mode}));
      history.append(section);
    });
    container.append(history);
  }

  function closeReplayDialog(dialog) {
    if (dialog.open) dialog.close();
    dialog.remove();
  }

  function confirmReplay(ids) {
    if (!ids.length || ids.some((id) => !id)) return;
    const selectedIds = [...ids];
    document.querySelectorAll(".replay-dialog").forEach((old) => {
      if (!old.open) old.remove();
    });
    const dialog = el("dialog", null, "replay-dialog");
    const form = el("form", null, "review-form");
    const title = el("h2", selectedIds.length === 1 ? "Replay this scenario?" : `Replay ${selectedIds.length} scenarios?`);
    title.id = "replay-dialog-title"; dialog.setAttribute("aria-labelledby", title.id);
    const mode = select([["rules", "Rules only — no hosted calls"], ["hosted", "Use configured hosted providers"]]);
    mode.id = "replay-provider-mode";
    mode.options[1].disabled = getConfig().provider_mode !== "hosted";
    const variant = select([
      ["question_first", getConfig().review_mode === "aligned_two_topic"
        ? "Question-first / full narrative + hints"
        : "Question-first / current version"],
      ["worked_first", "Worked-first / full Aitta narrative"]]);
    variant.id = "replay-feedback-variant";
    variant.options[1].disabled =
      getConfig().review_mode !== "aligned_two_topic";
    const variantNote = notice("", "meta small");
    variantNote.id = "replay-variant-note";
    variantNote.hidden = true;
    const updateVariantNote = () => {
      if (getConfig().review_mode !== "aligned_two_topic") {
        variantNote.hidden = true;
        return;
      }
      variantNote.textContent = "Aitta narrative is shape-checked, " +
        "not fact-verified; educator review is required." +
        (mode.value === "rules"
          ? " Rules only uses deterministic wording; no provider " +
            "narrative runs."
          : "");
      variantNote.hidden = false;
    };
    variant.addEventListener("change", updateVariantNote);
    updateVariantNote();
    const parallel = select([
      ["8", "8 scenarios at a time (parallel)"],
      ["6", "6 scenarios at a time (parallel)"],
      ["4", "4 scenarios at a time (parallel)"],
      ["1", "1 scenario at a time (sequential)"],
    ]);
    parallel.id = "replay-max-parallel";
    parallel.value = selectedIds.length === 1 ? "1" : "8";
    const parallelField = field("Parallel execution", parallel);
    parallelField.hidden = selectedIds.length === 1;
    const consent = el("input"); consent.type = "checkbox"; consent.id = "replay-hosted-consent";
    const approval = el("label", null, "attestation");
    const budget = getConfig().call_budget;
    approval.append(consent, el("span", budget.limit === null
      ? "I approve hosted calls for this run. The server sets no local cap on new calls; provider charges may apply. Every call is a fresh hosted request; no saved provider results are reused. Transient failures are retried automatically within limits."
      : "I approve hosted calls for this run within the server's remaining call budget. Every call is a fresh hosted request; no saved provider results are reused. Transient failures are retried automatically within limits."));
    approval.hidden = true;
    mode.addEventListener("change", () => {
      const hosted = mode.value === "hosted";
      approval.hidden = !hosted; consent.required = hosted;
      updateVariantNote();
    });
    const explanation = notice(`This creates ${selectedIds.length} separate test results using the original answers. It recomputes scoring, skill maps and feedback, and requests fresh frozen-model diagnostics. No model retraining. Hosted budget used: ${budget.used}/${budget.limit ?? "unlimited"}.`);
    const error = el("p", "", "error"); error.hidden = true; error.setAttribute("role", "alert");
    const start = el("button", "Start replay", "primary"); start.type = "submit"; start.id = "confirm-replay";
    const cancel = button("Cancel", () => closeReplayDialog(dialog));
    const actions = el("div", null, "replay-toolbar"); actions.append(start, cancel);
    form.append(title, explanation, field("Feedback execution", mode),
      field("Feedback variant", variant), variantNote, parallelField, approval,
      notice("Restart the server after Python changes. Historical outputs and reviews are preserved. One replay batch runs at a time; stop prevents further cases after the active wave finishes.", "meta small"), error, actions);
    dialog.append(form); document.body.append(dialog);
    dialog.addEventListener("close", () => dialog.remove());
    const requestId = [...crypto.getRandomValues(new Uint8Array(16))].map((b) => b.toString(16).padStart(2, "0")).join("");
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); if (start.disabled) return;
      start.disabled = true; cancel.disabled = true; error.hidden = true;
      try {
        const body = {request_id: requestId,
          report_sha256: library.report_sha256, scenario_ids: selectedIds, provider_mode: mode.value,
          feedback_variant: variant.value,
          allow_provider_calls: mode.value === "hosted" && consent.checked,
          max_parallel: Number(parallel.value)};
        const run = await api("/api/teacher/replays", {body});
        if (chosen && selectedIds.includes(chosen)) selectedRun = run.id;
        closeReplayDialog(dialog);
        await refreshRuns();
      } catch (err) {
        error.textContent = err.status === 409 ? `Replay not started: ${err.payload?.reason || "another run is active or the source changed"}.` :
          "Replay was not confirmed. Check the matching original answer source (--replay-source), provider configuration and server connection. The same request can be retried without creating duplicate runs.";
        error.hidden = false;
      } finally { start.disabled = false; cancel.disabled = false; }
    });
    dialog.showModal();
  }

  function confirmDeleteComparison(row) {
    document.querySelectorAll(".delete-comparison-dialog").forEach((old) => {
      if (!old.open) old.remove();
    });
    const dialog = el("dialog", null, "delete-comparison-dialog");
    const form = el("form", null, "review-form");
    const title = el("h2", "Delete saved review?");
    title.id = "delete-comparison-title"; dialog.setAttribute("aria-labelledby", title.id);
    const error = el("p", "", "error"); error.hidden = true; error.setAttribute("role", "alert");
    const confirmDelete = el("button", "Delete review", "danger"); confirmDelete.type = "submit";
    const cancel = button("Cancel", () => { if (dialog.open) dialog.close(); });
    const actions = el("div", null, "replay-toolbar"); actions.append(confirmDelete, cancel);
    form.append(title,
      el("p", `${row.reviewer_label} · ${row.audience} drafts · ${row.completed}/${row.total} judgments saved.`),
      notice("This permanently deletes this blind review and all its saved judgments. It cannot be undone. Scenarios, replay results, and teacher releases are not affected."),
      error, actions);
    dialog.append(form); document.body.append(dialog);
    dialog.addEventListener("close", () => dialog.remove());
    const request = comparisonRequest;
    let pending = false;
    dialog.addEventListener("cancel", (event) => {
      if (pending) event.preventDefault();
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); if (confirmDelete.disabled) return;
      confirmDelete.disabled = true; cancel.disabled = true; pending = true;
      error.hidden = true;
      try {
        await api(`/api/teacher/comparisons/${row.id}/delete`, {body: {}});
        if (dialog.open) dialog.close();
        if (request === comparisonRequest && currentStudy === null) {
          await loadComparisons(`Deleted saved review for ${row.reviewer_label}.`);
        }
      } catch (err) {
        if (err.status === 404) {
          if (dialog.open) dialog.close();
          if (request === comparisonRequest && currentStudy === null) {
            await loadComparisons(`Saved review for ${row.reviewer_label} was already deleted.`);
          }
          return;
        }
        error.textContent = "Deletion could not be confirmed. Retry or refresh the list to check its status.";
        error.hidden = false;
      } finally { confirmDelete.disabled = false; cancel.disabled = false; pending = false; }
    });
    dialog.showModal();
    cancel.focus();
  }

  async function loadComparisons(deletedNote) {
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
      const layout = el("div", null, "comparison-setup ui-section");
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
        const remove = button("×", () => confirmDeleteComparison(row), "delete-comparison");
        remove.setAttribute("aria-label", `Delete saved review for ${row.reviewer_label}`);
        remove.title = `Delete saved review for ${row.reviewer_label}`;
        const wrapper = el("div", null, "saved-comparison-row");
        wrapper.append(entry, remove);
        ledger.append(wrapper);
      });
      layout.append(form, ledger); comparisonPane.append(layout);
      if (deletedNote) {
        const status = notice(deletedNote);
        status.setAttribute("role", "status");
        status.tabIndex = -1;
        comparisonPane.append(status);
        status.focus();
      }
    } catch (err) { if (request === comparisonRequest) failure(comparisonPane, () => loadComparisons()); }
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
      button("All reviews", () => loadComparisons()));
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
    const form = el("form", null, "card ui-section judgment-form");
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
