// What the app shows about itself on every page: the Urgent badge, the automation banner and Profile badge with the
// polling behind them, and the deck statistics.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { els, registerSessionPoller, state } = App;

  // From app-ui.js.
  const { announce, announceWithUndo, element, plural, showError } = App;

  // From app-http.js.
  const { api, isAuthError } = App;

  // Defined in files that load later; looked up when called.
  const automationBreakerMessage = (...args) => App.automationBreakerMessage(...args);
  const automationHealthList = (...args) => App.automationHealthList(...args);
  const loadApplications = (...args) => App.loadApplications(...args);
  const repaintPauseWords = (...args) => App.repaintPauseWords(...args);
  const repaintThankYouHolds = (...args) => App.repaintThankYouHolds(...args);
  const setView = (...args) => App.setView(...args);
  const syncAutomationControls = (...args) => App.syncAutomationControls(...args);
  const withUndoNote = (...args) => App.withUndoNote(...args);

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

  // What the app does on its own, app-wide: the banner above the page (a pause,
  // or Gmail needing attention) and the Profile badge (automatic actions waiting
  // for the student, else unread notices). Checked after sign-in, every five
  // minutes, and when the window regains focus, at most once a minute. Like the
  // Urgent badge, a check never opens the sign-in gate or shows an error.
  const AUTOMATION_POLL_MS = 5 * 60_000;
  const AUTOMATION_FOCUS_MS = 60_000;
  // ticket numbers every automation read and write in the order it started;
  // shown is the newest one whose answer is on screen, lastWrite the newest
  // write, writing how many writes are still waiting for their answer, and
  // overlapped whether two writes were on their way at once since the last
  // time none was.
  // appliedSeen is the newest automatic change already on the page (null until the first answer after sign-in).
  const automationStatus = { lastChecked: 0, timer: null, controller: null, banner: "", ticket: 0, shown: 0, lastWrite: 0, writing: 0, overlapped: false, appliedSeen: null };

  // The server answers in its own order, so an answer read before a Pause
  // committed can land after the Pause's own answer and put the old banner
  // back. Nothing is shown over an answer that started later (shown). A
  // read's answer counts only when, besides that, no write was on its way
  // while it ran (it may have read before that write committed). A write's
  // answer is read after its own commit, so it counts unless a later one is
  // already on screen. Two writes at once are the one case tickets cannot
  // settle: the server may commit them in either order, so each answer can
  // miss the other's change. Once both have answered, one fresh read shows
  // what the server settled on.
  function startAutomationRead() {
    return { ticket: ++automationStatus.ticket, clean: automationStatus.writing === 0 };
  }

  function automationReadIsCurrent(read) {
    return Boolean(read?.clean) && read.ticket > automationStatus.lastWrite && read.ticket > automationStatus.shown;
  }

  // Runs one write that changes automation state (a switch, the pause, a
  // decision, notices read). An answer that carries { settings, health } is
  // shown at once, unless the answer of a write that started later is
  // already on screen. The caller still gets its own answer, for its own
  // words ("Write drafts automatically: on.").
  async function automationWrite(send) {
    const ticket = ++automationStatus.ticket;
    automationStatus.lastWrite = ticket;
    if (automationStatus.writing > 0) automationStatus.overlapped = true;
    automationStatus.writing += 1;
    // A background check already on its way would be dropped anyway.
    automationStatus.controller?.abort();
    try {
      const payload = await send();
      if ((payload?.settings || payload?.health) && ticket > automationStatus.shown) applyAutomationView(payload, ticket);
      return payload;
    } finally {
      automationStatus.writing -= 1;
      if (automationStatus.writing === 0 && automationStatus.overlapped) {
        automationStatus.overlapped = false;
        refreshAutomationStatus();
      }
    }
  }

  // Shows a read's answer when it is still the latest word. False when it was dropped.
  function applyAutomationRead(read, payload) {
    if (!automationReadIsCurrent(read)) return false;
    applyAutomationView(payload, read.ticket);
    return true;
  }

  function setProfileBadge(health) {
    const waiting = (Number(health?.counts?.proposed) || 0) + (Number(health?.counts?.shadow_unreviewed) || 0);
    const unread = Number(health?.unread_notices) || 0;
    const count = waiting > 0 ? waiting : unread;
    const show = Number.isInteger(count) && count > 0;
    els.profileBadge.hidden = !show;
    els.profileBadge.textContent = show ? (count > 99 ? "99+" : String(count)) : "";
    if (!show) els.profileNav.setAttribute("aria-label", "Profile");
    else if (waiting > 0) els.profileNav.setAttribute("aria-label", `Profile, ${count} waiting for you`);
    else els.profileNav.setAttribute("aria-label", `Profile, ${count} unread notice${count === 1 ? "" : "s"}`);
  }

  const AUTOMATION_BANNER_LEVELS = new Set(["info", "warning", "problem"]);

  function renderAutomationBanner(health) {
    const items = Array.isArray(health?.banner) ? health.banner : [];
    // Unchanged items are left alone, so a periodic check neither re-announces
    // the live region nor pulls focus off its button.
    const key = JSON.stringify(items.map((item) => [item.key, item.level, item.text]));
    if (key === automationStatus.banner) return;
    automationStatus.banner = key;
    els.automationBanner.replaceChildren();
    items.forEach((item) => {
      const level = AUTOMATION_BANNER_LEVELS.has(item.level) ? item.level : "info";
      const row = element("div", `automation-banner-item is-${level}`);
      row.appendChild(element("p", "", String(item.text || "")));
      if (item.key === "paused") {
        const resume = element("button", "secondary-button", "Resume");
        resume.type = "button";
        resume.id = "automation-banner-resume";
        resume.addEventListener("click", async () => {
          resume.disabled = true;
          try {
            await setAutomationPaused(false);
            announce("Automation resumed.");
            // The banner and its button are gone; land on the page heading.
            els.pageTitle.focus();
          } catch (error) {
            resume.disabled = false;
            if (!isAuthError(error)) showError(error.message);
          }
        });
        row.appendChild(resume);
      } else if (item.key === "gmail_needs_reconnect" || item.key === "gmail_expiring") {
        const open = element("button", "secondary-button", "Open Outreach");
        open.type = "button";
        open.addEventListener("click", () => setView("outreach"));
        row.appendChild(open);
      }
      els.automationBanner.appendChild(row);
    });
    els.automationBanner.hidden = !items.length;
  }

  // Whether automation is paused, by the latest answer shown.
  function automationPaused() {
    return Boolean(state.automation?.health?.paused ?? state.automation?.settings?.paused);
  }

  // The banner, the badge, and everything on the page that reads the pause or
  // health: the Profile section's Health list and each scheduled send's words.
  function applyAutomationStatus(health) {
    if (!health) return;
    const wasPaused = automationPaused();
    state.automation = { ...(state.automation || {}), health };
    renderAutomationBanner(health);
    setProfileBadge(health);
    document.getElementById("automation-health-host")?.replaceChildren(automationHealthList(health));
    if (wasPaused !== automationPaused()) repaintPauseWords();
    announceNewAutomatic(health);
  }

  // A change the app made on its own since the page last looked is announced
  // once, with Undo when it can be taken back: at most one announcement per
  // answer, naming how many more there are. What was already there when the
  // student signed in is not news, so the first answer only sets the mark.
  // A status line still offering the student's own Undo is theirs: the news
  // waits for a later answer rather than take that Undo away.
  function announceNewAutomatic(health) {
    const items = Array.isArray(health?.recent_applied) ? health.recent_applied : [];
    const newest = items.reduce((latest, item) => (String(item.applied_at || "") > latest ? String(item.applied_at) : latest), "");
    if (automationStatus.appliedSeen === null) {
      automationStatus.appliedSeen = newest;
      return;
    }
    const fresh = items.filter((item) => String(item.applied_at || "") > automationStatus.appliedSeen);
    if (!fresh.length) return;
    if (!els.actionStatus.hidden && els.actionStatus.querySelector(".status-undo:not([data-automatic])")) return;
    if (newest > automationStatus.appliedSeen) automationStatus.appliedSeen = newest;
    const [first] = fresh;
    // The server lists at most a few; when every one listed is new, there may be more than it sent.
    const capped = (Number(health?.recent_applied_total) || 0) > items.length && fresh.length === items.length;
    const more = fresh.length > 1
      ? `, and ${capped ? "at least " : ""}${plural(fresh.length - 1, "more change", "more changes")} (see Automation on your Profile)`
      : "";
    const message = `Automatic: ${String(first.summary || "a change").replace(/[.!?]+$/, "")}${more}.`;
    if (!first.undoable) {
      announce(message);
    } else {
      announceWithUndo(message, async () => {
        try {
          const result = await automationWrite(() => api(`/api/v1/automation/actions/${encodeURIComponent(first.id)}/undo`, { method: "POST" }));
          announce(withUndoNote(result.feature_paused ? automationBreakerMessage(first.feature, result.breaker_notice) : "Undid the automatic change.", result));
        } catch (error) {
          if (!isAuthError(error)) announce(error.message);
        }
        refreshAutomationStatus();
        refreshViewAfterAutomatic();
      }, { automatic: true });
    }
    refreshViewAfterAutomatic();
  }

  // What an automatic change, or its Undo, touched is shown where the student
  // is looking: the Automation lists on Profile, the board on Applications.
  // Nothing is redrawn under a field the student is typing in.
  function refreshViewAfterAutomatic() {
    if (state.view === "profile") {
      automationStatus.reloadLists?.();
      return;
    }
    if (state.view !== "applications") return;
    const active = document.activeElement;
    if (active && els.results.contains(active) && active.matches("input, textarea, select")) return;
    Promise.all([loadApplications(), loadStats()]).catch((error) => {
      if (!isAuthError(error)) showError(error.message);
    });
  }

  // { settings, health } from GET /api/v1/automation or a settings PUT. Every
  // control bound to a feature, wherever it is on the page, follows it.
  // Callers go through applyAutomationRead or automationWrite, never here.
  function applyAutomationView(payload, ticket) {
    automationStatus.shown = Math.max(automationStatus.shown, ticket);
    if (payload?.settings) {
      state.automation = { ...(state.automation || {}), settings: payload.settings };
      syncAutomationControls(payload.settings);
      repaintThankYouHolds();
    }
    if (payload?.health) applyAutomationStatus(payload.health);
    // The job-email block on the Profile page follows its switch at once.
    if (payload?.application_mail) automationStatus.onMail?.(payload.application_mail);
  }

  async function setAutomationPaused(paused) {
    return automationWrite(() => api("/api/v1/automation/settings", { method: "PUT", body: JSON.stringify({ paused }) }));
  }

  async function refreshAutomationStatus() {
    const userId = state.userId;
    if (!userId) return;
    automationStatus.controller?.abort();
    const controller = new AbortController();
    automationStatus.controller = controller;
    automationStatus.lastChecked = Date.now();
    const read = startAutomationRead();
    try {
      // Plain fetch: a background check must never open the sign-in gate or show an error.
      const response = await fetch("/api/v1/automation", { credentials: "same-origin", signal: controller.signal });
      if (!response.ok) throw new Error(`Automation check failed (${response.status})`);
      const payload = await response.json();
      if (userId !== state.userId) return;
      applyAutomationRead(read, payload);
    } catch (_) {
      // The last answer stays up; the next check tries again.
    } finally {
      if (automationStatus.controller === controller) automationStatus.controller = null;
    }
  }

  function startAutomationStatus() {
    window.clearInterval(automationStatus.timer);
    automationStatus.timer = window.setInterval(refreshAutomationStatus, AUTOMATION_POLL_MS);
    refreshAutomationStatus();
  }

  function clearAutomationStatus() {
    window.clearInterval(automationStatus.timer);
    automationStatus.timer = null;
    automationStatus.controller?.abort();
    automationStatus.controller = null;
    automationStatus.lastChecked = 0;
    automationStatus.banner = "";
    automationStatus.appliedSeen = null;
    state.automation = null;
    els.automationBanner.replaceChildren();
    els.automationBanner.hidden = true;
    setProfileBadge(null);
  }

  async function loadStats() {
    const stats = await api("/api/v1/stats");
    els.statActive.textContent = stats.active_unique.toLocaleString();
    els.statTotal.textContent = stats.total.toLocaleString();
    els.statTracked.textContent = (stats.applications ?? stats.tracked).toLocaleString();
    els.statScore.textContent = stats.top_score ? `${stats.top_score}/100` : "—";
  }

  // What this file does as it loads, run once by app.js in load order.
  function installStatus() {
    registerSessionPoller(clearUrgentBadge);
    registerSessionPoller(clearAutomationStatus);
  }

  Object.assign(App, {
    AUTOMATION_BANNER_LEVELS, AUTOMATION_FOCUS_MS, applyAutomationRead, applyFreshUrgentCount, automationPaused,
    automationStatus, automationWrite, installStatus, invalidateUrgentBadge, loadStats, refreshAutomationStatus,
    setAutomationPaused, startAutomationRead, startAutomationStatus, urgentBadge,
  });
})();
