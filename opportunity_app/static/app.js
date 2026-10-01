// Boot, loaded last: runs every file's registrations, wires the page-level events, and starts the session check.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { PAGE_SIZE, VIEWS, els, state } = App;

  // From app-ui.js.
  const { announce } = App;

  // From app-http.js.
  const { api, discardLegacyOutbox, flushOutbox, isAuthError, writeOutbox } = App;

  // From app-status.js.
  const { AUTOMATION_FOCUS_MS, automationStatus, refreshAutomationStatus } = App;

  // From app-nav.js.
  const { THEMES, applyTheme, currentTheme, loadCurrentView, setView, viewForPath } = App;

  // From app-session.js.
  const {
    REFRESH_POLL_MS, hideAuth, loadSystemStatus, pollRefresh, renderRefresh, renderSystemStatus, showAuth,
  } = App;

  // From app-opportunities.js.
  const {
    cardItems, discoverTagPicker, loadFacetsAndStats, loadOpportunities, renderCompanyFilter, runIntent,
  } = App;

  // From app-detail.js.
  const { closeDetail, openDetail } = App;

  // Every file's load-time registrations, once, in load order.
  App.installContext();
  App.installStatus();
  App.installSession();
  App.installOpportunities();
  App.installApplications();
  App.installOutreachSend();
  App.installOutreachTools();
  App.installOutreachPane();
  App.installOutreach();
  App.installProfile();
  App.installPreparation();
  App.installAgent();
  App.installPrograms();
  App.installUrgent();

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
      } else if (!isAuthError(error)) {
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
        isAuthError(error) ? "That token was not accepted." : error.message,
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
      if (!isAuthError(error)) message = `Sign-out may not have reached the server: ${error.message}`;
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

  els.companyFilterClear.addEventListener("click", () => {
    state.company = "";
    state.offset = 0;
    renderCompanyFilter();
    els.search.focus();
    loadCurrentView();
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
  Object.entries(VIEWS).forEach(([name, view]) => view.nav.addEventListener("click", () => setView(name)));
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
  window.addEventListener("focus", () => {
    if (state.userId && Date.now() - automationStatus.lastChecked >= AUTOMATION_FOCUS_MS) refreshAutomationStatus();
  });

  discardLegacyOutbox();
  initialize();
})();
