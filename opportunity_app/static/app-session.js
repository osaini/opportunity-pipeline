// The sign-in gate and ending a session, the manual refresh dialog and the system status panel.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { els, registerSessionPoller, state, stopSessionPollers } = App;

  // From app-ui.js.
  const { announce, chip, clearError, element, formatWeekdayDateTime, plural, setBackgroundInert } = App;

  // From app-http.js.
  const { api } = App;

  // From app-status.js.
  const { invalidateUrgentBadge, loadStats, startAutomationStatus } = App;

  // From app-nav.js.
  const { loadCurrentView } = App;

  // Defined in files that load later; looked up when called.
  const closeDetail = (...args) => App.closeDetail(...args);
  const refreshProgramsLabel = (...args) => App.refreshProgramsLabel(...args);
  const resetDeckFilters = (...args) => App.resetDeckFilters(...args);

  function resetWorkspace() {
    // Nothing from the previous session may stay readable behind the gate.
    closeDetail({ updateHistory: false });
    state.userId = null;
    state.sessionEpoch += 1;
    state.selectedId = null;
    state.loadSequence += 1;
    els.programsNavLabel.textContent = "Programs";
    els.results.replaceChildren();
    els.resultCount.textContent = "Loading opportunities…";
    els.pageStatus.textContent = "";
    [els.statActive, els.statTotal, els.statTracked, els.statScore].forEach((stat) => { stat.textContent = "—"; });
    [els.role, els.region, els.source, els.term].forEach((select) => { select.options.length = 1; });
    resetDeckFilters();
    els.personalizePrompt.hidden = true;
    stopSessionPollers();
    state.refresh = null;
    els.refreshOpen.hidden = true;
    if (els.refreshDialog.open) els.refreshDialog.close();
    clearError();
    announce("");
  }

  function showAuth(message = "", { focus = els.authEmail } = {}) {
    state.activeRecordingStop?.();
    if (state.userId) resetWorkspace();
    els.authGate.classList.add("is-visible");
    els.authGate.removeAttribute("aria-hidden");
    setBackgroundInert("auth", true);
    els.authError.textContent = message;
    window.setTimeout(() => focus.focus(), 0);
  }

  function hideAuth(session) {
    state.userId = session.user_id || "local-user";
    state.sessionEpoch += 1;
    els.authGate.classList.remove("is-visible");
    els.authGate.setAttribute("aria-hidden", "true");
    setBackgroundInert("auth", false);
    els.tokenInput.value = "";
    // Refreshing rewrites every student's data, so only the owner is offered it.
    els.refreshOpen.hidden = state.userId !== "local-user";
    if (!els.refreshOpen.hidden) {
      pollRefresh();
      loadSystemStatus();
    }
    invalidateUrgentBadge();
    startAutomationStatus();
    refreshProgramsLabel();
  }

  // Manual refresh and purge. Polling continues while a run is in progress even
  // with the dialog closed, so the deck reloads the moment the run finishes.
  const REFRESH_POLL_MS = 1000;

  function refreshFraction(step) {
    if (step.state === "done") return 1;
    return step.total ? Math.min(step.done / step.total, 1) : 0;
  }

  function refreshStepItem(step) {
    let item = els.refreshSteps.querySelector(`[data-step="${step.key}"]`);
    if (item) return item;
    item = element("li");
    item.dataset.step = step.key;
    const row = element("div", "refresh-row");
    const label = element("span", "refresh-label", step.label);
    label.id = `refresh-step-${step.key}`;
    row.append(label, element("span", "refresh-detail"));
    const bar = document.createElement("progress");
    bar.max = 100;
    bar.value = 0;
    bar.setAttribute("aria-labelledby", label.id);
    item.append(row, bar);
    els.refreshSteps.appendChild(item);
    return item;
  }

  function renderRefresh(payload) {
    state.refresh = payload;
    const running = payload.state === "running";
    const overall = Math.round(
      (payload.steps.reduce((sum, step) => sum + refreshFraction(step), 0) / payload.steps.length) * 100
    );
    els.refreshOverall.value = overall;
    els.refreshOverallPercent.textContent = `${overall}%`;
    payload.steps.forEach((step) => {
      const item = refreshStepItem(step);
      item.className = `is-${step.state}`;
      item.querySelector(".refresh-detail").textContent = step.detail || (step.state === "pending" ? "Waiting" : "");
      const bar = item.querySelector("progress");
      // A running step with no known size yet is indeterminate, not stuck at 0.
      if (step.state === "running" && !step.total) bar.removeAttribute("value");
      else bar.value = Math.round(refreshFraction(step) * 100);
    });
    const current = payload.steps.findIndex((step) => step.state === "running");
    els.refreshStatus.classList.toggle("is-error", payload.state === "failed");
    if (!payload.available) {
      els.refreshStatus.textContent = "Manual refresh is only available when the app runs against its main database.";
    } else if (running) {
      els.refreshStatus.textContent = current >= 0
        ? `Step ${current + 1} of ${payload.steps.length}: ${payload.steps[current].label}`
        : "Starting…";
    } else if (payload.state === "succeeded") {
      els.refreshStatus.textContent = `Finished ${new Date(payload.finished_at).toLocaleString()}.`;
    } else if (payload.state === "failed") {
      els.refreshStatus.textContent = payload.error || "The refresh failed.";
    } else {
      els.refreshStatus.textContent = "";
    }
    els.refreshStart.disabled = running || !payload.available;
    els.refreshStart.textContent = running ? "Refreshing…" : payload.state === "idle" ? "Start refresh" : "Run again";
    els.refreshOpen.textContent = running ? `Refreshing… ${overall}%` : "Refresh & purge";
    markRefreshAttention();
  }

  // What runs by itself, and whether it is working. The sidebar button carries
  // a marker while anything here needs attention, so a schedule that stopped
  // firing or a board that stopped answering does not stay invisible.
  function markRefreshAttention() {
    const problems = state.systemStatus?.problems || [];
    els.refreshOpen.classList.toggle("needs-attention", problems.length > 0);
    let note = els.refreshOpen.querySelector(".sr-only");
    if (!problems.length) {
      note?.remove();
      return;
    }
    if (!note) {
      note = element("span", "sr-only");
      els.refreshOpen.appendChild(note);
    }
    note.textContent = ` (needs attention: ${problems.join("; ")})`;
  }

  const SOURCE_HEALTH_LABELS = {
    failing: ["Failing", "is-alert"],
    stale: ["No answer in 3+ days", "is-soon"],
    never: ["Not fetched yet", ""],
    ok: ["OK", "is-good"],
  };

  async function loadSystemStatus() {
    try {
      state.systemStatus = await api("/api/v1/system/status");
    } catch (error) {
      state.systemStatus = null;
      return;
    }
    markRefreshAttention();
    renderSystemStatus();
  }

  const BOARD_KIND_LABELS = { greenhouse: "Greenhouse", ashby: "Ashby", lever: "Lever" };

  // Find a company's Greenhouse, Ashby or Lever board and track it. Only a
  // Greenhouse board that carries the company's own name is added on one
  // click; any other match waits until the student confirms, from its
  // postings, that it is the right company.
  function boardTrackerForm() {
    const wrap = element("div", "board-tracker");
    wrap.appendChild(element("h4", "", "Track another company's job board"));
    const form = element("form", "board-tracker-form");
    const label = element("label", "sr-only", "Company name");
    label.htmlFor = "board-company";
    const input = document.createElement("input");
    input.id = "board-company";
    input.required = true;
    input.maxLength = 120;
    input.placeholder = "Company name, e.g. Anduril";
    const find = element("button", "secondary-button", "Find board");
    find.type = "submit";
    form.append(label, input, find);
    const result = element("div", "board-tracker-result");
    result.setAttribute("aria-live", "polite");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const company = input.value.trim();
      if (!company) return;
      find.disabled = true;
      result.replaceChildren(element("p", "profile-help", "Checking Greenhouse, Ashby and Lever…"));
      try {
        renderBoardLookup(await api("/api/v1/sources/lookup", { method: "POST", body: JSON.stringify({ company }) }), result);
      } catch (error) {
        result.replaceChildren(element("p", "form-error", error.message));
      } finally {
        find.disabled = false;
      }
    });
    wrap.append(form, result);
    wrap.appendChild(element("p", "profile-help", "Workday and custom career sites cannot be found by name; add those to config/sources.local.json by hand."));
    return wrap;
  }

  function renderBoardLookup(found, host) {
    host.replaceChildren();
    const vendor = BOARD_KIND_LABELS[found.kind] || found.kind;
    if (found.status === "already-configured") {
      host.appendChild(element("p", "", `${found.company} is already tracked (${vendor}: ${found.slug}).`));
      return;
    }
    if (found.status !== "resolved") {
      host.appendChild(element("p", "", `No Greenhouse, Ashby or Lever board with postings was found for ${found.company}. That does not mean it has none: its careers page may use another system.`));
      return;
    }
    const facts = `${plural(found.total, "posting", "postings")}, ${found.matching} matching your search terms`;
    if (found.identity === "confirmed") {
      host.appendChild(element("p", "", `Found ${found.board_name}'s ${vendor} board (${found.slug}): ${facts}.`));
    } else if (found.identity === "review") {
      host.appendChild(element("p", "form-error", `This ${vendor} board is named “${found.board_name}”, not “${found.company}”. It may belong to a different company.`));
      host.appendChild(element("p", "", facts));
    } else {
      host.appendChild(element("p", "", `Found a ${vendor} board at “${found.slug}”: ${facts}. ${vendor} does not say whose board it is, so check the postings below are ${found.company}'s.`));
    }
    if (found.sample_titles.length) {
      const titles = element("ul", "board-tracker-titles");
      found.sample_titles.forEach((title) => titles.appendChild(element("li", "", title)));
      host.appendChild(titles);
    }
    const add = element("button", "primary-button", "Track this board");
    add.type = "button";
    let confirm = null;
    if (found.identity !== "confirmed") {
      const confirmLabel = element("label", "outreach-scope");
      confirm = document.createElement("input");
      confirm.type = "checkbox";
      confirmLabel.append(confirm, document.createTextNode(` These postings are ${found.company}'s`));
      add.disabled = true;
      confirm.addEventListener("change", () => { add.disabled = !confirm.checked; });
      host.appendChild(confirmLabel);
    }
    const note = element("p", "form-status");
    add.addEventListener("click", async () => {
      add.disabled = true;
      try {
        const added = await api("/api/v1/sources", {
          method: "POST",
          body: JSON.stringify({ lookup_id: found.lookup_id, student_confirmed: Boolean(confirm?.checked) }),
        });
        host.replaceChildren(element("p", "", added.added
          ? `Tracking ${added.entry.company}. The next refresh fetches its postings.`
          : `${found.company} was already tracked.`));
        announce(added.added ? `Tracking ${added.entry.company}.` : "Already tracked.");
      } catch (error) {
        note.textContent = error.message;
        add.disabled = Boolean(confirm && !confirm.checked);
      }
    });
    host.append(add, note);
  }

  function renderSystemStatus() {
    const payload = state.systemStatus;
    els.systemStatus.hidden = !payload?.available && !payload?.can_add_boards;
    if (els.systemStatus.hidden) return;
    const body = els.systemStatusBody;
    // Opening the dialog renders at once and again when a fresh status arrives: the board form is
    // kept across that, so a company typed or a lookup already shown is not wiped by the second render.
    const tracker = body.querySelector(".board-tracker") || boardTrackerForm();
    body.replaceChildren();
    if (!payload.available) {
      body.appendChild(tracker);
      return;
    }

    if (payload.problems.length) {
      const alert = element("ul", "system-status-problems");
      payload.problems.forEach((problem) => alert.appendChild(element("li", "", problem)));
      body.appendChild(alert);
    }

    const jobs = element("ul", "system-status-jobs");
    payload.jobs.forEach((job) => {
      const item = element("li", "system-status-job");
      const head = element("div", "refresh-row");
      head.append(element("span", "", job.label), chip(job.installed ? (job.state === "Running" ? "Running" : "Scheduled") : "Not scheduled", job.installed ? "is-good" : job.job === "autostart" ? "" : "is-alert"));
      item.appendChild(head);
      const facts = [job.schedule];
      if (job.installed) {
        if (job.last_run_at) facts.push(`last ran ${formatWeekdayDateTime(job.last_run_at)}`);
        if (job.next_run_at && job.job !== "autostart") facts.push(`next ${formatWeekdayDateTime(job.next_run_at)}`);
      }
      item.appendChild(element("p", "profile-help", facts.join(" · ")));
      if (job.job === "daily" && payload.daily) {
        const daily = payload.daily;
        const summary = !daily.ran
          ? "No daily refresh has run on this computer yet."
          : daily.in_progress
            ? `A daily refresh started ${formatWeekdayDateTime(daily.started_at)} and has not finished.`
            : `Last finished ${formatWeekdayDateTime(daily.finished_at)}${daily.exit_code ? ` with problems (exit ${daily.exit_code}); see data/run.log` : ""}.`;
        item.appendChild(element("p", daily.overdue && !daily.in_progress ? "form-error" : "profile-help", summary));
      }
      if (!job.installed && job.job !== "autostart") {
        const install = element("button", "secondary-button", "Schedule it");
        install.type = "button";
        install.setAttribute("aria-label", `Schedule the ${job.label.toLowerCase()}`);
        const note = element("p", "form-status");
        note.setAttribute("aria-live", "polite");
        install.addEventListener("click", async () => {
          install.disabled = true;
          note.textContent = "Adding it to this computer's scheduler…";
          try {
            state.systemStatus = await api(`/api/v1/system/schedules/${job.job}/install`, { method: "POST" });
            markRefreshAttention();
            renderSystemStatus();
            announce(`${job.label} scheduled.`);
          } catch (error) {
            note.textContent = error.message;
            install.disabled = false;
          }
        });
        item.append(install, note);
      } else if (!job.installed) {
        item.appendChild(element("p", "profile-help", "To add it, run: python -m opportunity_app.launch install-autostart"));
      }
      jobs.appendChild(item);
    });
    body.appendChild(jobs);

    const sources = payload.sources;
    if (sources?.available) {
      const counts = { failing: 0, stale: 0, never: 0, ok: 0 };
      sources.items.forEach((item) => { counts[item.health] += 1; });
      const summary = element("div", "application-facts");
      summary.append(
        chip(`${sources.enabled} boards enabled`),
        chip(`${counts.failing} failing`, counts.failing ? "is-alert" : ""),
        chip(`${counts.stale} quiet 3+ days`, counts.stale ? "is-soon" : ""),
        chip(`${counts.never} not fetched yet`),
      );
      body.appendChild(summary);
      const trouble = sources.items.filter((item) => item.health !== "ok");
      if (trouble.length) {
        const details = element("details", "system-status-sources");
        details.open = counts.failing > 0;
        details.appendChild(element("summary", "", `Boards that need a look (${trouble.length})`));
        const list = element("ul");
        trouble.forEach((item) => {
          const row = element("li");
          const [label, tone] = SOURCE_HEALTH_LABELS[item.health];
          row.append(element("strong", "", item.name), document.createTextNode(" "), chip(label, tone));
          const facts = [];
          if (item.failures_in_a_row) facts.push(`${plural(item.failures_in_a_row, "failed attempt", "failed attempts")} in a row`);
          facts.push(item.last_success_at ? `last answered ${formatWeekdayDateTime(item.last_success_at)}` : item.health === "never" ? "added since the last refresh, or never reached" : "never answered");
          row.appendChild(element("p", "profile-help", facts.join(" · ")));
          if (item.last_error) row.appendChild(element("p", "profile-help system-status-error", item.last_error));
          list.appendChild(row);
        });
        details.appendChild(list);
        body.appendChild(details);
      }
    }
    if (payload.can_add_boards) body.appendChild(tracker);
  }

  async function pollRefresh() {
    window.clearTimeout(state.refreshTimer);
    const wasRunning = state.refresh?.state === "running";
    let payload;
    try {
      payload = await api("/api/v1/refresh");
    } catch (error) {
      if (!state.userId) return;
      els.refreshStatus.textContent = error.message;
      if (wasRunning) state.refreshTimer = window.setTimeout(pollRefresh, REFRESH_POLL_MS * 3);
      return;
    }
    if (!state.userId) return;
    renderRefresh(payload);
    if (payload.state === "running") {
      state.refreshTimer = window.setTimeout(pollRefresh, REFRESH_POLL_MS);
    } else if (wasRunning) {
      await Promise.all([loadCurrentView(), loadStats(), loadSystemStatus()]);
      invalidateUrgentBadge();
    }
  }

  // What this file does as it loads, run once by app.js in load order.
  function installSession() {
    registerSessionPoller(() => window.clearTimeout(state.refreshTimer));
  }

  Object.assign(App, {
    REFRESH_POLL_MS, hideAuth, installSession, loadSystemStatus, pollRefresh, renderRefresh, renderSystemStatus,
    showAuth,
  });
})();
