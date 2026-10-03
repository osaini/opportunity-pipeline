// Outreach, part 2: the Gmail connection panel, the draft assistant and its history, contacts, and replies.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { state } = App;

  // From app-ui.js.
  const {
    WEEKDAY_DAY_FORMAT, announce, chip, element, externalLink, formatDate, formatDateTime, plural, safeExternalUrl,
    showError,
  } = App;

  // From app-http.js.
  const { api } = App;

  // From app-outreach-send.js.
  const {
    CALL_PREP_ACTIVE, CANDIDATE_METHOD_LABELS, CANDIDATE_VERIFICATION_LABELS, CONTACT_CONFIDENCE_LABELS,
    DRAFT_PROVIDER_LABELS, OUTREACH_EVENT_LABELS, OUTREACH_STATUS_LABELS, automaticSendWords, pauseWords, refocusOutreach,
    reloadOutreachAt,
  } = App;

  // Defined in files that load later; looked up when called.
  const loadOutreach = (...args) => App.loadOutreach(...args);
  const outreachLocationLine = (...args) => App.outreachLocationLine(...args);
  const patchOutreach = (...args) => App.patchOutreach(...args);
  const showCallPrepWriting = (...args) => App.showCallPrepWriting(...args);
  const watchCallPrep = (...args) => App.watchCallPrep(...args);

  // When Google will likely ask for the Gmail grant again, as the Outreach tab says it.
  // The date is gmail_health's estimate; once it has passed there is no date left to give.
  function gmailExpiryLine(likelyExpiresAt) {
    const when = new Date(likelyExpiresAt);
    if (!likelyExpiresAt || Number.isNaN(when.getTime()) || when.getTime() <= Date.now()) {
      return "Google may ask again soon; reconnecting now avoids a gap.";
    }
    const day = WEEKDAY_DAY_FORMAT.format(when);
    return `Google will likely ask again by ${day}; reconnecting now avoids a gap.`;
  }

  function gmailConnectPanel(gmail, automation) {
    if (!gmail?.configured) return null;
    // A working connection is offered Reconnect Gmail early when Google is
    // about to ask for it again, so reply and bounce checks never stop.
    const expiring = Boolean(gmail.connected && gmail.bounce_check && gmail.expiring_soon);
    // A connection that signed into another account than the outreach address, or that
    // lacks the permission to label outreach threads, is offered Reconnect Gmail too.
    const wrongAccount = Boolean(gmail.connected && gmail.wrong_account);
    const needsLabelPermission = Boolean(gmail.connected && gmail.bounce_check && gmail.label && !gmail.label_check);
    if (gmail.connected && gmail.bounce_check && !expiring && !wrongAccount && !needsLabelPermission) return null;
    const panel = element("div", "outreach-gmail-connect");
    const what = gmail.attachment ? ` with ${gmail.attachment} attached` : "";
    const reconnect = gmail.needs_reconnect || gmail.connected;
    // Not connected, and nothing wrong with a connection: the offer to connect, which says what sends without a click.
    const connecting = !gmail.connected && !gmail.needs_reconnect;
    const intro = element("p", "profile-help");
    const reason = (expiring
      ? gmailExpiryLine(gmail.likely_expires_at)
      : wrongAccount
      ? `Gmail is connected as ${gmail.connected_as}, but your outreach address is ${gmail.account}. Reconnect Gmail and choose ${gmail.account}.`
      : gmail.connected && gmail.bounce_check
      ? `Reconnect Gmail once so the app can add your “${gmail.label}” label to every outreach thread: the emails you send to companies, first emails included, and their replies. Google lists this permission as “Read, compose, and send emails”; tick it on Google's screen. The app uses it only to add that one label: it never deletes, archives, moves or marks mail as read. To find your outreach it also reads the recipients and subject (never the body) of the emails you send. To stop being asked, leave the label empty in Outreach settings.`
      : gmail.connected
      ? `Reconnect Gmail once so the app can catch bounces and log replies for you. It asks for permission to read mail; the app reads only delivery failure notices, mail from the companies you wrote to (Spam included), mail in the threads of the emails you sent them, and mail that names those companies or your emails' subjects. To find replies in those threads it lists recent mail by id, reading only what is in them.${gmail.label ? ` It also asks for the permission Google lists as “Read, compose, and send emails”, used only to add your “${gmail.label}” label to your outreach threads, sent emails and replies; to find the emails you send from Gmail it reads the recipients and subject (never the body) of each new email you send (it never deletes, archives, moves or marks mail as read); tick both boxes.` : ""}`
      : gmail.needs_reconnect
        ? "Gmail stopped accepting the connection. Reconnect it to keep creating drafts with attachments."
        : "");
    if (connecting) {
      // What sends without a click depends on the pause, so the sentence keeps both versions and a pause or
      // resume rewrites it in place (pauseWords), as the draft note does.
      const lead = `Connect Gmail to send approved emails${what} from here, or open them as drafts in Gmail first.`;
      const automatic = automaticSendWords(automation, ["bounce_auto_resend", "decline_thank_you", "form_submission"], gmail);
      const nothing = "Nothing sends until you press Send and confirm the recipient.";
      pauseWords(intro, `${lead} ${automatic?.running || nothing}`, `${lead} ${automatic?.paused || nothing}`);
    } else {
      intro.textContent = reason;
    }
    panel.appendChild(intro);
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

  function renderDraftChecks(host, subject, body, place = null) {
    // Mirrors outreach.targets.draft_checks, and for a cold email outreach.location.location_line_gap,
    // on the server so feedback is live while typing.
    const text = `${subject}\n${body}`;
    const dashes = (text.match(/[–—]/g) || []).length;
    const placeholders = [...new Set(text.match(/\[[^\]\n]{1,60}\]/g) || [])];
    const words = (body.match(/\b\w+\b/g) || []).length;
    host.replaceChildren(chip(plural(words, "word", "words"), words > 200 ? "is-soon" : ""));
    if (dashes) host.appendChild(chip(`${dashes} em/en dash${dashes === 1 ? "" : "es"}`, "is-warning"));
    if (placeholders.length) host.appendChild(chip(`Fill in ${placeholders.join(", ")}`, "is-soon"));
    if (body && !subject) host.appendChild(chip("No subject", "is-soon"));
    // "in Portland", not a bare "Portland": a school's name can carry the place (outreach.location.mentions_home).
    const flat = body.replace(/\s+/g, " ");
    const saysHome = (term) => new RegExp(`\\bin ${term.replace(/\s+/g, " ").replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "i").test(flat);
    if (place && place.terms && place.terms.length && body.trim() && !place.terms.some(saysHome)) {
      host.appendChild(chip(`Doesn't say you live in ${place.phrase}; add the location line or regenerate`, "is-warning"));
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
        } else if (event.detail && !["draft_generated", "gmail_draft_created", "gmail_sent", "call_prep_generated", "thank_you_sent", "thank_you_draft_created"].includes(event.event_type)) {
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
          await reloadOutreachAt(item.id, `[data-draft-kind="${kind}"] [data-draft-approve]`);
          return;
        }
        message.textContent = error.message;
        approve.disabled = false;
      }
    });

    // The saved draft never says the student lives near this company: the app's own
    // "(live in ...)" line goes in after the school's name, nothing else changes
    // (outreach/draft_location.py). An approved draft is approved again afterwards.
    if (kind === "initial" && bodyText && !item.sent_at && item.draft_location?.missing) {
      const addLine = element("button", "secondary-button", "Add the location line");
      addLine.type = "button";
      addLine.dataset.draftLocationLine = "";
      addLine.addEventListener("click", async () => {
        if (unsaved() && !window.confirm("Replace your unsaved edits with the saved draft plus the location line?")) return;
        addLine.disabled = true;
        try {
          await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/location-line`, { method: "POST" });
          state.outreachOpen = item.id;
          state.outreachDiscardEdits = true;
          announce(`Added "(live in ${item.draft_location.phrase})" to the ${item.company} draft.${status === "approved" ? " Approve it again to send it." : ""}`);
          await loadOutreach();
          refocusOutreach(item.id, `[data-draft-kind="${kind}"] [data-draft-approve]`);
        } catch (error) {
          message.textContent = error.message;
          addLine.disabled = false;
        }
      });
      buttons.appendChild(addLine);
    }
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
          const source = externalLink(sourceHref, "source ↗");
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
    section.appendChild(element("h4", "", "Contacts found"));
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
          const evidence = externalLink(evidenceHref, "Evidence ↗", { className: "outreach-evidence" });
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

  // An address the student found anywhere else. Its fields carry no name
  // attribute, so the pane's Save never sends them, and Enter adds rather than
  // submitting the pane's form.
  function outreachManualContactSection(item) {
    const section = element("section", "tracker-subsection outreach-contacts outreach-manual-contact");
    section.appendChild(element("h4", "", "Add a contact yourself"));
    section.appendChild(element("p", "outreach-note",
      "Found an address somewhere else? Add it and it becomes the contact. With no name, the draft greets the company's team."));
    const fields = element("div", "outreach-manual-fields");
    const field = (labelText, type, placeholder) => {
      const label = element("label", "profile-field");
      label.appendChild(element("span", "", labelText));
      const input = document.createElement("input");
      input.type = type;
      input.placeholder = placeholder;
      label.appendChild(input);
      fields.appendChild(label);
      return input;
    };
    const email = field("Email", "email", "hello@company.com");
    // Not "required": the field sits inside the pane's form, and a required
    // empty field there silently blocks every Save changes on the card. The
    // Add button checks the address itself.
    email.autocomplete = "off";
    const name = field("Name (optional)", "text", "Leave blank for a shared inbox");
    const role = field("Role (optional)", "text", "Founder, CTO…");
    const where = field("Where you found it (optional)", "url", "https://…");
    const confirmedLabel = element("label", "outreach-scope");
    const confirmed = document.createElement("input");
    confirmed.type = "checkbox";
    confirmedLabel.append(confirmed, document.createTextNode(" I confirmed this address: they gave it to me or published it"));
    const add = element("button", "secondary-button", "Add and use this contact");
    add.type = "button";
    const status = element("p", "form-status");
    status.setAttribute("aria-live", "polite");
    section.append(fields, confirmedLabel, add, status);

    const submit = async () => {
      if (!email.value.trim() || !email.checkValidity()) {
        status.textContent = "Enter an email address first.";
        email.focus();
        return;
      }
      add.disabled = true;
      try {
        const saved = await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/contacts`, {
          method: "POST",
          body: JSON.stringify({
            email: email.value.trim(), name: name.value.trim(), role: role.value.trim(),
            evidence_url: where.value.trim(), confirmed: confirmed.checked,
          }),
        });
        state.outreachOpen = item.id;
        announce(`${saved.contact_email} is now the ${item.company} contact${saved.contact_confidence === "confirmed" ? "" : ", marked unverified"}.`);
        await loadOutreach();
        refocusOutreach(item.id, ".outreach-manual-contact input");
      } catch (error) {
        status.textContent = error.message;
        add.disabled = false;
      }
    };
    add.addEventListener("click", submit);
    fields.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      submit();
    });
    return section;
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

  Object.assign(App, {
    draftAssistant, gmailConnectPanel, loadOutreachTimeline, outreachContactsSection, outreachManualContactSection,
    outreachReplySection, renderDraftChecks,
  });
})();
