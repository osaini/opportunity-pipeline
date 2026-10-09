// Shared by every script: the OpportunityApp namespace, constants, the state object, the cached DOM elements, the
// table of pages (VIEWS) and the registries that ending a session uses. It loads first and does nothing on its own:
// no network, no drawing.
(() => {
  "use strict";

  const App = {};
  window.OpportunityApp = App;

  const PAGE_SIZE = 24;
  // Best fit shows each employer's top postings and a "+N more" button, so one
  // large employer cannot fill the deck. The same default as
  // RANKED_VIEW_PER_COMPANY in pipeline_core/read_model.py.
  const RANKED_VIEW_PER_COMPANY = 5;
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
    // One employer picked from a "+N more" button; empty shows them all.
    company: "",
    // The per-employer cap the last list was served with (0: none).
    perCompany: 0,
    userId: null,
    // Counts sessions: the owner is the same 'local-user' every time they sign in, so the
    // user id cannot tell a request sent before sign-out from one sent after signing back in.
    sessionEpoch: 0,
    refresh: null,
    refreshTimer: null,
    detailReturnFocus: null,
    trackerStatus: null,
    // The active subtab per page; each page reads its own entry. Filled from
    // VIEWS, which holds every page's default.
    subtabs: {},
    // Outreach cards acted on in this tab stay visible after they move out of it.
    outreachKeep: new Set(),
    outreachQuery: "",
    outreachTag: "",
    outreachSort: "contact",
    // The company shown in the split view's pane, and the tab picked per company.
    outreachSelected: null,
    outreachTabs: {},
    // Set when the student picks an outreach tab in the rail, until the load it starts reads it.
    outreachRailPicked: false,
    // Companies ticked in a list that takes batch actions (Follow-ups due), by id.
    outreachPicked: new Set(),
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
    // The last answer from /api/v1/automation: { settings, health }.
    automation: null,
    // Set later, by the page that owns each (named here so the whole shape is in one place).
    deepSearchOpen: undefined,
    detailItem: undefined,
    launchTicketFailed: undefined,
    pdfAvailable: undefined,
    systemStatus: undefined,
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
    profileBadge: document.getElementById("profile-badge"),
    automationBanner: document.getElementById("automation-banner"),
    keyboardHint: document.getElementById("keyboard-hint"),
    pageTitle: document.getElementById("page-title"),
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
    companyFilter: document.getElementById("company-filter"),
    companyFilterName: document.getElementById("company-filter-name"),
    companyFilterClear: document.getElementById("company-filter-clear"),
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
    subnavTitle: document.getElementById("subnav-title"),
    subnavList: document.getElementById("subnav-list"),
    themeToggle: document.getElementById("theme-toggle"),
    themeLabel: document.getElementById("theme-label"),
  };

  // The nine pages, in navigation order. What differs from page to page lives
  // here, so a new page is one entry plus its loader (and the server's own route
  // for its path). nav is the sidebar button; deck marks the pages that list
  // opportunities (filter bar, stats, paging, display toggle); sections marks
  // the pages built from sections that the rail filters in place. This table
  // names no loader: each page registers how it lists its subtabs (tabs, read
  // each time, since some read live state) and how it loads (load) with
  // registerViewHandlers, beside its own loader. Programs' subnavTitle is
  // replaced by the student's own name for the page.
  const VIEWS = {
    discover: {
      nav: els.discoverNav, path: "/", subnavTitle: "Discover", defaultSubtab: "all", deck: true,
      eyebrow: "Ready for review", title: "Find the roles worth your time.",
    },
    urgent: {
      nav: els.urgentNav, path: "/urgent", subnavTitle: "Urgent", defaultSubtab: "all",
      eyebrow: "Overdue first, then the next 14 days", title: "What needs doing next.",
    },
    saved: {
      nav: els.savedNav, path: "/saved", subnavTitle: "Saved", defaultSubtab: "all", deck: true,
      eyebrow: "Saved shortlist", title: "Return to the roles you chose.",
    },
    applications: {
      nav: els.applicationsNav, path: "/applications", subnavTitle: "Applications", defaultSubtab: "all",
      eyebrow: "Application tracker", title: "Keep every application moving.",
    },
    outreach: {
      nav: els.outreachNav, path: "/outreach", subnavTitle: "Outreach", defaultSubtab: "to-contact",
      eyebrow: "Startup cold outreach", title: "Reach the startups before they post.",
    },
    programs: {
      nav: els.programsNav, path: "/programs", subnavTitle: "Programs", defaultSubtab: "open",
      eyebrow: "Soonest deadline first", title: "Programs that fit where you are.",
    },
    prepare: {
      nav: els.prepareNav, path: "/prepare", subnavTitle: "Prepare", defaultSubtab: "all", sections: true,
      eyebrow: "Evidence-grounded practice", title: "Prepare without inventing a thing.",
    },
    agent: {
      nav: els.agentNav, path: "/agent", subnavTitle: "Agent", defaultSubtab: "all", sections: true,
      eyebrow: "Auditable career copilot", title: "Ask your pipeline, then decide.",
    },
    profile: {
      nav: els.profileNav, path: "/profile", subnavTitle: "Profile", defaultSubtab: "all", sections: true,
      eyebrow: "Onboarding and evidence", title: "Build the profile behind every match.",
    },
  };

  // A page's own part of the table: load() draws it, tabs() lists its subtabs.
  // The pages built from sections list one tab, which is the default.
  function registerViewHandlers(name, { tabs = () => [{ id: "all", label: "All sections" }], load }) {
    Object.assign(VIEWS[name], { tabs, load });
  }

  // Every background poll belongs to the session that started it. One left
  // running after sign-out gets a 401 each time, and api() answers every 401 by
  // re-running showAuth, which blanks the sign-in error and moves focus to the
  // email field. So each poller registers one function that stops it, next to
  // its own timer, and ending the session runs them all. Signing in again
  // restarts what is still running, because the Outreach render watches every
  // active job.
  const sessionPollers = [];

  function registerSessionPoller(stop) {
    sessionPollers.push(stop);
  }

  function stopSessionPollers() {
    sessionPollers.forEach((stop) => stop());
  }

  // What this file does as it loads, run once by app.js in load order.
  function installContext() {
    Object.entries(VIEWS).forEach(([name, view]) => { state.subtabs[name] = view.defaultSubtab; });
  }

  Object.assign(App, {
    PAGE_SIZE, RANKED_VIEW_PER_COMPANY, SOON_DAYS, VIEWS, els, installContext, registerSessionPoller,
    registerViewHandlers, state, stopSessionPollers,
  });
})();
