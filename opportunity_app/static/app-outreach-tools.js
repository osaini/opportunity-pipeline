// Outreach, part 3: the tab and sort tables, deep search, find people, and the Settings tab.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { registerSessionPoller, state } = App;

  // From app-ui.js.
  const { announce, chip, element, formatDate, optionElement, plural, showError } = App;

  // From app-http.js.
  const { api } = App;

  // From app-status.js.
  const { automationWrite, refreshAutomationStatus } = App;

  // From app-automation.js.
  const { automationFields } = App;

  // From app-outreach-send.js.
  const { outreachDraftNeedsReview, outreachReachable } = App;

  // Defined in files that load later; looked up when called.
  const loadOutreach = (...args) => App.loadOutreach(...args);

  // Not contacted yet: nothing sent and still before the first email.
  function outreachToContact(item) {
    return !item.sent_at && ["not_started", "drafted"].includes(item.status);
  }

  // A company the student marked not interested is kept, never deleted, and
  // shows only under Not interested: every other tab, All companies included,
  // leaves it out.
  const outreachNotInterested = (item) => Boolean(item.not_interested_at);

  // The rail's outreach tabs. The server returns every company once; each tab
  // narrows that list here, so every count stays in step with the cards.
  const OUTREACH_TABS = [
    { id: "to-contact", label: "To contact", test: outreachToContact },
    { id: "ready", label: "Ready to send", group: "Before sending", tone: "is-good", test: (item) => outreachToContact(item) && item.draft_status === "approved" && outreachReachable(item) && !item.cc_bounced && item.scheduled?.initial?.state !== "scheduled" },
    { id: "scheduled", label: "Scheduled", group: "Before sending", tone: "is-region", test: (item) => ["initial", "follow_up"].some((kind) => ["scheduled", "sending", "transmitting", "failed"].includes(item.scheduled?.[kind]?.state)) || ["scheduled", "sending", "transmitting"].includes(item.scheduled?.thank_you?.state) },
    { id: "needs-review", label: "Drafts to review", group: "Before sending", tone: "is-soon", test: (item) => outreachDraftNeedsReview(item, "initial") || outreachDraftNeedsReview(item, "follow_up") },
    { id: "needs-contact", label: "Needs a contact", group: "Before sending", tone: "is-soon", test: (item) => outreachToContact(item) && !outreachReachable(item) },
    { id: "bounced", label: "Bounced", group: "Before sending", tone: "is-alert", test: (item) => Boolean(item.bounced_at) },
    { id: "needs-location", label: "Needs a location", group: "Before sending", tone: "is-soon", test: (item) => outreachToContact(item) && !item.location_verified },
    { id: "from-search", label: "From deep search", group: "Before sending", test: (item) => item.origin === "discovery" && outreachToContact(item) },
    { id: "follow-ups-due", label: "Follow-ups due", group: "Contacted", tone: "is-alert", test: (item) => item.follow_up_due },
    { id: "revisits-due", label: "Revisits due", group: "Contacted", tone: "is-alert", test: (item) => item.revisit_due },
    { id: "awaiting", label: "Awaiting reply", group: "Contacted", test: (item) => item.status === "sent" || item.status === "followed_up" },
    { id: "replied", label: "Replied", group: "Contacted", tone: "is-good", test: (item) => ["replied", "call_scheduled", "offer"].includes(item.status) },
    { id: "closed", label: "Closed or paused", group: "Contacted", test: (item) => ["declined", "no_response", "paused"].includes(item.status) },
    { id: "all", label: "All companies", group: "Everything", test: () => true },
    { id: "not-interested", label: "Not interested", group: "Everything", test: outreachNotInterested },
    { id: "deep-search", label: "Deep search", group: "Tools", tool: true },
    { id: "find-people", label: "Find people", group: "Tools", tool: true },
    { id: "add", label: "Add, import, export", group: "Tools", tool: true },
    { id: "settings", label: "Settings", group: "Tools", tool: true },
  ].map((tab) => (tab.tool || tab.id === "not-interested" ? tab : { ...tab, test: (item) => !outreachNotInterested(item) && tab.test(item) }));

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
      if (!state.userId || state.view !== "outreach") return;
      const epoch = state.sessionEpoch;
      try {
        const discovery = await api("/api/v1/outreach/discovery");
        if (state.sessionEpoch !== epoch) return;
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
        if (state.sessionEpoch === epoch) showError(error.message);
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
      if (!state.userId || state.view !== "outreach") return;
      const epoch = state.sessionEpoch;
      try {
        const recontact = await api("/api/v1/outreach/recontact");
        if (state.sessionEpoch !== epoch) return;
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
        if (state.sessionEpoch === epoch) showError(error.message);
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
    const label = element("label", "", "Who suggests reply and email outcomes");
    label.htmlFor = "settings-jev-inbox";
    const select = document.createElement("select");
    select.id = "settings-jev-inbox";
    select.disabled = true;
    const help = element("p", "profile-help");
    const status = element("p", "profile-help jev-inbox-status");
    status.setAttribute("aria-live", "polite");
    field.append(label, select, help, status);
    function show(setting) {
      select.replaceChildren(
        optionElement("rules", "Keyword rules (no AI)"),
        optionElement("jev", setting.available ? "Jev (TypeSafe)" : "Jev (TypeSafe, not set up)"),
      );
      select.value = setting.enabled ? "jev" : "rules";
      select.disabled = false;
      help.textContent = setting.available
        ? `${setting.sends} With Jev, these go to TypeSafe. When Jev is unsure or unreachable, the keyword rules suggest instead, and every suggestion says which one made it. You still confirm each change, except that Update applications from job emails and Send a thank-you when someone declines (both under Automation on your Profile) act on their own when Jev and the keyword rules agree; the thank-you is an email, sent from your Gmail.`
        : "Jev is not set up on this computer (TYPESAFE_API_KEY in .env), so the keyword rules make these suggestions. Nothing is sent anywhere.";
    }
    try {
      show(await api("/api/v1/typesafe/inbox-suggestions"));
    } catch (error) {
      help.textContent = `Jev inbox suggestions could not be checked: ${error.message}`;
      return field;
    }
    select.addEventListener("change", async () => {
      const wanted = select.value === "jev";
      select.disabled = true;
      status.textContent = "Saving…";
      try {
        // jev_inbox_suggestions is an automation switch, so the app-wide state is read again after.
        show(await automationWrite(() => api("/api/v1/typesafe/inbox-suggestions", { method: "PUT", body: JSON.stringify({ enabled: wanted }) })));
        refreshAutomationStatus();
        status.textContent = wanted ? "Jev inbox suggestions on." : "Jev inbox suggestions off.";
      } catch (error) {
        select.value = wanted ? "rules" : "jev";
        select.disabled = false;
        status.textContent = error.message;
      }
    });
    return field;
  }

  // The Gmail label the app adds to every outreach thread (the emails sent to companies, and the replies). Per
  // student, so it is saved through its own route rather than the .env settings.
  async function gmailLabelField() {
    const field = element("div", "settings-field gmail-label-setting");
    const label = element("label", "", "Gmail label for replies");
    label.htmlFor = "settings-gmail-label";
    const input = document.createElement("input");
    input.type = "text";
    input.id = "settings-gmail-label";
    input.maxLength = 100;
    input.autocomplete = "off";
    input.disabled = true;
    const help = element("p", "profile-help",
      "Every outreach thread gets this label in Gmail: the emails you send to companies, first emails included, and the replies, with those found before now too. To find that outreach, the app reads the recipients and subject (never the body) of the emails you send. Leave it empty to stop labelling; pausing automation pauses it too. Rename it here rather than in Gmail: the app adds the label under this name and creates it if it is missing. After a rename, threads keep the old label too; delete it in Gmail if you no longer want it.");
    const mailbox = element("p", "profile-help gmail-label-mailbox");
    const permission = element("p", "profile-help gmail-label-permission");
    const status = element("p", "profile-help gmail-label-status");
    status.id = "settings-gmail-label-status";
    status.setAttribute("aria-live", "polite");
    field.append(label, input, help, mailbox, permission, status);
    function show(setting) {
      input.value = setting.value || "";
      input.placeholder = setting.default ? `Empty keeps labelling off; the usual name is ${setting.default}` : "Empty keeps labelling off";
      input.disabled = false;
      const box = setting.mailbox || {};
      const expected = box.expected || "";
      const known = box.connected_as || "";
      mailbox.className = "profile-help gmail-label-mailbox";
      if (!box.connected) {
        mailbox.textContent = "Pipeline mailbox: not connected";
      } else if (!known) {
        mailbox.textContent = "Pipeline mailbox: checking which account Gmail is connected as…";
      } else if (expected && known.toLowerCase() !== expected.toLowerCase()) {
        mailbox.className = "form-error gmail-label-mailbox";
        mailbox.textContent = `Pipeline mailbox: ${known}. Your outreach address is ${expected}; reconnect Gmail and choose ${expected}.`;
      } else {
        mailbox.textContent = `Pipeline mailbox: ${known}`;
      }
      permission.textContent = box.connected && setting.value && !setting.permission
        ? "Labelling waits until you reconnect Gmail on the Outreach tab."
        : "";
    }
    try {
      show(await api("/api/v1/outreach/gmail-label"));
    } catch (error) {
      mailbox.textContent = `The Gmail label setting could not be loaded: ${error.message}`;
      return field;
    }
    input.addEventListener("change", async () => {
      const wanted = input.value;
      const focused = document.activeElement === input;
      input.disabled = true;
      input.removeAttribute("aria-invalid");
      input.removeAttribute("aria-describedby");
      status.className = "profile-help gmail-label-status";
      status.textContent = "Saving…";
      try {
        const saved = await api("/api/v1/outreach/gmail-label", { method: "PUT", body: JSON.stringify({ value: wanted }) });
        show(saved);
        status.textContent = saved.value ? `Outreach threads will be labelled “${saved.value}”.` : "Labelling is off.";
      } catch (error) {
        status.className = "form-error gmail-label-status";
        status.textContent = error.message;
        input.setAttribute("aria-invalid", "true");
        input.setAttribute("aria-describedby", status.id);
      } finally {
        input.disabled = false;
        // Saving with Enter leaves the field focused; disabling it drops focus to the page, so put it back.
        if (focused && document.activeElement === document.body) input.focus();
      }
    });
    return field;
  }

  // One titled card per kind of setting; each setting inside is a row with
  // what it does on the left and its control on the right.
  function settingsSection(title, intro = "") {
    const section = element("section", "settings-section");
    const heading = element("h3", "", title);
    heading.id = `settings-section-${title.toLowerCase().replace(/[^a-z]+/g, "-")}`;
    section.setAttribute("aria-labelledby", heading.id);
    section.appendChild(heading);
    if (intro) section.appendChild(element("p", "settings-section-intro", intro));
    return section;
  }

  async function outreachSettingsPanel() {
    // The page's own heading ("Outreach settings") names the panel.
    const panel = element("section", "outreach-settings");
    panel.setAttribute("aria-labelledby", "result-count");
    // Per student and stored in the database, so it shows for every account.
    const automation = settingsSection("Automation", "Work the app does on its own. Every switch starts off.");
    automation.appendChild(await automationFields());
    const mail = settingsSection("Replies and Gmail");
    mail.appendChild(await jevInboxField());
    mail.appendChild(await gmailLabelField());
    panel.append(automation, mail);
    if (state.userId !== "local-user") {
      panel.appendChild(element("p", "profile-help", "These settings belong to the owner of this computer's workspace."));
      return panel;
    }
    let settings;
    try {
      settings = await api("/api/v1/outreach/settings");
    } catch (error) {
      panel.appendChild(element("p", "form-error", error.message));
      return panel;
    }
    if (!settings.available) {
      panel.appendChild(element("p", "profile-help", "These settings live in this computer's .env file, so only the owner's main workspace can change them."));
      return panel;
    }
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
      return optionElement(option.id, option.available ? option.label : `${option.label} (not set up)`, option.id === current);
    }

    const drafts = settings.draft_provider;
    const defaultLabel = drafts.options.find((option) => option.id === drafts.default)?.label || drafts.default;
    const [draftField, draftSelect] = selectField("settings-draft-provider", "Who writes first-email drafts",
      "Every draft still waits for your approval, whoever writes it.");
    draftSelect.appendChild(optionElement("", `Automatic (now: ${defaultLabel})`, !drafts.value));
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

    // Follow-ups and call prep default to the first-email writer; each can differ.
    function followingField(key, id, labelText, help, what) {
      const setting = settings[key];
      const [field, select] = selectField(id, labelText, help);
      select.appendChild(optionElement("", "Same as first-email drafts", !setting.value));
      setting.options.forEach((option) => select.appendChild(providerOption(option, setting.value)));
      const hint = element("p", "profile-help");
      const showHint = () => {
        const chosen = setting.options.find((option) => option.id === select.value);
        hint.textContent = chosen && !chosen.available ? chosen.hint : "";
      };
      showHint();
      field.appendChild(hint);
      select.addEventListener("change", () => {
        showHint();
        save({ [key]: select.value }, what);
      });
      return field;
    }
    const followField = followingField("follow_up_provider", "settings-follow-up-provider", "Who writes follow-ups",
      "Follow-ups wait for your approval too.", "Follow-up writer");
    const prepField = followingField("call_prep_provider", "settings-call-prep-provider", "Who writes call prep",
      "Written in the background once a company replies.", "Call prep writer");
    const thanksField = followingField("thank_you_provider", "settings-thank-you-provider", "Who writes thank-yous after a decline",
      "Used when \"Send a thank-you when someone declines\" is on. Plain rules check every one, and the reviewer below reads it before it goes.",
      "Thank-you writer");

    const review = settings.review_provider;
    const [reviewField, reviewSelect] = selectField("settings-review-provider", "Who reviews follow-ups and thank-yous",
      "Used when \"Have a second model check each follow-up\" is on, and for every thank-you after a decline. Automatic picks a model from a different company than the writer, so it does not share its blind spots.");
    const reviewAutomatic = document.createElement("option");
    reviewAutomatic.value = "";
    const reviewerName = (choice) => review.options.find((option) => option.id === choice.id)?.label || choice.id;
    const thanksReview = review.automatic_thank_you || review.automatic;
    // Follow-ups and thank-yous can have different writers, so Automatic can pick a different reviewer for each.
    const splitReview = !review.automatic.problem && !thanksReview.problem && thanksReview.id !== review.automatic.id;
    reviewAutomatic.textContent = review.automatic.problem
      ? "Automatic (no model is set up)"
      : splitReview
        ? `Automatic (now: ${reviewerName(review.automatic)} for follow-ups, ${reviewerName(thanksReview)} for thank-yous)`
        : `Automatic (now: ${reviewerName(review.automatic)}${review.automatic.note || thanksReview.note ? ", same company as a writer" : ""})`;
    reviewAutomatic.selected = !review.value;
    reviewSelect.appendChild(reviewAutomatic);
    review.options.forEach((option) => reviewSelect.appendChild(providerOption(option, review.value)));
    const reviewHint = element("p", "profile-help");
    const showReviewHint = () => {
      const chosen = review.options.find((option) => option.id === reviewSelect.value);
      if (chosen && !chosen.available) reviewHint.textContent = chosen.hint;
      else if (!reviewSelect.value && review.automatic.problem) reviewHint.textContent = "No model is set up on this computer to review. Reviewed follow-ups wait until one is, and every thank-you after a decline is held for you.";
      else if (!reviewSelect.value && (review.automatic.note || thanksReview.note)) reviewHint.textContent = "Only one company's models are set up here, so the reviewer is from the same company as the writer. It still reads every follow-up and thank-you; set up another model for a fully independent check.";
      else reviewHint.textContent = "";
    };
    showReviewHint();
    reviewField.appendChild(reviewHint);
    reviewSelect.addEventListener("change", () => {
      showReviewHint();
      save({ review_provider: reviewSelect.value }, "Reviewer");
    });

    const research = settings.research_agent;
    const [researchField, researchSelect] = selectField("settings-research-agent", "Who does the web research",
      "Used by the deep search, placing companies, Find people, and searching other sites for addresses. It runs the CLI signed in on this computer.");
    research.options.forEach((option) => researchSelect.appendChild(providerOption(option, research.value)));
    researchSelect.addEventListener("change", () => save({ research_agent: researchSelect.value }, "Research agent"));

    const companyResearch = settings.company_research_agent;
    const [companyResearchField, companyResearchSelect] = selectField("settings-company-research-agent", "Who researches a company for call prep",
      "Reads a company's site, job posts, patents, papers, and news for what they build and how; a fact is kept only when its quote is found on the page it cites. Runs when a company replies, or when you press Research this company.");
    companyResearchSelect.appendChild(optionElement("", "Same as the web research above", !companyResearch.value));
    companyResearch.options.forEach((option) => companyResearchSelect.appendChild(providerOption(option, companyResearch.value)));
    companyResearchSelect.addEventListener("change", () => save({ company_research_agent: companyResearchSelect.value }, "Company research agent"));

    // The one LinkedIn account call prep may read interviewers' profiles as.
    const linkedinField = element("div", "settings-field");
    const linkedinLabel = element("label", "", "LinkedIn test account for reading interviewers' profiles");
    linkedinLabel.htmlFor = "settings-linkedin-account";
    const linkedinInput = document.createElement("input");
    linkedinInput.type = "text";
    linkedinInput.id = "settings-linkedin-account";
    linkedinInput.placeholder = "Profile link or username; empty keeps LinkedIn off";
    linkedinInput.value = settings.linkedin_account?.value || "";
    linkedinField.append(linkedinLabel, linkedinInput, element("p", "profile-help",
      "Use a separate test account, never your everyday one. Before every read the app checks that LinkedIn is signed in as exactly this account, with importing your browser's sign-in turned off, and it only ever reads. See SETUP.md step 7b, Call prep."));
    linkedinInput.addEventListener("change", () => save({ linkedin_account: linkedinInput.value }, "LinkedIn account"));

    const [attachField, attachSelect] = selectField("settings-attachment", "Attach to Gmail drafts",
      "A copy is attached under its original file name. Drafts are only created; you send them yourself.");
    const attachState = element("p", "profile-help");
    attachField.appendChild(attachState);
    function renderAttachment() {
      const attachment = settings.attachment;
      attachSelect.replaceChildren();
      attachSelect.appendChild(optionElement("", "Nothing"));
      if (attachment.name) attachSelect.appendChild(optionElement("__current", `Current: ${attachment.name}`));
      attachment.resumes.forEach((resume) => {
        attachSelect.appendChild(optionElement(resume.id, `${resume.name} (uploaded ${formatDate(resume.created_at)})`));
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

    const writing = settingsSection("Writing and review",
      "Saved to this computer's .env and used right away; no restart needed. API keys stay in .env and are never shown here.");
    writing.append(draftField, followField, thanksField, reviewField, attachField);
    const researching = settingsSection("Research and call prep", "Saved to .env too.");
    researching.append(researchField, companyResearchField, prepField, linkedinField);
    // One save line for both sections, pinned to the bottom of the screen so it shows wherever the change was made.
    panel.append(writing, researching, status);
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

  // What this file does as it loads, run once by app.js in load order.
  function installOutreachTools() {
    registerSessionPoller(() => {
      window.clearTimeout(deepSearchTimer);
      deepSearchTimer = null;
    });
    registerSessionPoller(() => {
      window.clearTimeout(recontactTimer);
      recontactTimer = null;
    });
  }

  Object.assign(App, {
    OUTREACH_SORTS, OUTREACH_TABS, deepSearchPanel, installOutreachTools, outreachMatchesQuery, outreachNotInterested,
    outreachSettingsPanel, recontactPanel, scheduleDeepSearchPoll,
  });
})();
