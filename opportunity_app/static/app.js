(() => {
  "use strict";

  const PAGE_SIZE = 24;
  // One threshold for "closes soon" on cards and the Urgent queue's window.
  const SOON_DAYS = 14;
  const state = {
    view: "discover",
    offset: 0,
    total: 0,
    loading: false,
    displayMode: "cards",
    applicationMode: "board",
    agentThreadId: null,
    selectedId: null,
    detailReturnPath: "/",
    searchTimer: null,
    loadSequence: 0,
    activeRecordingStop: null,
    personalized: true,
    userId: null,
    refresh: null,
    refreshTimer: null,
    detailReturnFocus: null,
    trackerStatus: null,
    // The active subtab per page; each page reads its own entry.
    subtabs: {
      discover: "all", saved: "all", urgent: "all", applications: "all",
      outreach: "to-contact", prepare: "all", agent: "all", profile: "all",
      programs: "open",
    },
    // Outreach cards acted on in this tab stay visible after they move out of it.
    outreachKeep: new Set(),
    outreachQuery: "",
    outreachTag: "",
    outreachSort: "contact",
    // The company shown in the split view's pane, and the tab picked per company.
    outreachSelected: null,
    outreachTabs: {},
    outreachOpen: null,
    outreachFlash: null,
    // Text typed into the open pane but not saved yet, carried across the one
    // reload an action triggers. outreachDiscardEdits opts a reload out.
    outreachPendingEdits: null,
    outreachDiscardEdits: false,
    applicationFocus: null,
    programsFocus: null,
    outreachFocus: null,
    profileStatus: null,
  };

  const els = {
    appShell: document.querySelector(".app-shell"),
    skipLink: document.querySelector(".skip-link"),
    authGate: document.getElementById("auth-gate"),
    authForm: document.getElementById("auth-form"),
    authSubmit: document.getElementById("auth-submit"),
    authError: document.getElementById("auth-error"),
    tokenInput: document.getElementById("token-input"),
    authEmail: document.getElementById("auth-email"),
    authPassword: document.getElementById("auth-password"),
    logout: document.getElementById("logout-button"),
    refreshOpen: document.getElementById("refresh-open"),
    refreshDialog: document.getElementById("refresh-dialog"),
    refreshClose: document.getElementById("refresh-close"),
    refreshStart: document.getElementById("refresh-start"),
    refreshSteps: document.getElementById("refresh-steps"),
    refreshStatus: document.getElementById("refresh-status"),
    refreshOverall: document.getElementById("refresh-overall"),
    refreshOverallPercent: document.getElementById("refresh-overall-percent"),
    systemStatus: document.getElementById("system-status"),
    systemStatusBody: document.getElementById("system-status-body"),
    discoverNav: document.getElementById("discover-nav"),
    savedNav: document.getElementById("saved-nav"),
    applicationsNav: document.getElementById("applications-nav"),
    outreachNav: document.getElementById("outreach-nav"),
    programsNav: document.getElementById("programs-nav"),
    programsNavLabel: document.getElementById("programs-nav-label"),
    prepareNav: document.getElementById("prepare-nav"),
    agentNav: document.getElementById("agent-nav"),
    profileNav: document.getElementById("profile-nav"),
    urgentNav: document.getElementById("urgent-nav"),
    urgentBadge: document.getElementById("urgent-badge"),
    keyboardHint: document.getElementById("keyboard-hint"),
    userChip: document.getElementById("user-chip"),
    userName: document.getElementById("user-name"),
    pageEyebrow: document.getElementById("page-eyebrow"),
    pageTitle: document.getElementById("page-title"),
    pageLede: document.getElementById("page-lede"),
    search: document.getElementById("search-input"),
    role: document.getElementById("role-filter"),
    region: document.getElementById("region-filter"),
    source: document.getElementById("source-filter"),
    term: document.getElementById("term-filter"),
    remote: document.getElementById("remote-filter"),
    year: document.getElementById("year-filter"),
    pay: document.getElementById("pay-filter"),
    posted: document.getElementById("posted-filter"),
    deadline: document.getElementById("deadline-filter"),
    sort: document.getElementById("sort-filter"),
    results: document.getElementById("results"),
    resultCount: document.getElementById("result-count"),
    resultsEyebrow: document.getElementById("results-eyebrow"),
    pageStatus: document.getElementById("page-status"),
    error: document.getElementById("error-banner"),
    actionStatus: document.getElementById("action-status"),
    personalizePrompt: document.getElementById("personalize-prompt"),
    personalizeButton: document.getElementById("personalize-button"),
    // The same controls render above and below the deck.
    paginations: [...document.querySelectorAll("nav.pagination")],
    displayToggle: document.getElementById("display-toggle"),
    cardView: document.getElementById("card-view-button"),
    listView: document.getElementById("list-view-button"),
    filterPanel: document.getElementById("filter-panel"),
    statsGrid: document.getElementById("stats-grid"),
    detail: document.getElementById("detail-panel"),
    detailContent: document.getElementById("detail-content"),
    detailClose: document.getElementById("detail-close"),
    detailScrim: document.getElementById("detail-scrim"),
    statActive: document.getElementById("stat-active"),
    statTotal: document.getElementById("stat-total"),
    statTracked: document.getElementById("stat-tracked"),
    statScore: document.getElementById("stat-score"),
    subnav: document.getElementById("subnav"),
    subnavTitle: document.getElementById("subnav-title"),
    subnavList: document.getElementById("subnav-list"),
    themeToggle: document.getElementById("theme-toggle"),
    themeLabel: document.getElementById("theme-label"),
  };

  // FastAPI reports a failed request body as a list of {loc, msg, type}
  // objects. Turn any detail shape into a sentence a person can read; never
  // "[object Object]".
  function errorDetailText(detail) {
    if (detail == null || detail === "") return "";
    if (typeof detail === "string") return detail;
    if (typeof detail === "number" || typeof detail === "boolean") return String(detail);
    if (Array.isArray(detail)) {
      const parts = detail.map((entry) => errorDetailText(entry)).filter(Boolean);
      return [...new Set(parts)].join(" ");
    }
    if (typeof detail === "object") {
      if (typeof detail.msg === "string") {
        const text = detail.msg.replace(/^Value error,\s*/i, "").trim();
        if (!text) return "";
        const sentence = /[.!?]$/.test(text) ? text : `${text}.`;
        // Name the field when the message alone would not say which one.
        const field = Array.isArray(detail.loc)
          ? [...detail.loc].reverse().find((part) => typeof part === "string" && !["body", "query", "path", "header"].includes(part))
          : "";
        if (!field || detail.type === "value_error") return sentence;
        const label = field.replace(/_/g, " ");
        return `${label.charAt(0).toUpperCase()}${label.slice(1)}: ${sentence.charAt(0).toLowerCase()}${sentence.slice(1)}`;
      }
      for (const key of ["detail", "message", "error"]) {
        const text = errorDetailText(detail[key]);
        if (text) return text;
      }
      try {
        return JSON.stringify(detail);
      } catch (_) {
        return "";
      }
    }
    return "";
  }

  async function api(path, options = {}) {
    const isFormData = options.body instanceof FormData;
    const csrf = document.cookie.split("; ").find((entry) => entry.startsWith("pipeline_csrf="))?.split("=")[1];
    const method = (options.method || "GET").toUpperCase();
    let response;
    try {
      response = await fetch(path, {
        credentials: "same-origin",
        ...options,
        headers: {
          ...(isFormData ? {} : { "Content-Type": "application/json" }),
          ...(csrf && !["GET", "HEAD", "OPTIONS"].includes(method) ? { "X-CSRF-Token": decodeURIComponent(csrf) } : {}),
          ...(options.headers || {}),
        },
      });
    } catch (cause) {
      // Only a request that never got an answer is a connection problem. Any
      // HTTP status, 5xx included, is the server's verdict and must be shown.
      const error = new Error("Connection lost.");
      error.network = true;
      error.cause = cause;
      throw error;
    }
    if (response.status === 401) {
      showAuth();
      throw new Error("Authentication required");
    }
    if (!response.ok) {
      let detail = `Request failed (${response.status})`;
      let body = null;
      try {
        body = await response.json();
        detail = errorDetailText(body.detail) || detail;
      } catch (_) {
        // The HTTP status remains a useful fallback.
      }
      const error = new Error(detail);
      error.status = response.status;
      error.detail = body?.detail;
      throw error;
    }
    if (!["GET", "HEAD", "OPTIONS"].includes(method)) invalidateUrgentBadge(path);
    if (response.status === 204) return null;
    return response.json();
  }

  // The Urgent nav badge. A refresh is fire-and-forget: it never delays or
  // fails the mutation that triggered it, and a failed refresh hides the count
  // instead of leaving a stale number on screen.
  const URGENT_BADGE_SKIP = [/^\/api\/v1\/session\b/, /^\/api\/v1\/auth\//, /^\/api\/v1\/account\b/];
  const URGENT_RETRY_MS = 30_000;
  const urgentBadge = { generation: 0, inFlight: false, trailing: false, retryTimer: null, controller: null };

  function setUrgentBadge(count) {
    const show = Number.isInteger(count) && count > 0;
    els.urgentBadge.hidden = !show;
    els.urgentBadge.textContent = show ? (count > 99 ? "99+" : String(count)) : "";
    if (show) els.urgentNav.setAttribute("aria-label", `Urgent, ${count} need${count === 1 ? "s" : ""} attention`);
    else els.urgentNav.setAttribute("aria-label", "Urgent");
  }

  function invalidateUrgentBadge(path = "") {
    if (!state.userId || URGENT_BADGE_SKIP.some((pattern) => pattern.test(path))) return;
    urgentBadge.generation += 1;
    if (urgentBadge.inFlight) {
      urgentBadge.trailing = true;
      return;
    }
    refreshUrgentBadge();
  }

  async function refreshUrgentBadge() {
    const generation = urgentBadge.generation;
    const userId = state.userId;
    const controller = new AbortController();
    urgentBadge.controller = controller;
    urgentBadge.inFlight = true;
    urgentBadge.trailing = false;
    try {
      // Plain fetch: a badge must never open the sign-in gate or show an error.
      const response = await fetch("/api/v1/urgent", { credentials: "same-origin", signal: controller.signal });
      if (!response.ok) throw new Error(`Urgent badge failed (${response.status})`);
      const payload = await response.json();
      if (generation === urgentBadge.generation && userId && userId === state.userId) {
        applyFreshUrgentCount(payload.counts?.attention ?? 0);
      }
    } catch (_) {
      if (!controller.signal.aborted && userId && userId === state.userId) {
        setUrgentBadge(null);
        if (!urgentBadge.retryTimer) {
          urgentBadge.retryTimer = window.setTimeout(() => {
            urgentBadge.retryTimer = null;
            invalidateUrgentBadge();
          }, URGENT_RETRY_MS);
        }
      }
    } finally {
      // An aborted request was already replaced by clearUrgentBadge().
      if (urgentBadge.controller === controller) {
        urgentBadge.controller = null;
        urgentBadge.inFlight = false;
        if (urgentBadge.trailing && state.userId) refreshUrgentBadge();
      }
    }
  }

  // Every successful read of the queue lands here, so a pending retry from an
  // earlier failure can never later hide a count that is now correct.
  function applyFreshUrgentCount(count) {
    window.clearTimeout(urgentBadge.retryTimer);
    urgentBadge.retryTimer = null;
    setUrgentBadge(count);
  }

  function clearUrgentBadge() {
    urgentBadge.generation += 1;
    // A stalled request from the old session must not block the next one.
    urgentBadge.controller?.abort();
    urgentBadge.controller = null;
    urgentBadge.inFlight = false;
    urgentBadge.trailing = false;
    window.clearTimeout(urgentBadge.retryTimer);
    urgentBadge.retryTimer = null;
    setUrgentBadge(null);
  }

  function csrfHeaders(method = "POST") {
    const csrf = document.cookie.split("; ").find((entry) => entry.startsWith("pipeline_csrf="))?.split("=")[1];
    return csrf && !["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase())
      ? { "X-CSRF-Token": decodeURIComponent(csrf) }
      : {};
  }

  function requestKey() {
    return globalThis.crypto?.randomUUID?.() || `web-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  // The outbox holds actions made while offline. It is keyed to the signed-in
  // user so a shared browser never replays one person's actions as another's.
  const LEGACY_OUTBOX_KEY = "opportunity-action-outbox";
  const MAX_OUTBOX_ATTEMPTS = 5;

  function outboxKey() {
    return state.userId ? `${LEGACY_OUTBOX_KEY}:${state.userId}` : null;
  }

  function readOutbox() {
    const key = outboxKey();
    if (!key) return [];
    try {
      const value = JSON.parse(localStorage.getItem(key) || "[]");
      return Array.isArray(value) ? value : [];
    } catch (_) {
      return [];
    }
  }

  function writeOutbox(items) {
    const key = outboxKey();
    if (!key) return;
    try {
      if (items.length) localStorage.setItem(key, JSON.stringify(items.slice(-100)));
      else localStorage.removeItem(key);
    } catch (_) {
      // The current action still has a visible error if storage is unavailable.
    }
  }

  function queueAction(entry) {
    writeOutbox([...readOutbox(), { ...entry, attempts: 0 }]);
  }

  function discardLegacyOutbox() {
    try {
      // Written before outboxes were per user; its owner cannot be attributed.
      localStorage.removeItem(LEGACY_OUTBOX_KEY);
    } catch (_) {
      // Storage may be unavailable; there is then nothing to discard.
    }
  }

  async function flushOutbox() {
    const pending = readOutbox();
    if (!pending.length) return;
    const remaining = [];
    let synced = 0;
    const rejected = [];
    for (const action of pending) {
      try {
        await api(action.path, {
          method: "POST",
          headers: { "Idempotency-Key": action.key },
          body: JSON.stringify({ action: action.action }),
        });
        synced += 1;
      } catch (error) {
        // The session ended mid-flush; leave the queue for this user's next sign-in.
        if (error.message === "Authentication required") return;
        const attempts = (action.attempts || 0) + 1;
        if (error.network && attempts < MAX_OUTBOX_ATTEMPTS) remaining.push({ ...action, attempts });
        else rejected.push(error.network ? "still unreachable after several attempts" : error.message);
      }
    }
    writeOutbox(remaining);
    if (synced) announce(`Synced ${plural(synced, "queued action", "queued actions")}.`);
    if (rejected.length) {
      showError(`${plural(rejected.length, "queued action was", "queued actions were")} not applied: ${rejected[0]}`);
    }
  }

  function plural(count, one, many) {
    return `${Number(count).toLocaleString()} ${count === 1 ? one : many}`;
  }

  // Selects that save the moment they change. Chromium fires `change` on every
  // ArrowUp/ArrowDown (and type-ahead letter) on a focused, closed select, so
  // saving on `change` alone would record each value a keyboard user passes
  // through. Keyboard browsing only marks the select dirty; Enter or leaving
  // the select commits it, Escape puts the saved value back. A pointer choice
  // from the open list still saves at once. `commit(value, trigger)` runs at
  // most once at a time; trigger is "pointer", "enter", or "blur".
  const SELECT_BROWSE_KEYS = new Set(["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"]);

  function autoSaveSelect(select, { saved, commit }) {
    let browsing = false;
    let dirty = false;
    let busy = false;
    const run = async (trigger) => {
      browsing = false;
      dirty = false;
      if (busy || select.value === saved()) return;
      busy = true;
      try {
        await commit(select.value, trigger);
      } finally {
        busy = false;
      }
    };
    select.addEventListener("pointerdown", () => { browsing = false; });
    select.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        if (dirty || select.value !== saved()) {
          event.preventDefault();
          run("enter");
        }
        return;
      }
      if (event.key === "Escape") {
        if (!busy && select.value !== saved()) select.value = saved();
        browsing = false;
        dirty = false;
        return;
      }
      // Alt+Arrow and F4 open the list; a choice made there is a deliberate pick.
      if (event.altKey || event.ctrlKey || event.metaKey) return;
      if (SELECT_BROWSE_KEYS.has(event.key) || (event.key.length === 1 && event.key !== " ")) browsing = true;
    });
    select.addEventListener("change", () => {
      if (browsing) {
        dirty = true;
        return;
      }
      run("pointer");
    });
    select.addEventListener("blur", () => {
      // A list rebuild removes the focused select, which fires blur; that is
      // not the user leaving it, so do not save a choice they are still making.
      queueMicrotask(() => {
        if (!select.isConnected) return;
        if (dirty || select.value !== saved()) run("blur");
        else browsing = false;
      });
    });
  }

  function announce(message) {
    els.actionStatus.textContent = message;
    els.actionStatus.hidden = !message;
  }

  function resetWorkspace() {
    // Nothing from the previous session may stay readable behind the gate.
    closeDetail({ updateHistory: false });
    state.userId = null;
    state.selectedId = null;
    state.loadSequence += 1;
    clearUrgentBadge();
    els.programsNavLabel.textContent = "Programs";
    els.results.replaceChildren();
    els.resultCount.textContent = "Loading opportunities…";
    els.pageStatus.textContent = "";
    [els.statActive, els.statTotal, els.statTracked, els.statScore].forEach((stat) => { stat.textContent = "—"; });
    [els.role, els.region, els.source, els.term].forEach((select) => { select.options.length = 1; });
    discoverTagPicker.reset();
    els.userName.textContent = "";
    els.userChip.hidden = true;
    els.personalizePrompt.hidden = true;
    window.clearTimeout(state.refreshTimer);
    state.refresh = null;
    els.refreshOpen.hidden = true;
    if (els.refreshDialog.open) els.refreshDialog.close();
    clearError();
    announce("");
  }

  // The gate and the detail panel are modal: while either is open nothing
  // behind it may take focus or reach a screen reader. Each holds its own claim
  // so closing one never releases the other.
  const inertClaims = new Set();

  function setBackgroundInert(claim, active) {
    if (active) inertClaims.add(claim);
    else inertClaims.delete(claim);
    const inert = inertClaims.size > 0;
    [els.appShell, els.skipLink].forEach((node) => {
      node.inert = inert;
      if (inert) node.setAttribute("aria-hidden", "true");
      else node.removeAttribute("aria-hidden");
    });
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
    els.authGate.classList.remove("is-visible");
    els.authGate.setAttribute("aria-hidden", "true");
    setBackgroundInert("auth", false);
    els.userName.textContent = session.display_name || "Local user";
    els.userChip.hidden = false;
    els.tokenInput.value = "";
    // Refreshing rewrites every student's data, so only the owner is offered it.
    els.refreshOpen.hidden = state.userId !== "local-user";
    if (!els.refreshOpen.hidden) {
      pollRefresh();
      loadSystemStatus();
    }
    invalidateUrgentBadge();
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

  function relativeWhen(stamp) {
    if (!stamp) return "";
    const moment = new Date(stamp);
    if (Number.isNaN(moment.getTime())) return "";
    return moment.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
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
    body.replaceChildren();
    if (!payload.available) {
      body.appendChild(boardTrackerForm());
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
        if (job.last_run_at) facts.push(`last ran ${relativeWhen(job.last_run_at)}`);
        if (job.next_run_at && job.job !== "autostart") facts.push(`next ${relativeWhen(job.next_run_at)}`);
      }
      item.appendChild(element("p", "profile-help", facts.join(" · ")));
      if (job.job === "daily" && payload.daily) {
        const daily = payload.daily;
        const summary = !daily.ran
          ? "No daily refresh has run on this computer yet."
          : daily.in_progress
            ? `A daily refresh started ${relativeWhen(daily.started_at)} and has not finished.`
            : `Last finished ${relativeWhen(daily.finished_at)}${daily.exit_code ? ` with problems (exit ${daily.exit_code}); see data/run.log` : ""}.`;
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
          facts.push(item.last_success_at ? `last answered ${relativeWhen(item.last_success_at)}` : item.health === "never" ? "added since the last refresh, or never reached" : "never answered");
          row.appendChild(element("p", "profile-help", facts.join(" · ")));
          if (item.last_error) row.appendChild(element("p", "profile-help system-status-error", item.last_error));
          list.appendChild(row);
        });
        details.appendChild(list);
        body.appendChild(details);
      }
    }
    if (payload.can_add_boards) body.appendChild(boardTrackerForm());
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

  function showError(message) {
    els.error.textContent = message;
    els.error.hidden = false;
  }

  function clearError() {
    els.error.textContent = "";
    els.error.hidden = true;
  }

  function showLoadError(error, sequence) {
    // A late failure from a view the user already left must not clobber the new one.
    if (sequence !== state.loadSequence) return;
    // Counts and paging from the previous view would sit beside the error as if they were current.
    els.results.replaceChildren();
    els.results.setAttribute("aria-busy", "false");
    els.resultCount.textContent = "Couldn't load this view";
    els.pageStatus.textContent = "";
    els.paginations.forEach((nav) => { nav.hidden = true; });
    renderSubnav(initialSubnavTabs(state.view));
    showError(error.message);
  }

  function setText(element, value, fallback = "Not provided") {
    element.textContent = value || fallback;
  }

  function formatDate(value) {
    if (!value) return "Not listed";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
    }).format(date);
  }

  // Deadlines and follow-ups are calendar days. Some arrive date-only and some
  // as UTC midnight; reading either as an instant shows the previous day west
  // of UTC, contradicting the posting text beside it.
  function calendarDateParts(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})(?:T00:00(?::00(?:\.0+)?)?(?:Z|[+-]00:00))?$/.exec(String(value || ""));
    return match ? [Number(match[1]), Number(match[2]) - 1, Number(match[3])] : null;
  }

  function calendarDate(value) {
    const parts = calendarDateParts(value);
    return parts ? new Date(...parts) : new Date(value);
  }

  function formatCalendarDate(value) {
    if (!value) return "Not listed";
    const date = calendarDate(value);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }).format(date);
  }

  function deadlineState(value) {
    if (!value) return null;
    const date = calendarDate(value);
    if (Number.isNaN(date.getTime())) return null;
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const target = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    const days = Math.round((target - today) / 86_400_000);
    return days < 0 ? "passed" : days <= SOON_DAYS ? "soon" : "open";
  }

  const NEW_WINDOW_MS = 48 * 60 * 60 * 1000;

  // First seen in the last 48 hours. A timestamp in the future (source clock
  // skew) is never "new".
  function isNewlySeen(value, now = Date.now()) {
    if (!value) return false;
    const seen = new Date(value).getTime();
    if (Number.isNaN(seen)) return false;
    const age = now - seen;
    return age >= 0 && age <= NEW_WINDOW_MS;
  }

  function uniqueLabels(labels) {
    const seen = new Set();
    return labels.filter((label) => {
      const key = String(label).trim().toLocaleLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  // A datetime-local input silently renders empty for anything but
  // YYYY-MM-DDTHH:MM in local time, and an empty input then reads as "clear".
  function toLocalInputValue(value) {
    if (!value) return "";
    const date = calendarDate(value);
    if (Number.isNaN(date.getTime())) return "";
    const pad = (number) => String(number).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function chip(text, tone = "") {
    return element("span", `chip ${tone}`.trim(), text);
  }

  function skeletons() {
    els.results.replaceChildren();
    for (let i = 0; i < 8; i += 1) {
      const card = element("article", "opportunity-card skeleton");
      card.innerHTML = "<div></div><div></div><div></div><div></div>";
      els.results.appendChild(card);
    }
  }

  function baseScoreReason() {
    return state.personalized
      ? "Base score only; verify the source posting."
      : "Base score only; complete your profile to see why roles match.";
  }

  // Keyboard triage needs the record behind a focused card.
  const cardItems = new WeakMap();

  function createOpportunityCard(item) {
    const article = element("article", "opportunity-card");
    cardItems.set(article, item);
    article.setAttribute("role", "listitem");
    article.dataset.opportunityId = item.id;
    const button = element("button", "card-button");
    button.type = "button";
    button.setAttribute("aria-label", `View ${item.title} at ${item.company}`);
    button.addEventListener("click", () => openDetail(item.id));
    let touchStartX = null;
    button.addEventListener("touchstart", (event) => {
      touchStartX = event.changedTouches[0]?.clientX ?? null;
    }, { passive: true });
    button.addEventListener("touchend", (event) => {
      if (touchStartX === null) return;
      const delta = (event.changedTouches[0]?.clientX ?? touchStartX) - touchStartX;
      touchStartX = null;
      if (Math.abs(delta) < 90) return;
      event.preventDefault();
      runIntent(item, delta > 0 ? "saved" : "passed", button);
    });

    const top = element("div", "card-top");
    const identity = element("div", "company-identity");
    const avatar = element("span", "company-avatar", (item.company || "?").slice(0, 1).toUpperCase());
    const names = element("div");
    names.appendChild(element("p", "company-name", item.company));
    names.appendChild(element("p", "source-line", `${item.source_name} · checked ${formatDate(item.last_seen_at)}`));
    identity.append(avatar, names);

    const score = element("div", "fit-score");
    score.appendChild(element("strong", "", String(item.score)));
    score.appendChild(element("span", "", "fit"));
    top.append(identity, score);

    const title = element("h3", "", item.title);
    const description = element(
      "p",
      "card-description",
      item.description || "Open the source posting for the full role description."
    );

    const meta = element("div", "chip-row");
    meta.append(chip(item.region || "Unknown", "is-region"));
    meta.append(chip(item.role_type || "other"));
    if (item.posted_at) meta.append(chip(`Posted ${formatCalendarDate(item.posted_at)}`));
    const deadline = deadlineState(item.deadline_at);
    if (deadline === "passed") meta.append(chip(`Deadline passed · ${formatCalendarDate(item.deadline_at)}`, "is-warning"));
    if (deadline === "soon") meta.append(chip(`Closes ${formatCalendarDate(item.deadline_at)}`, "is-soon"));
    if (item.user_deadline_on) {
      const yours = deadlineState(item.user_deadline_on);
      meta.append(chip(
        `Your deadline ${formatCalendarDate(item.user_deadline_on)}`,
        yours === "passed" ? "is-warning" : yours === "soon" ? "is-soon" : ""
      ));
    }
    if (isNewlySeen(item.first_seen_at)) {
      const fresh = chip("New", "is-new");
      fresh.title = "First seen in the last 48 hours";
      meta.prepend(fresh);
    }

    const reason = element("p", "top-reason");
    reason.appendChild(element("span", "reason-mark", "↳"));
    reason.appendChild(document.createTextNode(item.reasons?.[0] || baseScoreReason()));

    button.append(top, title, description, meta, reason);

    const actions = element("div", "card-actions");
    const save = element(
      "button",
      `card-action ${item.intent_state === "saved" ? "is-active" : ""}`.trim(),
      item.intent_state === "saved" ? "Saved ✓" : "Save"
    );
    save.type = "button";
    save.addEventListener("click", () => runIntent(item, item.intent_state === "saved" ? "undo" : "saved", save));

    const pass = element(
      "button",
      `card-action ${item.intent_state === "passed" ? "is-active is-pass" : ""}`.trim(),
      item.intent_state === "passed" ? "Passed · Undo" : "Pass"
    );
    pass.type = "button";
    pass.addEventListener("click", () => runIntent(item, item.intent_state === "passed" ? "undo" : "passed", pass));

    const apply = element("a", "card-action is-apply", "Apply ↗");
    apply.href = item.url;
    apply.target = "_blank";
    apply.rel = "noopener noreferrer";
    apply.addEventListener("click", (event) => {
      event.preventDefault();
      if (!window.confirm(`Open the employer application for ${item.title} at ${item.company}? Nothing will be submitted automatically.`)) return;
      const path = `/api/v1/opportunities/${encodeURIComponent(item.id)}/actions`;
      const key = requestKey();
      fetch(path, {
        method: "POST",
        credentials: "same-origin",
        keepalive: true,
        headers: { "Content-Type": "application/json", "Idempotency-Key": key, ...csrfHeaders("POST") },
        body: JSON.stringify({ action: "apply_opened" }),
      }).then(async (response) => {
        if (response.ok) return;
        let detail = `Request failed (${response.status})`;
        try {
          detail = (await response.json()).detail || detail;
        } catch (_) {
          // The HTTP status remains a useful fallback.
        }
        showError(`The employer page opened, but the tracker could not record it: ${detail}`);
      }, () => {
        queueAction({ path, action: "apply_opened", key });
        showError("The employer page opened, but tracker sync is queued until the connection recovers.");
      });
      window.open(item.url, "_blank", "noopener,noreferrer");
    });

    actions.append(save, pass, apply);
    // Outside the card button: a button may not contain other controls.
    article.append(button, createTagList(item), actions);
    return article;
  }

  async function runIntent(item, action, control) {
    control.disabled = true;
    clearError();
    const path = `/api/v1/opportunities/${encodeURIComponent(item.id)}/actions`;
    const key = requestKey();
    const cards = [...els.results.querySelectorAll(".opportunity-card")];
    const position = cards.findIndex((card) => card.dataset.opportunityId === item.id);
    const controlIndex = control.parentElement ? [...control.parentElement.children].indexOf(control) : -1;
    try {
      await api(path, {
        method: "POST",
        headers: { "Idempotency-Key": key },
        body: JSON.stringify({ action }),
      });
      await loadCurrentView();
      await loadStats();
      restoreFocusAfterRender(item.id, position, controlIndex);
      announce({
        saved: `Saved ${item.title}.`,
        passed: `Passed on ${item.title}.`,
        undo: `Undid your last choice on ${item.title}.`,
      }[action] || "");
    } catch (error) {
      if (error.network) {
        queueAction({ path, action, key });
        showError("Connection lost. Your action is queued and will retry after reconnection.");
      } else if (error.message !== "Authentication required") {
        showError(error.message);
      }
    } finally {
      control.disabled = false;
    }
  }

  // A re-render replaces the control that had focus. Put focus back on the same
  // control of the same card, or on the card that took its place, never body.
  function restoreFocusAfterRender(opportunityId, position, controlIndex) {
    if (state.selectedId) return;
    const card = els.results.querySelector(`[data-opportunity-id="${CSS.escape(opportunityId)}"]`);
    const actions = card?.querySelector(".card-actions");
    const sameControl = actions && controlIndex >= 0 ? actions.children[controlIndex] : null;
    const cards = els.results.querySelectorAll(".opportunity-card");
    const replacement = cards[Math.min(Math.max(position, 0), cards.length - 1)]?.querySelector(".card-button");
    const target = sameControl || card?.querySelector(".card-button") || replacement || els.results;
    if (target === els.results) els.results.setAttribute("tabindex", "-1");
    target.focus();
  }

  function renderResults(payload) {
    state.total = payload.total;
    state.personalized = payload.personalized !== false;
    els.personalizePrompt.hidden = state.personalized || state.view !== "discover";
    els.results.replaceChildren();
    els.results.classList.toggle("is-list", state.displayMode === "list");
    els.results.setAttribute("role", "list");
    if (!payload.items.length) {
      const empty = element("div", "empty-state");
      empty.appendChild(element(
        "strong",
        "",
        state.view === "saved" ? "No saved opportunities" : "No opportunities left to review"
      ));
      empty.appendChild(element(
        "p",
        "",
        state.view === "saved"
          ? "Save a role from Discover to build your shortlist."
          : "New matches will appear here. Clear a filter if you expected more."
      ));
      els.results.appendChild(empty);
    } else {
      payload.items.forEach((item) => els.results.appendChild(createOpportunityCard(item)));
    }
    const start = payload.total ? payload.offset + 1 : 0;
    const end = Math.min(payload.offset + payload.items.length, payload.total);
    els.resultCount.textContent = state.view === "saved"
      ? plural(payload.total, "saved role", "saved roles")
      : `${payload.total.toLocaleString()} to review`;
    els.pageStatus.textContent = payload.total ? `Showing ${start}–${end}` : "No results";
    const page = Math.floor(payload.offset / PAGE_SIZE) + 1;
    const pages = Math.max(1, Math.ceil(payload.total / PAGE_SIZE));
    els.paginations.forEach((nav) => {
      const input = nav.querySelector("[data-page-jump] input");
      nav.querySelector("[data-page-label]").textContent = `Page ${page} of ${pages}`;
      input.max = String(pages);
      input.value = String(page);
      nav.querySelector("[data-page-previous]").disabled = payload.offset === 0;
      nav.querySelector("[data-page-next]").disabled = payload.offset + PAGE_SIZE >= payload.total;
      nav.hidden = state.view !== "discover" && state.view !== "saved" || payload.total <= PAGE_SIZE;
    });
    renderSubnav(deckTabs(payload.total));
    els.results.setAttribute("aria-busy", "false");
  }

  function localIsoDate(offsetDays = 0) {
    const day = new Date();
    day.setDate(day.getDate() + offsetDays);
    return `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
  }

  // Quick views on Discover and Saved; each narrows the filters already set.
  const DECK_TABS = [
    { id: "all" },
    { id: "new", label: "Posted this week", group: "Quick views", apply: (params) => { if (!els.posted.value) params.set("posted_since", localIsoDate(-7)); } },
    {
      id: "closing", label: `Closing within ${SOON_DAYS} days`, group: "Quick views",
      apply: (params) => { if (!els.deadline.value) params.set("deadline_before", localIsoDate(SOON_DAYS)); params.set("sort", "deadline"); },
    },
    { id: "remote", label: "Remote", group: "Work mode", apply: (params) => params.set("remote_mode", "remote") },
    { id: "hybrid", label: "Hybrid", group: "Work mode", apply: (params) => params.set("remote_mode", "hybrid") },
    { id: "onsite", label: "On-site", group: "Work mode", apply: (params) => params.set("remote_mode", "onsite") },
  ];

  function deckTabs(total = null) {
    const active = state.subtabs[state.view];
    return DECK_TABS.map((tab) => ({
      ...tab,
      label: tab.id === "all" ? (state.view === "saved" ? "All saved" : "All matches") : tab.label,
      count: tab.id === active ? total : null,
    }));
  }

  function listParams() {
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(state.offset),
      sort: els.sort.value,
    });
    if (els.search.value.trim()) params.set("q", els.search.value.trim());
    if (discoverTagPicker.value) params.set("tag", discoverTagPicker.value);
    if (els.role.value) params.set("role_type", els.role.value);
    if (els.region.value) params.set("region", els.region.value);
    if (els.source.value) params.set("source", els.source.value);
    if (els.term.value) params.set("term", els.term.value);
    if (els.remote.value) params.set("remote_mode", els.remote.value);
    if (els.year.value) params.set("graduation_year", els.year.value);
    if (els.pay.value) params.set("min_hourly_pay", els.pay.value);
    if (els.posted.value) params.set("posted_since", els.posted.value);
    if (els.deadline.value) params.set("deadline_before", els.deadline.value);
    params.set("intent_state", state.view === "saved" ? "saved" : "undecided");
    DECK_TABS.find((tab) => tab.id === state.subtabs[state.view])?.apply?.(params);
    return params;
  }

  async function loadOpportunities() {
    const sequence = ++state.loadSequence;
    state.loading = true;
    clearError();
    els.results.setAttribute("aria-busy", "true");
    skeletons();
    try {
      const payload = await api(`/api/v1/opportunities?${listParams().toString()}`);
      if (sequence !== state.loadSequence || !["discover", "saved"].includes(state.view)) return;
      renderResults(payload);
    } catch (error) {
      showLoadError(error, sequence);
    } finally {
      state.loading = false;
    }
  }

  function populateSelect(select, values) {
    // Facets load on every sign-in; keep only the "All …" placeholder first.
    const current = select.value;
    select.options.length = 1;
    values.forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      select.appendChild(option);
    });
    if (values.includes(current)) select.value = current;
  }

  async function loadStats() {
    const stats = await api("/api/v1/stats");
    els.statActive.textContent = stats.active_unique.toLocaleString();
    els.statTotal.textContent = stats.total.toLocaleString();
    els.statTracked.textContent = (stats.applications ?? stats.tracked).toLocaleString();
    els.statScore.textContent = stats.top_score ? `${stats.top_score}/100` : "—";
  }

  async function loadFacetsAndStats() {
    const [facets] = await Promise.all([api("/api/v1/facets"), loadStats()]);
    populateSelect(els.role, facets.role_types || []);
    populateSelect(els.region, facets.regions || []);
    populateSelect(els.source, facets.sources || []);
    populateSelect(els.term, facets.terms || []);
    populateTagFilter(facets.tags || []);
  }

  // ---- Company tags ------------------------------------------------------
  // One-word industry tags per company. Automatic tags are inferred from the
  // company's postings, look different (dashed), and carry their evidence;
  // x removes a tag for this student only, and it stays removed on refresh.

  // The tag filter inside a search box: a trigger that names the active tag,
  // a separate x that clears it, and a panel of tag chips, most common first,
  // with a field to find one. Native <select> lists were an unsorted wall of
  // text, and unreadable in dark mode on Windows.
  function createTagPicker({ label, onChange }) {
    let tags = [];
    let value = "";
    let open = false;

    const root = element("div", "tag-picker");
    const trigger = element("button", "tag-picker-trigger");
    trigger.type = "button";
    trigger.setAttribute("aria-haspopup", "true");
    trigger.setAttribute("aria-expanded", "false");
    const clear = element("button", "tag-picker-clear", "×");
    clear.type = "button";
    const panel = element("div", "tag-picker-panel");
    panel.setAttribute("role", "group");
    panel.setAttribute("aria-label", label);
    panel.hidden = true;
    const find = document.createElement("input");
    find.type = "search";
    find.className = "tag-picker-find";
    find.placeholder = "Find a tag";
    find.autocomplete = "off";
    find.setAttribute("aria-label", "Find a tag");
    const options = element("div", "tag-picker-options");
    const empty = element("p", "tag-picker-empty", "No tags match.");
    const note = element("p", "tag-picker-note", "Sorted by how many companies carry each tag.");
    panel.append(find, options, empty, note);
    root.append(trigger, clear, panel);

    const counted = () => {
      const list = [...tags];
      // A tag being filtered on stays visible even when no company has it now.
      if (value && !list.some((entry) => entry.tag === value)) list.push({ tag: value, companies: 0 });
      return list.sort((a, b) => b.companies - a.companies || a.tag.localeCompare(b.tag));
    };

    function paintTrigger() {
      const active = counted().find((entry) => entry.tag === value);
      trigger.replaceChildren();
      trigger.classList.toggle("is-active", Boolean(value));
      if (value) {
        trigger.append(element("span", "tag-picker-hash", "#"), element("span", "tag-picker-label", value));
        trigger.append(element("span", "tag-picker-count", String(active?.companies ?? 0)));
        trigger.setAttribute("aria-label", `${label}: ${value}, ${plural(active?.companies ?? 0, "company", "companies")}. Change tag`);
      } else {
        trigger.append(element("span", "tag-picker-hash", "#"), element("span", "tag-picker-label", "Tags"));
        if (tags.length) trigger.append(element("span", "tag-picker-count", String(tags.length)));
        trigger.setAttribute("aria-label", `${label}: all companies. ${plural(tags.length, "tag", "tags")} to choose from`);
      }
      trigger.append(element("span", "tag-picker-caret", "▾"));
      trigger.querySelector(".tag-picker-caret").setAttribute("aria-hidden", "true");
      trigger.querySelector(".tag-picker-hash").setAttribute("aria-hidden", "true");
      clear.hidden = !value;
      clear.setAttribute("aria-label", value ? `Clear tag filter ${value}` : "Clear tag filter");
      trigger.disabled = !tags.length && !value;
    }

    function paintOptions() {
      const query = find.value.trim().replace(/^#/, "").toLowerCase();
      const shown = counted().filter((entry) => entry.tag.includes(query));
      options.replaceChildren(...shown.map((entry) => {
        const option = element("button", `tag-option${entry.tag === value ? " is-selected" : ""}`);
        option.type = "button";
        option.dataset.tag = entry.tag;
        option.setAttribute("aria-pressed", String(entry.tag === value));
        option.setAttribute("aria-label", `${entry.tag}, ${plural(entry.companies, "company", "companies")}`);
        option.append(element("span", "tag-option-name", `#${entry.tag}`), element("span", "tag-option-count", String(entry.companies)));
        option.addEventListener("click", () => choose(entry.tag === value ? "" : entry.tag));
        return option;
      }));
      empty.hidden = shown.length > 0;
    }

    function onOutside(event) {
      if (!root.contains(event.target)) setOpen(false);
    }

    function setOpen(next, { focusTrigger = false } = {}) {
      if (next === open) return;
      open = next;
      panel.hidden = !open;
      trigger.setAttribute("aria-expanded", String(open));
      root.classList.toggle("is-open", open);
      if (open) {
        find.value = "";
        paintOptions();
        document.addEventListener("pointerdown", onOutside, true);
        (options.querySelector(".is-selected") || find).focus();
      } else {
        document.removeEventListener("pointerdown", onOutside, true);
        if (focusTrigger) trigger.focus();
      }
    }

    function choose(tag) {
      setOpen(false, { focusTrigger: true });
      if (tag === value) return;
      value = tag;
      paintTrigger();
      onChange(value);
    }

    trigger.addEventListener("click", () => setOpen(!open));
    clear.addEventListener("click", () => {
      value = "";
      paintTrigger();
      trigger.focus();
      onChange(value);
    });
    find.addEventListener("input", paintOptions);
    root.addEventListener("keydown", (event) => {
      if (!open) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        setOpen(false, { focusTrigger: true });
        return;
      }
      if (event.key === "Enter" && event.target === find) {
        event.preventDefault();
        const first = options.querySelector(".tag-option");
        if (first) choose(first.dataset.tag === value ? value : first.dataset.tag);
        return;
      }
      if (!["ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
      const buttons = [...options.querySelectorAll(".tag-option")];
      if (!buttons.length) return;
      const index = buttons.indexOf(document.activeElement);
      if (index < 0 && event.target !== find) return;
      event.preventDefault();
      const step = event.key === "ArrowDown" || event.key === "ArrowRight" ? 1 : -1;
      const next = event.key === "Home" ? 0
        : event.key === "End" ? buttons.length - 1
          : index < 0 ? 0 : Math.min(buttons.length - 1, Math.max(0, index + step));
      buttons[next].focus();
    });
    root.addEventListener("focusout", (event) => {
      if (open && event.relatedTarget && !root.contains(event.relatedTarget)) setOpen(false);
    });

    paintTrigger();
    return {
      root,
      trigger,
      get value() { return value; },
      setTags(next) {
        tags = next || [];
        paintTrigger();
        if (open) paintOptions();
      },
      setValue(next) {
        value = next || "";
        paintTrigger();
        if (open) paintOptions();
      },
      reset() {
        setOpen(false);
        tags = [];
        value = "";
        paintTrigger();
      },
    };
  }

  function populateTagFilter(tags) {
    discoverTagPicker.setTags(tags);
  }

  const discoverTagPicker = createTagPicker({
    label: "Filter by company tag",
    onChange: () => {
      state.offset = 0;
      loadCurrentView();
    },
  });

  async function loadTagFacets() {
    try {
      const facets = await api("/api/v1/facets");
      populateTagFilter(facets.tags || []);
    } catch (_) {
      // The counts refresh on the next sign-in; the tag change itself stuck.
    }
  }

  function applyTagFilter(tag) {
    if (state.view === "outreach") {
      state.outreachTag = tag;
      loadOutreach().then(() => els.results.querySelector(".outreach-search .tag-picker-trigger")?.focus());
      announce(`Showing outreach companies tagged ${tag}.`);
      return;
    }
    discoverTagPicker.setValue(tag);
    if (state.selectedId) closeDetail();
    if (!["discover", "saved"].includes(state.view)) setView("discover");
    state.offset = 0;
    loadCurrentView();
    announce(`Showing companies tagged ${tag}.`);
  }

  function createTagList(item) {
    const list = element("ul", "tag-list");
    list.setAttribute("aria-label", `Tags for ${item.company}`);
    list.hidden = !item.tags?.length;
    (item.tags || []).forEach((tag) => {
      const inferred = tag.origin === "auto";
      const entry = element("li", `tag-chip ${inferred ? "is-auto" : "is-manual"}`);
      entry.title = inferred ? `${tag.evidence} Inferred, not stated by the company.` : tag.evidence;
      const name = element("button", "tag-name", `#${tag.tag}`);
      name.type = "button";
      name.dataset.tag = tag.tag;
      name.setAttribute("aria-label", `Show only companies tagged ${tag.tag}${inferred ? " (inferred tag)" : ""}`);
      name.addEventListener("click", () => applyTagFilter(tag.tag));
      const remove = element("button", "tag-remove", "×");
      remove.type = "button";
      remove.setAttribute("aria-label", `Remove tag ${tag.tag} from ${item.company}`);
      remove.addEventListener("click", () => changeCompanyTag(item, tag.tag, false));
      entry.append(name, remove);
      list.appendChild(entry);
    });
    return list;
  }

  // Swap every rendered tag list for this company, keeping keyboard focus on a
  // neighbouring tag (or the card) when the focused chip is the one removed.
  function applyCompanyTags(companyKey, tags) {
    const focusedList = document.activeElement?.closest?.(".tag-list");
    const focusedIndex = focusedList
      ? [...focusedList.querySelectorAll(".tag-name, .tag-remove")].indexOf(document.activeElement)
      : -1;
    let refocus = null;
    const replace = (oldList, item) => {
      item.tags = tags;
      const next = createTagList(item);
      if (oldList === focusedList) {
        const controls = [...next.querySelectorAll(".tag-name, .tag-remove")];
        refocus = controls[Math.min(focusedIndex, controls.length - 1)]
          || oldList.closest(".opportunity-card")?.querySelector(".card-button")
          || oldList.closest(".company-tags")?.querySelector("input")
          || null;
      }
      oldList.replaceWith(next);
    };
    els.results.querySelectorAll(".opportunity-card").forEach((card) => {
      const item = cardItems.get(card);
      const list = card.querySelector(".tag-list");
      if (item && list && item.company_sort_key === companyKey) replace(list, item);
    });
    const detailList = els.detailContent.querySelector(".company-tags .tag-list");
    if (detailList && state.detailItem?.company_sort_key === companyKey) replace(detailList, state.detailItem);
    refocus?.focus();
  }

  function announceWithUndo(message, undo) {
    announce(message);
    const button = element("button", "text-button status-undo", "Undo");
    button.type = "button";
    button.addEventListener("click", () => {
      announce("");
      undo();
    }, { once: true });
    els.actionStatus.append(" ", button);
  }

  async function changeCompanyTag(item, tag, present, { quiet = false } = {}) {
    clearError();
    try {
      const result = await api("/api/v1/company-tags", {
        method: "PUT",
        body: JSON.stringify({ company: item.company, tag, present }),
      });
      if (state.view === "outreach") {
        // The outreach list and its tag counts are rebuilt from the server.
        await loadOutreach();
        const pane = els.results.querySelector(".outreach-pane");
        if (!document.activeElement || document.activeElement === document.body) {
          (pane?.querySelector(`.tag-name[data-tag="${CSS.escape(tag)}"]`) || pane?.querySelector(".company-tags input"))?.focus();
        }
      } else {
        applyCompanyTags(result.company_key, result.tags);
      }
      loadTagFacets();
      if (quiet && (!document.activeElement || document.activeElement === document.body)) {
        // Undo removed its own button; land on the tag it restored, or the card.
        const card = [...els.results.querySelectorAll(".opportunity-card")]
          .find((node) => cardItems.get(node)?.company_sort_key === result.company_key);
        (card?.querySelector(`.tag-name[data-tag="${CSS.escape(tag)}"]`) || card?.querySelector(".card-button"))?.focus();
      }
      // Removing the tag being filtered on (or adding it back) changes which
      // cards belong in the deck.
      if (discoverTagPicker.value === tag && ["discover", "saved"].includes(state.view)) {
        await loadCurrentView();
      }
      if (!quiet) {
        announceWithUndo(
          present ? `Added tag ${tag} to ${item.company}.` : `Removed tag ${tag} from ${item.company}.`,
          () => changeCompanyTag(item, tag, !present, { quiet: true })
        );
      }
      return true;
    } catch (error) {
      if (error.message !== "Authentication required") showError(error.message);
      return false;
    }
  }

  function companyTagsSection(item, { outreach = false } = {}) {
    const section = element("section", `detail-section company-tags${outreach ? " is-outreach" : ""}`);
    section.appendChild(element("p", "eyebrow", "Company tags"));
    section.appendChild(element(
      "p",
      "tag-help",
      outreach
        ? "Dashed tags are inferred from your research summary; hover one to see the words behind it. × removes one here and on Discover."
        : "Dashed tags are inferred from this company's postings; hover one to see the words behind it. Tags apply to every role at this company, and × removes one for good."
    ));
    section.appendChild(createTagList(item));
    const form = element("form", "tag-add-form");
    const label = element("label", "profile-field");
    label.appendChild(element("span", "", "Add a tag"));
    const input = document.createElement("input");
    input.type = "text";
    input.maxLength = 25;
    input.placeholder = "One word, e.g. robotics";
    input.pattern = "#?[A-Za-z0-9][A-Za-z0-9\\-]{0,23}";
    input.title = "One word: letters, numbers, or hyphens.";
    input.autocomplete = "off";
    label.appendChild(input);
    const add = element("button", "secondary-button", "Add tag");
    add.type = "submit";
    form.append(label, add);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const tag = input.value.trim().replace(/^#/, "").toLowerCase();
      if (!tag) return;
      add.disabled = true;
      if (await changeCompanyTag(item, tag, true)) input.value = "";
      add.disabled = false;
      input.focus();
    });
    section.appendChild(form);
    return section;
  }

  function createApplicationCard(item, { board = false } = {}) {
    const card = element("article", "application-card");
    card.dataset.applicationId = item.id;
    if (board) {
      card.draggable = true;
      card.addEventListener("dragstart", (event) => {
        event.dataTransfer.setData("text/plain", item.id);
        event.dataTransfer.effectAllowed = "move";
      });
    }
    const heading = element("div", "application-heading");
    const identity = element("div");
    identity.appendChild(element("p", "company-name", item.company));
    identity.appendChild(element("h3", "", item.title));
    heading.appendChild(identity);
    heading.appendChild(chip(`${item.score} fit`, "is-region"));

    const facts = element("div", "application-facts");
    uniqueLabels([item.region || "Unknown", item.location || "Location unknown"]).forEach((label) => facts.appendChild(chip(label)));
    if (item.follow_up_at) {
      const followUpDay = formatCalendarDate(item.follow_up_on || item.follow_up_at);
      facts.appendChild(item.follow_up_overdue
        ? chip(`Overdue follow-up · ${followUpDay}`, "is-warning")
        : chip(`Follow up ${followUpDay}`, "is-region"));
    }

    const controls = element("div", "application-controls");
    const label = element("label", "");
    label.appendChild(element("span", "", "Stage"));
    const select = document.createElement("select");
    select.dataset.savedStage = item.stage;
    ["applying", "applied", "interview", "offer", "rejected", "withdrawn", "archived"].forEach((stage) => {
      const option = document.createElement("option");
      option.value = stage;
      option.textContent = stage[0].toUpperCase() + stage.slice(1);
      option.selected = item.stage === stage;
      select.appendChild(option);
    });
    // Every stage change is audited (and "applied" stamps a date), so keyboard
    // browsing through the list must not save each stage it passes.
    autoSaveSelect(select, {
      saved: () => item.stage,
      commit: async (stage, trigger) => {
        select.disabled = true;
        let moved = false;
        try {
          await api(`/api/v1/applications/${encodeURIComponent(item.id)}`, {
            method: "PATCH",
            body: JSON.stringify({ stage }),
          });
          moved = true;
        } catch (error) {
          select.value = item.stage;
          showError(error.message);
        } finally {
          select.disabled = false;
        }
        if (!moved) return;
        try {
          await Promise.all([loadApplications(), loadStats()]);
        } catch (error) {
          showError(error.message);
        }
        // The board re-renders into a new column; keep keyboard users on this
        // card, unless they already left the select for somewhere else.
        if (trigger !== "blur") els.results.querySelector(`[data-application-id="${CSS.escape(item.id)}"] select`)?.focus();
        announce(`Moved ${item.title} to ${stage}.`);
      },
    });
    label.appendChild(select);
    const source = element("a", "application-link", "Open posting ↗");
    source.href = item.url;
    source.target = "_blank";
    source.rel = "noopener noreferrer";
    controls.append(label, source);
    const trackerFields = element("form", "tracker-fields");
    const notesLabel = element("label", "");
    notesLabel.appendChild(element("span", "", "Notes"));
    const notes = document.createElement("textarea");
    notes.value = item.notes || "";
    notes.placeholder = "Add private notes";
    notesLabel.appendChild(notes);
    const followLabel = element("label", "");
    followLabel.appendChild(element("span", "", "Follow up"));
    const followUp = document.createElement("input");
    followUp.type = "datetime-local";
    followUp.value = toLocalInputValue(item.follow_up_at);
    const initialFollowUp = followUp.value;
    followLabel.appendChild(followUp);
    const saveTracker = element("button", "secondary-button", "Save details");
    saveTracker.type = "submit";
    const trackerStatus = element("p", "form-status");
    trackerStatus.setAttribute("aria-live", "polite");
    if (state.trackerStatus?.id === item.id) {
      trackerStatus.textContent = state.trackerStatus.message;
      state.trackerStatus = null;
    }
    trackerFields.append(notesLabel, followLabel, saveTracker, trackerStatus);
    trackerFields.addEventListener("submit", async (event) => {
      event.preventDefault();
      saveTracker.disabled = true;
      const body = { notes: notes.value };
      // Omitting follow_up_at means "unchanged" server-side; sending it only on
      // a real edit keeps an untouched save from clearing the reminder.
      if (followUp.value !== initialFollowUp) {
        body.follow_up_at = followUp.value || "";
        body.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
      }
      try {
        await api(`/api/v1/applications/${encodeURIComponent(item.id)}`, {
          method: "PATCH",
          body: JSON.stringify(body),
        });
        state.trackerStatus = { id: item.id, message: "Saved." };
        await Promise.all([loadApplications(), loadStats()]);
      } catch (error) {
        trackerStatus.textContent = error.message;
      } finally {
        saveTracker.disabled = false;
      }
    });

    const detail = element("details", "tracker-detail");
    detail.appendChild(element("summary", "", "Tasks, contacts, and timeline"));
    const detailBody = element("div", "tracker-detail-body");
    detail.appendChild(detailBody);
    detail.addEventListener("toggle", () => {
      if (detail.open && !detail.dataset.loaded) loadApplicationDetail(item.id, detailBody, detail);
    });
    card.append(heading, facts, controls, trackerFields, detail);
    return card;
  }

  async function loadApplicationDetail(applicationId, container, owner) {
    container.replaceChildren(element("p", "detail-loading", "Loading tracker history…"));
    try {
      const payload = await api(`/api/v1/applications/${encodeURIComponent(applicationId)}`);
      container.replaceChildren();

      const tasks = element("section", "tracker-subsection");
      tasks.appendChild(element("h4", "", "Tasks"));
      const taskList = element("div", "tracker-item-list");
      (payload.tasks || []).forEach((task) => {
        const label = element("label", "tracker-check");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = task.status === "done";
        checkbox.addEventListener("change", async () => {
          checkbox.disabled = true;
          try {
            await api(`/api/v1/application-tasks/${encodeURIComponent(task.id)}`, {
              method: "PATCH",
              body: JSON.stringify({ status: checkbox.checked ? "done" : "open" }),
            });
          } catch (error) {
            checkbox.checked = !checkbox.checked;
            showError(error.message);
          } finally {
            checkbox.disabled = false;
          }
        });
        const taskCopy = element("span", "", task.title);
        if (task.due_at) taskCopy.appendChild(element("small", "", `Due ${formatDate(task.due_at)}`));
        label.append(checkbox, taskCopy);
        taskList.appendChild(label);
      });
      if (!payload.tasks?.length) taskList.appendChild(element("p", "empty-inline", "No tasks yet."));
      const taskForm = element("form", "inline-create-form");
      const taskTitle = document.createElement("input");
      taskTitle.placeholder = "New task";
      taskTitle.required = true;
      taskTitle.setAttribute("aria-label", "New task");
      const taskDue = document.createElement("input");
      taskDue.type = "datetime-local";
      taskDue.setAttribute("aria-label", "Task due date");
      const addTask = element("button", "secondary-button", "Add task");
      addTask.type = "submit";
      taskForm.append(taskTitle, taskDue, addTask);
      taskForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        addTask.disabled = true;
        try {
          await api(`/api/v1/applications/${encodeURIComponent(applicationId)}/tasks`, {
            method: "POST",
            body: JSON.stringify({
              title: taskTitle.value,
              due_at: taskDue.value || null,
              timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || null,
            }),
          });
          owner.dataset.loaded = "";
          await loadApplicationDetail(applicationId, container, owner);
        } catch (error) {
          showError(error.message);
          addTask.disabled = false;
        }
      });
      tasks.append(taskList, taskForm);

      const contacts = element("section", "tracker-subsection");
      contacts.appendChild(element("h4", "", "Contacts"));
      const contactList = element("div", "tracker-item-list");
      (payload.contacts || []).forEach((contact) => {
        const row = element("p", "tracker-contact", contact.name);
        row.appendChild(element("span", "", [contact.role, contact.email, contact.phone].filter(Boolean).join(" · ")));
        contactList.appendChild(row);
      });
      if (!payload.contacts?.length) contactList.appendChild(element("p", "empty-inline", "No contacts yet."));
      const contactForm = element("form", "inline-create-form is-contact");
      const contactName = document.createElement("input");
      contactName.placeholder = "Contact name";
      contactName.required = true;
      const contactRole = document.createElement("input");
      contactRole.placeholder = "Role";
      const contactEmail = document.createElement("input");
      contactEmail.type = "email";
      contactEmail.placeholder = "Email";
      const addContact = element("button", "secondary-button", "Add contact");
      addContact.type = "submit";
      contactForm.append(contactName, contactRole, contactEmail, addContact);
      contactForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        addContact.disabled = true;
        try {
          await api(`/api/v1/applications/${encodeURIComponent(applicationId)}/contacts`, {
            method: "POST",
            body: JSON.stringify({ name: contactName.value, role: contactRole.value, email: contactEmail.value }),
          });
          owner.dataset.loaded = "";
          await loadApplicationDetail(applicationId, container, owner);
        } catch (error) {
          showError(error.message);
          addContact.disabled = false;
        }
      });
      contacts.append(contactList, contactForm);

      const timeline = element("section", "tracker-subsection");
      timeline.appendChild(element("h4", "", "Activity timeline"));
      const eventList = element("ol", "timeline-list");
      (payload.events || []).forEach((event) => {
        const row = element("li", "");
        row.appendChild(element("strong", "", event.event_type.replaceAll("_", " ")));
        row.appendChild(element("span", "", formatDate(event.created_at)));
        if (event.from_stage || event.to_stage) row.appendChild(element("p", "", `${event.from_stage || "start"} → ${event.to_stage || "unchanged"}`));
        eventList.appendChild(row);
      });
      timeline.appendChild(eventList);
      container.append(tasks, contacts, timeline);
      owner.dataset.loaded = "true";
    } catch (error) {
      container.replaceChildren(element("p", "form-error", error.message));
    }
  }

  function openCaptureDialog() {
    let dialog = document.getElementById("capture-dialog");
    if (!dialog) {
      dialog = element("dialog", "capture-dialog");
      dialog.id = "capture-dialog";
      const close = element("button", "icon-button capture-close", "Ã—");
      close.type = "button";
      close.setAttribute("aria-label", "Close manual capture");
      close.addEventListener("click", () => dialog.close());
      const heading = element("div", "");
      heading.appendChild(element("p", "eyebrow", "Manual capture"));
      heading.appendChild(element("h2", "", "Add a role from elsewhere"));
      heading.appendChild(element("p", "profile-help", "Paste a public job URL or upload a PDF/PNG/JPEG. Nothing enters your tracker until you review and confirm it."));
      const draftHost = element("div", "capture-draft-host");
      const sourceForm = element("form", "capture-source-form");
      const url = document.createElement("input");
      url.type = "url";
      url.placeholder = "https://company.example/jobs/role";
      url.setAttribute("aria-label", "Public job URL");
      const file = document.createElement("input");
      file.type = "file";
      file.accept = ".pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg";
      const fileLabel = element("label", "file-input-label", "Job PDF or screenshot");
      fileLabel.appendChild(file);
      const extract = element("button", "primary-button", "Create review draft");
      extract.type = "submit";
      const sourceStatus = element("p", "form-status");
      sourceStatus.setAttribute("aria-live", "polite");
      sourceForm.append(url, fileLabel, extract, sourceStatus);
      sourceForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!url.value.trim() && !file.files.length) {
          sourceStatus.textContent = "Enter a URL or choose a file.";
          return;
        }
        extract.disabled = true;
        sourceStatus.textContent = "Creating a private draft…";
        try {
          let draft;
          if (file.files.length) {
            const body = new FormData();
            body.append("capture", file.files[0]);
            draft = await api("/api/v1/opportunity-captures/file", { method: "POST", body });
          } else {
            draft = await api("/api/v1/opportunity-captures/url", {
              method: "POST",
              body: JSON.stringify({ url: url.value.trim() }),
            });
          }
          renderCaptureDraft(draft, draftHost, dialog);
          sourceForm.hidden = true;
        } catch (error) {
          sourceStatus.textContent = error.message;
          extract.disabled = false;
        }
      });
      dialog.append(close, heading, sourceForm, draftHost);
      document.body.appendChild(dialog);
    }
    const oldHost = dialog.querySelector(".capture-draft-host");
    oldHost.replaceChildren();
    const sourceForm = dialog.querySelector(".capture-source-form");
    sourceForm.hidden = false;
    sourceForm.reset();
    sourceForm.querySelector("button[type=submit]").disabled = false;
    sourceForm.querySelector(".form-status").textContent = "";
    dialog.showModal();
  }

  function renderCaptureDraft(draft, host, dialog) {
    const parsed = draft.parsed || {};
    const form = element("form", "capture-confirm-form");
    form.appendChild(element("p", "missing-note", parsed.extraction_note || "Review every extracted field before confirmation."));
    const company = profileField(form, "Company", "company", parsed.company);
    const title = profileField(form, "Title", "title", parsed.title);
    const url = profileField(form, "Source URL", "url", parsed.url || draft.source_url, { type: "url" });
    const location = profileField(form, "Location", "location", parsed.location);
    const roleType = profileField(form, "Role type", "role_type", "internship");
    const description = profileField(form, "Posting text", "description", parsed.description || draft.extracted_text, { multiline: true });
    company.required = true;
    title.required = true;
    url.required = true;
    const confirm = element("button", "primary-button", "Confirm and add to tracker");
    confirm.type = "submit";
    const statusLine = element("p", "form-status");
    statusLine.setAttribute("aria-live", "polite");
    form.append(confirm, statusLine);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      confirm.disabled = true;
      try {
        await api(`/api/v1/opportunity-captures/${encodeURIComponent(draft.id)}/confirm`, {
          method: "POST",
          body: JSON.stringify({
            company: company.value,
            title: title.value,
            url: url.value,
            location: location.value,
            role_type: roleType.value,
            description: description.value,
          }),
        });
        dialog.close();
        await Promise.all([loadApplications(), loadStats()]);
      } catch (error) {
        statusLine.textContent = error.message;
        confirm.disabled = false;
      }
    });
    host.replaceChildren(form);
  }

  // Arriving from Urgent: bring the application the row was about into view.
  function focusRequestedApplication() {
    const id = state.applicationFocus;
    state.applicationFocus = null;
    if (!id) return;
    const card = els.results.querySelector(`.application-card[data-application-id="${CSS.escape(id)}"]`);
    if (!card) return;
    card.setAttribute("tabindex", "-1");
    card.classList.add("is-requested");
    card.scrollIntoView({ block: "center" });
    card.focus({ preventScroll: true });
  }

  const APPLICATION_STAGES = ["applying", "applied", "interview", "offer", "rejected", "withdrawn", "archived"];
  const APPLICATION_TABS = [
    { id: "all", label: "All applications", test: () => true },
    { id: "active", label: "In progress", test: (item) => ["applying", "applied", "interview", "offer"].includes(item.stage) },
    ...APPLICATION_STAGES.map((stage) => ({
      id: stage,
      label: stage[0].toUpperCase() + stage.slice(1),
      group: "Stages",
      tone: stage === "interview" || stage === "offer" ? "is-good" : "",
      test: (item) => item.stage === stage,
    })),
  ];

  // Stage choices browsed with the keyboard but not saved yet, so a reload
  // triggered by another card's save does not throw them away.
  function unsavedStageChoices() {
    return [...els.results.querySelectorAll("[data-application-id] select[data-saved-stage]")]
      .filter((select) => select.value !== select.dataset.savedStage)
      .map((select) => ({
        id: select.closest("[data-application-id]").dataset.applicationId,
        value: select.value,
        focused: select === document.activeElement,
      }));
  }

  function restoreStageChoices(choices) {
    choices.forEach(({ id, value, focused }) => {
      const select = els.results.querySelector(`[data-application-id="${CSS.escape(id)}"] select[data-saved-stage]`);
      if (!select || select.dataset.savedStage === value) return;
      if (![...select.options].some((option) => option.value === value)) return;
      select.value = value;
      if (focused) select.focus();
    });
  }

  async function loadApplications() {
    const sequence = ++state.loadSequence;
    const carried = unsavedStageChoices();
    state.loading = true;
    clearError();
    els.results.setAttribute("aria-busy", "true");
    skeletons();
    try {
      const [payload, analytics] = await Promise.all([
        api("/api/v1/applications"),
        api("/api/v1/applications/analytics"),
      ]);
      if (sequence !== state.loadSequence || state.view !== "applications") return;
      state.total = payload.total;
      const tab = APPLICATION_TABS.find((entry) => entry.id === state.subtabs.applications) || APPLICATION_TABS[0];
      const shown = payload.items.filter(tab.test);
      renderSubnav(APPLICATION_TABS.map((entry) => ({ ...entry, count: payload.items.filter(entry.test).length })));
      els.results.replaceChildren();
      const toolbar = element("div", "tracker-toolbar");
      const modes = element("div", "display-toggle");
      modes.setAttribute("role", "group");
      modes.setAttribute("aria-label", "Tracker display");
      const boardButton = element("button", state.applicationMode === "board" ? "is-active" : "", "Board");
      boardButton.type = "button";
      boardButton.setAttribute("aria-pressed", String(state.applicationMode === "board"));
      const trackerListButton = element("button", state.applicationMode === "list" ? "is-active" : "", "List");
      trackerListButton.type = "button";
      trackerListButton.setAttribute("aria-pressed", String(state.applicationMode === "list"));
      boardButton.addEventListener("click", () => { state.applicationMode = "board"; loadApplications(); });
      trackerListButton.addEventListener("click", () => { state.applicationMode = "list"; loadApplications(); });
      modes.append(boardButton, trackerListButton);
      const exports = element("div", "tracker-exports");
      const capture = element("button", "secondary-button", "Capture role");
      capture.type = "button";
      capture.addEventListener("click", openCaptureDialog);
      const csvExport = element("a", "secondary-button", "Export CSV");
      csvExport.href = "/api/v1/applications/export?format=csv";
      const jsonExport = element("a", "secondary-button", "Export JSON");
      jsonExport.href = "/api/v1/applications/export?format=json";
      const importInput = document.createElement("input");
      importInput.type = "file";
      importInput.accept = ".csv,.json,text/csv,application/json";
      importInput.className = "sr-only";
      importInput.setAttribute("aria-label", "Import applications from CSV or JSON");
      const importButton = element("button", "secondary-button", "Import");
      importButton.type = "button";
      importButton.addEventListener("click", () => importInput.click());
      importInput.addEventListener("change", async () => {
        if (!importInput.files.length) return;
        importButton.disabled = true;
        const body = new FormData();
        body.append("upload", importInput.files[0]);
        try {
          const result = await api("/api/v1/applications/import", { method: "POST", body });
          // The reload rewrites the page status, so report the import after it.
          await loadApplications();
          if (state.view !== "applications") return;
          const summary = `Imported ${result.imported}; skipped ${result.skipped}`;
          els.pageStatus.textContent = summary;
          announce(`${summary}.`);
          const errors = Array.isArray(result.errors) ? result.errors : [];
          if (errors.length) {
            const report = element("div", "import-report");
            report.setAttribute("role", "status");
            report.appendChild(element("strong", "", `${plural(result.skipped || errors.length, "row was", "rows were")} not imported`));
            const list = element("ul", "");
            errors.forEach((entry) => {
              const detail = errorDetailText(entry && typeof entry === "object" ? entry.detail : entry) || "Not imported.";
              list.appendChild(element("li", "", entry && entry.row ? `Row ${entry.row}: ${detail}` : detail));
            });
            report.appendChild(list);
            if (result.skipped > errors.length) {
              report.appendChild(element("p", "", `Only the first ${errors.length} are listed.`));
            }
            const anchor = els.results.querySelector(".tracker-summary");
            if (anchor) anchor.after(report);
            else els.results.prepend(report);
          }
        } catch (error) {
          showError(error.message);
        } finally {
          importButton.disabled = false;
          importInput.value = "";
        }
      });
      exports.append(capture, csvExport, jsonExport, importButton, importInput);
      toolbar.append(modes, exports);
      els.results.appendChild(toolbar);
      const summary = element("div", "tracker-summary");
      summary.append(
        chip(plural(analytics.open_tasks, "open task", "open tasks"), "is-region"),
        chip(
          `${analytics.overdue_tasks + (analytics.overdue_follow_ups || 0)} overdue`,
          analytics.overdue_tasks + (analytics.overdue_follow_ups || 0) ? "is-warning" : ""
        ),
        chip(plural(analytics.scheduled_follow_ups, "follow-up", "follow-ups")),
        chip(`${plural(analytics.activity_last_30_days, "update", "updates")} / 30 days`)
      );
      summary.title = analytics.interpretation;
      els.results.appendChild(summary);
      if (!shown.length) {
        const empty = element("div", "empty-state");
        empty.appendChild(element("strong", "", payload.items.length ? `Nothing in ${tab.label}` : "No applications yet"));
        empty.appendChild(element("p", "", payload.items.length
          ? "Pick another stage in the list beside the page."
          : "Open Apply on an opportunity and it will appear here."));
        els.results.appendChild(empty);
      } else if (state.applicationMode === "list" || tab.id !== "all") {
        // One stage has no columns to spread across, so it reads as a list.
        shown.forEach((item) => els.results.appendChild(createApplicationCard(item)));
      } else {
        const board = element("div", "application-board");
        APPLICATION_STAGES.forEach((stage) => {
          const column = element("section", "board-column");
          const items = payload.items.filter((item) => item.stage === stage);
          const heading = element("div", "board-column-heading");
          heading.appendChild(element("h3", "", stage[0].toUpperCase() + stage.slice(1)));
          heading.appendChild(chip(String(items.length)));
          column.appendChild(heading);
          const list = element("div", "board-column-list");
          list.dataset.stage = stage;
          list.addEventListener("dragover", (event) => {
            event.preventDefault();
            event.dataTransfer.dropEffect = "move";
          });
          list.addEventListener("drop", async (event) => {
            event.preventDefault();
            const applicationId = event.dataTransfer.getData("text/plain");
            if (!applicationId) return;
            try {
              await api(`/api/v1/applications/${encodeURIComponent(applicationId)}`, {
                method: "PATCH",
                body: JSON.stringify({ stage }),
              });
              await Promise.all([loadApplications(), loadStats()]);
            } catch (error) {
              showError(error.message);
            }
          });
          items.forEach((item) => list.appendChild(createApplicationCard(item, { board: true })));
          if (!items.length) list.appendChild(element("p", "board-empty", "Drop an application here"));
          column.appendChild(list);
          board.appendChild(column);
        });
        els.results.appendChild(board);
      }
      els.resultCount.textContent = plural(payload.total, "application", "applications");
      els.pageStatus.textContent = "Every stage change is kept in the audit history";
      els.results.setAttribute("aria-busy", "false");
      focusRequestedApplication();
      restoreStageChoices(carried);
    } catch (error) {
      showLoadError(error, sequence);
    } finally {
      state.loading = false;
    }
  }

  const OUTREACH_STATUS_LABELS = {
    not_started: "Not started",
    drafted: "Drafted",
    sent: "Sent",
    followed_up: "Followed up",
    replied: "Replied",
    call_scheduled: "Call scheduled",
    offer: "Offer",
    declined: "Declined",
    no_response: "No response",
    paused: "Paused",
  };
  const CONTACT_CONFIDENCE_LABELS = {
    confirmed: "Contact confirmed",
    unverified: "Contact unverified",
    unknown: "Contact not confirmed",
  };

  function outreachField(form, labelText, name, value, options = {}) {
    const control = profileField(form, labelText, name, value, options);
    control.dataset.initial = control.value;
    if (options.wide) control.closest("label").classList.add("is-wide");
    return control;
  }

  function outreachChoice(form, labelText, name, value, choices) {
    const label = element("label", "profile-field");
    label.appendChild(element("span", "", labelText));
    const select = document.createElement("select");
    select.name = name;
    choices.forEach(([optionValue, text]) => {
      const option = document.createElement("option");
      option.value = optionValue;
      option.textContent = text;
      option.selected = optionValue === value;
      select.appendChild(option);
    });
    select.dataset.initial = select.value;
    label.appendChild(select);
    form.appendChild(label);
    return select;
  }

  const DRAFT_STATUS_LABELS = {
    generated: ["Draft needs review", "is-soon"],
    approved: ["Draft approved", "is-region"],
  };
  const CANDIDATE_METHOD_LABELS = {
    site_published: "Published on their site",
    site_generic: "Shared inbox on their site",
    site_person: "Named on their site, no address",
    pattern_guess: "Guessed from a name on their site",
    published_elsewhere: "Printed on another site",
    ai_research: "Named in deep search research",
  };
  // What the company's mail server said when asked about an address. Asking
  // sends no email, and even an accepted address stays unverified.
  const CANDIDATE_VERIFICATION_LABELS = {
    smtp_accepted: ["Mail server accepted it", "is-region"],
    smtp_rejected: ["Mail server: no such address", "is-warning"],
    catch_all: ["Server accepts any address", "is-soon"],
    smtp_unknown: ["Mail server gave no answer", ""],
  };
  const OUTREACH_EVENT_LABELS = {
    created: "Added",
    draft_edited: "Draft edited",
    follow_up_edited: "Follow-up edited",
    draft_generated: "Draft generated",
    follow_up_generated: "Follow-up generated",
    draft_approved: "Draft approved",
    follow_up_approved: "Follow-up approved",
    approval_withdrawn: "Approval withdrawn",
    reply_logged: "Reply logged",
    contacts_searched: "Searched their site for contacts",
    discovery_follow_through: "Deep search follow-up",
    research_confirmed: "Research confirmed",
    contact_applied: "Contact applied",
    gmail_draft_created: "Draft created in Gmail",
    gmail_sent: "Sent from Gmail",
    bounced: "Bounced",
    partly_bounced: "Partly bounced",
    greeting_updated: "Greeting updated for the new contact",
    auto_reply: "Automatic reply (out of office)",
    draft_restored: "Earlier draft restored",
    follow_up_restored: "Earlier follow-up restored",
    // Named apart from an ordinary "location recorded" on purpose: this is what
    // an import file claimed and the tracker refused, not what it accepted.
    location_import_claim: "Import file's unverified location claim",
    location_entered: "Location entered",
    call_prep_queued: "Call prep started",
    call_prep_generated: "Call prep written",
    call_prep_replaced: "Call prep replaced",
  };
  const DRAFT_PROVIDER_LABELS = {
    openai: "OpenAI",
    anthropic: "Anthropic",
    "claude-code": "Claude Code",
    "codex-cli": "Codex",
  };
  // Beyond this, some browsers and Gmail truncate a prefilled compose URL.
  const COMPOSE_URL_LIMIT = 1800;

  function safeExternalUrl(value) {
    try {
      const parsed = new URL(value);
      const host = parsed.hostname.toLowerCase().replace(/\.$/, "");
      if (!["http:", "https:"].includes(parsed.protocol) || !host || parsed.username || parsed.password) return null;
      if (host === "localhost" || host.endsWith(".localhost") || host.includes(":") || /^\d{1,3}(?:\.\d{1,3}){3}$/.test(host)) return null;
      return parsed.href;
    } catch (_error) {
      return null;
    }
  }

  function outreachDraftNeedsReview(item, kind) {
    if (kind === "initial") {
      return item.draft_status === "generated" && !item.sent_at && ["not_started", "drafted"].includes(item.status);
    }
    return item.follow_up_status === "generated" && item.status === "sent";
  }

  function composeHref(compose, to, subject, body, cc = "") {
    const encode = (value) => encodeURIComponent(value || "");
    if (compose?.provider === "gmail" && compose.account) {
      const copied = cc ? `&cc=${encode(cc)}` : "";
      const base = `https://mail.google.com/mail/?authuser=${encode(compose.account)}&view=cm&fs=1&to=${encode(to)}${copied}&su=${encode(subject)}`;
      return body ? `${base}&body=${encode(body)}` : base;
    }
    const params = [];
    if (cc) params.push(`cc=${encode(cc)}`);
    if (subject) params.push(`subject=${encode(subject)}`);
    if (body) params.push(`body=${encode(body)}`);
    return `mailto:${encode(to).replace(/%40/g, "@")}${params.length ? `?${params.join("&")}` : ""}`;
  }

  // Every hand-off (Gmail draft, compose link, copy) sends the approved draft
  // stored on the server, never the text box. If the box holds unsaved edits,
  // the student would get different words than they see, so the hand-off stops.
  function unsavedDraftEdits(control, kind) {
    const card = control.closest("[data-outreach-id]");
    const names = kind === "follow_up" ? ["follow_up_subject", "follow_up_body"] : ["email_subject", "email_body"];
    return names.some((name) => {
      const field = card?.querySelector(`[name="${name}"]`);
      return Boolean(field) && field.value !== field.dataset.initial;
    });
  }

  function refuseUnsavedHandOff(control, kind) {
    if (!unsavedDraftEdits(control, kind)) return false;
    const noun = kind === "follow_up" ? "follow-up" : "draft";
    showError(`The ${noun} text box has unsaved edits, and this would use the approved ${noun} instead. Save your changes, approve them, then try again.`);
    return true;
  }

  // An approved draft opens in the student's own email account. The app never
  // sends; a body too long for a compose URL is copied for pasting instead.
  function composeLink(compose, item, kind) {
    const subject = kind === "follow_up" ? item.follow_up_subject : item.email_subject;
    const body = kind === "follow_up" ? item.follow_up_body : item.email_body;
    const where = compose?.provider === "gmail" ? `Gmail (${compose.account})` : "email app";
    const link = element("a", "primary-button outreach-compose", kind === "follow_up" ? `Open follow-up in ${where} ↗` : `Open in ${where} ↗`);
    let href = composeHref(compose, item.contact_email, subject, body, item.contact_cc);
    const tooLong = href.length > COMPOSE_URL_LIMIT;
    if (tooLong) href = composeHref(compose, item.contact_email, subject, "", item.contact_cc);
    link.href = href;
    link.addEventListener("click", (event) => {
      if (!refuseUnsavedHandOff(link, kind)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    });
    if (compose?.provider === "gmail") {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
    if (tooLong) {
      link.addEventListener("click", async () => {
        try {
          await copyText(body);
          announce("The draft is too long for a compose link, so its body was copied. Paste it into the message.");
        } catch (error) {
          showError(error.message);
        }
      });
    }
    return link;
  }

  // With Gmail connected, the approved draft can be sent straight from here.
  // A sent email cannot be taken back, so the first click only asks: the
  // button turns into "Send to <address>?" and a second click sends. Leaving
  // it, pressing Escape, or waiting a few seconds puts it back.
  const SEND_CONFIRM_MS = 8000;

  function gmailSendButton(gmail, item, kind) {
    const attachment = gmail.attachment ? ` with ${gmail.attachment}` : "";
    const label = kind === "follow_up" ? `Send follow-up${attachment}` : `Send${attachment}`;
    const recipients = item.contact_cc ? `${item.contact_email} (Cc ${item.contact_cc})` : item.contact_email;
    const button = element("button", "primary-button outreach-compose outreach-send", label);
    button.type = "button";
    if (gmail.attachment_problem) {
      button.disabled = true;
      button.title = gmail.attachment_problem;
    }
    // After a 428 the server has named what the student must look for in
    // Gmail. The next confirmed click sends that check, once: whatever that
    // attempt's outcome, a later one has to be vouched for again.
    let check = "";
    let timer = null;
    const idleLabel = () => (check ? `Checked Gmail — send again${attachment}` : label);
    const reset = () => {
      clearTimeout(timer);
      timer = null;
      delete button.dataset.confirming;
      button.textContent = idleLabel();
    };
    button.addEventListener("blur", () => { if (button.dataset.confirming) reset(); });
    button.addEventListener("keydown", (event) => { if (event.key === "Escape" && button.dataset.confirming) reset(); });
    button.addEventListener("click", async () => {
      if (refuseUnsavedHandOff(button, kind)) return;
      if (!button.dataset.confirming) {
        button.dataset.confirming = "true";
        button.textContent = `Send to ${recipients}?`;
        announce(`Press again to send the ${kind === "follow_up" ? "follow-up" : "email"} to ${recipients} from ${gmail.account || "Gmail"}.`);
        timer = setTimeout(reset, SEND_CONFIRM_MS);
        return;
      }
      clearTimeout(timer);
      button.disabled = true;
      button.textContent = "Sending…";
      const payload = { kind, fingerprint: kind === "follow_up" ? item.follow_up_fingerprint : item.draft_fingerprint };
      if (check) payload.sent_folder_check = check;
      check = "";
      try {
        const sent = await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/gmail-send`, {
          method: "POST",
          body: JSON.stringify(payload),
        });
        state.outreachOpen = item.id;
        watchForBounces();
        if (sent.marked === false) {
          announce(`Sent to ${sent.to}, but ${item.company} could not be marked ${kind === "follow_up" ? "followed up" : "sent"}. Press "I sent it" to catch it up.`);
        } else {
          announce(kind === "follow_up"
            ? `Sent the follow-up to ${sent.to}. ${item.company} is marked followed up.`
            : `Sent to ${sent.to} from ${sent.account || "Gmail"}. ${item.company} is marked sent, with a follow-up set for ${formatCalendarDate(sent.follow_up_at)}.`);
        }
        await loadOutreach();
      } catch (error) {
        // Gmail may already have this email: the message says where to look,
        // and the next confirmed click vouches for having looked.
        if (error.status === 428 && typeof error.detail?.check === "string") check = error.detail.check;
        reset();
        button.disabled = false;
        // A changed or already-sent draft means the card is stale; reloading
        // clears errors, so the reason is shown after it.
        if (error.status === 409 || error.status === 422) {
          state.outreachOpen = item.id;
          await loadOutreach();
        }
        showError(error.message);
      }
    });
    return button;
  }

  // A bounce usually lands in Gmail within seconds of a send; a reply can come
  // any time. The app also checks in the background every few minutes; this
  // asks for a look on each load of the list and a few times right after a
  // send. The server spaces its own looks, so asking often costs nothing.
  const BOUNCE_LOOKS_MS = [15000, 45000, 120000, 300000];
  let bounceTimers = [];

  async function checkForBounces() {
    try {
      const result = await api("/api/v1/outreach/inbox-check", { method: "POST" });
      const news = [];
      if (result.bounced?.length) {
        const names = result.bounced.map((entry) => `${entry.company} (${entry.addresses.join(", ")})`).join("; ");
        news.push(`Bounced: ${names}. Moved back to Drafted; pick another contact and send again.`);
      }
      if (result.replies?.length) {
        const names = result.replies.map((entry) => `${entry.company} (${entry.from})`).join("; ");
        news.push(`New ${result.replies.length === 1 ? "reply" : "replies"} from ${names}, logged from Gmail.`);
      }
      if (!news.length) return;
      if (state.view === "outreach") await loadOutreach();
      announce(news.join(" "));
    } catch (_error) {
      // A failed look is retried on the next load; it never blocks the page.
    }
  }

  function watchForBounces() {
    bounceTimers.forEach(clearTimeout);
    bounceTimers = BOUNCE_LOOKS_MS.map((delay) => setTimeout(checkForBounces, delay));
  }

  // The approved draft can also be written into Gmail Drafts with the
  // configured attachment (a compose URL cannot attach a file), for editing
  // there before sending.
  function gmailDraftButton(gmail, item, kind) {
    const attachment = gmail.attachment ? ` with ${gmail.attachment}` : "";
    const label = kind === "follow_up" ? `Open follow-up in Gmail${attachment} ↗` : `Open in Gmail${attachment} ↗`;
    const button = element("button", "secondary-button outreach-compose", label);
    button.type = "button";
    if (gmail.attachment_problem) {
      button.disabled = true;
      button.title = gmail.attachment_problem;
    }
    button.addEventListener("click", async () => {
      if (refuseUnsavedHandOff(button, kind)) return;
      button.disabled = true;
      // Opened during the click so a popup blocker allows it; pointed at the
      // draft once Gmail has created it.
      const tab = window.open("about:blank", "_blank");
      try {
        const draft = await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/gmail-draft`, {
          method: "POST",
          body: JSON.stringify({ kind }),
        });
        if (tab) {
          tab.opener = null;
          tab.location.href = draft.url;
        } else {
          window.open(draft.url, "_blank", "noopener");
        }
        announce(draft.reused
          ? `Reopened the Gmail draft for ${item.company}.`
          : `Created the Gmail draft for ${item.company}${attachment}. Review it in Gmail and press Send there.`);
      } catch (error) {
        if (tab) tab.close();
        showError(error.message);
      } finally {
        button.disabled = false;
      }
    });
    return button;
  }

  function composeControl(context, item, kind) {
    if (!context.gmail?.connected) return composeLink(context.compose, item, kind);
    const controls = document.createDocumentFragment();
    // A paused company that was already written to is never sent the first email again.
    if (kind === "follow_up" || !item.sent_at) controls.append(gmailSendButton(context.gmail, item, kind));
    controls.append(gmailDraftButton(context.gmail, item, kind));
    return controls;
  }

  function gmailConnectPanel(gmail) {
    if (!gmail?.configured || (gmail.connected && gmail.bounce_check)) return null;
    const panel = element("div", "outreach-gmail-connect");
    const what = gmail.attachment ? ` with ${gmail.attachment} attached` : "";
    const reconnect = gmail.needs_reconnect || gmail.connected;
    panel.appendChild(element("p", "profile-help", gmail.connected
      ? "Reconnect Gmail once so the app can catch bounces and log replies for you. It asks for one more permission, to read mail; the app reads only delivery failure notices and mail from the companies you wrote to."
      : gmail.needs_reconnect
        ? "Gmail stopped accepting the connection. Reconnect it to keep creating drafts with attachments."
        : `Connect Gmail to send approved emails${what} from here, or open them as drafts in Gmail first. Nothing sends until you press Send and confirm the recipient.`));
    const connect = element("button", "secondary-button", reconnect ? "Reconnect Gmail" : `Connect Gmail${gmail.account ? ` (${gmail.account})` : ""}`);
    connect.type = "button";
    connect.addEventListener("click", async () => {
      connect.disabled = true;
      try {
        const start = await api("/api/v1/connections/oauth/gmail_drafts/start");
        window.location.assign(start.authorization_url);
      } catch (error) {
        showError(error.message);
        connect.disabled = false;
      }
    });
    panel.appendChild(connect);
    return panel;
  }

  async function copyText(text) {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const scratch = document.createElement("textarea");
    scratch.value = text;
    scratch.setAttribute("readonly", "");
    scratch.className = "sr-only";
    document.body.appendChild(scratch);
    scratch.select();
    const copied = document.execCommand("copy");
    scratch.remove();
    if (!copied) throw new Error("Copy is blocked in this browser; select the draft text instead.");
  }

  function renderDraftChecks(host, subject, body, place = null) {
    // Mirrors outreach.draft_checks, and for a cold email outreach.location_line_gap,
    // on the server so feedback is live while typing.
    const text = `${subject}\n${body}`;
    const dashes = (text.match(/[–—]/g) || []).length;
    const placeholders = [...new Set(text.match(/\[[^\]\n]{1,60}\]/g) || [])];
    const words = (body.match(/\b\w+\b/g) || []).length;
    host.replaceChildren(chip(plural(words, "word", "words"), words > 200 ? "is-soon" : ""));
    if (dashes) host.appendChild(chip(`${dashes} em/en dash${dashes === 1 ? "" : "es"}`, "is-warning"));
    if (placeholders.length) host.appendChild(chip(`Fill in ${placeholders.join(", ")}`, "is-soon"));
    if (body && !subject) host.appendChild(chip("No subject", "is-soon"));
    // "in Portland", not a bare "Portland": a school's name can carry the place (outreach.mentions_home).
    const flat = body.replace(/\s+/g, " ");
    const saysHome = (term) => new RegExp(`\\bin ${term.replace(/\s+/g, " ").replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "i").test(flat);
    if (place && place.terms && place.terms.length && body.trim() && !place.terms.some(saysHome)) {
      host.appendChild(chip(`Doesn't say you live in ${place.phrase}; regenerate`, "is-warning"));
    }
  }

  async function loadOutreachTimeline(targetId, host) {
    host.replaceChildren(element("p", "detail-loading", "Loading history…"));
    try {
      const payload = await api(`/api/v1/outreach/${encodeURIComponent(targetId)}`);
      const list = element("ol", "outreach-timeline");
      payload.events.forEach((event) => {
        const text = event.event_type === "status"
          ? `${OUTREACH_STATUS_LABELS[event.from_status] || event.from_status} → ${OUTREACH_STATUS_LABELS[event.to_status] || event.to_status}`
          : OUTREACH_EVENT_LABELS[event.event_type] || event.event_type.replace(/_/g, " ");
        const row = element("li");
        row.appendChild(element("span", "", text));
        row.appendChild(element("small", "", formatDate(event.created_at)));
        if (event.event_type === "call_prep_replaced" && event.detail) {
          // Whole notes: folded away, but there to copy back.
          const earlier = element("details", "outreach-claims");
          earlier.appendChild(element("summary", "", "The notes it replaced"));
          earlier.appendChild(element("pre", "outreach-event-detail outreach-prep-earlier", event.detail));
          row.appendChild(earlier);
        } else if (["bounced", "partly_bounced"].includes(event.event_type) && event.detail) {
          let bounce = {};
          try { bounce = JSON.parse(event.detail); } catch (_error) { bounce = {}; }
          const who = (bounce.addresses || []).join(", ");
          const how = bounce.source === "gmail" ? "Gmail's delivery notice" : "You marked it";
          row.appendChild(element("p", "outreach-event-detail", [who, bounce.reason, how].filter(Boolean).join(" · ")));
        } else if (event.detail && !["draft_generated", "gmail_draft_created", "gmail_sent", "call_prep_generated"].includes(event.event_type)) {
          row.appendChild(element("p", "outreach-event-detail", event.detail));
        }
        list.appendChild(row);
      });
      host.replaceChildren(list);
    } catch (error) {
      host.replaceChildren(element("p", "form-error", error.message));
    }
  }

  function draftAssistant(group, item, kind, subjectControl, bodyControl) {
    const status = kind === "follow_up" ? item.follow_up_status : item.draft_status;
    const fingerprint = kind === "follow_up" ? item.follow_up_fingerprint : item.draft_fingerprint;
    const claimsForKind = kind === "follow_up" ? item.follow_up_claims : item.draft_claims;
    const generatedBy = kind === "follow_up" ? item.follow_up_generated_by : item.draft_generated_by;
    const bodyText = kind === "follow_up" ? item.follow_up_body : item.email_body;
    const historyCount = (kind === "follow_up" ? item.follow_up_history_count : item.draft_history_count) || 0;
    const noun = kind === "follow_up" ? "follow-up" : "draft";
    const panel = element("div", "outreach-draft-assistant is-wide");
    panel.dataset.draftKind = kind;
    const buttons = element("div", "outreach-draft-buttons");
    const generate = element(
      "button",
      "secondary-button",
      kind === "follow_up"
        ? (item.follow_up_body ? "Regenerate follow-up" : "Generate follow-up")
        : (item.email_body ? "Regenerate draft" : "Generate draft")
    );
    generate.type = "button";
    const approve = element("button", "primary-button", kind === "follow_up" ? "Approve follow-up" : "Approve draft");
    approve.type = "button";
    approve.dataset.draftApprove = "";
    approve.hidden = status !== "generated";
    const message = element("p", "form-status");
    message.setAttribute("aria-live", "polite");
    const unsaved = () => subjectControl.value !== subjectControl.dataset.initial || bodyControl.value !== bodyControl.dataset.initial;

    // Comments steer a regeneration; they have no name, so Save never sends them.
    let comments = null;
    if (bodyText) {
      const label = element("label", "profile-field outreach-draft-comments");
      label.appendChild(element("span", "", `Comments for the next ${noun}`));
      comments = document.createElement("textarea");
      comments.rows = 2;
      comments.maxLength = 2000;
      comments.placeholder = "Optional. For example: shorter, lead with the robot arm results, drop the second project.";
      label.appendChild(comments);
      panel.appendChild(label);
    }

    generate.addEventListener("click", async () => {
      if (unsaved() && !window.confirm("Replace your unsaved edits with a newly generated draft?")) return;
      const asked = comments ? comments.value.trim() : "";
      generate.disabled = true;
      approve.disabled = true;
      message.textContent = asked
        ? "Rewriting the draft with your comments, from your confirmed profile and this research. This can take a minute…"
        : "Writing a draft from your confirmed profile and this research. This can take a minute…";
      try {
        await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/draft`, {
          method: "POST",
          body: JSON.stringify({ kind, comments: asked }),
        });
        state.outreachOpen = item.id;
        state.outreachDiscardEdits = true;
        announce(`Drafted ${kind === "follow_up" ? "a follow-up" : "an email"} for ${item.company}. Review it before approving.`);
        await loadOutreach();
        refocusOutreach(item.id, `[data-draft-kind="${kind}"] [data-draft-approve]`);
      } catch (error) {
        message.textContent = error.message;
        generate.disabled = false;
        approve.disabled = false;
      }
    });

    // Approving is one press. Edits still in the box are saved first, so what is
    // approved is the words on screen, under the fingerprint of what was saved.
    approve.addEventListener("click", async () => {
      const send = (print, acknowledge) => api(`/api/v1/outreach/${encodeURIComponent(item.id)}/approve`, {
        method: "POST",
        body: JSON.stringify({ kind, fingerprint: print, acknowledge_warnings: acknowledge }),
      });
      const editing = unsaved();
      approve.disabled = true;
      try {
        let print = fingerprint;
        if (editing) {
          message.textContent = `Saving your edits, then approving the ${noun}…`;
          const saved = await api(`/api/v1/outreach/${encodeURIComponent(item.id)}`, {
            method: "PATCH",
            body: JSON.stringify(kind === "follow_up"
              ? { follow_up_subject: subjectControl.value, follow_up_body: bodyControl.value }
              : { email_subject: subjectControl.value, email_body: bodyControl.value }),
          });
          print = kind === "follow_up" ? saved.follow_up_fingerprint : saved.draft_fingerprint;
        }
        try {
          await send(print, false);
        } catch (error) {
          if (error.status !== 422 || !String(error.message).startsWith("Review these warnings")) throw error;
          if (!window.confirm(`${error.message}\n\nApprove anyway?`)) {
            message.textContent = editing ? `Your edits are saved. The ${noun} is not approved.` : "";
            approve.disabled = false;
            return;
          }
          await send(print, true);
        }
        state.outreachOpen = item.id;
        announce(`${editing ? "Saved your edits and approved" : "Approved"} the ${item.company} ${noun}. It is ready to send.`);
        await loadOutreach();
        refocusOutreach(item.id, ".outreach-compose");
      } catch (error) {
        if (error.status === 409) {
          state.outreachFlash = { id: item.id, kind, message: error.message };
          state.outreachOpen = item.id;
          await loadOutreach();
          refocusOutreach(item.id, `[data-draft-kind="${kind}"] [data-draft-approve]`);
          return;
        }
        message.textContent = error.message;
        approve.disabled = false;
      }
    });

    buttons.append(generate, approve);
    // Right under the buttons: a message pushed below the claims and the
    // provenance note reads as nothing having happened at all.
    panel.append(buttons, message);
    if (historyCount) panel.appendChild(draftHistory(item, kind, unsaved));
    if (status === "approved") panel.appendChild(chip(kind === "follow_up" ? "Follow-up approved" : "Approved", "is-region"));
    if (claimsForKind.length) {
      const claims = element("details", "outreach-claims");
      const modelWritten = generatedBy && generatedBy !== "template";
      claims.appendChild(element("summary", "", `${modelWritten ? "What the model says this draft is based on" : "What this draft is based on"} (${claimsForKind.length})`));
      if (modelWritten) claims.appendChild(element("p", "outreach-note", "The model's own citations, not checked sentence by sentence."));
      const list = element("ul");
      claimsForKind.forEach((claim) => {
        const row = element("li");
        row.appendChild(element("span", "", claim.text));
        const sourceHref = safeExternalUrl(claim.basis);
        if (sourceHref) {
          const source = element("a", "", "source ↗");
          source.href = sourceHref;
          source.target = "_blank";
          source.rel = "noopener noreferrer";
          row.appendChild(source);
          if (item.research_confidence === "unverified") row.appendChild(element("small", "", "unverified deep-search source"));
        } else {
          const [where, field] = claim.basis.split(":");
          const label = where === "profile"
            ? "your profile"
            : where === "unverified"
              ? "unverified deep-search research"
              : claim.basis === "inference"
                ? "inference, not from your profile or research"
                : "research";
          row.appendChild(element("small", "", `${label}${field ? `: ${field.replace(/_/g, " ")}` : ""}`));
        }
        list.appendChild(row);
      });
      claims.appendChild(list);
      panel.appendChild(claims);
    }
    if (bodyText) {
      let provenance = "Draft origin not recorded; check every sentence before approving.";
      if (generatedBy === "template") {
        provenance = "Deterministic template built from your saved profile and outreach record. Check it before approving.";
      } else if (generatedBy) {
        const provider = generatedBy.split(":")[0];
        provenance = `Written by ${DRAFT_PROVIDER_LABELS[provider] || provider}. Check every sentence before approving.`;
      }
      panel.appendChild(element("p", "outreach-note", provenance));
    }
    if (state.outreachFlash?.id === item.id && state.outreachFlash.kind === kind) {
      message.textContent = state.outreachFlash.message;
      state.outreachFlash = null;
    }
    group.appendChild(panel);
  }

  function formatDateTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value || "";
    return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(date);
  }

  function draftVersionOrigin(version) {
    if (version.source === "saved") return "Saved from the editor, possibly hand edited";
    if (version.generated_by === "template") return "Deterministic template";
    const provider = (version.generated_by || "").split(":")[0];
    return provider ? `Written by ${DRAFT_PROVIDER_LABELS[provider] || provider}` : "Origin not recorded";
  }

  // Earlier drafts load when opened and are read-only here. "Use this draft"
  // puts one back in the editor through the server, which keeps the draft it
  // replaces, so stepping back never loses the newer one.
  function draftHistory(item, kind, unsaved) {
    const noun = kind === "follow_up" ? "follow-up" : "draft";
    const count = kind === "follow_up" ? item.follow_up_history_count : item.draft_history_count;
    const history = element("details", "outreach-draft-history");
    history.appendChild(element("summary", "", `Earlier ${noun}s (${count})`));
    const body = element("div", "outreach-draft-history-body");
    history.appendChild(body);
    let versions = [];
    let index = 0;

    const render = (focusSelector) => {
      const version = versions[index];
      const nav = element("div", "outreach-draft-buttons");
      const older = element("button", "secondary-button", "‹ Older");
      older.type = "button";
      older.disabled = index === 0;
      older.dataset.historyOlder = "";
      const newer = element("button", "secondary-button", "Newer ›");
      newer.type = "button";
      newer.disabled = index === versions.length - 1;
      newer.dataset.historyNewer = "";
      const label = `${noun[0].toUpperCase()}${noun.slice(1)} ${index + 1} of ${versions.length}${version.is_current ? " · in the editor now" : ""}`;
      const position = element("span", "outreach-draft-history-position", label);
      position.setAttribute("aria-live", "polite");
      nav.append(older, position, newer);
      // Keep focus on the arrow just pressed, or its partner once it disables.
      older.addEventListener("click", () => { index -= 1; render(["[data-history-older]", "[data-history-newer]"]); });
      newer.addEventListener("click", () => { index += 1; render(["[data-history-newer]", "[data-history-older]"]); });

      const preview = element("div", "outreach-draft-version");
      preview.appendChild(element("p", "outreach-note", [formatDateTime(version.created_at), draftVersionOrigin(version)].filter(Boolean).join(" · ")));
      if (version.comments) preview.appendChild(element("p", "outreach-draft-version-comments", `Asked for: ${version.comments}`));
      preview.appendChild(element("p", "outreach-draft-version-subject", version.subject || "(no subject)"));
      preview.appendChild(element("div", "outreach-draft-version-body", version.body));

      const status = element("p", "form-status");
      status.setAttribute("aria-live", "polite");
      const restore = element("button", "secondary-button", `Use this ${noun}`);
      restore.type = "button";
      restore.hidden = version.is_current;
      restore.addEventListener("click", async () => {
        if (unsaved() && !window.confirm(`Replace your unsaved edits with this earlier ${noun}?`)) return;
        restore.disabled = true;
        try {
          await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/drafts/${encodeURIComponent(version.id)}/restore`, { method: "POST" });
          state.outreachOpen = item.id;
          state.outreachDiscardEdits = true;
          announce(`Restored an earlier ${noun} for ${item.company}. The one it replaced is kept under Earlier ${noun}s. Review it before approving.`);
          await loadOutreach();
          refocusOutreach(item.id, `[data-draft-kind="${kind}"] [data-draft-approve]`);
        } catch (error) {
          status.textContent = error.message;
          restore.disabled = false;
        }
      });
      body.replaceChildren(nav, preview, restore, status);
      if (focusSelector) focusSelector.map((selector) => body.querySelector(selector)).find((node) => !node.disabled)?.focus();
    };

    history.addEventListener("toggle", async () => {
      if (!history.open || history.dataset.loaded) return;
      history.dataset.loaded = "true";
      body.replaceChildren(element("p", "detail-loading", `Loading earlier ${noun}s…`));
      try {
        const payload = await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/drafts?kind=${kind}`);
        versions = payload.items;
        if (!versions.length) {
          body.replaceChildren(element("p", "outreach-note", `No earlier ${noun}s are stored.`));
          return;
        }
        // Start on the newest draft that is not already in the editor.
        const earlier = versions.map((version, position) => (version.is_current ? -1 : position)).filter((position) => position >= 0);
        index = earlier.length ? earlier[earlier.length - 1] : versions.length - 1;
        render();
      } catch (error) {
        delete history.dataset.loaded;
        body.replaceChildren(element("p", "form-error", error.message));
      }
    });
    return history;
  }

  function outreachContactsSection(item) {
    const section = element("section", "tracker-subsection outreach-contacts");
    section.appendChild(element("h4", "", "Contacts from their website"));
    const intro = element("p", "outreach-note", item.website
      ? "Reads a few pages of their own site. Published addresses count as confirmed. Guesses are checked with their mail server, which sends no email, and stay unverified."
      : "Add their website under Research to search it for contacts.");
    const find = element("button", "secondary-button", "Find contacts");
    find.type = "button";
    find.disabled = !item.website;
    const status = element("p", "form-status");
    status.setAttribute("aria-live", "polite");
    const list = element("ul", "outreach-candidates");
    section.append(intro, find, status, list);

    const render = (candidates) => {
      list.replaceChildren();
      candidates.forEach((candidate) => {
        const row = element("li", "outreach-candidate");
        const who = element("div");
        who.appendChild(element("strong", "", candidate.name || candidate.email || "Unnamed"));
        const meta = [candidate.role, candidate.name && candidate.email ? candidate.email : ""].filter(Boolean).join(" · ");
        if (meta) who.appendChild(element("span", "", meta));
        if (candidate.note) who.appendChild(element("span", "outreach-candidate-note", candidate.note));
        const tags = element("div", "application-facts");
        tags.appendChild(chip(CANDIDATE_METHOD_LABELS[candidate.method] || candidate.method));
        tags.appendChild(chip(
          CONTACT_CONFIDENCE_LABELS[candidate.confidence],
          candidate.confidence === "confirmed" ? "is-region" : candidate.confidence === "unverified" ? "is-soon" : "is-warning"
        ));
        const verification = CANDIDATE_VERIFICATION_LABELS[candidate.verification];
        if (verification) tags.appendChild(chip(verification[0], verification[1]));
        const evidenceHref = safeExternalUrl(candidate.evidence_url);
        if (evidenceHref) {
          const evidence = element("a", "outreach-evidence", "Evidence ↗");
          evidence.href = evidenceHref;
          evidence.target = "_blank";
          evidence.rel = "noopener noreferrer";
          tags.appendChild(evidence);
        }
        row.append(who, tags);
        if (candidate.email) {
          const current = candidate.email === item.contact_email;
          const use = element("button", "secondary-button", current ? "Current contact" : "Use this contact");
          use.type = "button";
          use.disabled = current;
          use.setAttribute("aria-label", current ? `${candidate.email} is the current contact` : `Use ${candidate.email} as the contact`);
          use.addEventListener("click", async () => {
            use.disabled = true;
            try {
              await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/contacts/${encodeURIComponent(candidate.id)}/apply`, { method: "POST" });
              state.outreachOpen = item.id;
              announce(`${candidate.email} is now the ${item.company} contact${candidate.confidence === "confirmed" ? "" : ", marked unverified"}.`);
              await loadOutreach();
              refocusOutreach(item.id, ".outreach-contacts button");
            } catch (error) {
              status.textContent = error.message;
              use.disabled = false;
            }
          });
          row.appendChild(use);
        }
        list.appendChild(row);
      });
    };

    find.addEventListener("click", async () => {
      find.disabled = true;
      status.textContent = "Reading their website…";
      try {
        const result = await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/find-contacts`, { method: "POST" });
        render(result.candidates);
        const pages = plural(result.pages_checked.length, "page", "pages");
        const mail = (result.mail_domain_ok === false ? " Their domain does not accept email, so no addresses were guessed." : "")
          + (result.rendered ? " Their site builds its pages with scripts, so they were read in a browser." : "");
        status.textContent = result.candidates.length
          ? `Found ${plural(result.candidates.length, "candidate", "candidates")} on ${pages}.${mail}`
          : `Nothing published on ${pages}.${mail}`;
        // The same pages can say where the company is; show it without redrawing the pane.
        if (["recorded", "confirmed"].includes(result.location?.outcome)) {
          Object.assign(item, await api(`/api/v1/outreach/${encodeURIComponent(item.id)}`));
          document.querySelector(`.outreach-pane[data-outreach-id="${CSS.escape(item.id)}"] .outreach-location`)
            ?.replaceWith(outreachLocationLine(item));
          announce(`Recorded where ${item.company} is based: ${item.location}.`);
        }
      } catch (error) {
        status.textContent = error.message;
      } finally {
        find.disabled = false;
        if (document.activeElement === document.body) find.focus();
      }
    });

    return {
      element: section,
      load: async () => {
        try {
          const payload = await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/contacts`);
          render(payload.candidates);
        } catch (error) {
          status.textContent = error.message;
        }
      },
    };
  }

  function outreachReplySection(item) {
    const section = element("section", "tracker-subsection outreach-reply");
    section.appendChild(element("h4", "", "Log a reply"));
    const label = element("label", "profile-field");
    const fieldId = `outreach-reply-${item.id}`;
    label.htmlFor = fieldId;
    label.appendChild(element("span", "", "Paste their reply"));
    const text = document.createElement("textarea");
    text.id = fieldId;
    text.rows = 4;
    label.appendChild(text);
    const log = element("button", "secondary-button", "Log reply");
    log.type = "button";
    const result = element("div", "outreach-reply-result");
    result.setAttribute("aria-live", "polite");
    log.addEventListener("click", async () => {
      if (!text.value.trim()) {
        result.replaceChildren(element("p", "form-status", "Paste the reply text first."));
        return;
      }
      log.disabled = true;
      try {
        const pasted = text.value;
        const payload = await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/reply`, {
          method: "POST",
          body: JSON.stringify({ text: pasted }),
        });
        const suggested = payload.suggestion.status;
        if (suggested === "bounced") {
          result.replaceChildren(element("p", "form-status", `Not saved as a reply. ${payload.suggestion.reason}.`));
          const mark = element("button", "primary-button", "Mark bounced");
          mark.type = "button";
          mark.addEventListener("click", async () => {
            mark.disabled = true;
            try {
              await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/bounce`, { method: "POST", body: JSON.stringify({ text: pasted }) });
              text.value = "";
              state.outreachOpen = item.id;
              state.outreachKeep.add(item.id);
              await loadOutreach();
              announce(`${item.company} marked bounced and moved back to Drafted. Pick another contact, then send again.`);
            } catch (error) {
              showError(error.message);
              mark.disabled = false;
            }
          });
          // A person can write about a failed delivery too; the student decides.
          const reply = element("button", "secondary-button", "It's a real reply, log it");
          reply.type = "button";
          reply.addEventListener("click", async () => {
            reply.disabled = true;
            try {
              await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/reply`, {
                method: "POST", body: JSON.stringify({ text: pasted, as_reply: true }),
              });
              text.value = "";
              state.outreachOpen = item.id;
              state.outreachKeep.add(item.id);
              await loadOutreach();
              announce(`Logged the reply from ${item.company}.`);
            } catch (error) {
              showError(error.message);
              reply.disabled = false;
            }
          });
          result.append(mark, reply);
          return;
        }
        text.value = "";
        const writing = CALL_PREP_ACTIVE.includes(payload.target?.call_prep_job?.state);
        const fellBack = payload.suggestion.fallback_reason ? ` (${payload.suggestion.fallback_reason}, so the keyword rules suggested this)` : "";
        result.replaceChildren(element("p", "form-status", `Saved to history. ${payload.suggestion.reason}${fellBack}.${writing ? " Writing call prep in the background." : ""}`));
        if (writing) {
          watchCallPrep(item.id);
          showCallPrepWriting(section.closest("[data-outreach-id]"));
        }
        item.reply_count = (item.reply_count || 0) + 1;
        if (suggested !== item.status) {
          const apply = element("button", "primary-button", `Mark ${OUTREACH_STATUS_LABELS[suggested]}`);
          apply.type = "button";
          apply.addEventListener("click", async () => {
            apply.disabled = true;
            try {
              await patchOutreach(item, { status: suggested }, `${item.company} marked ${OUTREACH_STATUS_LABELS[suggested]}.`);
            } catch (error) {
              showError(error.message);
              apply.disabled = false;
            }
          });
          result.appendChild(apply);
        }
      } catch (error) {
        result.replaceChildren(element("p", "form-status", error.message));
      } finally {
        log.disabled = false;
        if (document.activeElement === document.body) (result.querySelector("button") || log).focus();
      }
    });
    section.append(label, log, result);
    return section;
  }

  // Not contacted yet: nothing sent and still before the first email.
  function outreachToContact(item) {
    return !item.sent_at && ["not_started", "drafted"].includes(item.status);
  }

  // The rail's outreach tabs. The server returns every company once; each tab
  // narrows that list here, so every count stays in step with the cards.
  const OUTREACH_TABS = [
    { id: "to-contact", label: "To contact", test: outreachToContact },
    { id: "ready", label: "Ready to send", group: "Before sending", tone: "is-good", test: (item) => outreachToContact(item) && item.draft_status === "approved" && Boolean(item.contact_email) && !item.contact_bounced && !item.cc_bounced },
    { id: "needs-review", label: "Drafts to review", group: "Before sending", tone: "is-soon", test: (item) => outreachDraftNeedsReview(item, "initial") || outreachDraftNeedsReview(item, "follow_up") },
    { id: "needs-contact", label: "Needs a contact", group: "Before sending", tone: "is-soon", test: (item) => outreachToContact(item) && (!item.contact_email || item.contact_bounced) },
    { id: "bounced", label: "Bounced", group: "Before sending", tone: "is-alert", test: (item) => Boolean(item.bounced_at) },
    { id: "needs-location", label: "Needs a location", group: "Before sending", tone: "is-soon", test: (item) => outreachToContact(item) && !item.location_verified },
    { id: "from-search", label: "From deep search", group: "Before sending", test: (item) => item.origin === "discovery" && outreachToContact(item) },
    { id: "follow-ups-due", label: "Follow-ups due", group: "Contacted", tone: "is-alert", test: (item) => item.follow_up_due },
    { id: "revisits-due", label: "Revisits due", group: "Contacted", tone: "is-alert", test: (item) => item.revisit_due },
    { id: "awaiting", label: "Awaiting reply", group: "Contacted", test: (item) => item.status === "sent" || item.status === "followed_up" },
    { id: "replied", label: "Replied", group: "Contacted", tone: "is-good", test: (item) => ["replied", "call_scheduled", "offer"].includes(item.status) },
    { id: "closed", label: "Closed or paused", group: "Contacted", test: (item) => ["declined", "no_response", "paused"].includes(item.status) },
    { id: "all", label: "All companies", group: "Everything", test: () => true },
    { id: "deep-search", label: "Deep search", group: "Tools", tool: true },
    { id: "find-people", label: "Find people", group: "Tools", tool: true },
    { id: "add", label: "Add, import, export", group: "Tools", tool: true },
    { id: "settings", label: "Settings", group: "Tools", tool: true },
  ];

  const OUTREACH_SORTS = {
    contact: ["Confirmed email first", () => 0],
    priority: ["Priority", (a, b) => a.priority.localeCompare(b.priority)],
    company: ["Company A to Z", (a, b) => a.company.localeCompare(b.company, undefined, { sensitivity: "base" })],
    follow_up: ["Next follow-up", (a, b) => (a.follow_up_at || "9999").localeCompare(b.follow_up_at || "9999")],
    recent: ["Recently updated", (a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || ""))],
  };

  function outreachMatchesQuery(item, query) {
    if (!query) return true;
    const haystack = [item.company, item.location, item.location_region, item.contact_name, item.contact_email, item.channel, item.summary, item.fit_rationale, item.notes]
      .filter(Boolean).join(" ").toLowerCase();
    return query.toLowerCase().split(/\s+/).filter(Boolean).every((word) => haystack.includes(word));
  }

  let deepSearchTimer = null;

  function scheduleDeepSearchPoll() {
    if (deepSearchTimer) return;
    deepSearchTimer = window.setTimeout(async () => {
      deepSearchTimer = null;
      if (state.view !== "outreach") return;
      try {
        const discovery = await api("/api/v1/outreach/discovery");
        if (discovery.active?.state === "running") {
          scheduleDeepSearchPoll();
          return;
        }
        const result = discovery.active?.result;
        announce(discovery.active?.state === "failed"
          ? `The deep search failed: ${discovery.active.error}`
          : `Deep search finished${result ? `: ${plural(result.imported, "new company", "new companies")} added` : ""}.`);
        // Watching the search: show what it found rather than the finished panel.
        if (result?.imported && state.subtabs.outreach === "deep-search") state.subtabs.outreach = "from-search";
        if (!state.loading) await loadOutreach();
      } catch (error) {
        showError(error.message);
      }
    }, 5000);
  }

  function deepSearchPanel(discovery) {
    const panel = element("details", "tracker-detail outreach-deep-search");
    panel.open = Boolean(state.deepSearchOpen);
    panel.addEventListener("toggle", () => { state.deepSearchOpen = panel.open; });
    const latest = discovery.runs[0];
    const running = discovery.active?.state === "running";
    const headline = running
      ? "Deep search: running now"
      : latest
        ? `Deep search: last run ${formatDate(latest.started_at)}${latest.status === "succeeded" ? `, ${plural(latest.imported, "company", "companies")} added` : `, ${latest.status}`}`
        : "Deep search: not run yet";
    panel.appendChild(element("summary", "", headline));
    const body = element("div", "outreach-deep-search-body");
    body.appendChild(element("p", "outreach-note",
      "Searches the web twice a week (Monday and Thursday mornings) for startups worth a cold email, one search for each kind you tick. Each company is added only if its website and at least one source actually load. Contacts come from their own sites, and every draft waits for your approval."));

    if (latest) {
      const facts = element("div", "application-facts");
      facts.append(
        chip(`${latest.proposed} proposed`),
        chip(`${latest.imported} added`, latest.imported ? "is-region" : ""),
        chip(`${latest.rejected.length} rejected`, latest.rejected.length ? "is-soon" : "")
      );
      body.appendChild(facts);
      if (latest.error) body.appendChild(element("p", "form-error", latest.error));
      if (latest.rejected.length) {
        const rejected = element("details", "outreach-rejected");
        rejected.appendChild(element("summary", "", "Why companies were rejected"));
        const list = element("ul");
        latest.rejected.slice(0, 50).forEach((entry) => list.appendChild(element("li", "", `${entry.company}: ${entry.reason}`)));
        rejected.appendChild(list);
        body.appendChild(rejected);
      }
    }

    if (discovery.available) {
      const scopes = element("fieldset", "outreach-scopes");
      scopes.appendChild(element("legend", "", "Search for"));
      discovery.scopes.forEach((scope) => {
        const label = element("label", "outreach-scope");
        const box = document.createElement("input");
        box.type = "checkbox";
        box.value = scope.id;
        box.checked = true;
        label.append(box, document.createTextNode(` ${scope.label}`));
        scopes.appendChild(label);
      });
      const run = element("button", "primary-button", running ? "Searching…" : "Run deep search now");
      run.type = "button";
      run.disabled = running;
      const status = element("p", "form-status");
      status.setAttribute("aria-live", "polite");
      if (running) status.textContent = "Searching the web. This usually takes 10 to 30 minutes; you can keep working.";
      run.addEventListener("click", async () => {
        const chosen = [...scopes.querySelectorAll("input:checked")].map((box) => box.value);
        if (!chosen.length) {
          status.textContent = "Choose at least one kind of company.";
          return;
        }
        run.disabled = true;
        try {
          await api("/api/v1/outreach/discovery", { method: "POST", body: JSON.stringify({ scopes: chosen }) });
          run.textContent = "Searching…";
          status.textContent = "Searching the web. This usually takes 10 to 30 minutes; you can keep working.";
          announce("Deep search started.");
          scheduleDeepSearchPoll();
        } catch (error) {
          status.textContent = error.message;
          run.disabled = false;
        }
      });
      body.append(scopes, run, status);
    }
    panel.appendChild(body);
    return panel;
  }

  let recontactTimer = null;

  function scheduleRecontactPoll() {
    if (recontactTimer) return;
    recontactTimer = window.setTimeout(async () => {
      recontactTimer = null;
      if (state.view !== "outreach") return;
      try {
        const recontact = await api("/api/v1/outreach/recontact");
        const active = recontact.active;
        if (active?.state === "running") {
          scheduleRecontactPoll();
          return;
        }
        if (active?.state === "failed") announce(`The contact search failed: ${active.error}`);
        else if (active?.mode === "apply") announce(`Updated ${plural(active.result?.upgraded || 0, "contact", "contacts")}.`);
        else announce(`Contact search finished: ${plural(active?.result?.upgraded || 0, "person", "people")} found.`);
        if (!state.loading) await loadOutreach();
      } catch (error) {
        showError(error.message);
      }
    }, 4000);
  }

  // A guess is never shown as confirmed: the label says what the address rests on.
  const RECONTACT_BASIS_LABELS = {
    confirmed: ["Confirmed on their site", "is-good"],
    strong_guess: ["Guess, strong evidence (not confirmed)", "is-soon"],
    weak_guess: ["Weak guess (not confirmed)", "is-alert"],
  };

  function recontactPanel(recontact) {
    const panel = element("section", "tracker-detail outreach-deep-search outreach-recontact");
    panel.setAttribute("aria-labelledby", "recontact-heading");
    const heading = element("h2", "outreach-recontact-heading", "Find people at shared-inbox companies");
    heading.id = "recontact-heading";
    panel.appendChild(heading);
    const body = element("div", "outreach-deep-search-body");
    body.appendChild(element("p", "outreach-note",
      `${plural(recontact.eligible, "company you have not written to yet has", "companies you have not written to yet have")} only a shared inbox or no address. This reads their sites again, checks guesses with their mail servers, and searches other sites for a person. Nothing changes until you tick a result and apply it. Companies you have sent to, or whose draft you approved, are left alone.`));

    const active = recontact.active;
    const running = active?.state === "running";
    const status = element("p", "form-status");
    status.setAttribute("aria-live", "polite");
    if (running) {
      status.textContent = active.mode === "apply"
        ? "Updating contacts and rewriting drafts…"
        : "Searching. This can take several minutes for many companies; you can keep working.";
      scheduleRecontactPoll();
    }
    if (active?.state === "failed") body.appendChild(element("p", "form-error", active.error));

    const result = active?.state === "succeeded" ? active.result : null;
    if (result && active.mode === "report") body.appendChild(recontactReport(result, recontact, status));
    if (result && active.mode === "apply") {
      body.appendChild(element("p", "", `Updated ${plural(result.upgraded, "contact", "contacts")} (${formatDate(active.finished_at)}).`));
      const skipped = result.results.filter((entry) => !entry.applied);
      if (skipped.length) {
        const list = element("ul", "outreach-recontact-skipped");
        skipped.forEach((entry) => list.appendChild(element("li", "", `${entry.company || "Removed company"}: ${entry.skipped || entry.error || "not changed"}`)));
        body.appendChild(list);
      }
      result.results
        .filter((entry) => entry.draft && entry.draft !== "generated")
        .forEach((entry) => body.appendChild(element("p", "form-error", `${entry.company}: draft ${entry.draft}`)));
    }

    if (recontact.available) {
      const run = element("button", result && active.mode === "report" ? "secondary-button" : "primary-button",
        running ? "Searching…" : result ? "Search again" : "Search now");
      run.type = "button";
      run.disabled = running || !recontact.eligible;
      run.addEventListener("click", async () => {
        run.disabled = true;
        try {
          await api("/api/v1/outreach/recontact", { method: "POST", body: "{}" });
          announce("Contact search started.");
          await loadOutreach();
        } catch (error) {
          status.textContent = error.message;
          run.disabled = false;
        }
      });
      body.append(run, status);
    } else {
      body.appendChild(element("p", "profile-help", "The bulk search runs only for the owner on the main database. Use Find contacts on each company instead."));
    }
    panel.appendChild(body);
    return panel;
  }

  // The .env values a student would otherwise edit by hand. Each change is
  // saved at once and applies without restarting the app.
  async function jevInboxField() {
    const field = element("div", "settings-field jev-inbox-setting");
    const label = element("label", "settings-checkbox");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.id = "settings-jev-inbox";
    box.disabled = true;
    label.append(box, document.createTextNode(" Suggest reply and email outcomes with Jev"));
    const help = element("p", "profile-help");
    const status = element("p", "profile-help jev-inbox-status");
    status.setAttribute("aria-live", "polite");
    field.append(label, help, status);
    function show(setting) {
      box.checked = setting.enabled;
      box.disabled = false;
      help.textContent = setting.available
        ? `${setting.sends} Both go to TypeSafe. When Jev is unsure or unreachable, the keyword rules suggest instead, and every suggestion says which one made it. You still confirm each change.`
        : "Jev is not set up on this computer (TYPESAFE_API_KEY in .env), so the keyword rules make these suggestions. Nothing is sent anywhere.";
    }
    try {
      show(await api("/api/v1/typesafe/inbox-suggestions"));
    } catch (error) {
      help.textContent = `Jev inbox suggestions could not be checked: ${error.message}`;
      return field;
    }
    box.addEventListener("change", async () => {
      box.disabled = true;
      status.textContent = "Saving…";
      try {
        show(await api("/api/v1/typesafe/inbox-suggestions", { method: "PUT", body: JSON.stringify({ enabled: box.checked }) }));
        status.textContent = box.checked ? "Jev inbox suggestions on." : "Jev inbox suggestions off.";
      } catch (error) {
        box.checked = !box.checked;
        box.disabled = false;
        status.textContent = error.message;
      }
    });
    return field;
  }

  async function outreachSettingsPanel() {
    const panel = element("section", "tracker-detail outreach-deep-search outreach-settings");
    panel.setAttribute("aria-labelledby", "outreach-settings-heading");
    const heading = element("h2", "outreach-recontact-heading", "Outreach settings");
    heading.id = "outreach-settings-heading";
    panel.appendChild(heading);
    const body = element("div", "outreach-deep-search-body");
    panel.appendChild(body);
    // Per student and stored in the database, so it shows for every account.
    body.appendChild(await jevInboxField());
    if (state.userId !== "local-user") {
      body.appendChild(element("p", "profile-help", "These settings belong to the owner of this computer's workspace."));
      return panel;
    }
    let settings;
    try {
      settings = await api("/api/v1/outreach/settings");
    } catch (error) {
      body.appendChild(element("p", "form-error", error.message));
      return panel;
    }
    if (!settings.available) {
      body.appendChild(element("p", "profile-help", "These settings live in this computer's .env file, so only the owner's main workspace can change them."));
      return panel;
    }
    body.appendChild(element("p", "outreach-note", "Saved to this computer's .env and used right away; no restart needed. API keys stay in .env and are never shown here."));
    const status = element("p", "form-status");
    status.setAttribute("aria-live", "polite");

    async function save(change, what) {
      status.textContent = "Saving…";
      try {
        settings = await api("/api/v1/outreach/settings", { method: "PUT", body: JSON.stringify(change) });
        status.textContent = `${what} saved.`;
        renderAttachment();
      } catch (error) {
        status.textContent = error.message;
      }
    }

    function selectField(id, labelText, help) {
      const field = element("div", "settings-field");
      const label = element("label", "", labelText);
      label.htmlFor = id;
      const select = document.createElement("select");
      select.id = id;
      field.append(label, select);
      if (help) field.appendChild(element("p", "profile-help", help));
      return [field, select];
    }

    function providerOption(option, current) {
      const node = document.createElement("option");
      node.value = option.id;
      node.textContent = option.available ? option.label : `${option.label} (not set up)`;
      node.selected = option.id === current;
      return node;
    }

    const drafts = settings.draft_provider;
    const defaultLabel = drafts.options.find((option) => option.id === drafts.default)?.label || drafts.default;
    const [draftField, draftSelect] = selectField("settings-draft-provider", "Who writes drafts",
      "Every draft still waits for your approval, whoever writes it.");
    const automatic = document.createElement("option");
    automatic.value = "";
    automatic.textContent = `Automatic (now: ${defaultLabel})`;
    automatic.selected = !drafts.value;
    draftSelect.appendChild(automatic);
    drafts.options.forEach((option) => draftSelect.appendChild(providerOption(option, drafts.value)));
    const draftHint = element("p", "profile-help");
    const showDraftHint = () => {
      const chosen = drafts.options.find((option) => option.id === draftSelect.value);
      draftHint.textContent = chosen && !chosen.available ? chosen.hint : "";
    };
    showDraftHint();
    draftField.appendChild(draftHint);
    draftSelect.addEventListener("change", () => {
      showDraftHint();
      save({ draft_provider: draftSelect.value }, "Draft writer");
    });

    const research = settings.research_agent;
    const [researchField, researchSelect] = selectField("settings-research-agent", "Who does the web research",
      "Used by the deep search and Find people. It runs the CLI signed in on this computer.");
    research.options.forEach((option) => researchSelect.appendChild(providerOption(option, research.value)));
    researchSelect.addEventListener("change", () => save({ research_agent: researchSelect.value }, "Research agent"));

    const [attachField, attachSelect] = selectField("settings-attachment", "Attach to Gmail drafts",
      "A copy is attached under its original file name. Drafts are only created; you send them yourself.");
    const attachState = element("p", "profile-help");
    attachField.appendChild(attachState);
    function renderAttachment() {
      const attachment = settings.attachment;
      attachSelect.replaceChildren();
      const none = document.createElement("option");
      none.value = "";
      none.textContent = "Nothing";
      attachSelect.appendChild(none);
      const current = document.createElement("option");
      current.value = "__current";
      current.textContent = `Current: ${attachment.name}`;
      if (attachment.name) attachSelect.appendChild(current);
      attachment.resumes.forEach((resume) => {
        const node = document.createElement("option");
        node.value = resume.id;
        node.textContent = `${resume.name} (uploaded ${formatDate(resume.created_at)})`;
        attachSelect.appendChild(node);
      });
      attachSelect.value = attachment.name ? "__current" : "";
      attachState.className = attachment.problem ? "form-error" : "profile-help";
      attachState.textContent = attachment.problem
        || (attachment.resumes.length ? "" : "Upload a resume under Profile → Resumes to attach it here.");
    }
    renderAttachment();
    attachSelect.addEventListener("change", () => {
      if (attachSelect.value === "__current") return;
      save({ attachment_resume_id: attachSelect.value }, "Attachment");
    });

    body.append(draftField, researchField, attachField, status);
    return panel;
  }

  function recontactReport(result, recontact, status) {
    const wrap = element("div", "outreach-recontact-report");
    const found = result.results.filter((entry) => entry.to);
    const missed = result.results.filter((entry) => !entry.to);
    wrap.appendChild(element("p", "", `Checked ${plural(result.checked, "company", "companies")}; found a person at ${plural(found.length, "company", "companies")}.`));
    if (found.length) {
      const form = element("fieldset", "outreach-recontact-list");
      form.appendChild(element("legend", "", "Tick the contacts to use"));
      found.forEach((entry) => {
        const row = element("label", "outreach-recontact-row");
        const box = document.createElement("input");
        box.type = "checkbox";
        // Weak guesses start unticked: the student opts in to each one.
        box.checked = entry.basis !== "weak_guess";
        box.dataset.targetId = entry.target_id;
        box.dataset.to = entry.to;
        const text = element("span", "outreach-recontact-text");
        text.appendChild(element("strong", "", entry.company));
        const who = [entry.name, entry.role].filter(Boolean).join(", ");
        text.appendChild(element("span", "", `${entry.was || "No address"} → ${entry.to}${who ? ` (${who})` : ""}${entry.cc ? `, Cc ${entry.cc}` : ""}`));
        const [label, tone] = RECONTACT_BASIS_LABELS[entry.basis] || [entry.basis, ""];
        text.appendChild(chip(label, tone));
        row.append(box, text);
        form.appendChild(row);
      });

      const redraftLabel = element("label", "outreach-scope");
      const redraft = document.createElement("input");
      redraft.type = "checkbox";
      redraftLabel.append(redraft, document.createTextNode(" Rewrite unapproved drafts so they greet the new person"));
      const apply = element("button", "primary-button", "Use ticked contacts");
      apply.type = "button";
      apply.disabled = !recontact.available || recontact.active?.state === "running";
      apply.addEventListener("click", async () => {
        const choices = [...form.querySelectorAll("input:checked")].map((box) => ({ target_id: box.dataset.targetId, to: box.dataset.to }));
        if (!choices.length) {
          status.textContent = "Tick at least one contact.";
          return;
        }
        apply.disabled = true;
        try {
          await api("/api/v1/outreach/recontact/apply", { method: "POST", body: JSON.stringify({ choices, redraft: redraft.checked }) });
          announce("Updating contacts.");
          await loadOutreach();
        } catch (error) {
          status.textContent = error.message;
          apply.disabled = false;
        }
      });
      wrap.append(form, redraftLabel, apply);
    }

    if (missed.length) {
      const details = element("details", "outreach-rejected");
      details.appendChild(element("summary", "", `${plural(missed.length, "company", "companies")} with no person found`));
      const list = element("ul");
      missed.forEach((entry) => list.appendChild(element("li", "", `${entry.company}${entry.skipped ? `: ${entry.skipped}` : entry.error ? `: ${entry.error}` : ""}`)));
      details.appendChild(list);
      wrap.appendChild(details);
    }
    return wrap;
  }

  // Re-rendering replaces the card, so return focus to where the action left
  // off instead of dropping keyboard users back at the top of the page.
  function refocusOutreach(id, ...selectors) {
    const card = els.results.querySelector(`[data-outreach-id="${CSS.escape(id)}"]`);
    if (!card) return;
    const target = selectors.map((selector) => card.querySelector(selector)).find((node) => node && !node.disabled && !node.hidden)
      || card.querySelector('[role="tab"][aria-selected="true"]');
    target?.focus();
  }

  // Reloading rebuilds the pane from the server, so anything typed and not yet
  // saved would go with it. Confirming research, moving the status, applying a
  // contact and the deep-search poll have nothing to do with those words, so the
  // words come across the reload, still unsaved. An action that deliberately
  // replaces the draft text asks first and then sets outreachDiscardEdits.
  function captureUnsavedOutreachEdits() {
    if (state.outreachDiscardEdits) {
      state.outreachDiscardEdits = false;
      state.outreachPendingEdits = null;
      return;
    }
    const card = els.results.querySelector(".outreach-pane[data-outreach-id]");
    const values = {};
    card?.querySelectorAll("form [name]").forEach((control) => {
      if (control.value !== control.dataset.initial) values[control.name] = control.value;
    });
    state.outreachPendingEdits = Object.keys(values).length ? { id: card.dataset.outreachId, values } : null;
  }

  // Typing them back in leaves dataset.initial alone, so the pane still knows
  // they are unsaved: the live checks recount and the hand-off stays shut.
  function restoreUnsavedOutreachEdits(item, form) {
    const pending = state.outreachPendingEdits;
    if (pending?.id !== item.id) return;
    form.querySelectorAll("[name]").forEach((control) => {
      const value = pending.values[control.name];
      if (value === undefined || value === control.value) return;
      control.value = value;
      control.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  async function patchOutreach(item, changes, message, ...focusSelectors) {
    const saved = await api(`/api/v1/outreach/${encodeURIComponent(item.id)}`, { method: "PATCH", body: JSON.stringify(changes) });
    const askedForReply = changes.status ? afterReplyStatus(item, saved) : false;
    state.outreachOpen = item.id;
    announce(message);
    await loadOutreach();
    if (askedForReply) goToReplyBox(item.id);
    else refocusOutreach(item.id, ...focusSelectors, ".application-controls select");
  }

  const OUTREACH_STEPS = ["Research", "Contact", "Draft", "Approve", "Send", "Reply"];
  // Statuses whose follow-up date means "get back in touch then", not "send the follow-up email".
  const OUTREACH_REVISIT = ["replied", "paused"];
  // Statuses after a company writes back, when there may be a call to prepare for.
  const OUTREACH_CALL_PREP = ["replied", "call_scheduled", "offer"];

  // How far a company has come: each step counts only once everything before it holds.
  function outreachProgress(item) {
    if (["replied", "call_scheduled", "offer"].includes(item.status)) return OUTREACH_STEPS.length;
    if (item.sent_at || ["sent", "followed_up", "declined", "no_response"].includes(item.status)) return 5;
    if (item.research_confidence === "unverified") return 0;
    if (!item.contact_email || item.contact_bounced) return 1;
    if (!item.email_body) return 2;
    if (item.draft_status !== "approved") return 3;
    return 4;
  }

  // The one thing to do next, in the words the bar and the list row use.
  // `tab` is where that work happens; the bar offers to go there.
  function outreachNextStep(item) {
    if (item.revisit_due) return { label: "Get back in touch", hint: `You planned to revisit on ${formatCalendarDate(item.follow_up_at)}. Write to them, then log it here.`, tab: "history", tone: "is-warning" };
    if (item.status === "paused") {
      return item.follow_up_at
        ? { label: `Paused until ${formatCalendarDate(item.follow_up_at)}`, hint: "It comes back under Revisits due on that day.", tab: null }
        : { label: "Paused", hint: "Set a date to get back in touch, or pick a status to pick this company back up.", tab: null };
    }
    if (["declined", "no_response"].includes(item.status)) return { label: "Closed", hint: "Nothing left to do unless they write back.", tab: "history" };
    if (OUTREACH_CALL_PREP.includes(item.status) && !item.call_prep) {
      if (CALL_PREP_ACTIVE.includes(item.call_prep_job?.state)) {
        return { label: "Writing call prep", hint: "In the background. It keeps going if you leave this page.", tab: "prep", tone: "is-region" };
      }
      if (!item.reply_count) {
        return { label: "Log their reply", hint: "Paste their reply. Call prep is written from it, and starts as soon as it is logged.", tab: "history", tone: "is-soon" };
      }
      return { label: "Prep for the call", hint: "Write call prep: what they do, talking points, what you can bring, and questions to ask.", tab: "prep", tone: "is-region" };
    }
    if (OUTREACH_CALL_PREP.includes(item.status)) {
      const revisit = item.status === "replied" && item.follow_up_at ? ` Revisit on ${formatCalendarDate(item.follow_up_at)}.` : "";
      return { label: "Keep the conversation going", hint: `Log each reply so the history stays complete.${revisit}`, tab: "history" };
    }
    if (item.follow_up_due) {
      return item.follow_up_status === "approved"
        ? { label: "Send the follow-up", hint: "It is approved; open it in your email and send it.", tab: null, tone: "is-warning" }
        : { label: "Follow up now", hint: "The follow-up date has passed. Draft and approve a short follow-up.", tab: "draft", tone: "is-warning" };
    }
    if (item.status === "sent" || item.status === "followed_up") {
      return { label: "Wait for a reply", hint: item.follow_up_at ? `Follow up on ${formatCalendarDate(item.follow_up_at)} if nothing arrives.` : "Log their reply here when it arrives.", tab: "history" };
    }
    if (item.contact_bounced) return { label: "Find a new contact", hint: `Your email to ${item.contact_email} bounced, so it reached no one. Pick another address; the greeting updates to match.`, tab: "contact", tone: "is-warning" };
    if (item.cc_bounced) return { label: "Fix the Cc", hint: `Email to ${item.contact_cc} bounced. Remove it or pick another before sending.`, tab: "contact", tone: "is-warning" };
    if (item.research_confidence === "unverified") return { label: "Confirm the research", hint: "The deep search summarized this company. Check the sources, then confirm.", tab: "research", tone: "is-soon" };
    if (!item.contact_email) return { label: "Find a contact", hint: "Search their site for a published address, or add one you found.", tab: "contact", tone: "is-soon" };
    if (!item.email_body) return { label: "Write the draft", hint: "Generate a draft from your confirmed profile and this research.", tab: "draft" };
    if (outreachDraftNeedsReview(item, "initial") || item.draft_status !== "approved") return { label: "Review the draft", hint: "Read it, fix anything, then approve it. Approving unlocks the email hand-off.", tab: "draft", tone: "is-soon" };
    return { label: "Send it from your email", hint: "Open the approved draft in your email, send it, then mark it sent here.", tab: null, tone: "is-region" };
  }

  const OUTREACH_PANE_TABS = [
    ["draft", "Draft"],
    ["research", "Research"],
    ["contact", "Contact"],
    ["timing", "Timing"],
    ["history", "Replies and history"],
  ];

  // Call prep appears once a company writes back, and stays while it holds notes.
  function outreachPaneTabs(item) {
    return OUTREACH_CALL_PREP.includes(item.status) || item.call_prep
      ? [["prep", "Call prep"], ...OUTREACH_PANE_TABS]
      : OUTREACH_PANE_TABS;
  }

  // The tab a company opens on: the one you last used for it, else where its next step lives.
  function outreachPaneTab(item) {
    const tabs = outreachPaneTabs(item).map(([id]) => id);
    const remembered = state.outreachTabs[item.id];
    if (remembered && tabs.includes(remembered)) return remembered;
    if (tabs.includes("prep")) return "prep";
    return item.contact_email ? "draft" : "contact";
  }

  // Moving a company to a reply status opens its call prep, the next thing to do.
  // Call prep is written from their reply, so with none logged it asks for it
  // first. Returns whether the student chose to paste it now.
  function afterReplyStatus(item, saved) {
    if (!OUTREACH_CALL_PREP.includes(saved.status) || OUTREACH_CALL_PREP.includes(item.status)) return false;
    state.outreachTabs[item.id] = "prep";
    return saved.reply_count ? false : askForReply(item);
  }

  // A native popup, like the research warning before approving a draft.
  function askForReply(item) {
    const ok = window.confirm(
      `Call prep for ${item.company} is written from their reply, and no reply is logged yet.\n\n`
      + "Paste their reply now? It starts writing as soon as you log it."
    );
    if (ok) state.outreachTabs[item.id] = "history";
    return ok;
  }

  // The Call prep tab was drawn before the job started; say it is writing now.
  function showCallPrepWriting(card) {
    const button = card?.querySelector("[data-call-prep-generate]");
    if (!button || card.querySelector("[data-call-prep-status]")) return;
    button.disabled = true;
    button.textContent = "Writing call prep…";
    const status = element("p", "outreach-note", CALL_PREP_WRITING);
    status.dataset.callPrepStatus = "queued";
    button.closest(".outreach-draft-buttons").after(status);
  }

  function goToReplyBox(id) {
    const card = els.results.querySelector(`[data-outreach-id="${CSS.escape(id)}"]`);
    if (!card) return;
    selectOutreachPaneTab(card, "history");
    card.querySelector(`#outreach-reply-${CSS.escape(id)}`)?.focus();
  }

  // Call prep is written by a background job on the server. While one runs,
  // its company is checked every few seconds and the list reloads when it
  // ends. The job's state is on the server, so a reload or a laptop waking up
  // finds it again; a hidden tab is checked the moment it is shown.
  const CALL_PREP_ACTIVE = ["queued", "running", "retry"];
  const CALL_PREP_WRITING = "Writing call prep in the background. It keeps going if you leave this page, and picks back up if your laptop sleeps or the app restarts.";
  const callPrepWatches = new Map();

  function watchCallPrep(id, delay = 5000) {
    if (callPrepWatches.has(id) && delay) return;
    window.clearTimeout(callPrepWatches.get(id));
    callPrepWatches.set(id, window.setTimeout(() => checkCallPrep(id), delay));
  }

  async function checkCallPrep(id) {
    let active = true;
    try {
      const target = await api(`/api/v1/outreach/${encodeURIComponent(id)}`);
      active = CALL_PREP_ACTIVE.includes(target.call_prep_job?.state);
    } catch (error) {
      active = error.status !== 404;
    }
    if (active) {
      callPrepWatches.set(id, window.setTimeout(() => checkCallPrep(id), 5000));
      return;
    }
    callPrepWatches.delete(id);
    if (state.view !== "outreach") return;
    // Unsaved words in the pane come across the reload.
    state.outreachOpen = id;
    await loadOutreach();
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") [...callPrepWatches.keys()].forEach((id) => watchCallPrep(id, 0));
  });

  function selectOutreachPaneTab(pane, id, { focus = false, remember = true } = {}) {
    if (remember) state.outreachTabs[pane.dataset.outreachId] = id;
    pane.querySelectorAll("[data-go-tab]").forEach((button) => { button.hidden = button.dataset.goTab === id; });
    pane.querySelectorAll('[role="tab"]').forEach((tab) => {
      const on = tab.dataset.paneTab === id;
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
      tab.classList.toggle("is-active", on);
      if (on && focus) tab.focus();
    });
    pane.querySelectorAll('[role="tabpanel"]').forEach((panel) => { panel.hidden = panel.dataset.paneTab !== id; });
  }

  function outreachRow(item, selected, onPick) {
    const row = element("button", `outreach-row${selected ? " is-selected" : ""}`);
    row.type = "button";
    row.dataset.rowId = item.id;
    row.setAttribute("aria-pressed", String(selected));
    const top = element("span", "outreach-row-top");
    top.append(
      element("strong", "outreach-row-company", item.company),
      element("span", "outreach-row-meta", [item.location_region || item.location, item.priority].filter(Boolean).join(" · "))
    );
    const contact = element("span", "outreach-row-contact");
    const health = item.contact_email ? (item.contact_confidence === "confirmed" ? "is-good" : "is-soon") : "is-bad";
    contact.append(
      element("span", `outreach-dot ${health}`),
      document.createTextNode(item.contact_email
        ? `${item.contact_name || item.contact_email} · ${item.contact_confidence === "confirmed" ? "confirmed" : "unverified"}`
        : "No contact yet")
    );
    const next = outreachNextStep(item);
    const bottom = element("span", "outreach-row-bottom");
    bottom.append(chip(next.label, next.tone || ""), element("span", "outreach-row-status", OUTREACH_STATUS_LABELS[item.status] || item.status));
    row.append(top, contact, bottom);
    if (item.tags?.length) {
      row.appendChild(element("span", "outreach-row-tags", item.tags.map((tag) => `#${tag.tag}`).join(" ")));
    }
    row.addEventListener("click", onPick);
    return row;
  }

  // "Hit us up in January": a date on a paused or replied company that brings it back.
  function outreachRevisitControl(item) {
    const wrap = element("label", "outreach-revisit");
    wrap.appendChild(element("span", "", "Revisit on"));
    const input = document.createElement("input");
    input.type = "date";
    input.className = "outreach-revisit-date";
    input.value = item.follow_up_at || "";
    input.addEventListener("change", async () => {
      if (input.value === (item.follow_up_at || "")) return;
      input.disabled = true;
      try {
        await patchOutreach(
          item,
          { follow_up_at: input.value },
          input.value ? `${item.company} comes back on ${formatCalendarDate(input.value)}.` : `Cleared the revisit date for ${item.company}.`,
          ".outreach-revisit-date"
        );
      } catch (error) {
        input.value = item.follow_up_at || "";
        input.disabled = false;
        showError(error.message);
      }
    });
    wrap.appendChild(input);
    return wrap;
  }

  function outreachSummaryCard(title, lines) {
    const card = element("section", "outreach-aside-card");
    card.appendChild(element("p", "eyebrow", title));
    lines.filter(Boolean).forEach(([text, className = ""]) => card.appendChild(element("p", className, text)));
    return card;
  }

  const LOCATION_BASIS_LABELS = {
    manual: "your entry",
    company_site: "the company's site",
    sec_form_d: "an SEC Form D filing",
    web_search: "a web search",
    research: "the deep search",
  };

  function outreachSourceLink(label, url) {
    const safe = safeExternalUrl(url);
    if (!safe) return document.createTextNode(label);
    const link = element("a", "", `${label} ↗`);
    link.href = safe;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  }

  // Where the company is decides whether the draft says you can be there in person,
  // so an unknown location is said plainly instead of left blank, and a location
  // nothing has checked says so.
  function outreachLocationLine(item) {
    if (!item.location) return element("p", "outreach-location is-missing", "Location not recorded. Add it under Research.");
    const line = element("p", "outreach-location");
    line.appendChild(element("strong", "", "Based in "));
    line.appendChild(document.createTextNode(item.location));
    if (item.location_region && !item.location.toLowerCase().includes(item.location_region.toLowerCase())) {
      line.appendChild(element("span", "outreach-location-region", item.location_region));
    }
    // The caveat is driven by location_verified alone. Keeping it inside a
    // "did we find a label" test is what let a location with no recorded basis
    // render as a bare, confirmed-looking "Based in Austin, TX" — and no basis
    // is exactly what an imported location now carries.
    const basis = LOCATION_BASIS_LABELS[item.location_basis];
    const source = element("span", `outreach-location-source${item.location_verified ? "" : " is-unverified"}`);
    if (basis) {
      source.append("from ", outreachSourceLink(basis, item.location_source_url));
    } else if (item.origin === "import") {
      // An import file's word, with no page behind it, so nothing to link to.
      source.append("from an import file");
    }
    if (item.location_inferred) source.append(", the only place it names");
    if (!item.location_verified) source.append(source.textContent ? ", not yet checked" : "not yet checked");
    if (source.textContent) line.appendChild(source);
    if (!item.location_verified) line.appendChild(outreachConfirmLocationButton(item));
    return line;
  }

  // Vouching for a location lets drafts rely on it. Typing a different one under
  // Research does the same for the new place.
  function outreachConfirmLocationButton(item) {
    const button = element("button", "text-button outreach-confirm-location", "Confirm location");
    button.type = "button";
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        // Send the place actually on screen. A background locator or profile
        // pass can change it between this render and the click, and "manual"
        // is permanent, so the server refuses rather than attaching the
        // student's name to a place they never saw.
        await api(`/api/v1/outreach/${encodeURIComponent(item.id)}`, {
          method: "PATCH",
          body: JSON.stringify({ confirm_location: item.location }),
        });
        state.outreachOpen = item.id;
        announce(`Confirmed ${item.company} is in ${item.location}.`);
        await loadOutreach();
      } catch (error) {
        showError(error.message);
        button.disabled = false;
        // It moved under them: show what it says now rather than a stale row.
        if (error.status === 409) await loadOutreach();
      }
    });
    return button;
  }

  function formatDollars(value) {
    if (typeof value !== "number") return "";
    if (value >= 1e6) return `$${(value / 1e6).toFixed(1)}M`;
    if (value >= 1e3) return `$${Math.round(value / 1e3)}K`;
    return `$${value}`;
  }

  // A Form D is matched by company name alone, so an uncertain match is labeled as one.
  function outreachFormDLine(item) {
    const record = item.sec_form_d;
    if (!record || !["found", "mismatch", "ambiguous"].includes(record.status)) return null;
    const line = element("p", `outreach-form-d${record.status === "found" ? "" : " is-uncertain"}`);
    line.appendChild(element("strong", "", "SEC Form D: "));
    if (record.status === "ambiguous") {
      line.append(`several issuers are named ${item.company}, so none is shown.`);
      return line;
    }
    const sold = formatDollars(record.total_sold);
    const offered = formatDollars(record.total_offering);
    const amount = sold ? `${sold} sold${offered && offered !== sold ? ` of ${offered} offered` : ""}, ` : "";
    line.append(`${amount}filed ${formatCalendarDate(record.filed_at)}. `);
    line.appendChild(outreachSourceLink("Filing", record.url));
    if (record.status === "mismatch") {
      line.append(` It lists ${record.location}, not ${record.mismatch_with}, so it may be another company with the same name.`);
    }
    return line;
  }

  // Notes for the call once a company writes back. The text is part of the
  // pane's form, so Save keeps hand edits; writing new prep replaces it, and the
  // server keeps the replaced notes in the history.
  function outreachCallPrep(item) {
    const group = element("fieldset", "outreach-group is-prep");
    group.appendChild(element("legend", "", "Call prep"));
    group.appendChild(element("p", "outreach-note is-wide", item.call_prep
      ? "Edit freely and fill in the blanks on the call. Save changes keeps your edits."
      : item.reply_count
        ? "Call prep covers what they do, what they said, talking points, what you can bring, and questions to ask. It starts on its own when a company replies."
        : "Call prep is written from their reply. Paste it under Replies and history, and the notes start writing as soon as it is logged."));
    const notes = outreachField(group, "Notes", "call_prep", item.call_prep || "", { multiline: true, wide: true });
    notes.rows = 24;
    notes.placeholder = "Call prep notes appear here. You can also write your own.";
    const assistant = element("div", "outreach-draft-assistant is-wide");
    const buttons = element("div", "outreach-draft-buttons");
    const job = item.call_prep_job;
    const active = CALL_PREP_ACTIVE.includes(job?.state);
    const generate = element("button", "primary-button", active ? "Writing call prep…" : item.call_prep ? "Rewrite call prep" : "Write call prep");
    generate.type = "button";
    generate.disabled = active;
    generate.dataset.callPrepGenerate = "";
    const copy = element("button", "secondary-button", "Copy notes");
    copy.type = "button";
    const message = element("p", "form-status");
    message.setAttribute("aria-live", "polite");
    generate.addEventListener("click", async () => {
      if (!item.reply_count) {
        if (askForReply(item)) goToReplyBox(item.id);
        return;
      }
      const unsaved = notes.value !== notes.dataset.initial;
      if (unsaved && !window.confirm("Replace your unsaved notes with new call prep? Save changes first to keep them in the history.")) return;
      generate.disabled = true;
      try {
        await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/call-prep`, { method: "POST" });
        state.outreachOpen = item.id;
        state.outreachDiscardEdits = true;
        announce(`Writing call prep for ${item.company} in the background.${item.call_prep ? " The earlier notes will be in the history." : ""}`);
        await loadOutreach();
        refocusOutreach(item.id, "[data-call-prep-status]");
      } catch (error) {
        generate.disabled = false;
        // The server's own check: no reply logged, so ask for it.
        if (error.status === 409) {
          if (askForReply(item)) goToReplyBox(item.id);
          return;
        }
        message.textContent = error.message;
      }
    });
    copy.addEventListener("click", async () => {
      if (!notes.value.trim()) {
        message.textContent = "Nothing to copy yet.";
        return;
      }
      try {
        await copyText(notes.value);
        announce(`Copied the ${item.company} call prep.`);
      } catch (error) {
        showError(error.message);
      }
    });
    buttons.append(generate, copy);
    assistant.append(buttons, message);
    if (job && job.state !== "succeeded") {
      const when = job.next_attempt_at ? formatDate(job.next_attempt_at) : "soon";
      const text = {
        queued: CALL_PREP_WRITING,
        running: CALL_PREP_WRITING,
        retry: `The last try did not finish (${job.error || "no reason given"}). Trying again ${when}.`,
        dead: `Could not write call prep after ${job.attempts} tries: ${job.error || "no reason given"}. Press the button to try again.`,
        cancelled: "The last call prep request was cancelled.",
      }[job.state];
      if (text) {
        const status = element("p", `outreach-note${job.state === "dead" ? " form-error" : ""}`, text);
        status.dataset.callPrepStatus = job.state;
        status.tabIndex = -1;
        status.setAttribute("aria-live", "polite");
        assistant.appendChild(status);
      }
    }
    if (active) watchCallPrep(item.id);
    const claims = item.call_prep_claims || [];
    if (claims.length) {
      const details = element("details", "outreach-claims");
      details.appendChild(element("summary", "", `What these notes are based on (${claims.length})`));
      if (item.call_prep_generated_by && item.call_prep_generated_by !== "template") {
        details.appendChild(element("p", "outreach-note", "The model's own citations. Lines marked (my guess) are its inference about the fit, not facts."));
      }
      const list = element("ul");
      claims.forEach((claim) => {
        const row = element("li");
        row.appendChild(element("span", "", claim.text));
        const sourceHref = safeExternalUrl(claim.basis);
        if (sourceHref) {
          const source = element("a", "", "source ↗");
          source.href = sourceHref;
          source.target = "_blank";
          source.rel = "noopener noreferrer";
          row.appendChild(source);
        } else {
          const [where, field] = claim.basis.split(":");
          const label = {
            profile: "your profile",
            research: "research",
            unverified: "unverified deep-search research",
            reply: "their reply",
            sent_email: "the email you sent",
            inference: "inference, not from your profile or research",
          }[where] || claim.basis;
          row.appendChild(element("small", "", `${label}${field ? `: ${field.replace(/_/g, " ")}` : ""}`));
        }
        list.appendChild(row);
      });
      details.appendChild(list);
      assistant.appendChild(details);
    }
    if (item.call_prep_generated_by) {
      const provider = item.call_prep_generated_by.split(":")[0];
      const who = item.call_prep_generated_by === "template"
        ? "Built from your saved profile and this research, with no model."
        : `Written by ${DRAFT_PROVIDER_LABELS[provider] || provider}.`;
      const when = item.call_prep_generated_at ? ` ${formatDate(item.call_prep_generated_at)}.` : "";
      assistant.appendChild(element("p", "outreach-note", `${who}${when} Check it against their reply before the call.`));
    }
    group.appendChild(assistant);
    return group;
  }

  function createOutreachCard(item, context = {}) {
    const card = element("article", "application-card outreach-card outreach-pane");
    card.dataset.outreachId = item.id;
    if (item.follow_up_due || item.revisit_due) card.classList.add("is-due");

    const head = element("div", "outreach-pane-head");
    const heading = element("div", "application-heading");
    const identity = element("div");
    identity.appendChild(element("p", "company-name", [item.channel, item.priority].filter(Boolean).join(" · ")));
    identity.appendChild(element("h3", "", item.company));
    identity.appendChild(outreachLocationLine(item));
    const formD = outreachFormDLine(item);
    if (formD) identity.appendChild(formD);
    if (item.summary) identity.appendChild(element("p", "outreach-summary", item.summary));
    if (item.research_confidence === "unverified") {
      identity.appendChild(element("p", "outreach-research-warning", "Summarized by the deep search; check the linked sources, then confirm the research."));
    }
    if (item.reply_suggestion) {
      const reply = item.reply_suggestion;
      const label = OUTREACH_STATUS_LABELS[reply.status] || reply.status;
      identity.appendChild(element("p", "outreach-fit", `${reply.from || "They"} replied ${formatDate(reply.received_at)}, found in Gmail. It reads as ${label}: ${reply.reason}.`));
    }
    if (item.bounced_at) {
      const failed = item.bounced_addresses.join(", ");
      const why = item.bounce_reason ? ` Gmail said: "${item.bounce_reason}"` : "";
      identity.appendChild(element("p", "outreach-research-warning", `Bounced ${formatDate(item.bounced_at)}: nothing reached ${failed}.${why}`));
    }
    if (item.fit_rationale) {
      const fit = element("p", "outreach-fit");
      fit.appendChild(element("strong", "", "Fit: "));
      fit.appendChild(document.createTextNode(item.fit_rationale));
      identity.appendChild(fit);
    }
    const side = element("div", "outreach-head-side");
    side.appendChild(chip(OUTREACH_STATUS_LABELS[item.status] || item.status, item.status === "replied" || item.status === "call_scheduled" || item.status === "offer" ? "is-region" : ""));
    const link = safeExternalUrl(item.website) || item.source_urls.map(safeExternalUrl).find(Boolean);
    if (link) {
      const site = element("a", "secondary-button", "Research source ↗");
      site.href = link;
      site.target = "_blank";
      site.rel = "noopener noreferrer";
      side.appendChild(site);
    }
    heading.append(identity, side);

    const facts = element("div", "application-facts");
    // A shared inbox has no person, so the address itself says who hears from you.
    const contactText = [item.contact_name, item.contact_role].filter(Boolean).join(", ") || item.contact_email;
    if (contactText) facts.appendChild(chip(contactText));
    facts.appendChild(chip(
      item.contact_confidence === "unknown" && !item.contact_email && !item.contact_name
        ? "No contact yet"
        : CONTACT_CONFIDENCE_LABELS[item.contact_confidence],
      item.contact_confidence === "confirmed" ? "is-region" : item.contact_confidence === "unverified" ? "is-soon" : "is-warning"
    ));
    if (item.deadline_date) facts.appendChild(chip(`Deadline ${formatCalendarDate(item.deadline_date)}`, "is-soon"));
    else if (item.deadline_label) facts.appendChild(chip(item.deadline_label));
    if (item.follow_up_at) {
      const verb = OUTREACH_REVISIT.includes(item.status)
        ? (item.revisit_due ? "Revisit due" : "Revisit")
        : item.status === "followed_up" ? "Followed up" : item.follow_up_due ? "Follow-up due" : "Follow up";
      facts.appendChild(chip(
        `${verb} ${formatCalendarDate(item.follow_up_at)}`,
        item.follow_up_due || item.revisit_due ? "is-warning" : "is-region"
      ));
    }
    if (item.sent_at) facts.appendChild(chip(`Sent ${formatCalendarDate(item.sent_at)}`));
    if (item.bounced_at) facts.appendChild(chip("Bounced", "is-warning"));
    // Part of the email still arrived, so the company stays where it was.
    else if (item.contact_bounced || item.cc_bounced) facts.appendChild(chip(item.contact_bounced ? "Contact address bounced" : "Cc bounced", "is-warning"));
    if (item.draft_status === "approved") facts.appendChild(chip(...DRAFT_STATUS_LABELS.approved));
    else if (outreachDraftNeedsReview(item, "initial")) facts.appendChild(chip(...DRAFT_STATUS_LABELS.generated));
    if (outreachDraftNeedsReview(item, "follow_up")) facts.appendChild(chip("Follow-up needs review", "is-soon"));
    if (item.origin === "discovery") facts.appendChild(chip("From deep search"));
    if (item.research_confidence === "unverified") facts.appendChild(chip("Research unverified", "is-soon"));

    const reached = outreachProgress(item);
    const steps = element("ol", "outreach-steps");
    steps.setAttribute("aria-label", "Progress");
    OUTREACH_STEPS.forEach((label, index) => {
      const done = index < reached;
      const now = index === reached;
      const step = element("li", `outreach-step${done ? " is-done" : now ? " is-now" : ""}`);
      step.appendChild(element("span", "outreach-step-mark", done ? "✓" : String(index + 1))).setAttribute("aria-hidden", "true");
      step.appendChild(element("span", "outreach-step-label", label));
      step.appendChild(element("span", "sr-only", done ? ", done" : now ? ", next" : ""));
      if (now) step.setAttribute("aria-current", "step");
      steps.appendChild(step);
    });
    head.append(heading, facts, companyTagsSection(item, { outreach: true }), steps);

    // The next-step bar: what to do now, the status, and every hand-off action.
    const next = outreachNextStep(item);
    const controls = element("div", `application-controls outreach-next${next.tone ? ` ${next.tone}` : ""}`);
    const nextText = element("div", "outreach-next-text");
    nextText.append(element("strong", "", `Next: ${next.label}`), element("span", "", next.hint));
    const label = element("label", "");
    label.appendChild(element("span", "", "Status"));
    const select = document.createElement("select");
    Object.entries(OUTREACH_STATUS_LABELS).forEach(([value, text]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      option.selected = item.status === value;
      select.appendChild(option);
    });
    autoSaveSelect(select, {
      saved: () => item.status,
      commit: async (status, trigger) => {
        const previous = item.status;
        select.disabled = true;
        try {
          const saved = await api(`/api/v1/outreach/${encodeURIComponent(item.id)}`, {
            method: "PATCH",
            body: JSON.stringify({ status }),
          });
          const askedForReply = afterReplyStatus(item, saved);
          state.outreachKeep.add(item.id);
          state.outreachSelected = item.id;
          await loadOutreach();
          if (askedForReply) goToReplyBox(item.id);
          else if (trigger !== "blur") els.results.querySelector(`[data-outreach-id="${CSS.escape(item.id)}"] .application-controls select`)?.focus();
          announce(`${item.company} marked ${OUTREACH_STATUS_LABELS[status]}.`);
        } catch (error) {
          select.value = previous;
          showError(error.message);
        } finally {
          select.disabled = false;
        }
      },
    });
    label.appendChild(select);
    const actions = element("div", "tracker-exports");
    const paneTabs = outreachPaneTabs(item);
    const tabNames = Object.fromEntries(paneTabs);
    if (next.tab) {
      const go = element("button", "secondary-button", `Go to ${tabNames[next.tab].toLowerCase()}`);
      go.type = "button";
      go.dataset.goTab = next.tab;
      go.addEventListener("click", () => selectOutreachPaneTab(card, next.tab, { focus: true }));
      actions.appendChild(go);
    }
    if (item.email_body && item.draft_status === "approved") {
      const copy = element("button", "secondary-button", "Copy draft");
      copy.type = "button";
      copy.addEventListener("click", async () => {
        if (refuseUnsavedHandOff(copy, "initial")) return;
        try {
          await copyText(item.email_subject ? `Subject: ${item.email_subject}\n\n${item.email_body}` : item.email_body);
          announce(`Copied the ${item.company} draft.`);
        } catch (error) {
          showError(error.message);
        }
      });
      actions.appendChild(copy);
    }
    if (item.follow_up_body && item.follow_up_status === "approved") {
      const copyFollowUp = element("button", "secondary-button", "Copy follow-up");
      copyFollowUp.type = "button";
      copyFollowUp.addEventListener("click", async () => {
        if (refuseUnsavedHandOff(copyFollowUp, "follow_up")) return;
        try {
          await copyText(item.follow_up_subject ? `Subject: ${item.follow_up_subject}\n\n${item.follow_up_body}` : item.follow_up_body);
          announce(`Copied the ${item.company} follow-up.`);
        } catch (error) {
          showError(error.message);
        }
      });
      actions.appendChild(copyFollowUp);
    }
    if (item.research_confidence === "unverified") {
      const confirmResearch = element("button", "secondary-button", "Confirm research");
      confirmResearch.type = "button";
      confirmResearch.addEventListener("click", async () => {
        confirmResearch.disabled = true;
        try {
          await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/confirm-research`, { method: "POST" });
          state.outreachOpen = item.id;
          announce(`Confirmed the research for ${item.company}.`);
          await loadOutreach();
        } catch (error) {
          showError(error.message);
          confirmResearch.disabled = false;
        }
      });
      actions.appendChild(confirmResearch);
    }
    const awaitingReply = item.status === "sent" || item.status === "followed_up";
    const deliverable = !item.contact_bounced && !item.cc_bounced;
    if (item.contact_email && deliverable && item.draft_status === "approved" && !awaitingReply && !["replied", "call_scheduled", "offer", "declined", "no_response"].includes(item.status)) {
      actions.appendChild(composeControl(context, item, "initial"));
      const sent = element("button", "secondary-button", "I sent it");
      sent.type = "button";
      sent.addEventListener("click", async () => {
        sent.disabled = true;
        try {
          await patchOutreach(item, { status: "sent" }, `${item.company} marked sent. A follow-up is set for a week from today.`);
        } catch (error) {
          showError(error.message);
          sent.disabled = false;
        }
      });
      actions.appendChild(sent);
    }
    // One follow-up per company; after it, the card suggests No response in time.
    if (item.contact_email && deliverable && item.follow_up_status === "approved" && item.status === "sent") {
      actions.appendChild(composeControl(context, item, "follow_up"));
      const followedUp = element("button", "secondary-button", "I sent the follow-up");
      followedUp.type = "button";
      followedUp.addEventListener("click", async () => {
        followedUp.disabled = true;
        try {
          await patchOutreach(item, { status: "followed_up" }, `${item.company} marked followed up.`);
        } catch (error) {
          showError(error.message);
          followedUp.disabled = false;
        }
      });
      actions.appendChild(followedUp);
    }
    if (item.suggestion) {
      const suggest = element("button", "secondary-button", `Mark ${OUTREACH_STATUS_LABELS[item.suggestion.status]}?`);
      suggest.type = "button";
      suggest.title = item.suggestion.reason;
      suggest.addEventListener("click", async () => {
        try {
          await patchOutreach(item, { status: item.suggestion.status }, `${item.company} marked ${OUTREACH_STATUS_LABELS[item.suggestion.status]}.`);
        } catch (error) {
          showError(error.message);
        }
      });
      actions.appendChild(suggest);
      if (item.reply_suggestion) {
        const dismiss = element("button", "secondary-button", "Not that");
        dismiss.type = "button";
        dismiss.title = "Keep the status as it is and drop this suggestion";
        dismiss.addEventListener("click", async () => {
          dismiss.disabled = true;
          try {
            await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/reply-suggestion`, { method: "DELETE" });
            state.outreachOpen = item.id;
            await loadOutreach();
            announce(`Dropped the suggestion; ${item.company} stays ${OUTREACH_STATUS_LABELS[item.status] || item.status}.`);
          } catch (error) {
            showError(error.message);
            dismiss.disabled = false;
          }
        });
        actions.appendChild(dismiss);
      }
    }
    controls.append(nextText, label, ...(OUTREACH_REVISIT.includes(item.status) ? [outreachRevisitControl(item)] : []), actions);

    // Tabs split the long form; one form still spans them, so Save keeps every edit.
    const tablist = element("div", "outreach-tabs");
    tablist.setAttribute("role", "tablist");
    tablist.setAttribute("aria-label", `${item.company} details`);
    paneTabs.forEach(([id, text]) => {
      const tab = element("button", "outreach-tab", text);
      tab.type = "button";
      tab.id = `outreach-tab-${item.id}-${id}`;
      tab.dataset.paneTab = id;
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-controls", `outreach-panel-${item.id}-${id}`);
      tab.addEventListener("click", () => selectOutreachPaneTab(card, id));
      tablist.appendChild(tab);
    });
    // Arrow keys move between tabs, as a tab list should.
    tablist.addEventListener("keydown", (event) => {
      const order = paneTabs.map(([id]) => id);
      const current = order.indexOf(tablist.querySelector('[aria-selected="true"]')?.dataset.paneTab);
      const moves = { ArrowRight: 1, ArrowLeft: -1, Home: -current, End: order.length - 1 - current };
      if (!(event.key in moves)) return;
      event.preventDefault();
      selectOutreachPaneTab(card, order[(current + moves[event.key] + order.length) % order.length], { focus: true });
    });

    const panel = (id) => {
      const node = element("div", `outreach-panel is-${id}`);
      node.id = `outreach-panel-${item.id}-${id}`;
      node.dataset.paneTab = id;
      node.setAttribute("role", "tabpanel");
      node.setAttribute("aria-labelledby", `outreach-tab-${item.id}-${id}`);
      return node;
    };

    const form = element("form", "outreach-form");
    const research = element("fieldset", "outreach-group");
    research.appendChild(element("legend", "", "Research"));
    outreachField(research, "What they build", "summary", item.summary, { multiline: true, wide: true });
    outreachField(research, "Fit rationale", "fit_rationale", item.fit_rationale, { multiline: true, wide: true });
    outreachField(research, "Hiring or activity signal", "activity_signal", item.activity_signal, { multiline: true, wide: true });
    outreachField(research, "Website", "website", item.website, { type: "url" });
    outreachField(research, "Based in", "location", item.location, { placeholder: "San Francisco, CA" });
    outreachField(research, "Researched on", "researched_at", item.researched_at, { type: "date" });
    outreachField(research, "Source URLs (one per line)", "source_urls", item.source_urls.join("\n"), { multiline: true, wide: true });

    const contact = element("fieldset", "outreach-group");
    contact.appendChild(element("legend", "", "Contact"));
    outreachField(contact, "Name", "contact_name", item.contact_name);
    outreachField(contact, "Role", "contact_role", item.contact_role);
    outreachField(contact, "Email", "contact_email", item.contact_email, { type: "email" });
    outreachField(contact, "Cc", "contact_cc", item.contact_cc || "", { type: "email" });
    outreachField(contact, "LinkedIn", "contact_linkedin", item.contact_linkedin);
    outreachChoice(contact, "Confidence", "contact_confidence", item.contact_confidence, Object.entries(CONTACT_CONFIDENCE_LABELS));
    outreachField(contact, "How to reach them", "contact_route", item.contact_route, { multiline: true, wide: true });

    const timing = element("fieldset", "outreach-group");
    timing.appendChild(element("legend", "", "Timing"));
    outreachChoice(timing, "Priority", "priority", item.priority, [["P1", "P1"], ["P2", "P2"], ["P3", "P3"]]);
    outreachField(timing, "Channel", "channel", item.channel, { placeholder: "Y Combinator, a local incubator…" });
    outreachField(timing, "Deadline note", "deadline_label", item.deadline_label, { placeholder: "Rolling, not posted yet…" });
    outreachField(timing, "Confirmed deadline", "deadline_date", item.deadline_date, { type: "date" });
    outreachField(timing, "Sent on", "sent_at", item.sent_at, { type: "date" });
    outreachField(timing, OUTREACH_REVISIT.includes(item.status) ? "Revisit on" : "Follow up on", "follow_up_at", item.follow_up_at, { type: "date" });

    const draft = element("fieldset", "outreach-group is-draft");
    draft.appendChild(element("legend", "", "Cold email draft"));
    if (item.contact_email) {
      const to = element("p", "outreach-to is-wide");
      to.append(element("strong", "", "To "), document.createTextNode(item.contact_name ? `${item.contact_name} <${item.contact_email}>` : item.contact_email));
      draft.appendChild(to);
      if (item.contact_cc) {
        const cc = element("p", "outreach-to is-wide");
        cc.append(element("strong", "", "Cc "), document.createTextNode(item.contact_cc));
        draft.appendChild(cc);
      }
      if (item.contact_confidence === "unverified") {
        draft.appendChild(element("p", "outreach-note outreach-guess is-wide", item.contact_cc
          ? `${item.contact_email} is a guessed address, not confirmed. ${item.contact_cc} is in Cc, so a wrong guess still reaches the company.`
          : `${item.contact_email} is a guessed address, not confirmed. Check it before you send.`));
      }
      if (item.contact_bounced) {
        draft.appendChild(element("p", "outreach-note outreach-guess is-wide", `Email to ${item.contact_email} bounced, so nothing more goes there. Pick another contact under Contact; the greeting updates to match.`));
      }
    }
    const subject = outreachField(draft, "Subject", "email_subject", item.email_subject, { wide: true });
    const body = outreachField(draft, "Body", "email_body", item.email_body, { multiline: true, wide: true });
    body.rows = 12;
    const checks = element("div", "outreach-checks");
    checks.setAttribute("aria-live", "polite");
    const refreshChecks = () => renderDraftChecks(checks, subject.value, body.value, item.sent_at ? null : item.draft_location);
    subject.addEventListener("input", refreshChecks);
    body.addEventListener("input", refreshChecks);
    refreshChecks();
    draft.appendChild(checks);
    draftAssistant(draft, item, "initial", subject, body);
    draft.appendChild(element("p", "outreach-note", context.gmail?.connected
      ? "Nothing sends on its own. Approving a draft unlocks Send, which asks you to confirm the recipient before the email goes out from your Gmail."
      : "Nothing sends from here. Approving a draft unlocks a link that opens it in your own email, where you press Send."));

    let followUpGroup = null;
    if (item.status === "sent" || item.status === "followed_up" || item.follow_up_body) {
      followUpGroup = element("fieldset", "outreach-group is-draft");
      followUpGroup.appendChild(element("legend", "", "Follow-up draft"));
      const followSubject = outreachField(followUpGroup, "Subject", "follow_up_subject", item.follow_up_subject, { wide: true });
      const followBody = outreachField(followUpGroup, "Body", "follow_up_body", item.follow_up_body, { multiline: true, wide: true });
      followBody.rows = 6;
      const followChecks = element("div", "outreach-checks");
      followChecks.setAttribute("aria-live", "polite");
      const refreshFollowChecks = () => renderDraftChecks(followChecks, followSubject.value, followBody.value);
      followSubject.addEventListener("input", refreshFollowChecks);
      followBody.addEventListener("input", refreshFollowChecks);
      refreshFollowChecks();
      followUpGroup.appendChild(followChecks);
      draftAssistant(followUpGroup, item, "follow_up", followSubject, followBody);
    }

    const notesGroup = element("fieldset", "outreach-group");
    notesGroup.appendChild(element("legend", "", "Notes"));
    outreachField(notesGroup, "Private notes", "notes", item.notes, { multiline: true, wide: true });

    // Draft sits beside a summary of who it goes to and when, so the other tabs
    // are only needed to change those details.
    const draftPanel = panel("draft");
    const draftMain = element("div", "outreach-draft-main");
    draftMain.append(draft, ...(followUpGroup ? [followUpGroup] : []));
    const aside = element("aside", "outreach-aside");
    aside.setAttribute("aria-label", "Contact and timing");
    aside.append(
      outreachSummaryCard("Contact", item.contact_email || item.contact_name ? [
        [item.contact_name || item.contact_email, "outreach-aside-strong"],
        item.contact_role ? [item.contact_role] : null,
        item.contact_name && item.contact_email ? [item.contact_email] : null,
        [CONTACT_CONFIDENCE_LABELS[item.contact_confidence], `outreach-aside-tag is-${item.contact_confidence}`],
      ] : [["No contact yet. Find one under Contact."]]),
      outreachSummaryCard("Timing", [
        [`Deadline: ${item.deadline_date ? formatCalendarDate(item.deadline_date) : item.deadline_label || "none recorded"}`],
        [item.sent_at ? `Sent ${formatCalendarDate(item.sent_at)}` : "Not sent yet"],
        OUTREACH_REVISIT.includes(item.status)
          ? [item.follow_up_at ? `Revisit ${formatCalendarDate(item.follow_up_at)}` : "No revisit date set"]
          : [item.follow_up_at ? `Follow up ${formatCalendarDate(item.follow_up_at)}` : "Follow-up is set when you mark it sent"],
      ])
    );
    draftPanel.append(draftMain, aside);

    const researchPanel = panel("research");
    researchPanel.append(research, notesGroup);
    const contactsSection = outreachContactsSection(item);
    const contactPanel = panel("contact");
    contactPanel.append(contact, contactsSection.element);
    const timingPanel = panel("timing");
    timingPanel.appendChild(timing);
    const historyPanel = panel("history");
    const contacted = Boolean(item.sent_at) || !["not_started", "drafted"].includes(item.status);
    const timeline = element("section", "tracker-subsection outreach-history");
    timeline.appendChild(element("h4", "", "History"));
    const timelineBody = element("div");
    timeline.appendChild(timelineBody);
    if (contacted) historyPanel.appendChild(outreachReplySection(item));
    else historyPanel.appendChild(element("p", "outreach-note", "Replies can be logged once the email is sent."));
    historyPanel.appendChild(timeline);

    const footer = element("div", "outreach-form-actions");
    const save = element("button", "primary-button", "Save changes");
    save.type = "submit";
    const remove = element("button", "danger-button", "Remove company");
    remove.type = "button";
    const formStatus = element("p", "form-status");
    formStatus.setAttribute("aria-live", "polite");
    footer.append(save, remove, formStatus);
    const prepPanels = [];
    if (tabNames.prep) {
      const prepPanel = panel("prep");
      prepPanel.appendChild(outreachCallPrep(item));
      prepPanels.push(prepPanel);
    }
    form.append(...prepPanels, draftPanel, researchPanel, contactPanel, timingPanel, historyPanel, footer);
    loadOutreachTimeline(item.id, timelineBody);
    contactsSection.load();

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const changes = {};
      form.querySelectorAll("[name]").forEach((control) => {
        if (control.value === control.dataset.initial) return;
        if (control.name === "source_urls") changes.source_urls = control.value.split("\n").map((url) => url.trim()).filter(Boolean);
        else if (control.type === "date") changes[control.name] = control.value || null;
        else changes[control.name] = control.value;
      });
      if (!Object.keys(changes).length) {
        formStatus.textContent = "Nothing changed.";
        return;
      }
      save.disabled = true;
      try {
        await api(`/api/v1/outreach/${encodeURIComponent(item.id)}`, { method: "PATCH", body: JSON.stringify(changes) });
        state.outreachOpen = item.id;
        // Everything in the form is now what the server holds.
        state.outreachDiscardEdits = true;
        announce(`Saved ${item.company}.`);
        await loadOutreach();
      } catch (error) {
        formStatus.textContent = error.message;
      } finally {
        save.disabled = false;
      }
    });
    remove.addEventListener("click", async () => {
      if (!window.confirm(`Remove ${item.company} and its outreach history? The deep search will not suggest it again.`)) return;
      try {
        await api(`/api/v1/outreach/${encodeURIComponent(item.id)}`, { method: "DELETE" });
        announce(`Removed ${item.company}.`);
        if (state.outreachSelected === item.id) state.outreachSelected = null;
        await loadOutreach();
      } catch (error) {
        showError(error.message);
      }
    });

    restoreUnsavedOutreachEdits(item, form);
    if (state.outreachFocus === item.id) card.dataset.requested = "true";
    card.append(head, controls, tablist, form);
    selectOutreachPaneTab(card, outreachPaneTab(item), { remember: false });
    return card;
  }

  // Arriving from Urgent: focus the company the row was about.
  function focusRequestedOutreach() {
    state.outreachFocus = null;
    const card = els.results.querySelector('[data-requested="true"]');
    if (!card) return;
    delete card.dataset.requested;
    card.setAttribute("tabindex", "-1");
    card.classList.add("is-requested");
    card.scrollIntoView({ block: "center" });
    card.focus({ preventScroll: true });
  }

  function outreachImportControls() {
    const exports = element("div", "tracker-exports");
    const csvExport = element("a", "secondary-button", "Export CSV");
    csvExport.href = "/api/v1/outreach/export?format=csv";
    const jsonExport = element("a", "secondary-button", "Export JSON");
    jsonExport.href = "/api/v1/outreach/export?format=json";
    const importInput = document.createElement("input");
    importInput.type = "file";
    importInput.accept = ".csv,.json,text/csv,application/json";
    importInput.className = "sr-only";
    importInput.setAttribute("aria-label", "Import outreach targets from CSV or JSON");
    const importButton = element("button", "secondary-button", "Import CSV or JSON");
    importButton.type = "button";
    importButton.addEventListener("click", () => importInput.click());
    importInput.addEventListener("change", async () => {
      if (!importInput.files.length) return;
      importButton.disabled = true;
      const body = new FormData();
      body.append("upload", importInput.files[0]);
      try {
        const result = await api("/api/v1/outreach/import", { method: "POST", body });
        const problems = result.errors.length ? `; ${plural(result.errors.length, "row", "rows")} rejected (${result.errors.map((row) => row.company || `row ${row.row}`).join(", ")})` : "";
        announce(`Imported ${result.imported}; kept ${result.skipped} existing${problems}.`);
        await loadOutreach();
      } catch (error) {
        showError(error.message);
      } finally {
        importButton.disabled = false;
        importInput.value = "";
      }
    });
    exports.append(importButton, csvExport, jsonExport, importInput);
    return exports;
  }

  function outreachAddForm() {
    const add = element("section", "profile-card outreach-add");
    add.appendChild(element("p", "eyebrow", "Add by hand"));
    add.appendChild(element("h3", "", "Add a company"));
    const addForm = element("form", "outreach-add-form");
    const company = profileField(addForm, "Company", "company", "");
    company.required = true;
    const channel = profileField(addForm, "Channel", "channel", "", { placeholder: "Y Combinator, a local incubator…" });
    const location = profileField(addForm, "Based in", "location", "", { placeholder: "San Francisco, CA" });
    const priorityLabel = element("label", "profile-field");
    priorityLabel.appendChild(element("span", "", "Priority"));
    const priority = document.createElement("select");
    ["P1", "P2", "P3"].forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      option.selected = value === "P2";
      priority.appendChild(option);
    });
    priorityLabel.appendChild(priority);
    addForm.appendChild(priorityLabel);
    const addButton = element("button", "primary-button", "Add");
    addButton.type = "submit";
    const addStatus = element("p", "form-status");
    addStatus.setAttribute("aria-live", "polite");
    addForm.append(addButton, addStatus);
    addForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      addButton.disabled = true;
      try {
        const created = await api("/api/v1/outreach", {
          method: "POST",
          body: JSON.stringify({ company: company.value, channel: channel.value, location: location.value, priority: priority.value }),
        });
        // A new company has not been contacted, so it lands on To contact, open.
        state.outreachOpen = created.id;
        state.subtabs.outreach = "to-contact";
        state.outreachQuery = "";
        announce(`Added ${created.company}. Fill in the research and draft below.`);
        await loadOutreach();
      } catch (error) {
        addStatus.textContent = error.message;
      } finally {
        addButton.disabled = false;
      }
    });
    add.appendChild(addForm);

    const transfer = element("section", "profile-card outreach-transfer");
    transfer.appendChild(element("p", "eyebrow", "Move a list in or out"));
    transfer.appendChild(element("h3", "", "Import and export"));
    transfer.appendChild(element("p", "profile-help", "Import a CSV or JSON list of companies; ones you already track are kept as they are. Exports include every company and its drafts."));
    transfer.appendChild(outreachImportControls());
    return [add, transfer];
  }

  function outreachListToolbar(tags = []) {
    const toolbar = element("div", "outreach-toolbar");
    const search = element("div", "search-field outreach-search");
    const label = element("label", "search-label");
    label.htmlFor = "outreach-search-input";
    label.appendChild(element("span", "sr-only", "Search outreach"));
    const glyph = element("span", "", "⌕");
    glyph.setAttribute("aria-hidden", "true");
    label.appendChild(glyph);
    search.appendChild(label);
    const input = document.createElement("input");
    input.id = "outreach-search-input";
    input.type = "search";
    input.placeholder = "Search company, contact, channel, or notes";
    input.autocomplete = "off";
    input.value = state.outreachQuery;
    let timer = null;
    input.addEventListener("input", () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(async () => {
        state.outreachQuery = input.value.trim();
        await loadOutreach();
        const next = els.results.querySelector(".outreach-search input");
        if (next) {
          next.focus();
          next.setSelectionRange(next.value.length, next.value.length);
        }
      }, 220);
    });
    search.appendChild(input);

    const picker = createTagPicker({
      label: "Filter outreach by company tag",
      onChange: async (tag) => {
        state.outreachTag = tag;
        await loadOutreach();
        els.results.querySelector(".outreach-search .tag-picker-trigger")?.focus();
      },
    });
    picker.setTags(tags);
    picker.setValue(state.outreachTag);
    search.appendChild(picker.root);

    const sortLabel = element("label", "outreach-filter");
    sortLabel.appendChild(element("span", "", "Sort"));
    const sort = document.createElement("select");
    Object.entries(OUTREACH_SORTS).forEach(([value, [text]]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      option.selected = state.outreachSort === value;
      sort.appendChild(option);
    });
    sort.addEventListener("change", async () => {
      state.outreachSort = sort.value;
      await loadOutreach();
      els.results.querySelector(".outreach-filter select")?.focus();
    });
    sortLabel.appendChild(sort);
    toolbar.append(search, sortLabel);
    return toolbar;
  }

  function outreachUnsavedEdits(pane) {
    return [...(pane?.querySelectorAll("form [name]") || [])].some((control) => control.value !== control.dataset.initial);
  }

  // The list of companies beside one company's pane. Picking a row swaps the
  // pane without refetching; the selection survives reloads after an action.
  function outreachSplitView(items, tab, context) {
    if (state.outreachOpen) state.outreachSelected = state.outreachOpen;
    state.outreachOpen = null;
    if (!items.some((item) => item.id === state.outreachSelected)) state.outreachSelected = items[0].id;
    const moved = (item) => {
      if (tab.test(item)) return "";
      const now = OUTREACH_TABS.find((entry) => !entry.tool && entry.id !== "all" && entry.test(item));
      return now ? `Now in ${now.label}` : "Moved out of this tab";
    };

    const split = element("div", "outreach-split");
    const list = element("div", "outreach-list");
    list.setAttribute("role", "group");
    list.setAttribute("aria-label", "Companies");
    const host = element("div", "outreach-pane-host");

    const show = (item) => {
      const pane = createOutreachCard(item, context);
      if (moved(item)) {
        pane.classList.add("is-moved");
        pane.querySelector(".application-facts")?.prepend(chip(moved(item), "is-new"));
      }
      host.replaceChildren(pane);
      list.querySelectorAll(".outreach-row").forEach((row) => {
        const on = row.dataset.rowId === item.id;
        row.classList.toggle("is-selected", on);
        row.setAttribute("aria-pressed", String(on));
      });
    };

    items.forEach((item) => {
      const row = outreachRow(item, item.id === state.outreachSelected, () => {
        if (state.outreachSelected === item.id) return;
        const current = host.querySelector(".outreach-pane");
        if (outreachUnsavedEdits(current) && !window.confirm(`Discard unsaved changes to ${current.querySelector("h3")?.textContent || "this company"}?`)) return;
        state.outreachSelected = item.id;
        show(item);
        // Stacked, the pane sits below the list. The split stacks by its own
        // width, not the window's (styles.css), so ask the layout, not a media query.
        if (getComputedStyle(split).gridTemplateColumns.trim().split(/\s+/).length === 1) host.scrollIntoView({ block: "start" });
      });
      if (moved(item)) {
        row.classList.add("is-moved");
        row.querySelector(".outreach-row-bottom")?.prepend(chip(moved(item), "is-new"));
      }
      list.appendChild(row);
    });
    // Up and down walk the list; each row it lands on opens.
    list.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      const rows = [...list.querySelectorAll(".outreach-row")];
      const index = rows.indexOf(document.activeElement);
      if (index < 0) return;
      event.preventDefault();
      const target = rows[Math.min(rows.length - 1, Math.max(0, index + (event.key === "ArrowDown" ? 1 : -1)))];
      target.focus();
      target.click();
    });

    split.append(list, host);
    show(items.find((item) => item.id === state.outreachSelected));
    return split;
  }

  function outreachToolCount(id, payload) {
    if (id === "deep-search") return payload.discovery.active?.state === "running" ? "…" : null;
    if (id === "find-people") return payload.recontact?.active?.state === "running" ? "…" : payload.recontact?.eligible || null;
    return null;
  }

  async function loadOutreach() {
    const sequence = ++state.loadSequence;
    state.loading = true;
    clearError();
    els.results.setAttribute("aria-busy", "true");
    if (!els.results.querySelector(".outreach-card, .outreach-toolbar, .outreach-deep-search, .outreach-recontact, .outreach-settings, .outreach-add")) skeletons();
    try {
      const payload = await api("/api/v1/outreach");
      if (sequence !== state.loadSequence || state.view !== "outreach") return;
      const tab = OUTREACH_TABS.find((entry) => entry.id === state.subtabs.outreach) || OUTREACH_TABS[0];
      const running = payload.discovery.active?.state === "running";
      // A search or tag narrows every tab, so each count reads "6 of 20" and
      // matches the list it opens.
      const filtering = Boolean(state.outreachQuery || state.outreachTag);
      const tagged = (item) => !state.outreachTag || (item.tags || []).some((tag) => tag.tag === state.outreachTag);
      const matches = (item) => outreachMatchesQuery(item, state.outreachQuery) && tagged(item);
      renderSubnav(OUTREACH_TABS.map((entry) => {
        if (entry.tool) return { ...entry, count: outreachToolCount(entry.id, payload) };
        const inTab = payload.items.filter(entry.test);
        return filtering && inTab.length
          ? { ...entry, count: inTab.filter(matches).length, total: inTab.length }
          : { ...entry, count: inTab.length };
      }));
      // Cards acted on here stay put after they move to another tab, so an
      // action never makes the card vanish from under the pointer.
      if (state.outreachOpen) state.outreachKeep.add(state.outreachOpen);
      captureUnsavedOutreachEdits();
      els.results.replaceChildren();

      if (tab.id === "deep-search") {
        state.deepSearchOpen = true;
        els.results.appendChild(deepSearchPanel(payload.discovery));
        els.resultCount.textContent = "Deep search";
      } else if (tab.id === "settings") {
        const panel = await outreachSettingsPanel();
        // The settings load after the list; a view switched meanwhile keeps its own page.
        if (sequence !== state.loadSequence || state.view !== "outreach") return;
        els.results.appendChild(panel);
        els.resultCount.textContent = "Outreach settings";
      } else if (tab.id === "find-people") {
        els.results.appendChild(recontactPanel(payload.recontact));
        els.resultCount.textContent = "Find people";
      } else if (tab.id === "add") {
        els.results.append(...outreachAddForm());
        const gmailConnect = gmailConnectPanel(payload.gmail_drafts);
        if (gmailConnect) els.results.appendChild(gmailConnect);
        els.resultCount.textContent = "Add, import, export";
      } else {
        const [, compare] = OUTREACH_SORTS[state.outreachSort] || OUTREACH_SORTS.contact;
        const kept = (item) => state.outreachKeep.has(item.id);
        const items = payload.items
          .filter((item) => (tab.test(item) && matches(item)) || kept(item))
          .sort(compare);
        els.results.appendChild(outreachListToolbar(payload.tags || []));
        if (running) {
          const banner = element("div", "outreach-banner");
          banner.appendChild(element("p", "", "The deep search is running. New companies land in From deep search when it finishes."));
          const view = element("button", "secondary-button", "View deep search");
          view.type = "button";
          view.addEventListener("click", () => selectSubtab("deep-search"));
          banner.appendChild(view);
          els.results.appendChild(banner);
        }
        const gmailConnect = gmailConnectPanel(payload.gmail_drafts);
        if (gmailConnect) els.results.appendChild(gmailConnect);

        if (!items.length) {
          const empty = element("div", "empty-state");
          const searching = Boolean(state.outreachQuery || state.outreachTag);
          empty.appendChild(element("strong", "", searching
            ? "No company matches that search"
            : payload.items.length ? `Nothing in ${tab.label}` : "No outreach targets yet"));
          empty.appendChild(element("p", "", searching
            ? `Clear the search${state.outreachTag ? " or the tag filter" : ""}, or look under All companies.`
            : !payload.items.length
              ? "Add a startup you want to cold email under Tools, or run a deep search."
              : tab.id === "to-contact"
                ? "Every company has been contacted. Run a deep search or add one under Tools."
                : "Pick another tab beside the page."));
          els.results.appendChild(empty);
        } else {
          els.results.appendChild(outreachSplitView(items, tab, { compose: payload.compose, gmail: payload.gmail_drafts }));
        }
        els.resultCount.textContent = `${plural(items.length, "company", "companies")} · ${tab.label}`;
      }
      els.pageStatus.textContent = "Nothing sends from here; approved drafts open in your own email";
      if (running) scheduleDeepSearchPoll();
      if (payload.gmail_drafts?.bounce_check) checkForBounces();
      els.results.setAttribute("aria-busy", "false");
      focusRequestedOutreach();
    } catch (error) {
      showLoadError(error, sequence);
    } finally {
      state.loading = false;
      // One reload carries them; a later one starts from what the server holds.
      // Both flags end here, so a load that returned early cannot strand either.
      state.outreachPendingEdits = null;
      state.outreachDiscardEdits = false;
    }
  }

  function profileField(form, labelText, name, value, options = {}) {
    const label = element("label", "profile-field");
    label.appendChild(element("span", "", labelText));
    const control = options.multiline ? document.createElement("textarea") : document.createElement("input");
    control.name = name;
    if (!options.multiline) control.type = options.type || "text";
    if (options.placeholder) control.placeholder = options.placeholder;
    if (options.min !== undefined) control.min = String(options.min);
    if (options.max !== undefined) control.max = String(options.max);
    control.value = value ?? "";
    label.appendChild(control);
    form.appendChild(label);
    return control;
  }

  function profileSelect(form, labelText, name, value) {
    const label = element("label", "profile-field");
    label.appendChild(element("span", "", labelText));
    const select = document.createElement("select");
    select.name = name;
    [["", "Not answered"], ["true", "Yes"], ["false", "No"]].forEach(([optionValue, text]) => {
      const option = document.createElement("option");
      option.value = optionValue;
      option.textContent = text;
      option.selected = value === null || value === undefined ? optionValue === "" : optionValue === String(value);
      select.appendChild(option);
    });
    label.appendChild(select);
    form.appendChild(label);
    return select;
  }

  function commaList(value) {
    if (!value) return [];
    return value.split(/[,\n]/).map((item) => item.trim()).filter(Boolean);
  }

  function nullableBoolean(value) {
    if (value === "") return null;
    return value === "true";
  }

  function renderProfileEditor(payload) {
    const profile = payload.profile || {};
    const card = element("section", "profile-card");
    const top = element("div", "profile-card-heading");
    const copy = element("div");
    copy.appendChild(element("p", "eyebrow", "Confirmed career facts"));
    copy.appendChild(element("h3", "", "Profile and preferences"));
    copy.appendChild(element("p", "profile-help", "Saving is an explicit confirmation of these fields. Changes feed the deterministic scoring profile."));
    const meter = element("div", "completeness-meter");
    meter.appendChild(element("strong", "", `${payload.completeness.percent}%`));
    meter.appendChild(element("span", "", "complete"));
    const progress = document.createElement("progress");
    progress.max = 100;
    progress.value = payload.completeness.percent;
    progress.setAttribute("aria-label", `Profile ${payload.completeness.percent}% complete`);
    meter.appendChild(progress);
    top.append(copy, meter);
    card.appendChild(top);

    if (payload.completeness.missing.length) {
      const labels = {
        interest_keywords: "interests",
        available_terms: "available terms",
        graduation_year: "graduation year",
        work_authorized_us: "U.S. work authorization",
        requires_sponsorship: "sponsorship needs",
        compensation_preferences: "pay preferences",
      };
      const missing = payload.completeness.missing.map((field) => labels[field] || field.replaceAll("_", " "));
      card.appendChild(element("p", "missing-note", `Still needed: ${missing.join(", ")}.`));
    }

    const form = element("form", "profile-form");
    const name = profileField(form, "Name", "name", profile.name);
    const school = profileField(form, "School", "school", profile.school);
    const degree = profileField(form, "Degree", "degree", profile.degree);
    const graduation = profileField(form, "Graduation year", "graduation_year", profile.graduation_year, { type: "number", min: 2000, max: 2200 });
    const skills = profileField(form, "Skills (comma or line separated)", "skills", (profile.skills || []).join(", "), { multiline: true });
    const interests = profileField(form, "Interests", "interest_keywords", (profile.interest_keywords || []).join(", "), { multiline: true });
    const terms = profileField(form, "Available terms", "available_terms", (profile.available_terms || []).join(", "), { multiline: true });
    const locations = profileField(
      form,
      "Target regions",
      "regions",
      (profile.regions || []).map((region) => typeof region === "string" ? region : region.name).filter(Boolean).join(", "),
      { placeholder: "Atlanta, Bay Area" }
    );
    const breakLocation = profileField(form, "Home during breaks and summers", "break_location", profile.break_location, { placeholder: "City, ST or a target region" });
    const hours = profileField(form, "Hours per week", "hours_per_week", profile.hours_per_week, { type: "number", min: 1, max: 80 });
    const workAuthorized = profileSelect(form, "Authorized to work in the U.S.", "work_authorized_us", profile.work_authorized_us);
    const citizen = profileSelect(form, "U.S. citizen", "us_citizen", profile.us_citizen);
    const sponsorship = profileSelect(form, "Requires sponsorship", "requires_sponsorship", profile.requires_sponsorship);
    const relocation = profileSelect(form, "Willing to relocate", "willing_to_relocate", profile.willing_to_relocate);
    const compensation = profile.compensation_preferences || {};
    const paidOnly = profileSelect(form, "Paid roles only", "paid_only", compensation.paid_only);
    const minimumPay = profileField(form, "Minimum hourly pay (USD)", "minimum_pay", compensation.minimum_hourly, { type: "number", min: 0 });

    const statusLine = element("p", "form-status");
    statusLine.setAttribute("aria-live", "polite");
    if (state.profileStatus) {
      statusLine.textContent = state.profileStatus;
      state.profileStatus = null;
    }
    const save = element("button", "primary-button", "Save and confirm profile");
    save.type = "submit";
    const exportLink = element("a", "secondary-button profile-export", "Export scoring profile");
    exportLink.href = "/api/v1/profile/export";
    form.append(save, exportLink, statusLine);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      save.disabled = true;
      statusLine.textContent = "Saving…";
      const regionNames = commaList(locations.value);
      const existingRegions = new Map((profile.regions || []).filter((item) => item && typeof item === "object").map((item) => [String(item.name).toLowerCase(), item]));
      const regions = regionNames.map((regionName) => existingRegions.get(regionName.toLowerCase()) || {
        name: regionName,
        radius: "selected",
        bonus: 10,
        state_markers: [],
        aliases: [],
        places: [regionName.toLowerCase()],
      });
      const updates = {
        name: name.value.trim(),
        school: school.value.trim(),
        degree: degree.value.trim(),
        graduation_year: graduation.value ? Number(graduation.value) : null,
        skills: commaList(skills.value),
        interest_keywords: commaList(interests.value),
        available_terms: commaList(terms.value),
        regions,
        preferred_locations: regionNames,
        break_location: breakLocation.value.trim(),
        hours_per_week: hours.value ? Number(hours.value) : null,
        work_authorized_us: nullableBoolean(workAuthorized.value),
        us_citizen: nullableBoolean(citizen.value),
        requires_sponsorship: nullableBoolean(sponsorship.value),
        willing_to_relocate: nullableBoolean(relocation.value),
        compensation_preferences: {
          paid_only: nullableBoolean(paidOnly.value),
          minimum_hourly: minimumPay.value ? Number(minimumPay.value) : null,
          currency: "USD",
        },
      };
      // "Not answered" is not a confirmation; the server enforces this too.
      const answered = (value) => Array.isArray(value)
        ? value.length > 0
        : value && typeof value === "object"
          ? Object.entries(value).some(([key, item]) => key !== "currency" && answered(item))
          : value !== null && value !== undefined && value !== "";
      try {
        await api("/api/v1/profile", {
          method: "PUT",
          body: JSON.stringify({ updates, confirmed_fields: Object.keys(updates).filter((field) => answered(updates[field])) }),
        });
        state.profileStatus = "Profile saved and confirmed.";
        await loadProfile();
      } catch (error) {
        statusLine.textContent = error.message;
      } finally {
        save.disabled = false;
      }
    });
    card.appendChild(form);
    return card;
  }

  function createResumeCard(record) {
    const card = element("article", "resume-card");
    const heading = element("div", "resume-heading");
    const identity = element("div");
    identity.appendChild(element("strong", "", record.original_name));
    identity.appendChild(element("span", "", `${Math.ceil(record.byte_size / 1024)} KB · ${record.status}`));
    heading.append(identity, chip(record.status === "confirmed" ? "Confirmed" : "Needs review", record.status === "confirmed" ? "is-region" : ""));
    card.appendChild(heading);

    const preview = element("details", "resume-preview");
    preview.appendChild(element("summary", "", "Review extracted text"));
    preview.appendChild(element("pre", "", record.extracted_text));
    card.appendChild(preview);

    const suggestions = record.parsed?.profile_suggestions || {};
    if (record.status !== "confirmed") {
      const review = element("form", "resume-review");
      review.appendChild(element("p", "profile-help", "Select only facts you reviewed and want copied into your profile."));
      const checkboxes = [];
      Object.entries(suggestions).forEach(([field, value]) => {
        const label = element("label", "confirmation-row");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.name = field;
        const display = typeof value === "object" ? JSON.stringify(value) : String(value);
        label.append(checkbox, element("span", "", `${field.replaceAll("_", " ")}: ${display}`));
        review.appendChild(label);
        checkboxes.push([checkbox, field, value]);
      });
      const confirm = element("button", "secondary-button", "Confirm selected facts");
      confirm.type = "submit";
      confirm.disabled = checkboxes.length === 0;
      const reviewStatus = element("p", "form-status");
      reviewStatus.setAttribute("aria-live", "polite");
      review.append(confirm, reviewStatus);
      review.addEventListener("submit", async (event) => {
        event.preventDefault();
        const selected = checkboxes.filter(([checkbox]) => checkbox.checked);
        if (!selected.length) {
          reviewStatus.textContent = "Select at least one reviewed fact.";
          return;
        }
        confirm.disabled = true;
        try {
          const profileUpdates = Object.fromEntries(selected.map(([, field, value]) => [field, value]));
          await api(`/api/v1/resumes/${encodeURIComponent(record.id)}/confirm`, {
            method: "POST",
            body: JSON.stringify({
              confirmed_data: profileUpdates,
              profile_updates: profileUpdates,
              confirmed_profile_fields: Object.keys(profileUpdates),
            }),
          });
          await loadProfile();
        } catch (error) {
          reviewStatus.textContent = error.message;
          confirm.disabled = false;
        }
      });
      card.appendChild(review);
    }

    const actions = element("div", "resume-actions");
    const download = element("a", "secondary-button", "Download original");
    download.href = `/api/v1/resumes/${encodeURIComponent(record.id)}/file`;
    const remove = element("button", "danger-button", "Delete");
    remove.type = "button";
    remove.addEventListener("click", async () => {
      if (!window.confirm(`Permanently delete ${record.original_name}?`)) return;
      remove.disabled = true;
      try {
        await api(`/api/v1/resumes/${encodeURIComponent(record.id)}`, { method: "DELETE" });
        await loadProfile();
      } catch (error) {
        showError(error.message);
        remove.disabled = false;
      }
    });
    actions.append(download, remove);
    card.appendChild(actions);
    return card;
  }

  function notificationSettingsSection(connections, preferences, events, applications) {
    const section = element("section", "profile-card connection-section");
    section.appendChild(element("p", "eyebrow", "Connections and notifications"));
    section.appendChild(element("h3", "", "Monitoring controls"));
    section.appendChild(element("p", "profile-help", "Provider activity is previewed before tracker changes. Google/Microsoft use least-privilege OAuth when configured; notification delivery remains sandbox-suppressed by default."));
    const connectionList = element("div", "connection-list");
    (connections.items || []).forEach((connector) => {
      const row = element("div", "connection-row");
      row.appendChild(element("strong", "", `${connector.provider} · ${connector.status}`));
      if (connector.status === "connected") {
        const disconnect = element("button", "danger-button", "Disconnect");
        disconnect.type = "button";
        disconnect.addEventListener("click", async () => {
          await api(`/api/v1/connections/${encodeURIComponent(connector.id)}`, { method: "DELETE" });
          await loadProfile();
        });
        row.appendChild(disconnect);
      }
      connectionList.appendChild(row);
    });
    if (!(connections.items || []).some((connector) => connector.status === "connected")) {
      const connect = element("button", "secondary-button", "Connect sandbox mailbox/calendar");
      connect.type = "button";
      connect.addEventListener("click", async () => {
        await api("/api/v1/connections", { method: "POST", body: JSON.stringify({ provider: "sandbox" }) });
        await loadProfile();
      });
      connectionList.appendChild(connect);
    }
    ["google", "microsoft"].forEach((provider) => {
      if ((connections.items || []).some((connector) => connector.provider === provider && connector.status === "connected")) return;
      const connect = element("button", "secondary-button", `Connect ${provider}`);
      connect.type = "button";
      connect.addEventListener("click", async () => {
        try {
          const start = await api(`/api/v1/connections/oauth/${provider}/start`);
          window.location.assign(start.authorization_url);
        } catch (error) { showError(error.message); }
      });
      connectionList.appendChild(connect);
    });
    section.appendChild(connectionList);

    const form = element("form", "notification-form");
    const timezone = profileField(form, "Timezone", "timezone", preferences.timezone);
    const quietStart = profileField(form, "Quiet hours start", "quiet_start", preferences.quiet_start, { type: "time" });
    const quietEnd = profileField(form, "Quiet hours end", "quiet_end", preferences.quiet_end, { type: "time" });
    const digestLabel = element("label", "profile-field");
    digestLabel.appendChild(element("span", "", "Digest frequency"));
    const digest = document.createElement("select");
    ["immediate", "daily", "weekly", "off"].forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      option.selected = value === preferences.digest_frequency;
      digest.appendChild(option);
    });
    digestLabel.appendChild(digest);
    form.appendChild(digestLabel);
    const toggles = element("div", "notification-toggles");
    const toggleControls = {};
    ["in_app", "email", "push", "sms", "voice"].forEach((channel) => {
      const label = element("label", "confirmation-row");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = Boolean(preferences[`${channel}_enabled`]);
      label.append(checkbox, element("span", "", channel.replaceAll("_", " ")));
      toggles.appendChild(label);
      toggleControls[channel] = checkbox;
    });
    form.appendChild(toggles);
    const save = element("button", "secondary-button", "Save notification preferences");
    save.type = "submit";
    const statusLine = element("p", "form-status");
    form.append(save, statusLine);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      save.disabled = true;
      try {
        const updates = {
          timezone: timezone.value,
          quiet_start: quietStart.value,
          quiet_end: quietEnd.value,
          digest_frequency: digest.value,
          ...Object.fromEntries(Object.entries(toggleControls).map(([channel, control]) => [`${channel}_enabled`, control.checked])),
        };
        await api("/api/v1/notification-preferences", { method: "PUT", body: JSON.stringify({ updates }) });
        statusLine.textContent = "Preferences saved.";
      } catch (error) {
        statusLine.textContent = error.message;
      } finally {
        save.disabled = false;
      }
    });
    section.appendChild(form);

    const phoneForm = element("form", "phone-form");
    const phone = document.createElement("input");
    phone.placeholder = "+15125550123";
    phone.setAttribute("aria-label", "Phone number in E.164 format");
    const requestCode = element("button", "secondary-button", preferences.phone_verified ? "Phone verified" : "Verify phone in sandbox");
    requestCode.type = "submit";
    requestCode.disabled = Boolean(preferences.phone_verified);
    const phoneStatus = element("p", "form-status");
    phoneForm.append(phone, requestCode, phoneStatus);
    phoneForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      requestCode.disabled = true;
      try {
        const challenge = await api("/api/v1/phone-verifications", { method: "POST", body: JSON.stringify({ phone_e164: phone.value }) });
        const entered = window.prompt(`Sandbox verification code: ${challenge.sandbox_code}. Enter it to confirm.`);
        if (!entered) return;
        await api("/api/v1/phone-verifications/confirm", { method: "POST", body: JSON.stringify({ challenge_id: challenge.id, code: entered }) });
        await loadProfile();
      } catch (error) {
        phoneStatus.textContent = error.message;
        requestCode.disabled = false;
      }
    });
    section.appendChild(phoneForm);

    const pending = (events.items || []).filter((item) => item.status === "pending");
    if (pending.length) {
      const eventList = element("div", "monitored-event-list");
      pending.forEach((item) => {
        const card = element("article", "monitored-event");
        const decidedBy = item.payload.classified_by?.source === "jev" ? "Jev suggestion" : "keyword rules";
        card.appendChild(element("strong", "", `${item.event_type.replaceAll("_", " ")} · ${Math.round(item.confidence * 100)}% · ${decidedBy}`));
        card.appendChild(element("p", "", item.payload.subject || item.payload.body_preview));
        const select = document.createElement("select");
        (applications.items || []).forEach((application) => {
          const option = document.createElement("option");
          option.value = application.id;
          option.textContent = `${application.company} — ${application.title}`;
          select.appendChild(option);
        });
        const controls = element("div", "preparation-actions");
        const confirm = element("button", "secondary-button", "Confirm tracker update");
        confirm.type = "button";
        const ignore = element("button", "danger-button", "Ignore");
        ignore.type = "button";
        confirm.addEventListener("click", async () => {
          await api(`/api/v1/monitored-events/${encodeURIComponent(item.id)}/decision`, { method: "POST", body: JSON.stringify({ decision: "confirm", application_id: select.value }) });
          await loadProfile();
        });
        ignore.addEventListener("click", async () => {
          await api(`/api/v1/monitored-events/${encodeURIComponent(item.id)}/decision`, { method: "POST", body: JSON.stringify({ decision: "ignore", application_id: null }) });
          await loadProfile();
        });
        controls.append(confirm, ignore);
        card.append(select, controls);
        eventList.appendChild(card);
      });
      section.appendChild(eventList);
    }
    return section;
  }

  function humanizeKey(key) {
    const text = String(key || "").replace(/[_.]+/g, " ").trim();
    return text ? text[0].toUpperCase() + text.slice(1) : "";
  }

  function dossierScalar(value) {
    if (value === true) return "Yes";
    if (value === false) return "No";
    if (value === null || value === undefined || value === "") return "Not set";
    return String(value);
  }

  // Dossier values are stored JSON; show them as readable facts, not raw JSON.
  function dossierValue(value) {
    const isScalar = (entry) => entry === null || typeof entry !== "object";
    if (isScalar(value)) return element("p", "dossier-scalar", dossierScalar(value));
    if (Array.isArray(value)) {
      if (!value.length) return element("p", "dossier-scalar", "None");
      if (value.every(isScalar)) {
        const row = element("div", "chip-row dossier-chips");
        value.forEach((entry) => row.appendChild(chip(dossierScalar(entry))));
        return row;
      }
      const list = element("ul", "dossier-entries");
      value.forEach((entry) => {
        const li = element("li", "dossier-entry");
        if (isScalar(entry)) {
          li.textContent = dossierScalar(entry);
        } else {
          const scalars = Object.entries(entry).filter(([, inner]) => isScalar(inner) && inner !== "" && inner !== null);
          const headline = scalars.slice(0, 3).map(([, inner]) => dossierScalar(inner)).join(" · ");
          li.appendChild(element("strong", "", headline || "Entry"));
          const rest = scalars.slice(3);
          if (rest.length) li.appendChild(element("p", "dossier-meta", rest.map(([key, inner]) => `${humanizeKey(key)}: ${dossierScalar(inner)}`).join(" · ")));
          Object.entries(entry).filter(([, inner]) => !isScalar(inner)).forEach(([key, inner]) => {
            const more = element("details", "dossier-more");
            more.appendChild(element("summary", "", `${humanizeKey(key)}${Array.isArray(inner) ? ` (${inner.length})` : ""}`));
            more.appendChild(dossierValue(inner));
            li.appendChild(more);
          });
        }
        list.appendChild(li);
      });
      return list;
    }
    const facts = element("dl", "dossier-facts");
    Object.entries(value).forEach(([key, inner]) => {
      facts.appendChild(element("dt", "", humanizeKey(key)));
      const dd = element("dd");
      if (isScalar(inner)) dd.textContent = dossierScalar(inner);
      else dd.appendChild(dossierValue(inner));
      facts.appendChild(dd);
    });
    return facts;
  }

  function dossierSection(payload) {
    const section = element("section", "profile-card dossier-section");
    section.appendChild(element("p", "eyebrow", "Private career memory"));
    section.appendChild(element("h3", "", "Evidence dossier and consent"));
    section.appendChild(element("p", "profile-help", "Nothing here is employer-visible until you preview and create a named, expiring share."));
    const settings = element("form", "notification-form");
    const pausedLabel = element("label", "channel-toggle");
    const paused = document.createElement("input"); paused.type = "checkbox"; paused.checked = payload.settings.paused;
    pausedLabel.append(paused, document.createTextNode(" Pause memory and sharing"));
    const retention = profileField(settings, "Retention days", "retention_days", payload.settings.retention_days, {type: "number"});
    const save = element("button", "secondary-button", "Save dossier settings"); save.type = "submit";
    settings.append(pausedLabel, save);
    settings.addEventListener("submit", async (event) => {
      event.preventDefault();
      await api("/api/v1/dossier/settings", {method: "PUT", body: JSON.stringify({paused: paused.checked, retention_days: Number(retention.value)})});
      await loadProfile();
    });
    section.appendChild(settings);
    const itemForm = element("form", "notification-form");
    const kindLabel = element("label", "profile-field"); kindLabel.appendChild(element("span", "", "Item classification"));
    const kind = document.createElement("select"); ["user_opinion", "deterministic_analysis", "ai_suggestion"].forEach((value) => { const option = document.createElement("option"); option.value = value; option.textContent = humanizeKey(value); kind.appendChild(option); }); kindLabel.appendChild(kind); itemForm.appendChild(kindLabel);
    const fieldPath = profileField(itemForm, "Field name", "field_path", "");
    const value = profileField(itemForm, "Value", "value", "", {multiline: true});
    const add = element("button", "secondary-button", "Add classified item"); add.type = "submit"; itemForm.appendChild(add);
    itemForm.addEventListener("submit", async (event) => { event.preventDefault(); await api("/api/v1/dossier/items", {method: "POST", body: JSON.stringify({item_type: kind.value, field_path: fieldPath.value, value: value.value, evidence: []})}); await loadProfile(); });
    section.appendChild(itemForm);
    const items = element("div", "dossier-list");
    (payload.items || []).forEach((item) => {
      const row = element("div", "dossier-item");
      const check = document.createElement("input"); check.type = "checkbox"; check.value = item.id; check.dataset.dossierItem = "true";
      check.id = `dossier-item-${item.id}`;
      const main = element("div", "dossier-item-main");
      const title = element("label", "dossier-item-title");
      title.htmlFor = check.id;
      title.append(element("strong", "", humanizeKey(item.field_path)), chip(humanizeKey(item.item_type)));
      main.append(title, dossierValue(item.value));
      const remove = element("button", "danger-button", "Delete"); remove.type = "button";
      remove.setAttribute("aria-label", `Delete ${humanizeKey(item.field_path)}`);
      remove.addEventListener("click", async () => { if (window.confirm(`Delete ${item.field_path} and revoke shares containing it?`)) { await api(`/api/v1/dossier/items/${encodeURIComponent(item.id)}`, {method: "DELETE"}); await loadProfile(); } });
      row.append(check, main, remove);
      items.appendChild(row);
    });
    section.appendChild(items);
    const shareForm = element("form", "notification-form");
    const recipient = profileField(shareForm, "Share recipient (exact organization name)", "recipient", "");
    const days = profileField(shareForm, "Expires in days", "expires", 30, {type: "number"});
    const share = element("button", "primary-button", "Preview and create share"); share.type = "submit";
    const shareStatus = element("p", "form-status"); shareStatus.setAttribute("aria-live", "polite");
    shareForm.append(share, shareStatus);
    shareForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const item_ids = [...items.querySelectorAll("input:checked")].map((input) => input.value);
      try {
        const preview = await api("/api/v1/dossier/shares/preview", {method: "POST", body: JSON.stringify({item_ids})});
        if (!window.confirm(`Share ${preview.items.length} previewed dossier item(s) with ${recipient.value}?`)) return;
        const result = await api("/api/v1/dossier/shares", {method: "POST", body: JSON.stringify({recipient: recipient.value, item_ids, expires_in_days: Number(days.value)})});
        shareStatus.textContent = `Share created. Copy this token now: ${result.share_token}`;
        await loadProfile();
      } catch (error) { shareStatus.textContent = error.message; }
    });
    section.appendChild(shareForm);
    (payload.shares || []).forEach((grant) => {
      const row = element("div", "connection-row");
      row.appendChild(element("span", "", `${grant.recipient} · ${grant.status} · expires ${formatDate(grant.expires_at)} · ${plural(grant.access_log.length, "log event", "log events")}`));
      if (grant.status === "active") {
        const revoke = element("button", "danger-button", "Revoke"); revoke.type = "button";
        revoke.addEventListener("click", async () => { await api(`/api/v1/dossier/shares/${encodeURIComponent(grant.id)}`, {method: "DELETE"}); await loadProfile(); });
        row.appendChild(revoke);
      }
      section.appendChild(row);
    });
    const actions = element("div", "tracker-exports");
    const exportLink = element("a", "secondary-button", "Export dossier"); exportLink.href = "/api/v1/dossier/export";
    const deleteAll = element("button", "danger-button", "Delete dossier"); deleteAll.type = "button";
    deleteAll.addEventListener("click", async () => { if (window.confirm("Delete every dossier item and revoke every share?")) { await api("/api/v1/dossier", {method: "DELETE"}); await loadProfile(); } });
    actions.append(exportLink, deleteAll); section.appendChild(actions);
    return section;
  }

  function accountSection() {
    const section = element("section", "profile-card account-section");
    section.appendChild(element("p", "eyebrow", "Account controls"));
    section.appendChild(element("h3", "", "Export or permanently delete"));
    const actions = element("div", "tracker-exports");
    const exportLink = element("a", "secondary-button", "Export full account"); exportLink.href = "/api/v1/account/export";
    const remove = element("button", "danger-button", "Delete account"); remove.type = "button";
    remove.addEventListener("click", async () => {
      if (window.prompt("Type DELETE to permanently remove private account data and files.") !== "DELETE") return;
      await api("/api/v1/account", {method: "DELETE", headers: {"X-Confirm-Delete": "DELETE"}});
      await api("/api/v1/session", {method: "DELETE"}); showAuth("Account deleted.");
    });
    actions.append(exportLink, remove); section.appendChild(actions); return section;
  }

  function extensionSection(devicesPayload = {items: []}) {
    const section = element("section", "profile-card extension-section");
    section.appendChild(element("p", "eyebrow", "Assisted Apply extension"));
    section.appendChild(element("h3", "", "Pair or revoke Chrome devices"));
    section.appendChild(element("p", "profile-help", "Pairing codes expire after 10 minutes and work once. The extension receives a revocable Apply-only credential, never your account token."));
    const pairingActions = element("div", "tracker-exports");
    const create = element("button", "secondary-button", "Create one-time pairing code");
    create.type = "button";
    const pairingStatus = element("p", "form-status");
    pairingStatus.setAttribute("aria-live", "polite");
    create.addEventListener("click", async () => {
      create.disabled = true;
      try {
        const result = await api("/api/v1/extension/pairings", {method: "POST", body: JSON.stringify({})});
        pairingStatus.textContent = `Pairing code: ${result.code} · expires ${formatDate(result.expires_at)}. Paste it into the extension side panel now.`;
      } catch (error) {
        pairingStatus.textContent = error.message;
      } finally {
        create.disabled = false;
      }
    });
    pairingActions.appendChild(create);
    section.append(pairingActions, pairingStatus);

    const devices = devicesPayload.items || [];
    if (!devices.length) {
      section.appendChild(element("p", "empty-inline", "No Chrome devices paired."));
    } else {
      const list = element("div", "connection-list");
      devices.forEach((device) => {
        const row = element("div", "connection-row");
        const stateLabel = device.revoked_at ? `revoked ${formatDate(device.revoked_at)}` : `last used ${device.last_used_at ? formatDate(device.last_used_at) : "never"}`;
        row.appendChild(element("span", "", `${device.device_name} · ${stateLabel}`));
        if (!device.revoked_at) {
          const revoke = element("button", "danger-button", "Revoke");
          revoke.type = "button";
          revoke.addEventListener("click", async () => {
            await api(`/api/v1/extension/devices/${encodeURIComponent(device.id)}`, {method: "DELETE"});
            await loadProfile();
          });
          row.appendChild(revoke);
        }
        list.appendChild(row);
      });
      section.appendChild(list);
    }
    return section;
  }

  function renderProfile(profilePayload, resumesPayload = null, connections = { items: [] }, preferences = {}, events = { items: [] }, applications = { items: [] }, dossier = {settings: {}, items: [], shares: []}, extensionDevices = {items: []}) {
    els.results.replaceChildren();
    els.results.removeAttribute("role");
    els.resultCount.textContent = "Your career profile";
    els.pageStatus.textContent = `${profilePayload.completeness.completed} of ${profilePayload.completeness.total} onboarding fields complete`;
    // The phone bottom bar holds the eight destinations; Sign out lives here.
    const signOutRow = element("div", "mobile-signout");
    const signOut = element("button", "secondary-button", "Sign out");
    signOut.type = "button";
    signOut.id = "profile-signout";
    signOut.addEventListener("click", () => els.logout.click());
    signOutRow.appendChild(signOut);
    els.results.appendChild(signOutRow);
    els.results.appendChild(tagSection(renderProfileEditor(profilePayload), "profile", "Career profile"));

    const resumeSection = element("section", "profile-card resume-section");
    resumeSection.appendChild(element("p", "eyebrow", "Private documents"));
    resumeSection.appendChild(element("h3", "", "Resume versions"));
    resumeSection.appendChild(element("p", "profile-help", "PDF and DOCX only, up to 5 MB. Parsed suggestions remain drafts until you confirm each fact."));
    const upload = element("form", "upload-form");
    const file = document.createElement("input");
    file.type = "file";
    file.name = "resume";
    file.accept = ".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document";
    file.required = true;
    const fileLabel = element("label", "file-input-label", "Resume PDF or DOCX");
    fileLabel.appendChild(file);
    const submit = element("button", "secondary-button", "Upload and extract");
    submit.type = "submit";
    const uploadStatus = element("p", "form-status");
    uploadStatus.setAttribute("aria-live", "polite");
    upload.append(fileLabel, submit, uploadStatus);
    upload.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!file.files.length) return;
      submit.disabled = true;
      uploadStatus.textContent = "Scanning and extracting…";
      const body = new FormData();
      body.append("resume", file.files[0]);
      try {
        await api("/api/v1/resumes", { method: "POST", body });
        await loadProfile();
      } catch (error) {
        uploadStatus.textContent = error.message;
        submit.disabled = false;
      }
    });
    resumeSection.appendChild(upload);
    const resumes = resumesPayload?.items || [];
    if (!resumes.length) {
      resumeSection.appendChild(element("p", "empty-inline", "No resume versions uploaded yet."));
    } else {
      const list = element("div", "resume-list");
      resumes.forEach((record) => list.appendChild(createResumeCard(record)));
      resumeSection.appendChild(list);
    }
    els.results.appendChild(tagSection(resumeSection, "resumes", "Resumes"));
    els.results.appendChild(tagSection(notificationSettingsSection(connections, preferences, events, applications), "alerts", "Connections and alerts"));
    els.results.appendChild(tagSection(dossierSection(dossier), "dossier", "Evidence dossier"));
    els.results.appendChild(tagSection(extensionSection(extensionDevices), "extension", "Browser extension"));
    els.results.appendChild(tagSection(accountSection(), "account", "Account"));
    sectionSubnav();
    els.results.setAttribute("aria-busy", "false");
  }

  async function loadProfile() {
    const sequence = ++state.loadSequence;
    clearError();
    els.results.setAttribute("aria-busy", "true");
    els.results.replaceChildren(element("p", "detail-loading", "Loading your private profile…"));
    try {
      const [profile, resumes, connections, preferences, events, applications, dossier, extensionDevices] = await Promise.all([
        api("/api/v1/profile"),
        api("/api/v1/resumes"),
        api("/api/v1/connections"),
        api("/api/v1/notification-preferences"),
        api("/api/v1/monitored-events"),
        api("/api/v1/applications"),
        api("/api/v1/dossier"),
        api("/api/v1/extension/devices"),
      ]);
      if (sequence !== state.loadSequence || state.view !== "profile") return;
      renderProfile(profile, resumes, connections, preferences, events, applications, dossier, extensionDevices);
    } catch (error) {
      showLoadError(error, sequence);
    }
  }

  function opportunityOptions(select, applications) {
    applications.forEach((application) => {
      const option = document.createElement("option");
      option.value = application.opportunity_id;
      option.textContent = `${application.company} — ${application.title}`;
      select.appendChild(option);
    });
  }

  function preparationDocumentCard(documentRecord) {
    const card = element("article", "preparation-item");
    const heading = element("div", "preparation-heading");
    const identity = element("div");
    identity.appendChild(element("strong", "", `${documentRecord.document_type.replaceAll("_", " ")} v${documentRecord.version}`));
    identity.appendChild(element("span", "", `${documentRecord.company} · ${documentRecord.title}`));
    heading.append(identity, chip(documentRecord.status, documentRecord.status === "approved" ? "is-region" : ""));
    const editor = document.createElement("textarea");
    editor.className = "document-editor";
    editor.value = documentRecord.content;
    editor.setAttribute("aria-label", `Edit ${documentRecord.document_type} version ${documentRecord.version}`);
    const evidence = element("div", "document-evidence");
    evidence.appendChild(element("p", "eyebrow", "Confirmed evidence"));
    const fields = [...new Set((documentRecord.evidence || []).map((item) => item.profile_field))];
    fields.forEach((field) => evidence.appendChild(chip(`profile.${field}`, "is-region")));
    if (!fields.some((field) => field !== "name")) {
      // A grounded draft can only use confirmed facts; say why this one is thin.
      evidence.appendChild(element("p", "score-note", "Add confirmed skills and experience in Profile to fill this draft."));
    }
    const actions = element("div", "preparation-actions");
    const save = element("button", "secondary-button", "Save edit");
    save.type = "button";
    const approve = element("button", "secondary-button", documentRecord.status === "approved" ? "Approved" : "Approve version");
    approve.type = "button";
    approve.disabled = documentRecord.status === "approved";
    const download = element("a", "secondary-button", "Download Markdown");
    download.href = `/api/v1/preparation/documents/${encodeURIComponent(documentRecord.id)}/download`;
    const remove = element("button", "danger-button", "Delete version");
    remove.type = "button";
    const statusLine = element("p", "form-status");
    statusLine.setAttribute("aria-live", "polite");
    // Fetched rather than linked, so a missing browser build shows a message
    // here instead of navigating the tab to an error page.
    const pdf = element("button", "secondary-button", "Download PDF");
    pdf.type = "button";
    pdf.hidden = !state.pdfAvailable;
    pdf.addEventListener("click", async () => {
      pdf.disabled = true;
      statusLine.textContent = "Rendering the PDF…";
      try {
        const response = await fetch(`/api/v1/preparation/documents/${encodeURIComponent(documentRecord.id)}/pdf`, { credentials: "same-origin" });
        if (!response.ok) {
          const detail = await response.json().catch(() => ({}));
          throw new Error(detail.detail || `The PDF could not be made (HTTP ${response.status}).`);
        }
        const url = URL.createObjectURL(await response.blob());
        const link = document.createElement("a");
        link.href = url;
        link.download = `${documentRecord.document_type}-v${documentRecord.version}.pdf`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
        statusLine.textContent = "PDF downloaded. Check it before you send it.";
      } catch (error) {
        statusLine.textContent = error.message;
      } finally {
        pdf.disabled = false;
      }
    });
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        await api(`/api/v1/preparation/documents/${encodeURIComponent(documentRecord.id)}`, {
          method: "PUT",
          body: JSON.stringify({ content: editor.value, evidence_fields: fields }),
        });
        statusLine.textContent = "Saved as an evidence-linked draft.";
        approve.disabled = false;
        approve.textContent = "Approve version";
      } catch (error) {
        statusLine.textContent = error.message;
      } finally {
        save.disabled = false;
      }
    });
    remove.addEventListener("click", async () => {
      if (!window.confirm("Delete this document version and its attachable PDF?")) return;
      await api(`/api/v1/preparation/documents/${encodeURIComponent(documentRecord.id)}`, {method: "DELETE"});
      await loadPreparation();
    });
    approve.addEventListener("click", async () => {
      approve.disabled = true;
      try {
        await api(`/api/v1/preparation/documents/${encodeURIComponent(documentRecord.id)}/approve`, { method: "POST" });
        approve.textContent = "Approved";
        statusLine.textContent = "Approved for your use. Nothing was sent.";
      } catch (error) {
        approve.disabled = false;
        statusLine.textContent = error.message;
      }
    });
    actions.append(save, approve, download, pdf, remove);
    card.append(heading, editor, evidence, actions, statusLine);
    if (documentRecord.diff) {
      const diff = element("details", "document-diff");
      diff.appendChild(element("summary", "", "Compare with previous version"));
      diff.appendChild(element("pre", "", documentRecord.diff));
      card.appendChild(diff);
    }
    return card;
  }

  // The recording streams from the server with the session cookie; nothing
  // leaves the machine, and preload="none" fetches it only when played.
  function answerRecording(answerId, label) {
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.preload = "none";
    audio.src = `/api/v1/preparation/answers/${encodeURIComponent(answerId)}/audio`;
    audio.setAttribute("aria-label", label);
    return audio;
  }

  function earlierAnswers(question, index) {
    const answers = question.answers || [];
    if (!answers.length) return null;
    const details = element("details", "interview-earlier");
    details.appendChild(element("summary", "", plural(answers.length, "earlier answer", "earlier answers")));
    answers.forEach((answer) => {
      const item = element("div", "interview-earlier-answer");
      item.appendChild(element("p", "eyebrow", `${formatDate(answer.created_at)} · ${answer.score}/100`));
      item.appendChild(element("p", "", answer.answer_text));
      if (answer.has_audio) item.appendChild(answerRecording(answer.id, `Recording of your answer to question ${index + 1}, ${formatDate(answer.created_at)}`));
      (answer.feedback || []).forEach((line) => item.appendChild(element("p", "profile-help", line)));
      details.appendChild(item);
    });
    return details;
  }

  function renderInterview(interview, host) {
    host.replaceChildren();
    host.appendChild(element("h3", "", `${interview.company}: ${interview.title}`));
    interview.questions.forEach((question, index) => {
      const card = element("article", "interview-question");
      card.appendChild(element("p", "eyebrow", `Question ${index + 1}`));
      card.appendChild(element("h4", "", question.prompt));
      const promptActions = element("div", "preparation-actions");
      const speak = element("button", "secondary-button", "Read aloud");
      speak.type = "button";
      speak.addEventListener("click", () => {
        if (!("speechSynthesis" in window)) return;
        window.speechSynthesis.cancel();
        window.speechSynthesis.speak(new SpeechSynthesisUtterance(question.prompt));
      });
      promptActions.appendChild(speak);
      card.appendChild(promptActions);
      const earlier = earlierAnswers(question, index);
      if (earlier) card.appendChild(earlier);
      const form = element("form", "interview-answer-form");
      const answer = document.createElement("textarea");
      answer.placeholder = "Type your answer, or use voice transcription and review the text before submitting.";
      answer.required = true;
      const voice = element("button", "secondary-button", "Transcribe voice");
      voice.type = "button";
      voice.setAttribute("aria-pressed", "false");
      const record = element("button", "secondary-button", "Record answer");
      record.type = "button";
      record.setAttribute("aria-pressed", "false");
      const recordingStatus = element("p", "profile-help", "No audio recording saved.");
      recordingStatus.setAttribute("aria-live", "polite");
      let recordingBlob = null;
      let recorder = null;
      let recordingStream = null;
      let stopActiveRecording = null;
      const finishRecording = () => {
        recordingStream?.getTracks().forEach((track) => track.stop());
        recordingStream = null;
        recorder = null;
        if (state.activeRecordingStop === stopActiveRecording) state.activeRecordingStop = null;
        record.textContent = "Record again";
        record.setAttribute("aria-pressed", "false");
      };
      stopActiveRecording = () => {
        if (recorder?.state === "recording") recorder.stop();
        else finishRecording();
      };
      if (!("MediaRecorder" in window) || !navigator.mediaDevices?.getUserMedia) {
        record.disabled = true;
        record.title = "Audio recording is unavailable in this browser; typed and transcribed answers remain available.";
      } else {
        record.addEventListener("click", async () => {
          if (recorder?.state === "recording") {
            recorder.stop();
            return;
          }
          try {
            recordingStream = await navigator.mediaDevices.getUserMedia({ audio: true });
            const chunks = [];
            recorder = new MediaRecorder(recordingStream);
            recorder.addEventListener("dataavailable", (event) => {
              if (event.data.size) chunks.push(event.data);
            });
            recorder.addEventListener("stop", () => {
              recordingBlob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
              recordingStatus.textContent = `Recording ready for private upload (${Math.max(1, Math.ceil(recordingBlob.size / 1024))} KB).`;
              finishRecording();
            }, { once: true });
            recorder.addEventListener("error", () => {
              recordingStatus.textContent = "Recording failed; no audio was saved.";
              finishRecording();
            }, { once: true });
            recorder.start();
            state.activeRecordingStop = stopActiveRecording;
            record.textContent = "Stop recording";
            record.setAttribute("aria-pressed", "true");
            recordingStatus.textContent = "Microphone active — select Stop recording when finished.";
          } catch (_) {
            recordingStatus.textContent = "Microphone permission was not granted; no audio was saved.";
            finishRecording();
          }
        });
      }
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SpeechRecognition) {
        voice.disabled = true;
        voice.title = "Voice transcription is unavailable in this browser; the text path remains fully supported.";
      } else {
        voice.addEventListener("click", () => {
          const recognition = new SpeechRecognition();
          recognition.lang = navigator.language || "en-US";
          recognition.interimResults = false;
          voice.disabled = true;
          voice.textContent = "Microphone active…";
          voice.setAttribute("aria-pressed", "true");
          recognition.addEventListener("result", (event) => {
            const transcript = event.results[0]?.[0]?.transcript || "";
            answer.value = `${answer.value} ${transcript}`.trim();
          });
          recognition.addEventListener("end", () => {
            voice.disabled = false;
            voice.textContent = "Transcribe voice";
            voice.setAttribute("aria-pressed", "false");
          });
          recognition.addEventListener("error", () => {
            voice.disabled = false;
            voice.textContent = "Transcribe voice";
            voice.setAttribute("aria-pressed", "false");
          });
          recognition.start();
        });
      }
      const submit = element("button", "primary-button", "Score reviewed answer");
      submit.type = "submit";
      const feedback = element("div", "interview-feedback");
      form.append(answer, voice, record, recordingStatus, submit, feedback);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        submit.disabled = true;
        try {
          let path = `/api/v1/preparation/questions/${encodeURIComponent(question.id)}/answers`;
          let body = JSON.stringify({ answer_text: answer.value, transcript: "" });
          if (recordingBlob) {
            path = `/api/v1/preparation/questions/${encodeURIComponent(question.id)}/recorded-answers`;
            body = new FormData();
            body.append("answer_text", answer.value);
            body.append("transcript", answer.value);
            body.append("audio", recordingBlob, `mock-answer.${recordingBlob.type.includes("ogg") ? "ogg" : "webm"}`);
          }
          const result = await api(path, { method: "POST", body });
          feedback.replaceChildren(element("strong", "", `${result.score}/100`));
          result.feedback.forEach((item) => feedback.appendChild(element("p", "", item)));
          if (result.has_audio) {
            recordingStatus.textContent = "Recording saved privately with this scored answer.";
            feedback.appendChild(answerRecording(result.id, `Recording of your answer to question ${index + 1}`));
            recordingBlob = null;
          }
          submit.textContent = "Retry and rescore";
        } catch (error) {
          feedback.replaceChildren(element("p", "form-error", error.message));
        } finally {
          submit.disabled = false;
        }
      });
      card.appendChild(form);
      host.appendChild(card);
    });
  }

  async function loadPreparation() {
    const sequence = ++state.loadSequence;
    clearError();
    els.results.setAttribute("aria-busy", "true");
    els.results.replaceChildren(element("p", "detail-loading", "Loading preparation workspace…"));
    try {
      const [documents, answers, applications, providersPayload, interviews] = await Promise.all([
        api("/api/v1/preparation/documents"),
        api("/api/v1/preparation/answers"),
        api("/api/v1/applications"),
        api("/api/v1/agent/providers"),
        api("/api/v1/preparation/interviews"),
      ]);
      if (sequence !== state.loadSequence || state.view !== "prepare") return;
      els.results.replaceChildren();
      els.resultCount.textContent = "Preparation workspace";
      els.pageStatus.textContent = "Drafts never send or submit themselves";

      const documentSection = element("section", "profile-card preparation-section");
      documentSection.appendChild(element("p", "eyebrow", "Documents"));
      documentSection.appendChild(element("h3", "", "Resume and cover-letter versions"));
      const generator = element("form", "preparation-create-form");
      const opportunity = document.createElement("select");
      opportunityOptions(opportunity, applications.items || []);
      opportunity.setAttribute("aria-label", "Application opportunity");
      const type = document.createElement("select");
      type.setAttribute("aria-label", "Document type");
      [["resume", "Resume variant"], ["cover_letter", "Cover letter"]].forEach(([value, label]) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        type.appendChild(option);
      });
      const provider = document.createElement("select");
      provider.setAttribute("aria-label", "Draft generation method");
      const groundedOption = document.createElement("option");
      groundedOption.value = "";
      groundedOption.textContent = "Grounded template (no AI)";
      provider.appendChild(groundedOption);
      (providersPayload.items || []).filter((item) => item.configured).forEach((item) => {
        const option = document.createElement("option");
        option.value = item.id;
        option.textContent = `${item.display_name} · ${item.model}`;
        provider.appendChild(option);
      });
      const generate = element("button", "primary-button", "Generate grounded draft");
      generate.type = "submit";
      const generatorStatus = element("p", "form-status");
      generator.append(opportunity, type, provider, generate, generatorStatus);
      generator.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!opportunity.value) {
          generatorStatus.textContent = "Start or capture an application first.";
          return;
        }
        generate.disabled = true;
        try {
          await api("/api/v1/preparation/documents", {
            method: "POST",
            body: JSON.stringify({
              opportunity_id: opportunity.value,
              document_type: type.value,
              provider: provider.value || null,
            }),
          });
          await loadPreparation();
        } catch (error) {
          generatorStatus.textContent = error.message;
          generate.disabled = false;
        }
      });
      documentSection.appendChild(generator);
      const documentList = element("div", "preparation-list");
      state.pdfAvailable = Boolean(documents.pdf_available);
      documents.items.forEach((record) => documentList.appendChild(preparationDocumentCard(record)));
      if (!documents.items.length) documentList.appendChild(element("p", "empty-inline", "No generated documents yet."));
      documentSection.appendChild(documentList);

      const answerSection = element("section", "profile-card preparation-section");
      answerSection.appendChild(element("p", "eyebrow", "Reusable answers"));
      answerSection.appendChild(element("h3", "", "Answer library"));
      const answerForm = element("form", "answer-create-form");
      const question = document.createElement("input");
      question.placeholder = "Application question";
      question.required = true;
      const answerText = document.createElement("textarea");
      answerText.placeholder = "Your reviewed answer";
      answerText.required = true;
      const company = document.createElement("input");
      company.placeholder = "Company (optional)";
      const tags = document.createElement("input");
      tags.placeholder = "Tags, comma separated";
      const saveAnswer = element("button", "secondary-button", "Save answer");
      saveAnswer.type = "submit";
      answerForm.append(question, answerText, company, tags, saveAnswer);
      answerForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        saveAnswer.disabled = true;
        try {
          await api("/api/v1/preparation/answers", {
            method: "POST",
            body: JSON.stringify({ question: question.value, answer: answerText.value, company: company.value, tags: commaList(tags.value) }),
          });
          await loadPreparation();
        } catch (error) {
          showError(error.message);
          saveAnswer.disabled = false;
        }
      });
      answerSection.appendChild(answerForm);
      const answerList = element("div", "answer-list");
      answers.items.forEach((item) => {
        const card = element("article", "answer-item");
        card.appendChild(element("strong", "", item.question));
        card.appendChild(element("p", "", item.answer));
        card.appendChild(element("span", "", [item.company, ...(item.tags || [])].filter(Boolean).join(" · ")));
        const remove = element("button", "danger-button", "Delete");
        remove.type = "button";
        remove.addEventListener("click", async () => {
          if (!window.confirm("Delete this saved answer?")) return;
          await api(`/api/v1/preparation/answers/${encodeURIComponent(item.id)}`, { method: "DELETE" });
          await loadPreparation();
        });
        card.appendChild(remove);
        answerList.appendChild(card);
      });
      if (answers.items.length) {
        const deleteAll = element("button", "danger-button", "Delete all answers");
        deleteAll.type = "button";
        deleteAll.addEventListener("click", async () => {
          if (!window.confirm("Permanently delete every saved answer?")) return;
          await api("/api/v1/preparation/answers/all", { method: "DELETE" });
          await loadPreparation();
        });
        answerSection.appendChild(deleteAll);
      }
      answerSection.appendChild(answerList);

      const interviewSection = element("section", "profile-card preparation-section");
      interviewSection.appendChild(element("p", "eyebrow", "Practice"));
      interviewSection.appendChild(element("h3", "", "Mock interview"));
      interviewSection.appendChild(element("p", "profile-help", "Text works everywhere. Voice transcription and optional private audio recording ask for microphone permission and show a visible active state. You review the answer before it is scored."));
      const interviewForm = element("form", "preparation-create-form");
      const interviewOpportunity = document.createElement("select");
      opportunityOptions(interviewOpportunity, applications.items || []);
      interviewOpportunity.setAttribute("aria-label", "Interview opportunity");
      const start = element("button", "primary-button", "Start mock interview");
      start.type = "submit";
      interviewForm.append(interviewOpportunity, start);
      const interviewHost = element("div", "interview-host");
      interviewForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!interviewOpportunity.value) return;
        start.disabled = true;
        try {
          const interview = await api("/api/v1/preparation/interviews", {
            method: "POST",
            body: JSON.stringify({ opportunity_id: interviewOpportunity.value }),
          });
          renderInterview(interview, interviewHost);
        } catch (error) {
          showError(error.message);
        } finally {
          start.disabled = false;
        }
      });
      interviewSection.append(interviewForm);
      if (interviews.items.length) {
        const past = element("details", "interview-past");
        past.appendChild(element("summary", "", `Earlier practice (${interviews.items.length})`));
        const list = element("ul", "interview-past-list");
        interviews.items.forEach((entry) => {
          const row = element("li", "interview-past-row");
          const facts = [plural(entry.answers, "answer", "answers")];
          if (entry.recordings) facts.push(plural(entry.recordings, "recording", "recordings"));
          row.appendChild(element("span", "", `${entry.company}: ${entry.title} · ${formatDate(entry.created_at)} · ${facts.join(", ")}`));
          const open = element("button", "secondary-button", "Open");
          open.type = "button";
          open.setAttribute("aria-label", `Open the ${entry.company} practice from ${formatDate(entry.created_at)}`);
          open.addEventListener("click", async () => {
            open.disabled = true;
            try {
              renderInterview(await api(`/api/v1/preparation/interviews/${encodeURIComponent(entry.id)}`), interviewHost);
              interviewHost.querySelector("h3")?.setAttribute("tabindex", "-1");
              interviewHost.querySelector("h3")?.focus();
            } catch (error) {
              showError(error.message);
            } finally {
              open.disabled = false;
            }
          });
          row.appendChild(open);
          list.appendChild(row);
        });
        past.appendChild(list);
        interviewSection.appendChild(past);
      }
      interviewSection.appendChild(interviewHost);
      els.results.append(
        tagSection(documentSection, "documents", "Documents"),
        tagSection(answerSection, "answers", "Saved answers"),
        tagSection(interviewSection, "interview", "Interview practice"),
      );
      sectionSubnav();
      els.results.setAttribute("aria-busy", "false");
    } catch (error) {
      showLoadError(error, sequence);
    }
  }

  async function loadAgent() {
    const sequence = ++state.loadSequence;
    clearError();
    els.results.setAttribute("aria-busy", "true");
    els.results.replaceChildren(element("p", "detail-loading", "Loading agent history…"));
    try {
      const [threadsPayload, activityPayload, providersPayload] = await Promise.all([
        api("/api/v1/agent/threads"),
        api("/api/v1/agent/activity"),
        api("/api/v1/agent/providers"),
      ]);
      if (sequence !== state.loadSequence || state.view !== "agent") return;
      const threads = threadsPayload.items || [];
      const providers = (providersPayload.items || []).filter((provider) => provider.configured);
      if (!state.agentThreadId || !threads.some((thread) => thread.id === state.agentThreadId)) {
        state.agentThreadId = threads[0]?.id || null;
      }
      els.results.replaceChildren();
      els.resultCount.textContent = "Student agent";
      els.pageStatus.textContent = "No mutation runs without your approval";

      const shell = element("section", "agent-shell");
      const sidebar = element("aside", "agent-thread-list");
      const providerSelect = document.createElement("select");
      providerSelect.className = "agent-provider-select";
      providerSelect.setAttribute("aria-label", "Agent provider for new thread");
      const legacyOption = document.createElement("option");
      legacyOption.value = "legacy";
      legacyOption.textContent = "Built-in grounded assistant · no API key";
      providerSelect.appendChild(legacyOption);
      providers.forEach((provider) => {
        const option = document.createElement("option");
        option.value = provider.id;
        option.textContent = `${provider.display_name} · ${provider.model}`;
        providerSelect.appendChild(option);
      });
      const newThread = element("button", "secondary-button", "New thread");
      newThread.type = "button";
      newThread.addEventListener("click", async () => {
        newThread.disabled = true;
        try {
          const created = await api("/api/v1/agent/threads", {
            method: "POST",
            body: JSON.stringify({ title: "Career planning", provider: providerSelect.value }),
          });
          state.agentThreadId = created.id;
          await loadAgent();
        } catch (error) {
          showError(error.message);
          newThread.disabled = false;
        }
      });
      sidebar.append(providerSelect, newThread);
      if (!providers.length) {
        sidebar.appendChild(element("p", "empty-inline", "The built-in grounded assistant is ready. Add OPENAI_API_KEY or ANTHROPIC_API_KEY for a model-backed thread."));
      }
      threads.forEach((thread) => {
        const button = element("button", `agent-thread-button ${thread.id === state.agentThreadId ? "is-active" : ""}`.trim());
        button.type = "button";
        button.appendChild(element("strong", "", thread.title));
        button.appendChild(element("span", "", `${thread.provider} · ${thread.status} · ${thread.messages_used}/${thread.message_budget} messages`));
        button.addEventListener("click", () => {
          state.agentThreadId = thread.id;
          loadAgent();
        });
        sidebar.appendChild(button);
      });

      const main = element("div", "agent-main");
      const thread = threads.find((item) => item.id === state.agentThreadId);
      if (!thread) {
        const empty = element("div", "empty-state");
        empty.appendChild(element("strong", "", "Start an agent thread"));
        empty.appendChild(element("p", "", "Threads preserve messages, tool runs, budgets, and every proposed action."));
        main.appendChild(empty);
      } else {
        const toolbar = element("div", "agent-toolbar");
        const title = element("div", "");
        title.appendChild(element("h3", "", thread.title));
        title.appendChild(element("p", "", `${thread.tools_used}/${thread.tool_budget} tool calls used`));
        toolbar.appendChild(title);
        if (thread.status === "active") {
          const cancel = element("button", "danger-button", "Cancel thread");
          cancel.type = "button";
          cancel.addEventListener("click", async () => {
            if (!window.confirm("Cancel this thread and reject its pending actions?")) return;
            await api(`/api/v1/agent/threads/${encodeURIComponent(thread.id)}/cancel`, { method: "POST" });
            await loadAgent();
          });
          toolbar.appendChild(cancel);
        }
        main.appendChild(toolbar);

        const messages = element("div", "agent-messages");
        thread.messages.forEach((message) => {
          const bubble = element("article", `agent-message is-${message.role}`);
          bubble.appendChild(element("p", "agent-role", message.role === "user" ? "You" : "Pipeline agent"));
          bubble.appendChild(element("p", "agent-content", message.content));
          if (message.citations?.length) {
            const citations = element("div", "agent-citations");
            message.citations.forEach((citation) => {
              citations.appendChild(chip(
                citation.id ? `${citation.type}:${citation.id}` : citation.field ? `profile.${citation.field}` : citation.type,
                "is-region"
              ));
            });
            bubble.appendChild(citations);
          }
          messages.appendChild(bubble);
        });
        if (!thread.messages.length) messages.appendChild(element("p", "empty-inline", "Ask about top matches, profile gaps, deadlines, or next tasks."));
        main.appendChild(messages);

        const pendingActions = thread.proposed_actions.filter((action) => action.status === "pending");
        if (pendingActions.length) {
          const proposals = element("section", "agent-proposals");
          proposals.appendChild(element("p", "eyebrow", "Awaiting your decision"));
          pendingActions.forEach((proposal) => {
            const card = element("article", "proposal-card");
            card.appendChild(element("strong", "", proposal.action_type.replaceAll("_", " ")));
            card.appendChild(element("p", "", proposal.expected_effect));
            card.appendChild(element("code", "", proposal.scope));
            const controls = element("div", "preparation-actions");
            const approve = element("button", "secondary-button", "Approve");
            approve.type = "button";
            const reject = element("button", "danger-button", "Reject");
            reject.type = "button";
            const decide = async (decision) => {
              approve.disabled = true;
              reject.disabled = true;
              try {
                await api(`/api/v1/agent/proposals/${encodeURIComponent(proposal.id)}/decision`, {
                  method: "POST",
                  body: JSON.stringify({ decision }),
                });
                await Promise.all([loadAgent(), loadStats()]);
              } catch (error) {
                showError(error.message);
                approve.disabled = false;
                reject.disabled = false;
              }
            };
            approve.addEventListener("click", () => decide("approve"));
            reject.addEventListener("click", () => decide("reject"));
            controls.append(approve, reject);
            card.appendChild(controls);
            proposals.appendChild(card);
          });
          main.appendChild(proposals);
        }

        if (thread.status === "active") {
          const prompts = element("div", "agent-quick-prompts");
          ["What should I apply to?", "What am I missing?", "What closes soon?", "What should I do next?"].forEach((prompt) => {
            const button = element("button", "chip", prompt);
            button.type = "button";
            prompts.appendChild(button);
          });
          const composer = element("form", "agent-composer");
          const input = document.createElement("textarea");
          input.placeholder = "Ask about your evidence-backed pipeline";
          input.required = true;
          const send = element("button", "primary-button", "Send");
          send.type = "submit";
          const composerStatus = element("p", "form-status");
          const sendContent = async (content) => {
            send.disabled = true;
            composerStatus.textContent = "Checking your workspace…";
            try {
              await api(`/api/v1/agent/threads/${encodeURIComponent(thread.id)}/messages`, {
                method: "POST",
                body: JSON.stringify({ content }),
              });
              await loadAgent();
            } catch (error) {
              composerStatus.textContent = error.message;
              send.disabled = false;
              await loadAgent();
            }
          };
          prompts.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => sendContent(button.textContent)));
          composer.addEventListener("submit", (event) => {
            event.preventDefault();
            sendContent(input.value);
          });
          composer.append(input, send, composerStatus);
          main.append(prompts, composer);
        }
      }
      shell.append(sidebar, main);

      const activity = element("section", "profile-card agent-activity");
      activity.appendChild(element("p", "eyebrow", "Background activity"));
      activity.appendChild(element("h3", "", "Tool and approval audit"));
      const activityList = element("ol", "timeline-list");
      activityPayload.items.forEach((item) => {
        const row = element("li", "");
        row.appendChild(element("strong", "", item.label.replaceAll("_", " ")));
        row.appendChild(element("span", "", `${item.status} · ${formatDate(item.created_at)}`));
        activityList.appendChild(row);
      });
      if (!activityPayload.items.length) activityList.appendChild(element("li", "", "No tool activity yet."));
      activity.appendChild(activityList);
      els.results.append(tagSection(shell, "chat", "Conversation"), tagSection(activity, "activity", "Tool and approval audit"));
      sectionSubnav();
      els.results.setAttribute("aria-busy", "false");
    } catch (error) {
      showLoadError(error, sequence);
    }
  }

  const URGENT_KIND_LABELS = {
    posting_deadline: "Deadline",
    your_deadline: "Your deadline",
    program_deadline: "Program deadline",
    outreach_deadline: "Outreach deadline",
    task: "Task",
    application_follow_up: "Follow-up",
    outreach_follow_up: "Outreach follow-up",
    outreach_revisit: "Outreach revisit",
  };
  const URGENT_GROUPS = [
    ["overdue", "Overdue", (item) => item.days_until < 0],
    ["today", "Today", (item) => item.days_until === 0],
    ["week", "This week", (item) => item.days_until >= 1 && item.days_until <= 6],
    ["later", "Later", (item) => item.days_until >= 7],
  ];

  async function loadUrgent() {
    const sequence = ++state.loadSequence;
    clearError();
    els.results.setAttribute("aria-busy", "true");
    els.results.replaceChildren(element("p", "detail-loading", "Loading what is due…"));
    try {
      const payload = await api(`/api/v1/urgent?days=${SOON_DAYS}`);
      if (sequence !== state.loadSequence || state.view !== "urgent") return;
      renderUrgent(payload);
      urgentBadge.generation += 1;
      applyFreshUrgentCount(payload.counts.attention);
    } catch (error) {
      showLoadError(error, sequence);
    }
  }

  function urgentWhen(item) {
    if (item.days_until < 0) return `${plural(-item.days_until, "day", "days")} overdue`;
    if (item.days_until === 0) return "Today";
    if (item.days_until === 1) return "Tomorrow";
    return `In ${item.days_until} days`;
  }

  function urgentHeadline(item) {
    if (item.kind.startsWith("outreach_")) return [item.company, "Cold outreach"];
    if (item.kind === "task") return [item.title, [item.company, item.subtitle].filter(Boolean).join(" · ")];
    return [item.title, item.company];
  }

  function urgentAction(item) {
    const [headline, context] = urgentHeadline(item);
    if (item.kind === "posting_deadline" || item.kind === "your_deadline") {
      return ["Open role", `Open ${headline} at ${context}`, () => openDetail(item.opportunity_id)];
    }
    if (item.kind === "program_deadline") {
      return ["Open program", `Open ${headline} in ${programsMeta.label}`, () => {
        state.subtabs.programs = "all";
        state.programsFocus = item.program_id;
        setView("programs");
      }];
    }
    if (item.kind === "task" || item.kind === "application_follow_up") {
      return ["Open application", `Open the application for ${item.company}`, () => {
        state.applicationFocus = item.application_id;
        setView("applications");
      }];
    }
    return ["Open outreach", `Open outreach for ${item.company}`, () => {
      state.outreachOpen = item.outreach_target_id;
      state.outreachFocus = item.outreach_target_id;
      setView("outreach");
    }];
  }

  function urgentRow(item, items) {
    const row = element("li", `urgent-row${item.overdue ? " is-overdue" : ""}`);
    const when = element("div", "urgent-when");
    when.appendChild(element("strong", "", formatCalendarDate(item.date)));
    when.appendChild(element("span", "", urgentWhen(item)));

    const body = element("div", "urgent-body");
    const [headline, context] = urgentHeadline(item);
    body.appendChild(element("p", "urgent-kind", URGENT_KIND_LABELS[item.kind] || item.kind));
    body.appendChild(element("h4", "", headline));
    if (context) body.appendChild(element("p", "urgent-context", context));
    body.appendChild(element("p", "urgent-source", item.source_name ? `${item.date_source} · ${item.source_name}` : item.date_source));
    // A researched date can carry its own caveat ("estimated", "rolling");
    // show it so an estimate never reads as a confirmed deadline.
    if (item.date_note) body.appendChild(element("p", "urgent-source urgent-date-note", item.date_note));
    if (item.kind === "posting_deadline" || item.kind === "your_deadline") {
      const twin = items.find((other) => other !== item
        && other.opportunity_id === item.opportunity_id
        && (other.kind === "posting_deadline" || other.kind === "your_deadline"));
      if (twin) body.appendChild(element("p", "urgent-also", `Also: ${twin.date_source.toLocaleLowerCase()} ${formatCalendarDate(twin.date)}`));
    }

    const [label, name, run] = urgentAction(item);
    const action = element("button", "secondary-button urgent-action", label);
    action.type = "button";
    action.setAttribute("aria-label", name);
    action.addEventListener("click", run);
    row.append(when, body, action);
    return row;
  }

  const URGENT_TABS = [
    { id: "all", label: "Everything dated", test: () => true },
    ...URGENT_GROUPS.map(([key, label, test]) => ({ id: key, label, group: "When", tone: key === "overdue" ? "is-alert" : key === "today" ? "is-soon" : "", test })),
    { id: "deadlines", label: "Deadlines", group: "Kind", test: (item) => ["posting_deadline", "your_deadline", "program_deadline", "outreach_deadline"].includes(item.kind) },
    { id: "follow-ups", label: "Follow-ups", group: "Kind", test: (item) => ["application_follow_up", "outreach_follow_up", "outreach_revisit"].includes(item.kind) },
    { id: "tasks", label: "Tasks", group: "Kind", test: (item) => item.kind === "task" },
  ];

  function renderUrgent(payload) {
    els.results.replaceChildren();
    els.results.removeAttribute("role");
    const tab = URGENT_TABS.find((entry) => entry.id === state.subtabs.urgent) || URGENT_TABS[0];
    renderSubnav(URGENT_TABS.map((entry) => ({ ...entry, count: payload.items.filter(entry.test).length })));
    const shown = payload.items.filter(tab.test);
    const total = payload.items.length;
    els.resultCount.textContent = total ? `${plural(total, "item", "items")} with a date` : "Nothing due";
    els.pageStatus.textContent = `${payload.counts.overdue} overdue · ${payload.counts.upcoming} in the next ${payload.window_days} days`;

    const toolbar = element("div", "urgent-toolbar");
    const zone = payload.timezone === "system-local"
      ? `this computer's time zone (UTC${payload.utc_offset})`
      : `${payload.timezone.replaceAll("_", " ")} (UTC${payload.utc_offset})`;
    toolbar.appendChild(element("p", "urgent-note", `Today is ${formatCalendarDate(payload.today)} in ${zone}.`));
    const calendar = element("button", "secondary-button", "Add to calendar (.ics)");
    calendar.type = "button";
    calendar.disabled = !total;
    calendar.addEventListener("click", () => downloadUrgentCalendar(payload));
    toolbar.appendChild(calendar);
    els.results.appendChild(toolbar);

    if (!total) {
      const empty = element("div", "empty-state urgent-empty");
      empty.appendChild(element("h3", "", "Nothing is due in the next two weeks"));
      empty.appendChild(element(
        "p",
        "",
        "Urgent fills from dated records: deadlines stated in posting text, deadlines you enter on a role, outreach deadlines, open application tasks, and follow-up dates. Most postings list no deadline, so open a role and add the one you find on the employer's site."
      ));
      els.results.appendChild(empty);
    }

    if (total && !shown.length) {
      const empty = element("div", "empty-state urgent-empty");
      empty.appendChild(element("h3", "", `Nothing under ${tab.label}`));
      empty.appendChild(element("p", "", "Everything else that is due is under Everything dated."));
      els.results.appendChild(empty);
    }

    URGENT_GROUPS.forEach(([key, label, test]) => {
      const items = shown.filter(test);
      if (!items.length) return;
      const section = element("section", `urgent-group is-${key}`);
      const heading = element("h3", "urgent-group-title", label);
      heading.id = `urgent-group-${key}`;
      const count = element("span", "urgent-count", String(items.length));
      count.setAttribute("aria-label", plural(items.length, "item", "items"));
      heading.appendChild(count);
      section.setAttribute("aria-labelledby", heading.id);
      const list = element("ul", "urgent-list");
      items.forEach((item) => list.appendChild(urgentRow(item, payload.items)));
      section.append(heading, list);
      els.results.appendChild(section);
    });

    const notes = [];
    if (payload.older_overdue) notes.push(`${plural(payload.older_overdue, "item is", "items are")} more than 60 days overdue and not shown.`);
    if (payload.skipped_count) {
      const named = payload.skipped.map((entry) => `${URGENT_KIND_LABELS[entry.kind] || entry.kind} for ${entry.company || entry.title || entry.key}`).join("; ");
      notes.push(`${plural(payload.skipped_count, "item has", "items have")} a date that could not be read: ${named}.`);
    }
    notes.forEach((note) => els.results.appendChild(element("p", "urgent-footnote", note)));
    els.results.setAttribute("aria-busy", "false");
  }

  // Early programs: the student's own researched list, from a private config
  // file. Its name, audience, and evidence wording come from that file, so the
  // view carries nothing about any one student. The server buckets each entry
  // against today in the student's time zone; this view filters, labels, and
  // records the student's own status.
  const PROGRAM_BUCKETS = [
    ["open", "Open now"],
    ["upcoming", "Opens later"],
    ["done", "Applied or skipped"],
    ["closed", "Closed"],
  ];
  const PROGRAM_STATUS_LABELS = { todo: "Not started", applied: "Applied", skipped: "Skipped" };
  const PROGRAM_EVIDENCE_TONES = { explicit: "is-region", not_named: "", unverified: "is-soon" };
  const programsMeta = { label: "Programs", audience: "", evidence: { explicit: "Names your class year" } };

  function programsTabs(items = null) {
    const count = (test) => (items ? items.filter(test).length : undefined);
    return [
      { id: "all", label: "All programs", count: count(() => true), test: () => true },
      ...PROGRAM_BUCKETS.map(([key, label]) => {
        const test = (item) => item.bucket === key;
        return { id: key, label, group: "Status", tone: key === "open" ? "is-soon" : "", count: count(test), test };
      }),
      {
        id: "explicit", label: programsMeta.evidence.explicit, group: "Evidence",
        count: count((item) => item.evidence === "explicit"), test: (item) => item.evidence === "explicit",
      },
    ];
  }

  function programsEyebrow() {
    return programsMeta.audience ? `Programs that take ${programsMeta.audience}` : "Programs for your stage";
  }

  function applyProgramsMeta(payload) {
    programsMeta.label = payload.label || "Programs";
    programsMeta.audience = payload.audience || "";
    programsMeta.evidence = payload.evidence_labels || programsMeta.evidence;
    els.programsNavLabel.textContent = programsMeta.label;
    SUBNAV_TITLES.programs = programsMeta.label;
    if (state.view === "programs") els.pageEyebrow.textContent = programsEyebrow();
  }

  // The nav shows the student's own name for the tab from the moment they sign in.
  async function refreshProgramsLabel() {
    const userId = state.userId;
    try {
      const payload = await api("/api/v1/early-programs");
      if (userId === state.userId) applyProgramsMeta(payload);
    } catch (error) {
      // The generic "Programs" label stays; the tab reports errors when opened.
    }
  }

  async function loadPrograms() {
    const sequence = ++state.loadSequence;
    clearError();
    els.results.setAttribute("aria-busy", "true");
    els.results.replaceChildren(element("p", "detail-loading", "Loading programs…"));
    try {
      const payload = await api("/api/v1/early-programs");
      if (sequence !== state.loadSequence || state.view !== "programs") return;
      applyProgramsMeta(payload);
      renderPrograms(payload);
    } catch (error) {
      showLoadError(error, sequence);
    }
  }

  // Re-render after a status save without the loading placeholder, so focus
  // survives: the rebuilt list gets focus back on the matching control.
  async function reloadPrograms({ trigger, programId, control }) {
    const sequence = ++state.loadSequence;
    try {
      const payload = await api("/api/v1/early-programs");
      if (sequence !== state.loadSequence || state.view !== "programs") return;
      // Read focus now, not when the save started: a keyboard user may have
      // moved on while the save was in flight.
      const active = document.activeElement;
      const order = [...els.results.querySelectorAll(".program-row")].map((row) => row.dataset.programId);
      const activeRow = active && els.results.contains(active) ? active.closest(".program-row") : null;
      const inList = activeRow && active.dataset.programControl && active !== control
        ? { id: activeRow.dataset.programId, role: active.dataset.programControl }
        : null;
      const subtab = active && els.subnavList.contains(active) ? active.dataset.subtab : null;
      const idle = !active || active === document.body || active === control || !active.isConnected;
      // Another row may hold a keyboard choice that is not saved yet; carry it
      // into the rebuilt list so Enter or leaving the select still saves it.
      const unsaved = [...els.results.querySelectorAll('select[data-program-control="status"]')]
        .filter((select) => select !== control && select.value !== select.dataset.savedStatus)
        .map((select) => ({ id: select.closest(".program-row")?.dataset.programId, value: select.value }));
      clearError();
      applyProgramsMeta(payload);
      renderPrograms(payload);
      unsaved.forEach(({ id, value }) => {
        const select = id && els.results.querySelector(
          `.program-row[data-program-id="${CSS.escape(id)}"] select[data-program-control="status"]`
        );
        if (select && [...select.options].some((option) => option.value === value)) select.value = value;
      });
      if (inList) {
        focusProgramControl(inList.id, inList.role, order);
      } else if (subtab) {
        els.subnavList.querySelector(`.subnav-item[data-subtab="${CSS.escape(subtab)}"]`)?.focus();
      } else if (trigger !== "blur" && idle) {
        focusProgramControl(programId, "status", order);
      }
    } catch (error) {
      showLoadError(error, sequence);
    }
  }

  // Focus a program's control in the rebuilt list. When the row left the
  // current sub-tab, take the next row that was below it (else the one above),
  // and with no rows left, the active sub-tab.
  function focusProgramControl(programId, role, order) {
    const find = (id) => els.results.querySelector(
      `.program-row[data-program-id="${CSS.escape(id)}"] [data-program-control="${CSS.escape(role)}"]`
    );
    const index = order.indexOf(programId);
    const candidates = [programId, ...order.slice(index + 1), ...order.slice(0, Math.max(index, 0)).reverse()];
    for (const id of candidates) {
      const target = id && find(id);
      if (target) {
        target.focus();
        return;
      }
    }
    els.subnavList.querySelector(".subnav-item.is-active")?.focus();
  }

  function programWhen(item) {
    if (item.bucket === "upcoming" && item.opens_on) return [formatCalendarDate(item.opens_on), "Opens"];
    // A closed entry is closed whatever its date says: a closed note can end a
    // program before (or without) its listed deadline.
    if (item.bucket === "closed") {
      const past = item.deadline_on && item.days_left !== null && item.days_left < 0;
      const relative = past ? `Closed ${plural(-item.days_left, "day", "days")} ago` : "Closed";
      return [item.deadline_on ? formatCalendarDate(item.deadline_on) : "No date", relative];
    }
    if (!item.deadline_on) return ["No date", item.deadline_note || "None published"];
    const days = item.days_left;
    const relative = days < 0 ? `Closed ${plural(-days, "day", "days")} ago`
      : days === 0 ? "Closes today"
      : days === 1 ? "Closes tomorrow"
      : `${days} days left`;
    return [formatCalendarDate(item.deadline_on), relative];
  }

  function programRow(item) {
    const soon = item.bucket === "open" && item.days_left !== null && item.days_left <= SOON_DAYS;
    const muted = item.bucket === "closed" || item.bucket === "done";
    const row = element("li", `urgent-row program-row${soon ? " is-soon" : ""}${muted ? " is-muted" : ""}`);
    row.dataset.programId = item.id;
    const when = element("div", "urgent-when");
    const [date, relative] = programWhen(item);
    when.appendChild(element("strong", "", date));
    when.appendChild(element("span", "", relative));

    const body = element("div", "urgent-body");
    body.appendChild(element("p", "urgent-kind", [item.kind, item.sector].filter(Boolean).join(" · ") || "Program"));
    body.appendChild(element("h4", "", item.name));
    body.appendChild(element("p", "urgent-context", item.host));
    const chips = element("div", "chip-row program-chips");
    chips.appendChild(element("span", `chip ${PROGRAM_EVIDENCE_TONES[item.evidence] || ""}`.trim(), item.evidence_label));
    if (item.pay) chips.appendChild(element("span", "chip", item.pay));
    if (item.deadline_on && item.deadline_note) chips.appendChild(element("span", "chip", item.deadline_note));
    body.appendChild(chips);
    if (item.eligibility) body.appendChild(element("p", "urgent-context", item.eligibility));
    if (item.closed_note) body.appendChild(element("p", "urgent-also", item.closed_note));
    if (item.notes) body.appendChild(element("p", "urgent-context", item.notes));
    if (item.source_note) body.appendChild(element("p", "urgent-source", `Source: ${item.source_note}`));

    const actions = element("div", "program-actions");
    const link = element("a", "secondary-button", "Official page");
    link.href = item.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.setAttribute("aria-label", `Official page for ${item.name} at ${item.host} (opens in a new tab)`);
    link.dataset.programControl = "official";
    const select = element("select", "program-status");
    select.dataset.programControl = "status";
    select.dataset.savedStatus = item.status;
    select.setAttribute("aria-label", `Your status for ${item.name} at ${item.host}`);
    Object.entries(PROGRAM_STATUS_LABELS).forEach(([value, label]) => {
      const option = element("option", "", label);
      option.value = value;
      option.selected = value === item.status;
      select.appendChild(option);
    });
    autoSaveSelect(select, {
      saved: () => item.status,
      commit: async (status, trigger) => {
        select.disabled = true;
        try {
          await api(`/api/v1/early-programs/${encodeURIComponent(item.id)}/status`, {
            method: "PUT",
            body: JSON.stringify({ status }),
          });
        } catch (error) {
          select.value = item.status;
          select.disabled = false;
          showError(error.message);
          return;
        }
        announce(`${item.name}: ${PROGRAM_STATUS_LABELS[status]}`);
        await reloadPrograms({ trigger, programId: item.id, control: select });
      },
    });
    actions.append(link, select);
    row.append(when, body, actions);
    return row;
  }

  function renderPrograms(payload) {
    els.results.replaceChildren();
    els.results.removeAttribute("role");
    const tabs = programsTabs(payload.items);
    const tab = tabs.find((entry) => entry.id === state.subtabs.programs) || tabs[0];
    renderSubnav(tabs);
    const shown = payload.items.filter(tab.test);
    els.resultCount.textContent = payload.total ? plural(payload.total, "program", "programs") : "No programs";
    els.pageStatus.textContent = `${payload.counts.open} open now · ${payload.counts.upcoming} opening later`;

    const toolbar = element("div", "urgent-toolbar");
    const checked = payload.checked_on
      ? ` Researched ${formatCalendarDate(payload.checked_on)}; confirm on the official page before you apply.`
      : "";
    toolbar.appendChild(element("p", "urgent-note", `Today is ${formatCalendarDate(payload.today)}.${checked}`));
    els.results.appendChild(toolbar);

    if (payload.error || !payload.total) {
      const empty = element("div", "empty-state urgent-empty");
      empty.appendChild(element("h3", "", payload.error ? "Your program list could not be read" : "No programs researched yet"));
      empty.appendChild(element("p", "", payload.error
        || "Ask your coding agent to research programs for you by following SETUP.md, “Programs for your stage”. It asks about your year and field, checks each program on its official page, and writes a private list only you can see."));
      els.results.appendChild(empty);
    } else if (!shown.length) {
      const empty = element("div", "empty-state urgent-empty");
      empty.appendChild(element("h3", "", `Nothing under ${tab.label}`));
      empty.appendChild(element("p", "", "Everything else is under All programs."));
      els.results.appendChild(empty);
    }

    PROGRAM_BUCKETS.forEach(([key, label]) => {
      const items = shown.filter((item) => item.bucket === key);
      if (!items.length) return;
      const section = element("section", `urgent-group is-programs-${key}`);
      const heading = element("h3", "urgent-group-title", label);
      heading.id = `programs-group-${key}`;
      const count = element("span", "urgent-count", String(items.length));
      count.setAttribute("aria-label", plural(items.length, "program", "programs"));
      heading.appendChild(count);
      section.setAttribute("aria-labelledby", heading.id);
      const list = element("ul", "urgent-list");
      items.forEach((item) => list.appendChild(programRow(item)));
      section.append(heading, list);
      els.results.appendChild(section);
    });
    if (payload.skipped) {
      els.results.appendChild(element("p", "urgent-footnote",
        `${plural(payload.skipped, "entry was", "entries were")} skipped because a required field was missing or invalid.`));
    }
    els.results.setAttribute("aria-busy", "false");
    focusRequestedProgram();
  }

  // Arriving from Urgent: bring the program the row was about into view.
  function focusRequestedProgram() {
    const id = state.programsFocus;
    state.programsFocus = null;
    if (!id) return;
    const row = els.results.querySelector(`.program-row[data-program-id="${CSS.escape(id)}"]`);
    if (!row) return;
    row.setAttribute("tabindex", "-1");
    row.classList.add("is-requested");
    row.scrollIntoView({ block: "center" });
    row.focus({ preventScroll: true });
  }

  // RFC 5545 text: escape, CRLF line ends, and fold at 75 octets without ever
  // splitting a UTF-8 sequence (iteration is by code point).
  function icsText(value) {
    return String(value ?? "")
      .replace(/\\/g, "\\\\")
      .replace(/;/g, "\\;")
      .replace(/,/g, "\\,")
      .replace(/\r\n|\r|\n/g, "\\n");
  }

  function icsFold(line) {
    const encoder = new TextEncoder();
    if (encoder.encode(line).length <= 75) return line;
    const parts = [];
    let current = "";
    let size = 0;
    let limit = 75;
    for (const character of line) {
      const bytes = encoder.encode(character).length;
      if (size + bytes > limit) {
        parts.push(current);
        current = character;
        size = bytes;
        // A continuation line starts with a space, which counts toward 75.
        limit = 74;
      } else {
        current += character;
        size += bytes;
      }
    }
    parts.push(current);
    return parts.join("\r\n ");
  }

  function icsDate(value) {
    return value.replaceAll("-", "");
  }

  function icsNextDay(value) {
    const [year, month, day] = value.split("-").map(Number);
    return new Date(Date.UTC(year, month - 1, day + 1)).toISOString().slice(0, 10).replaceAll("-", "");
  }

  function buildUrgentCalendar(items, now = new Date()) {
    const stamp = now.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
    const lines = [
      "BEGIN:VCALENDAR",
      "VERSION:2.0",
      "PRODID:-//Opportunity Pipeline//Urgent export//EN",
      "CALSCALE:GREGORIAN",
      "METHOD:PUBLISH",
    ];
    items.forEach((item) => {
      const [headline, context] = urgentHeadline(item);
      const label = URGENT_KIND_LABELS[item.kind] || item.kind;
      const summary = context && !item.kind.startsWith("outreach_")
        ? `${label}: ${headline} (${context})`
        : `${label}: ${headline}`;
      lines.push(
        "BEGIN:VEVENT",
        // Independent of the date, so re-importing after an edit updates the event.
        `UID:${icsText(item.key)}@opportunity-pipeline.local`,
        `DTSTAMP:${stamp}`,
        `DTSTART;VALUE=DATE:${icsDate(item.date)}`,
        `DTEND;VALUE=DATE:${icsNextDay(item.date)}`,
        `SUMMARY:${icsText(summary)}`,
        `DESCRIPTION:${icsText(`${item.date_source}. Exported from Opportunity Pipeline.`)}`,
        "TRANSP:TRANSPARENT",
        "END:VEVENT"
      );
    });
    lines.push("END:VCALENDAR");
    return `${lines.map(icsFold).join("\r\n")}\r\n`;
  }

  function downloadUrgentCalendar(payload) {
    const blob = new Blob([buildUrgentCalendar(payload.items)], { type: "text/calendar;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = element("a");
    link.href = url;
    link.download = `urgent-${payload.today}.ics`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    announce(`Downloaded ${plural(payload.items.length, "calendar event", "calendar events")}. Nothing was sent anywhere.`);
  }

  // The student's own deadline for a role. It is labelled as theirs, feeds the
  // Urgent queue, and never changes the posting or the purge.
  function userDeadlineSection(item) {
    if (!item.can_set_user_deadline && !item.user_deadline) return null;
    const section = element("section", "detail-section user-deadline");
    section.appendChild(element("p", "eyebrow", "Your deadline (you entered)"));
    const current = element("p", "user-deadline-current");
    current.setAttribute("tabindex", "-1");
    const status = element("p", "form-status");
    status.setAttribute("role", "status");

    const form = element("form", "user-deadline-form");
    const dateLabel = element("label", "profile-field");
    dateLabel.appendChild(element("span", "", "Deadline"));
    const dateInput = document.createElement("input");
    dateInput.type = "date";
    dateInput.required = true;
    dateInput.min = "2020-01-01";
    dateInput.max = "2100-12-31";
    dateLabel.appendChild(dateInput);
    const noteLabel = element("label", "profile-field");
    noteLabel.appendChild(element("span", "", "Where you saw it (optional)"));
    const noteInput = document.createElement("input");
    noteInput.type = "text";
    noteInput.maxLength = 200;
    noteInput.placeholder = "Careers page, recruiter email…";
    noteLabel.appendChild(noteInput);
    const save = element("button", "primary-button", "Save deadline");
    save.type = "submit";
    form.append(dateLabel, noteLabel, save);

    const clear = element("button", "secondary-button", "Clear deadline");
    clear.type = "button";

    function paint() {
      const value = item.user_deadline;
      current.textContent = value
        ? `You entered ${formatCalendarDate(value.deadline_on)}${value.note ? ` · ${value.note}` : ""}.`
        : "No deadline entered. Most postings list none; add the one you find on the employer's site.";
      dateInput.value = value?.deadline_on || "";
      noteInput.value = value?.note || "";
      form.hidden = !item.can_set_user_deadline;
      clear.hidden = !value;
    }

    function refreshBehind() {
      if (["discover", "saved", "urgent"].includes(state.view)) loadCurrentView();
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      save.disabled = true;
      status.textContent = "";
      try {
        item.user_deadline = await api(`/api/v1/opportunities/${encodeURIComponent(item.id)}/deadline`, {
          method: "PUT",
          body: JSON.stringify({ deadline_on: dateInput.value, note: noteInput.value }),
        });
        paint();
        status.textContent = "Saved. It now appears in Urgent as a date you entered.";
        refreshBehind();
      } catch (error) {
        status.textContent = error.message;
      } finally {
        save.disabled = false;
      }
    });
    clear.addEventListener("click", async () => {
      clear.disabled = true;
      status.textContent = "";
      try {
        await api(`/api/v1/opportunities/${encodeURIComponent(item.id)}/deadline`, { method: "DELETE" });
        item.user_deadline = null;
        paint();
        status.textContent = "Cleared.";
        (item.can_set_user_deadline ? dateInput : current).focus();
        refreshBehind();
      } catch (error) {
        status.textContent = error.message;
      } finally {
        clear.disabled = false;
      }
    });

    paint();
    section.append(current, form, clear, status);
    return section;
  }

  const SUBNAV_TITLES = {
    discover: "Discover", urgent: "Urgent", saved: "Saved", applications: "Applications",
    outreach: "Outreach", prepare: "Prepare", agent: "Agent", profile: "Profile",
    programs: "Programs",
  };

  // Each page lists its subtabs in the rail: { id, label, count, tone, group }.
  function renderSubnav(tabs) {
    els.subnavTitle.textContent = SUBNAV_TITLES[state.view] || "";
    const active = state.subtabs[state.view];
    const nodes = [];
    let group = null;
    tabs.forEach((tab) => {
      if (tab.group && tab.group !== group) {
        group = tab.group;
        nodes.push(element("p", "subnav-group", group));
      }
      const button = element("button", `subnav-item${tab.id === active ? " is-active" : ""}`);
      button.type = "button";
      button.dataset.subtab = tab.id;
      button.appendChild(element("span", "subnav-label", tab.label));
      if (tab.count !== undefined && tab.count !== null) {
        const text = tab.total === undefined ? String(tab.count) : `${tab.count} of ${tab.total}`;
        const count = element("span", `subnav-count ${tab.count ? tab.tone || "" : "is-zero"}`.trim(), text);
        count.setAttribute("aria-label", `(${text})`);
        button.appendChild(count);
      }
      if (tab.id === active) button.setAttribute("aria-current", "true");
      button.addEventListener("click", () => selectSubtab(tab.id));
      nodes.push(button);
    });
    els.subnavList.replaceChildren(...nodes);
  }

  function markActiveSubtab() {
    const active = state.subtabs[state.view];
    els.subnavList.querySelectorAll(".subnav-item").forEach((button) => {
      const on = button.dataset.subtab === active;
      button.classList.toggle("is-active", on);
      if (on) button.setAttribute("aria-current", "true");
      else button.removeAttribute("aria-current");
    });
  }

  // Pages built from sections (Profile, Prepare, Agent) filter what is already
  // rendered; list pages reload with the tab's filter.
  const SECTION_VIEWS = new Set(["prepare", "agent", "profile"]);

  function selectSubtab(id) {
    const changed = state.subtabs[state.view] !== id;
    state.subtabs[state.view] = id;
    markActiveSubtab();
    if (SECTION_VIEWS.has(state.view)) {
      applySectionFilter();
      els.results.querySelector(":scope > :not([hidden])")?.scrollIntoView?.({ block: "nearest" });
      return;
    }
    if (!changed && state.view !== "outreach") return;
    state.offset = 0;
    if (state.view === "outreach") state.outreachKeep.clear();
    loadCurrentView();
    window.scrollTo?.({ top: 0 });
  }

  // Tag each top-level section with data-subtab and data-subtab-label; the
  // rail then lists them and "All" shows everything.
  function sectionSubnav() {
    const sections = [...els.results.querySelectorAll(":scope > [data-subtab]")];
    const seen = new Map();
    sections.forEach((node) => { if (!seen.has(node.dataset.subtab)) seen.set(node.dataset.subtab, node.dataset.subtabLabel); });
    if (state.subtabs[state.view] !== "all" && !seen.has(state.subtabs[state.view])) state.subtabs[state.view] = "all";
    renderSubnav([
      { id: "all", label: "All sections" },
      ...[...seen].map(([id, label]) => ({ id, label, group: "Sections" })),
    ]);
    applySectionFilter();
  }

  function applySectionFilter() {
    const active = state.subtabs[state.view] || "all";
    els.results.querySelectorAll(":scope > [data-subtab]").forEach((node) => {
      node.hidden = active !== "all" && node.dataset.subtab !== active;
    });
  }

  function tagSection(node, id, label) {
    node.dataset.subtab = id;
    node.dataset.subtabLabel = label;
    return node;
  }

  const THEMES = ["system", "light", "dark"];

  function currentTheme() {
    try {
      const saved = window.localStorage.getItem("pipeline-theme");
      return THEMES.includes(saved) ? saved : "system";
    } catch (error) {
      return "system";
    }
  }

  function applyTheme(theme) {
    if (theme === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
    const label = theme[0].toUpperCase() + theme.slice(1);
    els.themeLabel.textContent = `Theme: ${label}`;
    els.themeToggle.setAttribute("aria-label", `Color theme: ${label}. Switch theme`);
  }

  function initialSubnavTabs(view) {
    if (view === "discover" || view === "saved") return deckTabs();
    if (view === "urgent") return URGENT_TABS;
    if (view === "applications") return APPLICATION_TABS;
    if (view === "outreach") return OUTREACH_TABS;
    if (view === "programs") return programsTabs();
    return [{ id: "all", label: "All sections" }];
  }

  async function loadCurrentView() {
    if (state.view === "urgent") return loadUrgent();
    if (state.view === "agent") return loadAgent();
    if (state.view === "prepare") return loadPreparation();
    if (state.view === "profile") return loadProfile();
    if (state.view === "applications") return loadApplications();
    if (state.view === "outreach") return loadOutreach();
    if (state.view === "programs") return loadPrograms();
    return loadOpportunities();
  }

  function viewForPath(pathname) {
    if (pathname === "/urgent") return "urgent";
    if (pathname === "/saved") return "saved";
    if (pathname === "/applications") return "applications";
    if (pathname === "/outreach") return "outreach";
    if (pathname === "/programs") return "programs";
    if (pathname === "/profile") return "profile";
    if (pathname === "/prepare") return "prepare";
    if (pathname === "/agent") return "agent";
    return "discover";
  }

  function setView(view, { updateHistory = true } = {}) {
    if (view !== state.view) state.activeRecordingStop?.();
    if (updateHistory) announce("");
    state.loadSequence += 1;
    state.view = view;
    state.offset = 0;
    els.discoverNav.classList.toggle("is-active", view === "discover");
    els.urgentNav.classList.toggle("is-active", view === "urgent");
    els.savedNav.classList.toggle("is-active", view === "saved");
    els.applicationsNav.classList.toggle("is-active", view === "applications");
    els.outreachNav.classList.toggle("is-active", view === "outreach");
    els.programsNav.classList.toggle("is-active", view === "programs");
    els.prepareNav.classList.toggle("is-active", view === "prepare");
    els.agentNav.classList.toggle("is-active", view === "agent");
    els.profileNav.classList.toggle("is-active", view === "profile");
    document.querySelectorAll(".nav-list .nav-item").forEach((button) => {
      if (button.id === `${view}-nav`) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    const isCollection = view === "discover" || view === "saved";
    els.keyboardHint.hidden = !isCollection;
    if (isCollection) els.results.setAttribute("aria-describedby", "keyboard-hint");
    else els.results.removeAttribute("aria-describedby");
    if (!isCollection) els.results.removeAttribute("role");
    els.filterPanel.hidden = !isCollection;
    els.personalizePrompt.hidden = view !== "discover" || state.personalized;
    els.statsGrid.hidden = view === "urgent" || view === "applications" || view === "outreach" || view === "programs" || view === "profile" || view === "prepare" || view === "agent";
    els.paginations.forEach((nav) => { nav.hidden = !isCollection; });
    els.displayToggle.hidden = !isCollection;
    els.results.classList.toggle("is-profile", view === "profile" || view === "prepare" || view === "agent");
    els.resultsEyebrow.textContent = view === "programs" ? "Soonest deadline first" : view === "urgent" ? "Overdue first, then the next 14 days" : view === "outreach" ? "Startup cold outreach" : view === "agent" ? "Auditable career copilot" : view === "prepare" ? "Evidence-grounded practice" : view === "profile" ? "Onboarding and evidence" : view === "applications" ? "Application tracker" : view === "saved" ? "Saved shortlist" : "Ready for review";
    els.pageEyebrow.textContent = view === "programs" ? programsEyebrow() : view === "urgent" ? "Every date has a source" : view === "outreach" ? "Companies without a posting" : view === "agent" ? "Tools, evidence, approval" : view === "prepare" ? "Draft, review, approve" : view === "profile" ? "Private and confirmed by you" : view === "applications" ? "Your applications" : view === "saved" ? "Your chosen opportunities" : "Your live opportunity workspace";
    els.pageTitle.textContent = view === "programs" ? "Programs that fit where you are." : view === "urgent" ? "What needs doing next." : view === "outreach" ? "Reach the startups before they post." : view === "agent" ? "Ask your pipeline, then decide." : view === "prepare" ? "Prepare without inventing a thing." : view === "profile" ? "Build the profile behind every match." : view === "applications" ? "Keep every application moving." : view === "saved" ? "Return to the roles you chose." : "Find the roles worth your time.";
    els.pageLede.textContent = view === "programs"
      ? "Internships, research, scholarships, and externships researched for you, each labeled with how strongly its host says a student at your stage may apply. Dates come from your own research of each program page, so check them on the official page before you rely on one."
      : view === "urgent"
      ? "Deadlines, tasks, and follow-ups that carry a real date, with overdue items first. Each one says where its date came from; nothing is estimated from a posting's age."
      : view === "outreach"
      ? "Research, contacts, cold email drafts, and follow-ups for startups you pitch directly. Unverified contacts stay labeled until you confirm them."
      : view === "agent"
      ? "The agent reads only your authorized workspace, cites its tools, abstains when evidence is missing, and puts every mutation behind an approval card."
      : view === "prepare"
      ? "Every generated claim cites a confirmed profile field. You stay in control of edits, approvals, downloads, and practice recordings."
      : view === "profile"
      ? "Review every imported fact before it affects your profile. Your original files stay private and removable."
      : view === "applications"
      ? "Stages, notes, follow-ups, and tasks stay in one audited place."
      : view === "saved"
      ? "Your shortlist stays focused here, separate from new opportunities that still need a decision."
      : "Every result keeps its source, freshness, and an honest explanation of why it ranked here.";
    if (view !== "outreach") state.outreachKeep.clear();
    renderSubnav(initialSubnavTabs(view));
    if (updateHistory) {
      const path = view === "discover" ? "/" : `/${view}`;
      window.history.pushState({ view }, "", path);
    }
    loadCurrentView();
    if (view !== "urgent") invalidateUrgentBadge();
  }

  function detailLine(label, value) {
    const row = element("div", "detail-line");
    row.appendChild(element("span", "", label));
    row.appendChild(element("strong", "", value || "Not provided"));
    return row;
  }

  function jevProbabilityDetails(answer) {
    const details = element("details", "jev-probabilities");
    details.appendChild(element("summary", "", "Probability distribution"));
    const list = element("ul", "reason-list");
    Object.entries(answer.probabilities || {})
      .sort((left, right) => Number(right[1]) - Number(left[1]))
      .forEach(([label, probability]) => {
        list.appendChild(element("li", "", `${label.replaceAll("_", " ")} · ${Math.round(Number(probability) * 100)}%`));
      });
    details.appendChild(list);
    return details;
  }

  function renderJevReview(host, result) {
    const cards = element("div", "jev-answer-grid");
    Object.values(result.answers || {}).forEach((answer) => {
      const card = element("article", "jev-answer");
      card.appendChild(element("h4", "", answer.name));
      const value = answer.type === "score"
        ? `${answer.score}/${answer.maximum}`
        : String(answer.choice || "unclear").replaceAll("_", " ");
      card.appendChild(element("strong", "jev-answer-value", value));
      card.appendChild(element("p", "", answer.label));
      if (Number.isFinite(answer.confidence)) {
        card.appendChild(element("small", "", `Jev confidence · ${Math.round(answer.confidence * 100)}%`));
      }
      card.appendChild(jevProbabilityDetails(answer));
      cards.appendChild(card);
    });
    const metadata = element("p", "jev-meta",
      `Unconfirmed AI suggestion · ${result.model_resolved} · ${result.usage?.input_tokens || 0} input tokens · score unchanged`);
    const disclosure = element("details", "jev-disclosure");
    disclosure.appendChild(element("summary", "", "Data sent for this review"));
    disclosure.appendChild(element("p", "", `Posting fields: ${(result.opportunity_fields_sent || []).join(", ") || "none"}.`));
    disclosure.appendChild(element("p", "", `Profile fields: ${(result.profile_fields_sent || []).join(", ") || "none"}.`));
    host.replaceChildren(cards, metadata, element("p", "score-note", result.notice), disclosure);
  }

  function jevReviewSection(item) {
    const section = element("section", "detail-section jev-review");
    section.appendChild(element("p", "eyebrow", "Jev second opinion"));
    section.appendChild(element("p", "jev-intro",
      "Run an on-demand semantic review. It sends the posting plus your degree, graduation, skills and interests, location and term preferences, and any authorization, citizenship, or sponsorship answers to TypeSafe. It returns probabilities and never changes this score."));
    const status = element("p", "form-status", "Checking Jev setup…");
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    const button = element("button", "secondary-button", "Run Jev review");
    button.type = "button";
    button.disabled = true;
    const output = element("div", "jev-output");
    section.append(status, button, output);

    api("/api/v1/typesafe").then((configuration) => {
      if (!configuration.configured) {
        status.textContent = `Jev is optional. ${configuration.setup_hint || "Set TYPESAFE_API_KEY in .env and restart the app to enable it."}`;
        return;
      }
      status.textContent = `Ready · ${configuration.model} · nothing is sent until you click.`;
      button.disabled = false;
    }).catch((error) => {
      status.textContent = `Jev setup could not be checked: ${error.message}`;
    });

    button.addEventListener("click", async () => {
      button.disabled = true;
      status.textContent = "Jev is reviewing eight narrow questions…";
      output.replaceChildren();
      try {
        const result = await api(`/api/v1/opportunities/${encodeURIComponent(item.id)}/jev-review`, {
          method: "POST",
        });
        renderJevReview(output, result);
        status.textContent = "Review complete. Treat every result as a suggestion to verify.";
      } catch (error) {
        status.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    });
    return section;
  }

  async function openDetail(id, { updateHistory = true } = {}) {
    if (!state.selectedId) {
      const opener = document.activeElement;
      state.detailReturnFocus = opener && opener !== document.body && els.appShell.contains(opener) ? opener : null;
    }
    state.selectedId = id;
    setBackgroundInert("detail", true);
    els.detail.hidden = false;
    els.detail.classList.add("is-open");
    els.detail.setAttribute("aria-hidden", "false");
    els.detail.removeAttribute("inert");
    els.detailScrim.hidden = false;
    els.detailContent.replaceChildren(element("p", "detail-loading", "Loading opportunity…"));
    document.body.classList.add("has-panel");
    if (updateHistory) {
      state.detailReturnPath = window.location.pathname;
      window.history.pushState({ opportunityId: id }, "", `/opportunities/${encodeURIComponent(id)}`);
    }
    try {
      const item = await api(`/api/v1/opportunities/${encodeURIComponent(id)}`);
      renderDetail(item);
      els.detailClose.focus();
    } catch (error) {
      els.detailContent.replaceChildren(element("p", "form-error", error.message));
    }
  }

  function renderDetail(item) {
    state.detailItem = item;
    const content = document.createDocumentFragment();
    content.appendChild(element("p", "detail-company", item.company));
    const title = element("h2", "", item.title);
    title.id = "detail-title";
    content.appendChild(title);

    const scoreBlock = element("div", "detail-score");
    scoreBlock.appendChild(element("strong", "", `${item.score}/100`));
    const scoreCopy = element("div");
    scoreCopy.appendChild(element("span", "", "Transparent fit score"));
    const progress = document.createElement("progress");
    progress.max = 100;
    progress.value = item.score;
    progress.setAttribute("aria-label", `Fit score ${item.score} out of 100`);
    scoreCopy.appendChild(progress);
    scoreBlock.appendChild(scoreCopy);
    content.appendChild(scoreBlock);

    const facts = element("div", "detail-facts");
    const compensation = item.compensation?.known
      ? `${item.compensation.currency} ${item.compensation.minimum}${item.compensation.maximum !== item.compensation.minimum ? `–${item.compensation.maximum}` : ""} per ${item.compensation.period}`
      : "Not listed";
    facts.append(
      detailLine("Location", item.location),
      detailLine("Region", item.region),
      detailLine("Work mode", item.remote_mode),
      detailLine("Role type", item.role_type),
      detailLine("Term", item.terms?.join(", ") || "Not detected"),
      detailLine("Compensation", compensation),
      detailLine("Posted", formatCalendarDate(item.posted_at)),
      detailLine(
        "Deadline in posting text",
        `${formatCalendarDate(item.deadline_at)}${deadlineState(item.deadline_at) === "passed" ? " (passed)" : ""}`
      ),
      detailLine("Last checked", formatDate(item.last_seen_at)),
      detailLine("Source", item.source_name),
      detailLine("Score version", `${item.score_version || "unknown"} · ${formatDate(item.score_created_at)}`)
    );
    content.appendChild(facts);
    const deadlineSection = userDeadlineSection(item);
    if (deadlineSection) content.appendChild(deadlineSection);
    if (item.company_sort_key) content.appendChild(companyTagsSection(item));

    const why = element("section", "detail-section");
    why.appendChild(element("p", "eyebrow", "Why it ranked here"));
    const reasons = element("ul", "reason-list");
    // Show the starting points too, so the listed adjustments account for the
    // whole score instead of leaving part of it unexplained.
    const adjustments = (item.reasons || []).map((reason) => /^([+-])(\d+)\s/.exec(reason)).filter(Boolean);
    if (Number.isInteger(item.score_base) && adjustments.length) {
      reasons.appendChild(element("li", "", `Starting score +${item.score_base}`));
    }
    (item.reasons?.length ? item.reasons : [baseScoreReason()]).forEach((reason) => {
      reasons.appendChild(element("li", "", reason));
    });
    why.appendChild(reasons);
    if (Number.isInteger(item.score_base) && adjustments.length) {
      const listed = adjustments.reduce((total, [, sign, points]) => total + (sign === "+" ? 1 : -1) * Number(points), item.score_base);
      if (listed !== item.score) {
        const note = listed > 100 || listed < 0
          ? `These add up to ${listed}; scores are capped between 0 and 100.`
          : `These add up to ${listed}; some smaller adjustments are not listed.`;
        why.appendChild(element("p", "score-note", note));
      }
    }
    const evidence = element("div", "evidence-list");
    (item.score_evidence || []).forEach((entry) => {
      evidence.appendChild(element(
        "p",
        "",
        `Evidence: profile.${entry.profile_field} ↔ posting ${entry.opportunity_fields.join(", ")}`
      ));
    });
    why.appendChild(evidence);
    content.appendChild(why);

    if (item.gaps?.length) {
      const gaps = element("section", "detail-section");
      gaps.appendChild(element("p", "eyebrow", "Potential gaps to verify"));
      const gapList = element("ul", "reason-list is-gap");
      item.gaps.forEach((gap) => gapList.appendChild(element("li", "", gap)));
      gaps.appendChild(gapList);
      content.appendChild(gaps);
    }

    content.appendChild(jevReviewSection(item));

    const overview = element("section", "detail-section");
    overview.appendChild(element("p", "eyebrow", "Role overview"));
    overview.appendChild(element("p", "detail-description", item.description || "No description was captured. Use the original posting below."));
    content.appendChild(overview);

    const sourceLink = element("a", "primary-link", "Open original posting ↗");
    sourceLink.href = item.url;
    sourceLink.target = "_blank";
    sourceLink.rel = "noopener noreferrer";
    content.appendChild(sourceLink);

    els.detailContent.replaceChildren(content);
  }

  function closeDetail({ updateHistory = true } = {}) {
    const closedId = state.selectedId;
    const wasOpen = !els.detail.hidden;
    state.selectedId = null;
    els.detail.classList.remove("is-open");
    els.detail.setAttribute("aria-hidden", "true");
    els.detail.setAttribute("inert", "");
    els.detail.hidden = true;
    els.detailScrim.hidden = true;
    document.body.classList.remove("has-panel");
    setBackgroundInert("detail", false);
    if (updateHistory && window.location.pathname !== state.detailReturnPath) {
      window.history.pushState({}, "", state.detailReturnPath || "/");
    }
    const opener = state.detailReturnFocus;
    state.detailReturnFocus = null;
    if (!wasOpen || inertClaims.size) return;
    // Return focus to what opened the panel. A re-render may have replaced that
    // card, so fall back to the same opportunity's card, then the results.
    const target = opener?.isConnected
      ? opener
      : els.results.querySelector(`[data-opportunity-id="${CSS.escape(closedId || "")}"] .card-button`) || els.results;
    if (target === els.results && !els.results.hasAttribute("tabindex")) els.results.setAttribute("tabindex", "-1");
    target.focus();
  }

  // The local launcher opens the app at #launch=<ticket>: a one-time ticket
  // it minted with the owner token from .env. The fragment never reaches the
  // server's logs or a Referer header, and is cleared before anything else runs.
  async function redeemLaunchTicket() {
    const match = window.location.hash.match(/^#launch=([A-Za-z0-9_-]{16,128})$/);
    if (!match) return;
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    try {
      await api("/api/v1/session", { method: "POST", body: JSON.stringify({ launch_ticket: match[1] }) });
    } catch {
      // Fall through to the session check, which shows the sign-in gate.
      state.launchTicketFailed = true;
    }
  }

  async function initialize() {
    try {
      await redeemLaunchTicket();
      const session = await api("/api/v1/session");
      hideAuth(session);
      await flushOutbox();
      const initialView = viewForPath(window.location.pathname);
      state.view = initialView;
      setView(initialView, { updateHistory: false });
      await loadFacetsAndStats();
      const match = window.location.pathname.match(/^\/opportunities\/(.+)$/);
      if (match) await openDetail(decodeURIComponent(match[1]), { updateHistory: false });
    } catch (error) {
      if (state.launchTicketFailed) {
        showAuth("That sign-in link was already used or expired. Run the launcher again, or sign in below.");
      } else if (error.message !== "Authentication required") {
        showAuth(error.message);
      }
    }
  }

  els.authForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    els.authError.textContent = "";
    els.authSubmit.disabled = true;
    els.authSubmit.textContent = "Opening…";
    const usedToken = Boolean(els.tokenInput.value);
    try {
      const session = await api("/api/v1/session", {
        method: "POST",
        body: JSON.stringify(usedToken
          ? { token: els.tokenInput.value }
          : { email: els.authEmail.value, password: els.authPassword.value }),
      });
      hideAuth(session);
      // The submit button focus vanishes with the gate; land on the page heading.
      els.pageTitle.focus();
      await flushOutbox();
      await loadFacetsAndStats();
      const initialView = viewForPath(window.location.pathname);
      setView(initialView, { updateHistory: false });
      const match = window.location.pathname.match(/^\/opportunities\/(.+)$/);
      if (match) await openDetail(decodeURIComponent(match[1]), { updateHistory: false });
    } catch (error) {
      showAuth(
        error.message === "Authentication required" ? "That token was not accepted." : error.message,
        { focus: usedToken ? els.tokenInput : els.authEmail }
      );
    } finally {
      els.authSubmit.disabled = false;
      els.authSubmit.textContent = "Continue";
    }
  });

  document.getElementById("register-submit").addEventListener("click", async () => {
    els.authError.textContent = "";
    try {
      const result = await api("/api/v1/auth/register", {method: "POST", body: JSON.stringify({
        // A blank invitation is no invitation; "" fails the server's length rule.
        invite_token: document.getElementById("register-invite").value.trim() ? document.getElementById("register-invite").value : null,
        display_name: document.getElementById("register-name").value,
        email: document.getElementById("register-email").value,
        password: document.getElementById("register-password").value,
      })});
      els.authEmail.value = result.email;
      els.authError.textContent = "Registration complete. Sign in with your email and password.";
    } catch (error) { els.authError.textContent = error.message; }
  });

  document.getElementById("recovery-request").addEventListener("click", async () => {
    els.authError.textContent = "";
    try {
      const result = await api("/api/v1/auth/recovery", {method: "POST", body: JSON.stringify({email: document.getElementById("recovery-email").value})});
      if (result.challenge_id) {
        document.getElementById("recovery-id").value = result.challenge_id;
        document.getElementById("recovery-code").value = result.sandbox_code;
      }
      els.authError.textContent = result.delivery === "unavailable"
        ? "Email recovery is not set up on this computer. Open the app with the launcher (Open Pipeline, or python -m opportunity_app.launch open) to sign in without a password."
        : result.delivery === "sent"
          ? "If that account exists, a recovery code is on its way to it."
          : "If that account exists, a sandbox-suppressed recovery challenge was created.";
    } catch (error) { els.authError.textContent = error.message; }
  });

  document.getElementById("recovery-complete").addEventListener("click", async () => {
    els.authError.textContent = "";
    try {
      await api("/api/v1/auth/recovery/complete", {method: "POST", body: JSON.stringify({
        challenge_id: document.getElementById("recovery-id").value,
        code: document.getElementById("recovery-code").value,
        new_password: document.getElementById("recovery-password").value,
      })});
      els.authError.textContent = "Password changed. Sign in with the new password.";
    } catch (error) { els.authError.textContent = error.message; }
  });

  els.logout.addEventListener("click", async () => {
    let message = "";
    try {
      await flushOutbox();
      await api("/api/v1/session", { method: "DELETE" });
    } catch (error) {
      if (error.message !== "Authentication required") message = `Sign-out may not have reached the server: ${error.message}`;
    } finally {
      // An explicit sign-out on a shared browser must not leave actions behind.
      writeOutbox([]);
      showAuth(message);
    }
  });

  els.search.closest(".search-field").appendChild(discoverTagPicker.root);
  [els.role, els.region, els.source, els.sort, els.term, els.remote, els.year, els.pay, els.posted, els.deadline].forEach((select) => {
    select.addEventListener("change", () => {
      state.offset = 0;
      loadCurrentView();
    });
  });

  els.search.addEventListener("input", () => {
    window.clearTimeout(state.searchTimer);
    state.searchTimer = window.setTimeout(() => {
      state.offset = 0;
      loadCurrentView();
    }, 250);
  });

  let themeChoice = currentTheme();
  applyTheme(themeChoice);
  els.themeToggle.addEventListener("click", () => {
    const next = THEMES[(THEMES.indexOf(themeChoice) + 1) % THEMES.length];
    themeChoice = next;
    try {
      window.localStorage.setItem("pipeline-theme", next);
    } catch (error) {
      // Storage can be blocked; the theme still applies for this page view.
    }
    applyTheme(next);
    announce(`Theme set to ${next}.`);
  });
  els.discoverNav.addEventListener("click", () => setView("discover"));
  els.urgentNav.addEventListener("click", () => setView("urgent"));
  els.savedNav.addEventListener("click", () => setView("saved"));
  els.applicationsNav.addEventListener("click", () => setView("applications"));
  els.outreachNav.addEventListener("click", () => setView("outreach"));
  els.programsNav.addEventListener("click", () => setView("programs"));
  els.prepareNav.addEventListener("click", () => setView("prepare"));
  els.agentNav.addEventListener("click", () => setView("agent"));
  els.profileNav.addEventListener("click", () => setView("profile"));
  els.personalizeButton.addEventListener("click", () => setView("profile"));

  function setDisplayMode(mode) {
    state.displayMode = mode;
    els.cardView.classList.toggle("is-active", mode === "cards");
    els.listView.classList.toggle("is-active", mode === "list");
    els.cardView.setAttribute("aria-pressed", String(mode === "cards"));
    els.listView.setAttribute("aria-pressed", String(mode === "list"));
    loadCurrentView();
  }
  els.cardView.addEventListener("click", () => setDisplayMode("cards"));
  els.listView.addEventListener("click", () => setDisplayMode("list"));

  els.paginations.forEach((nav) => {
    nav.querySelector("[data-page-previous]").addEventListener("click", () => {
      state.offset = Math.max(0, state.offset - PAGE_SIZE);
      loadOpportunities();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });

    nav.querySelector("[data-page-next]").addEventListener("click", () => {
      if (state.offset + PAGE_SIZE < state.total) state.offset += PAGE_SIZE;
      loadOpportunities();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });

    const jump = nav.querySelector("[data-page-jump]");
    const input = jump.querySelector("input");
    jump.addEventListener("submit", (event) => {
      event.preventDefault();
      const pages = Math.max(1, Math.ceil(state.total / PAGE_SIZE));
      const requested = Number.parseInt(input.value, 10);
      const current = Math.floor(state.offset / PAGE_SIZE) + 1;
      // Out-of-range entries clamp to the nearest real page instead of erroring.
      const page = Number.isNaN(requested) ? current : Math.min(Math.max(requested, 1), pages);
      input.value = String(page);
      if (page === current) return;
      state.offset = (page - 1) * PAGE_SIZE;
      loadOpportunities();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  });

  els.refreshOpen.addEventListener("click", () => {
    els.refreshDialog.showModal();
    pollRefresh();
    renderSystemStatus();
    loadSystemStatus();
  });
  els.refreshClose.addEventListener("click", () => els.refreshDialog.close());
  els.refreshStart.addEventListener("click", async () => {
    els.refreshStart.disabled = true;
    els.refreshStatus.classList.remove("is-error");
    els.refreshStatus.textContent = "Starting…";
    try {
      renderRefresh(await api("/api/v1/refresh", { method: "POST" }));
      state.refreshTimer = window.setTimeout(pollRefresh, REFRESH_POLL_MS);
    } catch (error) {
      els.refreshStatus.classList.add("is-error");
      els.refreshStatus.textContent = error.message;
      els.refreshStart.disabled = false;
    }
  });

  els.detailClose.addEventListener("click", () => closeDetail());
  els.detailScrim.addEventListener("click", () => closeDetail());
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.selectedId) closeDetail();
  });

  // Triage from the keyboard. Only while focus is on a result card, never in a
  // form field, never with a modifier, and never behind the detail panel or a
  // dialog. Enter is left alone: the focused card button already opens detail.
  const TRIAGE_KEYS = new Set(["j", "k", "s", "p", "o"]);
  document.addEventListener("keydown", (event) => {
    if (!TRIAGE_KEYS.has(event.key) || event.defaultPrevented || event.repeat) return;
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (!["discover", "saved"].includes(state.view) || state.selectedId) return;
    if (els.authGate.classList.contains("is-visible") || document.querySelector("dialog[open]")) return;
    const target = event.target instanceof Element ? event.target : null;
    if (!target || target.closest("input, textarea, select, [contenteditable]")) return;
    const card = target.closest(".opportunity-card");
    const item = card && els.results.contains(card) ? cardItems.get(card) : null;
    if (!item) return;
    event.preventDefault();
    if (event.key === "j" || event.key === "k") {
      const cards = [...els.results.querySelectorAll(".opportunity-card")].filter((node) => cardItems.has(node));
      const next = cards[cards.indexOf(card) + (event.key === "j" ? 1 : -1)];
      next?.querySelector(".card-button")?.focus();
      return;
    }
    if (event.key === "o") {
      openDetail(item.id);
      return;
    }
    const [save, pass] = card.querySelectorAll(".card-actions .card-action");
    if (event.key === "s" && save && !save.disabled) runIntent(item, item.intent_state === "saved" ? "undo" : "saved", save);
    if (event.key === "p" && pass && !pass.disabled) runIntent(item, item.intent_state === "passed" ? "undo" : "passed", pass);
  });
  window.addEventListener("popstate", () => {
    const match = window.location.pathname.match(/^\/opportunities\/(.+)$/);
    if (match) openDetail(decodeURIComponent(match[1]), { updateHistory: false });
    else {
      closeDetail({ updateHistory: false });
      setView(viewForPath(window.location.pathname), { updateHistory: false });
    }
  });
  window.addEventListener("online", async () => {
    if (!state.userId) return;
    await flushOutbox();
    await loadCurrentView();
  });

  discardLegacyOutbox();
  initialize();
})();
