// Discover and Saved: the card deck, Save and Pass, filters and facets, the tag picker and company tags.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { PAGE_SIZE, RANKED_VIEW_PER_COMPANY, SOON_DAYS, els, registerViewHandlers, state } = App;

  // From app-ui.js.
  const {
    announce, announceWithUndo, chip, clearError, deadlineState, element, externalLink, formatCalendarDate, formatDate,
    isNewlySeen, optionElement, plural, showError, skeletons,
  } = App;

  // From app-http.js.
  const { api, csrfHeaders, isAuthError, queueAction, requestKey } = App;

  // From app-status.js.
  const { loadStats } = App;

  // From app-nav.js.
  const { loadCurrentView, renderSubnav, runViewLoad, setView } = App;

  // Defined in files that load later; looked up when called.
  const closeDetail = (...args) => App.closeDetail(...args);
  const loadOutreach = (...args) => App.loadOutreach(...args);
  const openDetail = (...args) => App.openDetail(...args);
  const resumePickText = (...args) => App.resumePickText(...args);

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
    // The résumé variant picked for a saved role (student/resume_variants.py); changed on the role's page.
    if (item.resume_pick) button.appendChild(element("p", "card-resume", resumePickText(item.resume_pick)));

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

    const apply = externalLink(item.url, "Apply ↗", { className: "card-action is-apply" });
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
    // On the employer's last card under the cap, say how many it has beyond it.
    const beyondCap = (item.company_total || 0) - state.perCompany;
    if (state.perCompany && item.company_rank === state.perCompany && beyondCap > 0) {
      const more = element("button", "company-more", `+${beyondCap} more from ${item.company}`);
      more.type = "button";
      more.addEventListener("click", () => showCompany(item.company));
      article.appendChild(more);
    }
    return article;
  }

  function renderCompanyFilter() {
    els.companyFilter.hidden = !state.company;
    els.companyFilterName.textContent = state.company;
  }

  // Back to no tag and no single-company filter, as at sign-in.
  function resetDeckFilters() {
    discoverTagPicker.reset();
    state.company = "";
    renderCompanyFilter();
  }

  function showCompany(company) {
    state.company = company;
    state.offset = 0;
    renderCompanyFilter();
    // The button is about to be replaced; the filter note names what changed.
    els.companyFilter.focus();
    loadCurrentView();
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
      } else if (!isAuthError(error)) {
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
    state.perCompany = payload.per_company || 0;
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
    // Only the ranked Discover deck is capped: Saved holds the student's own
    // picks, and other sorts are read in order rather than by fit.
    if (state.company) params.set("company", state.company);
    else if (state.view === "discover" && params.get("sort") === "score") {
      params.set("per_company", String(RANKED_VIEW_PER_COMPANY));
    }
    return params;
  }

  async function loadOpportunities() {
    await runViewLoad({ views: ["discover", "saved"], placeholder: skeletons, tracksLoading: true }, async ({ isCurrent }) => {
      const payload = await api(`/api/v1/opportunities?${listParams().toString()}`);
      if (!isCurrent()) return;
      renderResults(payload);
    });
  }

  function populateSelect(select, values) {
    // Facets load on every sign-in; keep only the "All …" placeholder first.
    const current = select.value;
    select.options.length = 1;
    values.forEach((value) => {
      select.appendChild(optionElement(value, value));
    });
    if (values.includes(current)) select.value = current;
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
      if (!isAuthError(error)) showError(error.message);
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

  // What this file does as it loads, run once by app.js in load order.
  function installOpportunities() {
    registerViewHandlers("discover", { tabs: deckTabs, load: loadOpportunities });
    registerViewHandlers("saved", { tabs: deckTabs, load: loadOpportunities });
  }

  Object.assign(App, {
    baseScoreReason, cardItems, companyTagsSection, createTagPicker, discoverTagPicker, installOpportunities,
    loadFacetsAndStats, loadOpportunities, renderCompanyFilter, resetDeckFilters, runIntent,
  });
})();
