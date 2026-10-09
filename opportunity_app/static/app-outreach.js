// Outreach, part 5: the list page: toolbar, split view, the add and import forms, and loadOutreach.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { els, registerViewHandlers, state } = App;

  // From app-ui.js.
  const {
    announce, armConfirm, chip, element, optionElement, plural, profileField, revealRequested, showError, skeletons,
  } = App;

  // From app-http.js.
  const { api } = App;

  // From app-status.js.
  const { automationPaused } = App;

  // From app-nav.js.
  const { renderSubnav, runViewLoad, selectSubtab } = App;

  // From app-opportunities.js.
  const { createTagPicker } = App;

  // From app-outreach-send.js.
  const { checkForBounces, outreachSendStatus, runFollowUpBatch } = App;

  // From app-outreach-drafts.js.
  const { gmailConnectPanel } = App;

  // From app-outreach-tools.js.
  const {
    OUTREACH_SORTS, OUTREACH_TABS, deepSearchPanel, outreachMatchesQuery, outreachSettingsPanel, recontactPanel,
    scheduleDeepSearchPoll,
  } = App;

  // From app-outreach-pane.js.
  const { captureUnsavedOutreachEdits, createOutreachCard, outreachRow } = App;

  // Arriving from Urgent: focus the company the row was about.
  function focusRequestedOutreach() {
    state.outreachFocus = null;
    const card = els.results.querySelector('[data-requested="true"]');
    if (!card) return;
    delete card.dataset.requested;
    revealRequested(card);
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
      priority.appendChild(optionElement(value, value, value === "P2"));
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

  // ``leaving``: cards acted on here that stay put (so nothing moves from under
  // the pointer) but no longer belong to this tab or search. Refresh files them.
  function outreachListToolbar(tags = [], leaving = 0) {
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
      sort.appendChild(optionElement(value, text, state.outreachSort === value));
    });
    sort.addEventListener("change", async () => {
      state.outreachSort = sort.value;
      await loadOutreach();
      els.results.querySelector(".outreach-filter select")?.focus();
    });
    sortLabel.appendChild(sort);

    const refresh = element("button", "secondary-button outreach-refresh");
    refresh.type = "button";
    refresh.appendChild(element("span", "", "Refresh"));
    if (leaving) {
      const count = element("span", "outreach-refresh-count", String(leaving));
      count.setAttribute("aria-hidden", "true");
      refresh.appendChild(count);
      refresh.setAttribute("aria-label", `Refresh: ${plural(leaving, "company moves", "companies move")} out of this list`);
    }
    refresh.title = leaving
      ? `${plural(leaving, "company", "companies")} you acted on will move to where ${leaving === 1 ? "it" : "they"} now belong${leaving === 1 ? "s" : ""}`
      : "Load the latest from the server";
    refresh.addEventListener("click", async () => {
      refresh.disabled = true;
      state.outreachKeep.clear();
      await loadOutreach();
      announce(leaving ? `${plural(leaving, "company", "companies")} moved out of this list.` : "Outreach is up to date.");
      // Back on the button, without scrolling the page to it.
      els.results.querySelector(".outreach-refresh")?.focus({ preventScroll: true });
    });
    toolbar.append(search, sortLabel, refresh);
    return toolbar;
  }

  function outreachUnsavedEdits(pane) {
    return [...(pane?.querySelectorAll("form [name]") || [])].some((control) => control.value !== control.dataset.initial);
  }

  // The rail tab whose company list is on the page, so a reload of that same list
  // keeps its place in it.
  let listedTab = null;

  // A batch of follow-ups running now ({ how }), and what the last one did, kept
  // until the next batch or a new pick of the rail tab.
  let followUpBatch = null;
  let followUpBatchResult = null;

  function followUpBatchSummary({ how, done, skipped, paused }) {
    const parts = [];
    if (!done.length) parts.push(`Nothing was ${how === "queue" ? "queued" : "sent"}.`);
    else if (how === "queue") {
      parts.push(`Queued ${plural(done.length, "follow-up", "follow-ups")}${paused
        ? ". Automation is paused, so they go out after you resume."
        : ", each for its recipient's next weekday morning."}`);
    } else parts.push(`Sent ${plural(done.length, "follow-up", "follow-ups")}.`);
    const unmarked = done.filter((entry) => entry.marked === false).length;
    if (unmarked) parts.push(`${plural(unmarked, "was", "were")} sent but could not be marked followed up; press "I sent the follow-up" on ${unmarked === 1 ? "it" : "each"}.`);
    if (skipped.length) parts.push(`${plural(skipped.length, "company was", "companies were")} left out:`);
    return parts.join(" ");
  }

  // Follow-ups due lists each company with a box to tick, as Gmail lists mail:
  // tick some, or all, then queue their follow-ups for each recipient's next
  // weekday morning or send them now. Both ask again before anything goes, and
  // name every company left out and why (runFollowUpBatch).
  function followUpBatchBar(pickable, context, { onSelectAll }) {
    const bar = element("div", "outreach-batch");
    bar.setAttribute("role", "group");
    bar.setAttribute("aria-label", "Ticked follow-ups");
    const allLabel = element("label", "outreach-batch-all");
    const all = document.createElement("input");
    all.type = "checkbox";
    allLabel.append(all, document.createTextNode("Select all"));
    const count = element("span", "outreach-batch-count");
    count.setAttribute("aria-live", "polite");
    const gmail = context.gmail || {};
    // The check just before a scheduled send reads Gmail, so queuing needs read access.
    const canQueue = Boolean(gmail.connected && gmail.bounce_check && context.automation?.scheduled_sending);
    const why = element("p", "outreach-note outreach-batch-why", !gmail.connected
      ? "Connect Gmail to queue or send follow-ups from here."
      : !context.automation?.scheduled_sending
        ? "Turn on Send on their weekday morning under Settings to queue follow-ups; Send now works without it."
        : !gmail.bounce_check ? "Reconnect Gmail once to queue follow-ups: the check before a scheduled send reads Gmail." : "");
    const queue = element("button", "primary-button outreach-batch-queue");
    queue.type = "button";
    const send = element("button", "secondary-button outreach-batch-send");
    send.type = "button";
    if (gmail.attachment_problem) send.title = gmail.attachment_problem;
    const status = element("div", "outreach-batch-status");
    status.setAttribute("aria-live", "polite");

    const picked = () => pickable.filter((item) => state.outreachPicked.has(item.id));
    const howMany = (n) => (n && n === pickable.length ? "all" : String(n));
    const queueLabel = () => {
      const n = picked().length;
      return n ? `Queue ${howMany(n)} for their morning` : "Queue for their morning";
    };
    const sendLabel = () => {
      const n = picked().length;
      return n ? `Send ${howMany(n)} now` : "Send now";
    };
    // What the second press is about to do, in words.
    const approving = () => {
      const n = picked().filter((item) => item.follow_up_status === "generated").length;
      return n ? ` ${plural(n, "follow-up is", "follow-ups are")} not approved yet and will be approved as written; any with warnings to review waits for you to say.` : "";
    };
    // The open company's follow-up would go as saved, not as typed.
    const unsavedPick = () => {
      const pane = els.results.querySelector(".outreach-pane[data-outreach-id]");
      if (!pane || !state.outreachPicked.has(pane.dataset.outreachId)) return false;
      const edited = ["follow_up_subject", "follow_up_body"].some((name) => {
        const field = pane.querySelector(`[name="${name}"]`);
        return Boolean(field) && field.value !== field.dataset.initial;
      });
      if (edited) showError(`The ${pane.querySelector("h3")?.textContent || "open"} follow-up has unsaved edits. Save them first, so what goes out is what you see.`);
      return edited;
    };

    const update = () => {
      const n = picked().length;
      all.checked = n > 0 && n === pickable.length;
      all.indeterminate = n > 0 && n < pickable.length;
      count.textContent = `${n} of ${pickable.length} selected`;
      const running = Boolean(followUpBatch);
      all.disabled = running || !pickable.length;
      queue.disabled = running || !n || !canQueue;
      send.disabled = running || !n || !gmail.connected || Boolean(gmail.attachment_problem);
    };

    const run = async (how) => {
      const chosen = picked();
      if (!chosen.length) return;
      followUpBatch = { how };
      followUpBatchResult = null;
      status.replaceChildren();
      update();
      let result;
      try {
        result = await runFollowUpBatch(chosen, how, (index, total, item) => {
          // A reload meanwhile draws a new bar; write to whichever one is on the page.
          const live = els.results.querySelector(".outreach-batch-status");
          if (live) live.textContent = `${how === "queue" ? "Queuing" : "Sending"} ${index + 1} of ${total}: ${item.company}…`;
        });
      } catch (error) {
        showError(error.message);
        return;
      } finally {
        followUpBatch = null;
        // Disarmed, so a later press asks again.
        changed();
      }
      if (result.stopped) return;
      result.done.forEach(({ item }) => state.outreachPicked.delete(item.id));
      followUpBatchResult = { how, ...result, paused: automationPaused() };
      announce(followUpBatchSummary(followUpBatchResult));
      await loadOutreach();
    };

    const resetQueue = armConfirm(queue, {
      idleLabel: queueLabel,
      armedLabel: () => `Queue ${plural(picked().length, "follow-up", "follow-ups")}?`,
      prompt: () => `Press again to queue the follow-ups for ${plural(picked().length, "company", "companies")}, each for its recipient's next weekday morning.${approving()} Any that cannot go is left out and named.`,
      beforeClick: unsavedPick,
      onConfirm: () => run("queue"),
    });
    const resetSend = armConfirm(send, {
      idleLabel: sendLabel,
      armedLabel: () => `Send ${plural(picked().length, "follow-up", "follow-ups")} now?`,
      prompt: () => `Press again to send the follow-ups for ${plural(picked().length, "company", "companies")} now from ${gmail.account || "Gmail"}.${approving()} Any that cannot go is left out and named.`,
      beforeClick: unsavedPick,
      onConfirm: () => run("send"),
    });
    // A press armed for one set of companies never confirms another.
    const changed = () => {
      resetQueue();
      resetSend();
      update();
    };
    all.addEventListener("change", () => {
      onSelectAll(all.checked);
      changed();
    });

    if (followUpBatch) status.textContent = `${followUpBatch.how === "queue" ? "Queuing" : "Sending"} the ticked follow-ups…`;
    else if (followUpBatchResult) {
      status.appendChild(element("p", "", followUpBatchSummary(followUpBatchResult)));
      if (followUpBatchResult.skipped.length) {
        const left = element("ul", "outreach-batch-left");
        followUpBatchResult.skipped.forEach(({ item, reason }) => left.appendChild(element("li", "", `${item.company}: ${reason}`)));
        status.appendChild(left);
      }
    }
    bar.append(allLabel, count, queue, send, why, status);
    changed();
    return { element: bar, changed };
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

    // Ticks survive a reload; one whose company left the list goes with it.
    const pickable = tab.batch ? items.filter(tab.test) : [];
    const pickableIds = new Set(pickable.map((item) => item.id));
    [...state.outreachPicked].forEach((id) => { if (!pickableIds.has(id)) state.outreachPicked.delete(id); });
    const boxes = new Map();
    const setPicked = (item, on) => {
      if (on) state.outreachPicked.add(item.id);
      else state.outreachPicked.delete(item.id);
      const box = boxes.get(item.id);
      if (box) box.checked = on;
    };
    const batch = tab.batch ? followUpBatchBar(pickable, context, { onSelectAll: (on) => pickable.forEach((item) => setPicked(item, on)) }) : null;
    let lastPick = null;

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
      if (!batch) {
        list.appendChild(row);
        return;
      }
      // A box over the row's left edge, not in it: a button cannot hold a checkbox (styles.css lines the two up). Shift
      // picks the run since the last tick. A row with no box keeps the same indent, so every name starts at one edge.
      const wrap = element("div", "outreach-row-pick");
      if (pickableIds.has(item.id)) {
        const box = document.createElement("input");
        box.type = "checkbox";
        box.className = "outreach-pick";
        box.checked = state.outreachPicked.has(item.id);
        box.setAttribute("aria-label", `Select ${item.company}`);
        box.addEventListener("click", (event) => {
          const index = pickable.indexOf(item);
          const [from, to] = event.shiftKey && lastPick !== null ? [Math.min(lastPick, index), Math.max(lastPick, index)] : [index, index];
          pickable.slice(from, to + 1).forEach((other) => setPicked(other, box.checked));
          lastPick = index;
          batch.changed();
        });
        boxes.set(item.id, box);
        wrap.appendChild(box);
      }
      wrap.appendChild(row);
      list.appendChild(wrap);
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

    split.append(...(batch ? [batch.element] : []), list, host);
    show(items.find((item) => item.id === state.outreachSelected));
    return split;
  }

  function outreachToolCount(id, payload) {
    if (id === "deep-search") return payload.discovery.active?.state === "running" ? "…" : null;
    if (id === "find-people") return payload.recontact?.active?.state === "running" ? "…" : payload.recontact?.eligible || null;
    return null;
  }

  async function loadOutreach() {
    await runViewLoad({
      views: ["outreach"],
      placeholder: () => {
        if (!els.results.querySelector(".outreach-card, .outreach-toolbar, .outreach-deep-search, .outreach-recontact, .outreach-settings, .outreach-add")) skeletons();
      },
      tracksLoading: true,
      finalize: () => {
        // One reload carries them; a later one starts from what the server holds.
        // Both flags end here, so a load that returned early cannot strand either.
        state.outreachPendingEdits = null;
        state.outreachDiscardEdits = false;
      },
    }, async ({ isCurrent }) => {
      const payload = await api("/api/v1/outreach");
      if (!isCurrent()) return;
      const tab = OUTREACH_TABS.find((entry) => entry.id === state.subtabs.outreach) || OUTREACH_TABS[0];
      const railPicked = state.outreachRailPicked;
      state.outreachRailPicked = false;
      // A new pick of the rail tab starts with nothing ticked and no batch summary.
      if (railPicked) {
        state.outreachPicked.clear();
        followUpBatchResult = null;
      }
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
      // A reload of the same list (Refresh, or after an action) keeps the list scrolled where it was; picking a rail
      // tab starts it at the top.
      const listTop = !railPicked && listedTab === tab.id ? els.results.querySelector(".outreach-list")?.scrollTop || 0 : 0;
      listedTab = null;
      els.results.replaceChildren();

      if (tab.id === "deep-search") {
        state.deepSearchOpen = true;
        els.results.appendChild(deepSearchPanel(payload.discovery));
        els.resultCount.textContent = "Deep search";
      } else if (tab.id === "settings") {
        const panel = await outreachSettingsPanel(payload.gmail_drafts, payload.automation);
        // The settings load after the list; a view switched meanwhile keeps its own page.
        if (!isCurrent()) return;
        els.results.appendChild(panel);
        els.resultCount.textContent = "Outreach settings";
      } else if (tab.id === "find-people") {
        els.results.appendChild(recontactPanel(payload.recontact));
        els.resultCount.textContent = "Find people";
      } else if (tab.id === "add") {
        els.results.append(...outreachAddForm());
        const gmailConnect = gmailConnectPanel(payload.gmail_drafts, payload.automation);
        if (gmailConnect) els.results.appendChild(gmailConnect);
        els.resultCount.textContent = "Add, import, export";
      } else {
        const [, compare] = OUTREACH_SORTS[state.outreachSort] || OUTREACH_SORTS.contact;
        const kept = (item) => state.outreachKeep.has(item.id);
        const items = payload.items
          .filter((item) => (tab.test(item) && matches(item)) || kept(item))
          .sort(compare);
        const leaving = items.filter((item) => kept(item) && !(tab.test(item) && matches(item))).length;
        // Picked in the rail, a tab with a pane tab of its own opens each of its companies there (Follow-ups due on
        // Follow-up). Only on that pick: a reload after an action keeps whichever pane tab the student moved to.
        if (railPicked && tab.paneTab) items.filter(tab.test).forEach((item) => { state.outreachTabs[item.id] = tab.paneTab; });
        els.results.appendChild(outreachListToolbar(payload.tags || [], leaving));
        if (running) {
          const banner = element("div", "outreach-banner");
          banner.appendChild(element("p", "", "The deep search is running. New companies land in From deep search when it finishes."));
          const view = element("button", "secondary-button", "View deep search");
          view.type = "button";
          view.addEventListener("click", () => selectSubtab("deep-search"));
          banner.appendChild(view);
          els.results.appendChild(banner);
        }
        const gmailConnect = gmailConnectPanel(payload.gmail_drafts, payload.automation);
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
                : tab.id === "not-interested"
                  ? "Mark a company Not interested on its card to file it here. It is kept, and left out of every other tab."
                  : tab.id === "applied-directly"
                    ? "Mark a company Applied directly on its card when you applied through its own site. It is kept, and nothing automatic goes to it."
                    : "Pick another tab beside the page."));
          els.results.appendChild(empty);
        } else {
          const split = outreachSplitView(items, tab, {
            compose: payload.compose, gmail: payload.gmail_drafts, automation: payload.automation, research: payload.company_research,
          });
          els.results.appendChild(split);
          listedTab = tab.id;
          if (listTop) split.querySelector(".outreach-list").scrollTop = listTop;
        }
        els.resultCount.textContent = `${plural(items.length, "company", "companies")} · ${tab.label}`;
      }
      els.pageStatus.textContent = outreachSendStatus(payload.gmail_drafts, payload.automation);
      if (running) scheduleDeepSearchPoll();
      if (payload.gmail_drafts?.bounce_check) checkForBounces();
      els.results.setAttribute("aria-busy", "false");
      focusRequestedOutreach();
    });
  }

  // What this file does as it loads, run once by app.js in load order.
  function installOutreach() {
    registerViewHandlers("outreach", { tabs: () => OUTREACH_TABS, load: loadOutreach });
  }

  Object.assign(App, {
    installOutreach, loadOutreach,
  });
})();
