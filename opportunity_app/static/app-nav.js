// The router: the subtab rail, setView, the scaffolding every page loader shares, and the color theme.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { VIEWS, els, state } = App;

  // From app-ui.js.
  const { announce, clearError, element, showError } = App;

  // From app-status.js.
  const { invalidateUrgentBadge } = App;

  // What every page loader opens and closes with. It takes the next load
  // sequence (a load still in flight from before goes stale), clears the error,
  // marks the results busy and draws the loader's own placeholder, then runs
  // body({ carried, isCurrent }). A failure shows as the load error unless a
  // newer load has taken over. `before` runs ahead of the placeholder, for what
  // the placeholder wipes. `tracksLoading` keeps state.loading set, which the
  // background polls consult. `finalize` runs last, whether or not it failed.
  // isCurrent() holds while this load is the newest and the page it draws is
  // still the one on screen.
  async function runViewLoad({ views, before, placeholder, tracksLoading = false, finalize }, body) {
    const sequence = ++state.loadSequence;
    const carried = before?.();
    if (tracksLoading) state.loading = true;
    clearError();
    els.results.setAttribute("aria-busy", "true");
    placeholder();
    const isCurrent = () => sequence === state.loadSequence && views.includes(state.view);
    try {
      await body({ carried, isCurrent });
    } catch (error) {
      showLoadError(error, sequence);
    } finally {
      if (tracksLoading) state.loading = false;
      finalize?.();
    }
  }

  function loadingLine(text) {
    return () => els.results.replaceChildren(element("p", "detail-loading", text));
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

  // Each page lists its subtabs in the rail: { id, label, count, tone, group }.
  function renderSubnav(tabs) {
    els.subnavTitle.textContent = VIEWS[state.view]?.subnavTitle || "";
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
  function selectSubtab(id) {
    const changed = state.subtabs[state.view] !== id;
    state.subtabs[state.view] = id;
    markActiveSubtab();
    if (VIEWS[state.view].sections) {
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
    return VIEWS[view].tabs();
  }

  async function loadCurrentView() {
    return VIEWS[state.view].load();
  }

  // An unknown path is the Discover page.
  function viewForPath(pathname) {
    return Object.keys(VIEWS).find((name) => VIEWS[name].path === pathname) || "discover";
  }

  function setView(view, { updateHistory = true } = {}) {
    if (view !== state.view) state.activeRecordingStop?.();
    if (updateHistory) announce("");
    state.loadSequence += 1;
    state.view = view;
    state.offset = 0;
    Object.entries(VIEWS).forEach(([name, entry]) => entry.nav.classList.toggle("is-active", view === name));
    document.querySelectorAll(".nav-list .nav-item").forEach((button) => {
      if (button.id === `${view}-nav`) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    const isCollection = Boolean(VIEWS[view].deck);
    els.keyboardHint.hidden = !isCollection;
    if (isCollection) els.results.setAttribute("aria-describedby", "keyboard-hint");
    else els.results.removeAttribute("aria-describedby");
    if (!isCollection) els.results.removeAttribute("role");
    els.filterPanel.hidden = !isCollection;
    els.personalizePrompt.hidden = view !== "discover" || state.personalized;
    els.statsGrid.hidden = !VIEWS[view].deck;
    els.paginations.forEach((nav) => { nav.hidden = !isCollection; });
    els.displayToggle.hidden = !isCollection;
    els.results.classList.toggle("is-profile", Boolean(VIEWS[view].sections));
    els.resultsEyebrow.textContent = VIEWS[view].eyebrow;
    els.pageTitle.textContent = VIEWS[view].title;
    if (view !== "outreach") state.outreachKeep.clear();
    renderSubnav(initialSubnavTabs(view));
    if (updateHistory) {
      window.history.pushState({ view }, "", VIEWS[view].path);
    }
    loadCurrentView();
    if (view !== "urgent") invalidateUrgentBadge();
  }

  Object.assign(App, {
    THEMES, applyTheme, currentTheme, loadCurrentView, loadingLine, renderSubnav, runViewLoad, sectionSubnav,
    selectSubtab, setView, showLoadError, tagSection, viewForPath,
  });
})();
