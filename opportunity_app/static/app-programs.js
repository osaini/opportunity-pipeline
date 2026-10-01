// Programs: the student's own researched list, with their status on each.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { SOON_DAYS, VIEWS, els, registerViewHandlers, state } = App;

  // From app-ui.js.
  const {
    announce, autoSaveSelect, clearError, element, externalLink, formatCalendarDate, groupSection, optionElement, plural,
    revealRequested, showError,
  } = App;

  // From app-http.js.
  const { api } = App;

  // From app-nav.js.
  const { loadingLine, renderSubnav, runViewLoad, showLoadError } = App;

  // Early programs: the student's own researched list, from a private config
  // file. Its name and evidence wording come from that file, so the
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
  const programsMeta = { label: "Programs", evidence: { explicit: "Names your class year" } };

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

  function applyProgramsMeta(payload) {
    programsMeta.label = payload.label || "Programs";
    programsMeta.evidence = payload.evidence_labels || programsMeta.evidence;
    els.programsNavLabel.textContent = programsMeta.label;
    VIEWS.programs.subnavTitle = programsMeta.label;
  }

  // The student's own name for the tab, remembered in this browser so a reload
  // shows it at once instead of flashing the generic "Programs" first. Only a
  // convenience: the server's answer always replaces it.
  const programsLabelKey = (userId) => `programs-label:${userId}`;

  function rememberedProgramsMeta(userId) {
    try {
      return JSON.parse(localStorage.getItem(programsLabelKey(userId)) || "null");
    } catch (_error) {
      return null;
    }
  }

  function rememberProgramsMeta(userId, payload) {
    try {
      localStorage.setItem(programsLabelKey(userId), JSON.stringify({ label: payload.label || "" }));
    } catch (_error) {
      // Storage blocked (private window): the label just arrives a moment later.
    }
  }

  // The nav shows the student's own name for the tab from the moment they sign in.
  async function refreshProgramsLabel() {
    const userId = state.userId;
    const remembered = rememberedProgramsMeta(userId);
    if (remembered?.label) applyProgramsMeta(remembered);
    try {
      const payload = await api("/api/v1/early-programs");
      if (userId === state.userId) {
        applyProgramsMeta(payload);
        rememberProgramsMeta(userId, payload);
      }
    } catch (error) {
      // The generic "Programs" label stays; the tab reports errors when opened.
    }
  }

  async function loadPrograms() {
    await runViewLoad({ views: ["programs"], placeholder: loadingLine("Loading programs…") }, async ({ isCurrent }) => {
      const payload = await api("/api/v1/early-programs");
      if (!isCurrent()) return;
      applyProgramsMeta(payload);
      renderPrograms(payload);
    });
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
    const link = externalLink(item.url, "Official page", { className: "secondary-button", ariaLabel: `Official page for ${item.name} at ${item.host} (opens in a new tab)` });
    link.dataset.programControl = "official";
    const select = element("select", "program-status");
    select.dataset.programControl = "status";
    select.dataset.savedStatus = item.status;
    select.setAttribute("aria-label", `Your status for ${item.name} at ${item.host}`);
    Object.entries(PROGRAM_STATUS_LABELS).forEach(([value, label]) => {
      select.appendChild(optionElement(value, label, value === item.status));
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
    const checked = payload.checked_on ? `, last checked ${formatCalendarDate(payload.checked_on)}` : "";
    toolbar.appendChild(element("p", "urgent-note", `Today is ${formatCalendarDate(payload.today)}. Dates come from your own research of each program page${checked}; confirm on the official page before you apply.`));
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
      els.results.appendChild(groupSection({
        id: `programs-group-${key}`, className: `is-programs-${key}`, label, items, noun: "program", nouns: "programs",
        row: (item) => programRow(item),
      }));
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
    revealRequested(row);
  }

  // What this file does as it loads, run once by app.js in load order.
  function installPrograms() {
    registerViewHandlers("programs", { tabs: programsTabs, load: loadPrograms });
  }

  Object.assign(App, {
    installPrograms, programsMeta, refreshProgramsLabel,
  });
})();
