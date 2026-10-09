// Outreach, part 4: a company's pane: next step, call prep, thank-you, possible replies, the research brief, and the
// card itself.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { els, registerSessionPoller, state } = App;

  // From app-ui.js.
  const {
    announce, armConfirm, autoSaveSelect, chip, copyText, element, externalLink, formatCalendarDate, formatDate,
    formatDateTime, gmailOpenLink, optionElement, safeExternalUrl, showError,
  } = App;

  // From app-http.js.
  const { api } = App;

  // From app-automation.js.
  const { automationFeature } = App;

  // From app-opportunities.js.
  const { companyTagsSection } = App;

  // From app-outreach-send.js.
  const {
    CALL_PREP_ACTIVE, CALL_PREP_WRITING, CONTACT_CONFIDENCE_LABELS, DRAFT_PROVIDER_LABELS, DRAFT_STATUS_LABELS, approveAndScheduleButton,
    approveFollowUpButtons, automaticSendWords, canApproveAndSchedule, OUTREACH_STATUS_LABELS,
    composeControl, formHost, formSendControls, outreachChoice, outreachContactFormSection, outreachDraftNeedsReview, outreachField,
    outreachReachable, pauseWords, refocusOutreach, refuseUnsavedHandOff, reloadOutreachAt, scheduleText, scheduleWords, sentFolderCheck,
  } = App;

  // From app-outreach-drafts.js.
  const {
    draftAssistant, loadOutreachTimeline, outreachContactsSection, outreachManualContactSection, outreachRecipients,
    outreachReplySection, renderDraftChecks,
  } = App;

  // From app-outreach-tools.js.
  const { outreachAppliedDirectly, outreachSetAside } = App;

  // Defined in files that load later; looked up when called.
  const loadOutreach = (...args) => App.loadOutreach(...args);

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
    if (!outreachReachable(item)) return 1;
    if (!item.email_body) return 2;
    if (item.draft_status !== "approved") return 3;
    return 4;
  }

  // The one thing to do next, in the words the bar and the list row use.
  // `tab` is where that work happens; the bar offers to go there.
  function outreachNextStep(item) {
    if (outreachAppliedDirectly(item)) {
      return { label: "Applied directly", hint: "You applied through their own site. It is kept here, and nothing automatic goes to it. Move it back to outreach to pick it up again.", tab: null };
    }
    if (outreachSetAside(item)) {
      return { label: "Nothing while not interested", hint: "It is kept here, and nothing automatic goes to it. Move it back to outreach to pick it up again.", tab: null };
    }
    if (item.possible_reply_count) {
      const one = item.possible_reply_count === 1;
      return { label: one ? "Check a possible reply" : "Check possible replies", hint: `${one ? "An email" : "Emails"} from them may be a reply. Say whether ${one ? "it is" : "each is"} on the card; follow-ups wait until you do.`, tab: null, tone: "is-warning" };
    }
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
      return { label: "Prep for the call", hint: "Write call prep: the company from web research, what they said, and questions to ask.", tab: "prep", tone: "is-region" };
    }
    if (OUTREACH_CALL_PREP.includes(item.status)) {
      const revisit = item.status === "replied" && item.follow_up_at ? ` Revisit on ${formatCalendarDate(item.follow_up_at)}.` : "";
      return { label: "Keep the conversation going", hint: `Log each reply so the history stays complete.${revisit}`, tab: "history" };
    }
    if (item.follow_up_due) {
      const queued = item.scheduled?.follow_up;
      if (queued?.state === "scheduled") {
        const suffix = ". Cancel it or send it now below.";
        return { label: "Follow-up scheduled", hint: `${scheduleWords(queued.label)}${suffix}`, schedule: { label: queued.label, suffix }, tab: null, tone: "is-region" };
      }
      if (queued?.state === "sending" || queued?.state === "transmitting") return { label: "Sending the follow-up", hint: "It is going out now.", tab: null, tone: "is-region" };
      if (queued?.state === "failed") return { label: "Scheduled follow-up stopped", hint: `${queued.error}.`, tab: "follow-up", tone: "is-warning" };
      return item.follow_up_status === "approved"
        ? { label: "Send the follow-up", hint: "It is approved; open it in your email and send it.", tab: null, tone: "is-warning" }
        : { label: "Follow up now", hint: "The follow-up date has passed. Draft and approve a short follow-up.", tab: "follow-up", tone: "is-warning" };
    }
    if (item.status === "sent" || item.status === "followed_up") {
      return { label: "Wait for a reply", hint: item.follow_up_at ? `Follow up on ${formatCalendarDate(item.follow_up_at)} if nothing arrives.` : "Log their reply here when it arrives.", tab: "history" };
    }
    if (item.contact_bounced) return { label: "Find a new contact", hint: `Your email to ${item.contact_email} bounced, so it reached no one. Pick another address; the greeting updates to match.`, tab: "contact", tone: "is-warning" };
    if (item.cc_bounced) return { label: "Fix the Cc", hint: `Email to ${item.contact_cc} bounced. Remove it or pick another before sending.`, tab: "contact", tone: "is-warning" };
    if (item.research_confidence === "unverified") return { label: "Confirm the research", hint: "The deep search summarized this company. Check the sources, then confirm.", tab: "research", tone: "is-soon" };
    if (!item.contact_email && !item.contact_form) return { label: "Find a contact", hint: "Search their site for a published address or a contact form, or add one you found.", tab: "contact", tone: "is-soon" };
    if (!item.email_body) return { label: "Write the draft", hint: "Generate a draft from your confirmed profile and this research.", tab: "draft" };
    if (outreachDraftNeedsReview(item, "initial") || item.draft_status !== "approved") return { label: "Review the draft", hint: "Read it, fix anything, then approve it. Approving unlocks the email hand-off.", tab: "draft", tone: "is-soon" };
    const scheduled = item.scheduled?.initial;
    if (scheduled?.state === "scheduled") {
      // schedule lets the hint follow a pause or resume in place (scheduleText).
      const suffix = ". Cancel it or send it now below.";
      return { label: "Scheduled", hint: `${scheduleWords(scheduled.label)}${suffix}`, schedule: { label: scheduled.label, suffix }, tab: null, tone: "is-region" };
    }
    if (scheduled?.state === "failed") return { label: "Scheduled send stopped", hint: `${scheduled.error}.`, tab: null, tone: "is-warning" };
    if (!item.contact_email && item.contact_form) {
      const form = item.contact_form;
      if (form.state === "needs_you") return { label: "Finish the contact form", hint: form.note || "The form needs you before it can go.", tab: null, tone: "is-warning" };
      if (form.state === "failed") return { label: "Contact form did not send", hint: form.note || "Nothing was sent. Try again.", tab: null, tone: "is-warning" };
      if (form.asks) return { label: "Did the form go?", hint: "You pressed its send button in Finish in browser. Say whether their page said your message was sent.", tab: null, tone: "is-warning" };
      if (form.state === "unconfirmed") return { label: "Check the form arrived", hint: "It may have been sent: their page did not confirm it. Look for a confirmation email.", tab: null, tone: "is-warning" };
      return { label: "Send through their contact form", hint: "They publish no email. The approved draft goes in through the form on their site, as you.", tab: null, tone: "is-region" };
    }
    return { label: "Send it from your email", hint: "Open the approved draft in your email, send it, then mark it sent here.", tab: null, tone: "is-region" };
  }

  const OUTREACH_PANE_TABS = [
    ["draft", "Draft"],
    ["follow-up", "Follow-up"],
    ["research", "Research"],
    ["contact", "Contact"],
    ["timing", "Timing"],
    ["history", "Replies and history"],
  ];

  // A follow-up has somewhere to go once the first email is out, and keeps it while one is written.
  const outreachHasFollowUp = (item) => item.status === "sent" || item.status === "followed_up" || Boolean(item.follow_up_body);

  // Call prep appears once a company writes back, and stays while it holds notes.
  function outreachPaneTabs(item) {
    const tabs = OUTREACH_PANE_TABS.filter(([id]) => id !== "follow-up" || outreachHasFollowUp(item));
    return OUTREACH_CALL_PREP.includes(item.status) || item.call_prep ? [["prep", "Call prep"], ...tabs] : tabs;
  }

  // The tab a company opens on: the one you last used for it, else where its next step lives.
  function outreachPaneTab(item) {
    const tabs = outreachPaneTabs(item).map(([id]) => id);
    const remembered = state.outreachTabs[item.id];
    if (remembered && tabs.includes(remembered)) return remembered;
    if (tabs.includes("prep")) return "prep";
    return item.contact_email || item.contact_form ? "draft" : "contact";
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

  const callPrepWatches = new Map();

  function watchCallPrep(id, delay = 5000) {
    if (callPrepWatches.has(id) && delay) return;
    window.clearTimeout(callPrepWatches.get(id));
    callPrepWatches.set(id, window.setTimeout(() => checkCallPrep(id), delay));
  }

  async function checkCallPrep(id) {
    const epoch = state.sessionEpoch;
    let active = true;
    try {
      const target = await api(`/api/v1/outreach/${encodeURIComponent(id)}`);
      active = CALL_PREP_ACTIVE.includes(target.call_prep_job?.state) || CALL_PREP_ACTIVE.includes(target.tech_brief_job?.state);
    } catch (error) {
      active = error.status !== 404;
    }
    // Signed out meanwhile (or by this very request's 401), possibly signed back in
    // since: this watch belongs to the old session, and the new one starts its own.
    if (!state.userId || state.sessionEpoch !== epoch) {
      if (state.sessionEpoch === epoch) callPrepWatches.delete(id);
      return;
    }
    if (active) {
      callPrepWatches.set(id, window.setTimeout(() => checkCallPrep(id), 5000));
      return;
    }
    callPrepWatches.delete(id);
    if (state.view !== "outreach") return;
    // Unsaved words in the pane come across the reload.
    await reloadOutreachAt(id);
  }

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
    const health = item.contact_email ? (item.contact_confidence === "confirmed" ? "is-good" : "is-soon") : item.contact_form ? "is-soon" : "is-bad";
    contact.append(
      element("span", `outreach-dot ${health}`),
      document.createTextNode(item.contact_email
        ? `${item.contact_name || item.contact_email} · ${item.contact_confidence === "confirmed" ? "confirmed" : "unverified"}`
        : item.contact_form ? "Contact form only" : "No contact yet")
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
    const link = externalLink(safe, `${label} ↗`);
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

  // Who the call is with (outreach/interviewer.py): found in your inbox, or
  // named here. A name or LinkedIn link typed here wins, and the next call prep
  // looks them up again.
  function outreachInterviewer(item) {
    const box = element("div", "outreach-interviewer is-wide");
    let record = item.interviewer || {};
    // What was stored was looked up for whoever was named then. When you name someone else, it is not theirs.
    const plainName = (value) => String(value || "").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
    const linkedinUser = (value) => (String(value || "").match(/\/in\/([^/?#\s]+)/i) || [])[1]?.toLowerCase() || String(value || "").trim().toLowerCase();
    const namedName = plainName(item.interviewer_name);
    const namedLink = linkedinUser(item.interviewer_linkedin);
    const otherPerson = Boolean(record.name || record.linkedin) && (
      (namedName && namedName !== plainName(record.name))
      || (namedLink && record.linkedin?.url && namedLink !== linkedinUser(record.linkedin.url))
    );
    let namedNow = "";
    if (otherPerson) {
      namedNow = String(item.interviewer_name || "").trim();
      record = {};
    }
    const said = element("p", "outreach-note");
    if (namedNow) {
      said.textContent = `Talking to ${namedNow} (you named them); LinkedIn not read yet. It is read when call prep next runs.`;
    } else if (record.name) {
      said.textContent = `Talking to ${record.name}${record.email ? ` (${record.email})` : ""}: ${record.evidence || ""}.${record.meeting ? ` Call: ${record.meeting}.` : ""}`;
    } else {
      said.textContent = "Who you are talking to is not known yet. It comes from your inbox once someone at the company writes, or name them below.";
    }
    box.appendChild(said);
    const linkedin = record.linkedin;
    if (linkedin) {
      const line = element("p", "outreach-note");
      line.append(`LinkedIn read ${formatDate(linkedin.read_at)} through your test account${linkedin.confirmed ? "" : `; ${linkedin.why || "it is not confirmed as this person"}, so check it is them`}. `);
      const href = safeExternalUrl(linkedin.url);
      if (href) {
        const link = externalLink(href, "Profile ↗");
        line.appendChild(link);
      }
      box.appendChild(line);
    }
    if (item.interviewer_error) box.appendChild(element("p", "outreach-note form-error", `Last look: ${item.interviewer_error}`));
    const fields = element("div", "outreach-interviewer-fields");
    outreachField(fields, "Talking to someone else? Their name", "interviewer_name", item.interviewer_name || "", { placeholder: record.name || "Full name" });
    outreachField(fields, "Their LinkedIn link", "interviewer_linkedin", item.interviewer_linkedin || "", { placeholder: "https://www.linkedin.com/in/…" });
    box.appendChild(fields);
    return box;
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
        ? "Call prep opens with what to ask, in call order, and your talking points, then what to know: who you're talking to, a reading of the company, and web research with numbered sources. Lines are short, for copying out by hand. It starts on its own when a company replies."
        : "Call prep is written from their reply. Paste it under Replies and history, and the notes start writing as soon as it is logged."));
    group.appendChild(outreachInterviewer(item));
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
        details.appendChild(element("p", "outreach-note", "Company facts link to the page each was confirmed on. Talking points carry the model's own citations."));
      }
      const list = element("ul");
      claims.forEach((claim) => {
        const row = element("li");
        row.appendChild(element("span", "", claim.text));
        const sourceHref = safeExternalUrl(claim.basis);
        if (sourceHref) {
          const source = externalLink(sourceHref, "source ↗");
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
          if (claim.section === "reading") {
            // The reading is built only on the checked research facts it cites, and is still not a fact itself.
            row.appendChild(element("small", "", "my read of the research (inference, not a checked fact)"));
            (Array.isArray(claim.sources) ? claim.sources : []).forEach((url) => {
              const href = safeExternalUrl(url);
              if (!href) return;
              const from = externalLink(href, "source ↗");
              row.appendChild(from);
            });
            list.appendChild(row);
            return;
          }
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

  // The thank-you the app sends on its own after a plain decline (Send a
  // thank-you when someone declines). While it waits it can be cancelled, or
  // moved to Gmail Drafts to edit, which stops the automatic send. One a check
  // held shows why, with Send it anyway (the student's own confirmed send) and
  // Dismiss. Once sent, when, and a link to the thread.
  function thankYouWho(thankYou) {
    // The server's first name leaves out a title ("Dr. Priya Shah" is Priya), as the email's greeting does.
    return String(thankYou.to_first_name || "").trim() || thankYou.to_email;
  }

  function thankYouText(thankYou) {
    const details = element("details", "outreach-claims outreach-thank-you-text");
    details.appendChild(element("summary", "", "Read the thank-you"));
    const to = thankYou.to_name ? `${thankYou.to_name} <${thankYou.to_email}>` : thankYou.to_email;
    details.appendChild(element("p", "outreach-thank-you-meta", `To ${to} · ${thankYou.subject}`));
    details.appendChild(element("pre", "outreach-event-detail", thankYou.body));
    const writer = String(thankYou.generated_by || "").split(":")[0];
    details.appendChild(element("p", "outreach-thank-you-meta", thankYou.generated_by === "template"
      ? "Fixed words, with no model. Checked by plain rules, and read by a second model before it goes."
      : `Written by ${DRAFT_PROVIDER_LABELS[writer] || writer}. Checked by plain rules, and read by a second model before it goes.`));
    return details;
  }

  function thankYouLink(href, text, label) {
    return externalLink(href, text, { className: "secondary-button", ariaLabel: label });
  }

  function thankYouAction(item, text, run, label) {
    const button = element("button", "secondary-button", text);
    button.type = "button";
    if (label) button.setAttribute("aria-label", label);
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const message = await run();
        await reloadOutreachAt(item.id);
        if (message) announce(message);
      } catch (error) {
        button.disabled = false;
        state.outreachOpen = item.id;
        if (error.status === 409 || error.status === 502) await loadOutreach();
        showError(error.message);
      }
    });
    return button;
  }

  function thankYouCancel(item, text) {
    const who = thankYouWho(item.thank_you);
    return thankYouAction(item, text, async () => {
      const result = await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/thank-you`, { method: "DELETE" });
      if (result.cancelled) return text === "Cancel" ? `Cancelled the thank-you to ${who}.` : "Dismissed. The thank-you will not be sent.";
      // Nothing was left to stop: it went (or is going) to Gmail, or a check had already stopped it.
      const now = result.thank_you || {};
      if (now.state === "sent" || now.state === "transmitting" || now.state === "sending" || now.send_state === "transmitting") {
        return `Too late to ${text === "Cancel" ? "cancel" : "dismiss"}: the thank-you to ${who} had already gone to Gmail. Check the history.`;
      }
      return `The thank-you to ${who} had already stopped${now.note ? `: ${String(now.note).replace(/[.!?]+$/, "")}` : ""}.`;
    }, text === "Cancel" ? `Cancel the thank-you to ${who}` : `Dismiss the thank-you to ${who}`);
  }

  // Why a waiting thank-you will be held at its time rather than sent: its switch, or Jev inbox
  // suggestions, turned off. Read from the switches on the page when they are loaded, else from the server.
  function thankYouHoldReason(serverReason) {
    const feature = automationFeature(state.automation?.settings, "decline_thank_you");
    if (!feature) return serverReason || "";
    if (feature.mode !== "on") return `${feature.label} is off`;
    return feature.requirement || "";
  }

  function paintThankYouHold(node) {
    const reason = thankYouHoldReason(node.dataset.thankYouHold);
    node.textContent = reason ? `It will be held at its time, not sent: ${reason}.` : "";
    node.hidden = !reason;
    return node;
  }

  function repaintThankYouHolds() {
    document.querySelectorAll("[data-thank-you-hold]").forEach(paintThankYouHold);
  }

  function thankYouEdit(item) {
    return thankYouAction(item, "Edit", async () => {
      await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/thank-you/edit`, { method: "POST" });
      return "The thank-you is in your Gmail Drafts, in their thread, to edit and send yourself. The app will not send it.";
    }, `Edit the thank-you to ${thankYouWho(item.thank_you)} in your Gmail Drafts`);
  }

  // Like Send: the first click only asks, and a second click sends. After a
  // 428 the server named what to look for in Gmail, and the next click vouches for it.
  function thankYouSendAnyway(item) {
    const thankYou = item.thank_you;
    const label = "Send it anyway";
    const button = element("button", "primary-button", label);
    button.type = "button";
    const sentCheck = sentFolderCheck();
    const reset = armConfirm(button, {
      idleLabel: () => (sentCheck.pending ? "Checked Gmail — send again" : label),
      armedLabel: () => `Send to ${thankYou.to_email}?`,
      prompt: () => `Press again to send the thank-you to ${thankYou.to_email}.`,
      onConfirm: async () => {
        button.disabled = true;
        button.textContent = "Sending…";
        const payload = { fingerprint: thankYou.fingerprint };
        sentCheck.attach(payload);
        try {
          await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/thank-you/send`, { method: "POST", body: JSON.stringify(payload) });
          await reloadOutreachAt(item.id);
          announce(`Sent the thank-you to ${thankYou.to_email}.`);
        } catch (error) {
          sentCheck.note(error);
          reset();
          button.disabled = false;
          if (error.status === 409 || error.status === 422) {
            await reloadOutreachAt(item.id);
          }
          showError(error.message);
        }
      },
    });
    return button;
  }

  function thankYouSection(item) {
    const thankYou = item.thank_you;
    if (!thankYou) return null;
    const who = thankYouWho(thankYou);
    const section = element("div", "outreach-thank-you");
    section.dataset.thankYouState = thankYou.state;
    const actions = element("div", "outreach-thank-you-actions");
    const going = thankYou.send_state === "transmitting" || ["transmitting", "sending"].includes(thankYou.state);
    const waiting = ["scheduled", "sending"].includes(thankYou.send_state);
    if (going) {
      section.append(element("p", "outreach-fit", `Thank-you to ${who} is being sent now.`), thankYouText(thankYou));
    } else if (waiting) {
      const line = pauseWords(element("p", "outreach-fit outreach-thank-you-when"),
        `Thank-you to ${who} goes out ${thankYou.label}.`,
        `Thank-you to ${who}. ${scheduleWords(thankYou.label, { paused: true })}.`);
      section.appendChild(line);
      const hold = element("p", "outreach-research-warning outreach-thank-you-hold");
      hold.dataset.thankYouHold = thankYou.will_hold || "";
      section.appendChild(paintThankYouHold(hold));
      if (thankYou.note && !/^paused/i.test(thankYou.note)) section.appendChild(element("p", "outreach-thank-you-meta", thankYou.note));
      actions.append(thankYouCancel(item, "Cancel"), thankYouEdit(item));
      section.append(thankYouText(thankYou), actions);
    } else if (thankYou.state === "sent") {
      section.appendChild(element("p", "outreach-fit", `Thank-you sent ${formatDateTime(thankYou.sent_at)}.`));
      if (thankYou.thread_url) actions.appendChild(thankYouLink(thankYou.thread_url, "Open the thread in Gmail ↗", `Open the thread in Gmail, with ${who}`));
      section.append(thankYouText(thankYou), actions);
    } else if (["held", "failed"].includes(thankYou.state)) {
      // 'failed' includes a send Gmail may have carried out, so it is "stopped", never "was not sent".
      const what = thankYou.state === "held" ? "held" : "stopped";
      section.appendChild(element("p", "outreach-research-warning", `Thank-you to ${who} ${what}: ${thankYou.note || "no reason was given"}.`.replace(/\.\.$/, ".")));
      actions.append(thankYouSendAnyway(item), thankYouCancel(item, "Dismiss"));
      section.append(thankYouText(thankYou), actions);
    } else if (thankYou.state === "cancelled") {
      if (thankYou.draft_url) {
        section.appendChild(element("p", "outreach-fit", `Thank-you to ${who} is in your Gmail Drafts, to edit and send yourself.`));
        actions.appendChild(thankYouLink(thankYou.draft_url, "Open the draft in Gmail ↗", `Open the draft in Gmail: the thank-you to ${who}`));
        section.appendChild(actions);
      } else {
        const note = thankYou.note || "cancelled";
        // Closed after a try Gmail never confirmed: it may have gone, and the note says so.
        const what = /Sent folder/.test(note) ? "stopped" : "not sent";
        // The reply failed one of the rules for sending on its own; the note already says it was not thanked.
        const text = /^Not thanked automatically:/.test(note) ? `${note}.` : `Thank-you to ${who} ${what}: ${note}`;
        section.appendChild(element("p", "outreach-thank-you-meta", text));
      }
    } else {
      return null;
    }
    return section;
  }

  // Emails from the company that may be replies (outreach/inbox.py). Not
  // counted until the student says; follow-ups and closing as No response wait.
  function outreachPossibleReplies(item) {
    const waiting = item.possible_replies || [];
    if (!waiting.length) return null;
    const box = element("div", "outreach-possible-replies");
    waiting.forEach((mail) => {
      const entry = element("section", "outreach-possible-reply");
      const subject = mail.subject || "(no subject)";
      entry.setAttribute("aria-label", `Possible reply from ${mail.from}`);
      const head = element("p", "outreach-possible-reply-head");
      head.appendChild(element("strong", "", "Possible reply: "));
      head.appendChild(document.createTextNode(`${mail.from} wrote ${formatDate(mail.received_at)}, “${subject}”.${mail.in_spam ? " Gmail put it in Spam." : ""}`));
      entry.appendChild(head);
      if (mail.preview) entry.appendChild(element("blockquote", "outreach-possible-reply-text", mail.preview));
      const others = (mail.companies || []).filter((company) => company.id !== item.id).map((company) => company.company);
      const also = others.length ? ` It could also be from ${others.join(", ")}; saying it is a reply here logs it for ${item.company}.` : "";
      entry.appendChild(element("p", "outreach-possible-reply-why", `Not counted as a reply yet: ${mail.reason_text || mail.reason}. Follow-ups and closing as No response wait until you say.${also}`));
      const actions = element("div", "outreach-possible-reply-actions");
      const decide = async (decision, button) => {
        actions.querySelectorAll("button").forEach((control) => { control.disabled = true; });
        try {
          await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/possible-replies/${encodeURIComponent(mail.gmail_id)}`, {
            method: "POST",
            body: JSON.stringify({ decision }),
          });
          await reloadOutreachAt(item.id, ".outreach-possible-reply-actions button", ".outreach-next select");
          announce(decision === "reply"
            ? `Logged ${mail.from}'s email as ${item.company}'s reply.`
            : `Set aside ${mail.from}'s email; it is not a reply.`);
        } catch (error) {
          // Settled already (another tab) or gone: the card is stale, so it is shown again as it is now.
          if (error.status === 409 || error.status === 404) {
            await reloadOutreachAt(item.id, ".outreach-possible-reply-actions button", ".outreach-next select");
          } else {
            actions.querySelectorAll("button").forEach((control) => { control.disabled = false; });
            button.focus();
          }
          showError(error.message);
        }
      };
      const about = `${mail.from}'s email “${subject}”`;
      const yes = element("button", "primary-button", "It's a reply, log it");
      yes.type = "button";
      yes.setAttribute("aria-label", `It's a reply, log it: ${about}, for ${item.company}`);
      yes.addEventListener("click", () => decide("reply", yes));
      const no = element("button", "secondary-button", "Not a reply");
      no.type = "button";
      no.setAttribute("aria-label", `Not a reply: ${about}`);
      // A follow-up is in line for this company, or for another it could be from: a misclick here would
      // let it go to someone who may have answered, so the button asks once more, as Send does.
      const releases = Boolean(mail.holds_follow_up) || ["scheduled", "sending"].includes(item.scheduled?.follow_up?.state);
      armConfirm(no, {
        idleLabel: () => "Not a reply",
        armedLabel: () => "Not a reply: let the follow-up go?",
        idleAriaLabel: () => `Not a reply: ${about}`,
        armedAriaLabel: () => `Not a reply: let the follow-up go? ${about}`,
        prompt: () => "Press Not a reply again to let the follow-up go.",
        confirmValue: "1",
        shouldArm: () => releases,
        onConfirm: () => decide("not_reply", no),
      });
      actions.append(yes, no);
      const open = gmailOpenLink(mail.gmail_url, subject);
      if (open) actions.appendChild(open);
      entry.appendChild(actions);
      box.appendChild(entry);
    });
    return box;
  }

  // Research from the web (outreach/research.py). Every fact's quote was found
  // on the page it cites, and the fact says no more than the quote and the
  // lines around it; one from the company's own site that turned the check away
  // is kept but says it was not checked.
  const TECH_BRIEF_SECTIONS = [
    ["product", "What they build"],
    ["customers", "Who they sell to"],
    ["edge", "What they say sets them apart"],
    ["competitors", "Competitors"],
    ["technology", "How it works"],
    ["engineering", "What they build it with"],
    ["growth", "Where they're expanding"],
    ["hiring", "Where they're hiring"],
    ["team", "Who builds it"],
    ["traction", "Funding, customers, and partners"],
    ["news", "Recent news"],
  ];
  const TECH_BRIEF_RESEARCHING = "Researching this company on the web in the background. It takes a few minutes and keeps going if you leave this page. A fact is kept only when its quote is found on the page it cites and a separate read of that page's own passage confirms the page says it.";

  function sourceHost(url) {
    try {
      return new URL(url).hostname.replace(/^www\./, "");
    } catch (_error) {
      return "source";
    }
  }

  // Only a quote found on the page is shown as the page's words.
  function briefSourceLink(url, quote, found) {
    const href = safeExternalUrl(url);
    if (!href) return null;
    const link = externalLink(href, `${sourceHost(url)} ↗`);
    if (quote) {
      link.title = found
        ? `The page says: "${quote}"`
        : `The research agent's quote, not checked: "${quote}"`;
    }
    return link;
  }

  function outreachTechBrief(item, context = {}) {
    const group = element("fieldset", "outreach-group is-brief");
    group.appendChild(element("legend", "", "Company research"));
    const brief = item.tech_brief || {};
    const facts = brief.facts || [];
    const job = item.tech_brief_job;
    // Call prep researches the company first when there is no fresh brief, so the job that is writing it counts too.
    const briefAge = item.tech_brief_at ? Date.now() - new Date(item.tech_brief_at).getTime() : Infinity;
    const freshBrief = facts.some((fact) => fact.checked) && briefAge < 30 * 24 * 3600 * 1000;
    const preppingFirst = !freshBrief && CALL_PREP_ACTIVE.includes(item.call_prep_job?.state);
    const active = CALL_PREP_ACTIVE.includes(job?.state) || preppingFirst;
    if (facts.length) {
      const unchecked = facts.filter((fact) => !fact.checked).length;
      const by = item.tech_brief_by ? ` by ${DRAFT_PROVIDER_LABELS[item.tech_brief_by] || item.tech_brief_by}` : "";
      group.appendChild(element("p", "outreach-note is-wide",
        `From the web, ${formatDate(item.tech_brief_at)}${by}. Each fact's quote was found on the page it links to, and a separate read of the page's own passage confirmed the page says it${unchecked ? `, except the ${unchecked} marked not checked` : ""}. That shows the page says it, not that the page is right. Call prep is built from this.`));
    } else {
      group.appendChild(element("p", "outreach-note is-wide",
        "No research yet. It reads the company's site, job posts, patents, papers, grants, and news for what they build, how it works, what they build it with, and who built it, and keeps a fact only when its quote is found on the page it cites and a separate read of that page's own passage confirms the page says it. Call prep runs it on its own when a company replies."));
    }
    if (brief.note) group.appendChild(element("p", "outreach-note is-wide", brief.note));
    if (item.tech_brief_error) group.appendChild(element("p", "outreach-note form-error is-wide", `Last try: ${item.tech_brief_error}`));
    TECH_BRIEF_SECTIONS.forEach(([key, label]) => {
      const chosen = facts.filter((fact) => fact.section === key);
      if (!chosen.length) return;
      const section = element("section", "outreach-brief-section is-wide");
      section.appendChild(element("h4", "", label));
      const list = element("ul", "outreach-brief-list");
      chosen.forEach((fact) => {
        const row = element("li");
        row.appendChild(element("span", "", fact.text));
        const link = briefSourceLink(fact.source_url, fact.quote, fact.checked);
        if (link) row.appendChild(link);
        if (!fact.checked) row.appendChild(element("small", "", `not checked: ${fact.note || "it could not be checked"}`));
        if (key === "competitors" && String(fact.note || "").startsWith("picked")) row.appendChild(element("small", "", "the research agent's pick of a competitor"));
        list.appendChild(row);
      });
      section.appendChild(list);
      group.appendChild(section);
    });
    const gaps = brief.gaps || [];
    if (gaps.length) {
      const section = element("section", "outreach-brief-section is-wide");
      section.appendChild(element("h4", "", "Not found online (the research agent's list, not checked; worth asking)"));
      const list = element("ul", "outreach-brief-list");
      gaps.forEach((gap) => list.appendChild(element("li", "", gap)));
      section.appendChild(list);
      group.appendChild(section);
    }
    const refused = brief.refused || [];
    if (refused.length) {
      const details = element("details", "outreach-rejected is-wide");
      details.appendChild(element("summary", "", `Left out (${refused.length}): facts the checks did not keep, each with why`));
      const list = element("ul");
      refused.forEach((fact) => {
        const row = element("li", "", `${fact.text} (${fact.reason})`);
        const link = briefSourceLink(fact.source_url);
        if (link) row.append(" ", link);
        list.appendChild(row);
      });
      details.appendChild(list);
      group.appendChild(details);
    }
    const assistant = element("div", "outreach-brief-actions is-wide");
    const buttons = element("div", "outreach-draft-buttons");
    const run = element("button", facts.length ? "secondary-button" : "primary-button",
      active ? "Researching…" : facts.length ? "Research again" : "Research this company");
    run.type = "button";
    const available = context.research?.available !== false;
    run.disabled = active || !available;
    const message = element("p", "form-status");
    message.setAttribute("aria-live", "polite");
    if (!available) message.textContent = context.research?.reason || "Research is not available in this app. It runs in the app on your own database.";
    run.addEventListener("click", async () => {
      run.disabled = true;
      try {
        await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/research`, { method: "POST" });
        state.outreachOpen = item.id;
        announce(`Researching ${item.company} in the background.`);
        await loadOutreach();
      } catch (error) {
        run.disabled = false;
        message.textContent = error.message;
      }
    });
    buttons.appendChild(run);
    assistant.append(buttons, message);
    if (job && job.state !== "succeeded") {
      const when = job.next_attempt_at ? formatDate(job.next_attempt_at) : "soon";
      const text = {
        queued: TECH_BRIEF_RESEARCHING,
        running: TECH_BRIEF_RESEARCHING,
        retry: `The last try did not finish (${job.error || "no reason given"}). Trying again ${when}.`,
        dead: `Could not research after ${job.attempts} tries: ${job.error || "no reason given"}. Press the button to try again.`,
        cancelled: "The last research request was cancelled.",
      }[job.state];
      if (text) {
        // Drawn fresh on each load, so it is announced where it changes, not here.
        assistant.appendChild(element("p", `outreach-note${job.state === "dead" ? " form-error" : ""}`, text));
      }
    }
    if (active) watchCallPrep(item.id);
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
    // How the latest reply found in Gmail was matched to the company, always: it may not be from the address written to.
    if (item.gmail_reply) {
      const found = item.gmail_reply;
      identity.appendChild(element("p", "outreach-fit", `Latest reply found in Gmail ${formatDate(found.received_at)}: ${found.reason_text}.`));
    }
    const thanks = thankYouSection(item);
    if (thanks) identity.appendChild(thanks);
    const possible = outreachPossibleReplies(item);
    if (possible) identity.appendChild(possible);
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
    const setAside = outreachSetAside(item);
    const applied = outreachAppliedDirectly(item);
    if (setAside) side.appendChild(chip(`${applied ? "Applied directly, marked" : "Not interested since"} ${formatDate(item.not_interested_at)}`, applied ? "is-region" : "is-warning"));
    const link = safeExternalUrl(item.website) || item.source_urls.map(safeExternalUrl).find(Boolean);
    if (link) {
      const site = externalLink(link, "Research source ↗", { className: "secondary-button" });
      side.appendChild(site);
    }
    // Filed under Not interested or Applied directly, never deleted; automation leaves it alone until it is moved back.
    const setAsideButton = (label, payload, message) => {
      const button = element("button", "secondary-button outreach-interest", label);
      button.type = "button";
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await patchOutreach(item, payload, message, ".outreach-interest");
        } catch (error) {
          showError(error.message);
          button.disabled = false;
        }
      });
      side.appendChild(button);
    };
    if (setAside) {
      setAsideButton("Move back to outreach", { not_interested: false }, `${item.company} moved back into your outreach.`);
    } else {
      setAsideButton("Applied directly", { applied_directly: true },
        `${item.company} moved to Applied directly. It is kept there, and nothing automatic goes to it.`);
      setAsideButton("Not interested", { not_interested: true },
        `${item.company} moved to Not interested. It is kept there, and nothing automatic goes to it.`);
    }
    heading.append(identity, side);

    const facts = element("div", "application-facts");
    // A shared inbox has no person, so the address itself says who hears from you.
    const contactText = [item.contact_name, item.contact_role].filter(Boolean).join(", ") || item.contact_email;
    if (contactText) facts.appendChild(chip(contactText));
    facts.appendChild(chip(
      item.contact_confidence === "unknown" && !item.contact_email && !item.contact_name
        ? (item.contact_form ? "Contact form only" : "No contact yet")
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
    const hint = element("span", "", next.hint);
    if (next.schedule) scheduleText(hint, next.schedule.label, { suffix: next.schedule.suffix });
    nextText.append(element("strong", "", `Next: ${next.label}`), hint);
    const label = element("label", "");
    label.appendChild(element("span", "", "Status"));
    const select = document.createElement("select");
    Object.entries(OUTREACH_STATUS_LABELS).forEach(([value, text]) => {
      select.appendChild(optionElement(value, text, item.status === value));
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
    // One press for the usual path; the separate buttons above and in the Draft tab stay.
    if (canApproveAndSchedule(context, item)) actions.appendChild(approveAndScheduleButton(item));
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
    // With no email, the approved draft goes through the company's contact form.
    if (!item.contact_email && item.contact_form && item.draft_status === "approved" && !item.sent_at && ["not_started", "drafted", "paused"].includes(item.status)) {
      const controls = formSendControls(item);
      actions.appendChild(controls);
      if (item.contact_form.state === "unconfirmed") {
        // Asked after the student's own press in Finish in browser, "It arrived" is their answer: Yes, it was sent.
        const asks = Boolean(item.contact_form.asks);
        const arrived = element("button", asks ? "primary-button" : "secondary-button", asks ? "Yes, it was sent" : "It arrived");
        arrived.type = "button";
        arrived.addEventListener("click", async () => {
          arrived.disabled = true;
          try {
            await patchOutreach(item, { status: "sent" }, `${item.company} marked sent. A follow-up is set for a week from today.`);
          } catch (error) {
            showError(error.message);
            arrived.disabled = false;
          }
        });
        if (asks) controls.insertBefore(arrived, controls.querySelector("button"));
        else actions.appendChild(arrived);
      }
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
            await reloadOutreachAt(item.id);
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
    if (!item.contact_email && item.contact_form) {
      const to = element("p", "outreach-to is-wide");
      to.append(element("strong", "", "To "), document.createTextNode(`${item.company}'s contact form (${formHost(item)})`));
      draft.appendChild(to);
    }
    // A company reached only through its form has no To to choose until an address turns up.
    const recipients = item.contact_email || !item.contact_form ? outreachRecipients(item) : null;
    if (recipients) draft.appendChild(recipients.element);
    if (item.contact_email) {
      if (item.contact_confidence === "unverified") {
        draft.appendChild(element("p", "outreach-note outreach-guess is-wide", item.contact_cc
          ? `${item.contact_email} is a guessed address, not confirmed. ${item.contact_cc} is in Cc, so a wrong guess still reaches the company.`
          : `${item.contact_email} is ${item.contact_route?.startsWith("You added this address") ? "an address you added" : "a guessed address"}, not confirmed. Check it before you send.`));
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
    const formNote = (automatic) => `They publish no email, so this goes through the contact form on their site, as you. Approving it unlocks Send through contact form, which asks you to confirm first${automatic}.`;
    const sendNote = element("p", "outreach-note");
    // What this note promises is built from the switches that send without a click (automaticSendWords): a
    // thank-you after a decline can follow any company's email, a resend after a bounce an emailed first message,
    // and a form submission a company that has only a form. A pause holds all of them (automation/ledger.py).
    const viaForm = !item.contact_email && item.contact_form;
    const automatic = automaticSendWords(context.automation, viaForm ? ["decline_thank_you"] : ["bounce_auto_resend", "decline_thank_you"], context.gmail);
    const formAutomatic = viaForm && Boolean(context.automation?.form_submission);
    const lead = viaForm
      ? [formNote(formAutomatic ? "; with sending through contact forms on in Settings, an approved draft goes on its own" : ""),
         formNote(formAutomatic ? "; with sending through contact forms on in Settings, an approved draft goes on its own once you resume automation" : "")]
      : context.gmail?.connected
      ? [`${automatic ? "" : "Nothing sends on its own. "}Approving a draft unlocks Send, which asks you to confirm the recipient before the email goes out from your Gmail. To send at a set time with this computer off, use Open in Gmail and Gmail's Schedule send; the app marks it sent when Google sends it.`]
      : [`${automatic ? "" : "Nothing sends from here. "}Approving a draft unlocks a link that opens it in your own email, where you press Send.`];
    const [leadRunning, leadPaused = leadRunning] = lead;
    pauseWords(sendNote,
      automatic ? `${leadRunning} ${automatic.running}` : leadRunning,
      automatic ? `${leadPaused} ${automatic.paused}` : leadPaused);
    draft.appendChild(sendNote);

    let followUpGroup = null;
    if (tabNames["follow-up"]) {
      followUpGroup = element("fieldset", "outreach-group is-draft");
      followUpGroup.appendChild(element("legend", "", "Follow-up draft"));
      if (item.contact_email) {
        const to = element("p", "outreach-to is-wide");
        to.append(element("strong", "", "To "), document.createTextNode(item.contact_cc ? `${item.contact_email} (Cc ${item.contact_cc})` : item.contact_email));
        followUpGroup.appendChild(to);
      }
      const followSubject = outreachField(followUpGroup, "Subject", "follow_up_subject", item.follow_up_subject, { wide: true });
      const followBody = outreachField(followUpGroup, "Body", "follow_up_body", item.follow_up_body, { multiline: true, wide: true });
      followBody.rows = 8;
      const followChecks = element("div", "outreach-checks");
      followChecks.setAttribute("aria-live", "polite");
      const refreshFollowChecks = () => renderDraftChecks(followChecks, followSubject.value, followBody.value);
      followSubject.addEventListener("input", refreshFollowChecks);
      followBody.addEventListener("input", refreshFollowChecks);
      refreshFollowChecks();
      followUpGroup.appendChild(followChecks);
      draftAssistant(followUpGroup, item, "follow_up", followSubject, followBody, { extra: approveFollowUpButtons(context, item, followSubject, followBody) });
    }

    const notesGroup = element("fieldset", "outreach-group");
    notesGroup.appendChild(element("legend", "", "Notes"));
    outreachField(notesGroup, "Private notes", "notes", item.notes, { multiline: true, wide: true });

    // Each draft sits beside a summary of who it goes to and when, so the other
    // tabs are only needed to change those details.
    const draftAside = () => {
      const aside = element("aside", "outreach-aside");
      aside.setAttribute("aria-label", "Contact and timing");
      aside.append(
        outreachSummaryCard("Contact", item.contact_email || item.contact_name ? [
          [item.contact_name || item.contact_email, "outreach-aside-strong"],
          item.contact_role ? [item.contact_role] : null,
          item.contact_name && item.contact_email ? [item.contact_email] : null,
          [CONTACT_CONFIDENCE_LABELS[item.contact_confidence], `outreach-aside-tag is-${item.contact_confidence}`],
        ] : item.contact_form ? [["Contact form", "outreach-aside-strong"], [formHost(item)]] : [["No contact yet. Find one under Contact."]]),
        outreachSummaryCard("Timing", [
          [`Deadline: ${item.deadline_date ? formatCalendarDate(item.deadline_date) : item.deadline_label || "none recorded"}`],
          [item.sent_at ? `Sent ${formatCalendarDate(item.sent_at)}` : "Not sent yet"],
          OUTREACH_REVISIT.includes(item.status)
            ? [item.follow_up_at ? `Revisit ${formatCalendarDate(item.follow_up_at)}` : "No revisit date set"]
            : [item.follow_up_at ? `Follow up ${formatCalendarDate(item.follow_up_at)}` : "Follow-up is set when you mark it sent"],
        ])
      );
      return aside;
    };
    const draftPanel = panel("draft");
    const draftMain = element("div", "outreach-draft-main");
    draftMain.appendChild(draft);
    draftPanel.append(draftMain, draftAside());
    const followUpPanels = [];
    if (followUpGroup) {
      const followUpPanel = panel("follow-up");
      const followUpMain = element("div", "outreach-draft-main");
      followUpMain.appendChild(followUpGroup);
      followUpPanel.append(followUpMain, draftAside());
      followUpPanels.push(followUpPanel);
    }

    const researchPanel = panel("research");
    researchPanel.append(research, outreachTechBrief(item, context), notesGroup);
    const contactsSection = outreachContactsSection(item, { onCandidates: recipients?.update });
    const contactPanel = panel("contact");
    contactPanel.append(contact, outreachManualContactSection(item), outreachContactFormSection(item), contactsSection.element);
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
    remove.hidden = outreachSetAside(item);
    const formStatus = element("p", "form-status");
    formStatus.setAttribute("aria-live", "polite");
    footer.append(save, remove, formStatus);
    const prepPanels = [];
    if (tabNames.prep) {
      const prepPanel = panel("prep");
      prepPanel.appendChild(outreachCallPrep(item));
      prepPanels.push(prepPanel);
    }
    form.append(...prepPanels, draftPanel, ...followUpPanels, researchPanel, contactPanel, timingPanel, historyPanel, footer);
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

  // What this file does as it loads, run once by app.js in load order.
  function installOutreachPane() {
    registerSessionPoller(() => {
      callPrepWatches.forEach((timer) => window.clearTimeout(timer));
      callPrepWatches.clear();
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") [...callPrepWatches.keys()].forEach((id) => watchCallPrep(id, 0));
    });
  }

  Object.assign(App, {
    captureUnsavedOutreachEdits, createOutreachCard, installOutreachPane, outreachLocationLine, outreachRow,
    patchOutreach, repaintThankYouHolds, showCallPrepWriting, watchCallPrep,
  });
})();
