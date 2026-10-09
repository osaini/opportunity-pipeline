// Outreach, part 1: labels, sending (Gmail, contact form, scheduling), bounce checks, and putting a company back on
// screen after an action.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { els, registerSessionPoller, state } = App;

  // From app-ui.js.
  const {
    announce, armConfirm, chip, copyText, element, externalLink, formatCalendarDate, optionElement, profileField,
    showError,
  } = App;

  // From app-http.js.
  const { api } = App;

  // From app-status.js.
  const { automationPaused } = App;

  // Defined in files that load later; looked up when called.
  const loadOutreach = (...args) => App.loadOutreach(...args);

  const OUTREACH_STATUS_LABELS = {
    not_started: "Not started",
    drafted: "Drafted",
    sent: "Sent",
    followed_up: "Followed up",
    replied: "Replied",
    call_scheduled: "Call scheduled",
    offer: "Offer",
    declined: "Declined",
    no_response: "No response",
    paused: "Paused",
  };
  const CONTACT_CONFIDENCE_LABELS = {
    confirmed: "Contact confirmed",
    unverified: "Contact unverified",
    unknown: "Contact not confirmed",
  };

  function outreachField(form, labelText, name, value, options = {}) {
    const control = profileField(form, labelText, name, value, options);
    control.dataset.initial = control.value;
    if (options.wide) control.closest("label").classList.add("is-wide");
    return control;
  }

  function outreachChoice(form, labelText, name, value, choices) {
    const label = element("label", "profile-field");
    label.appendChild(element("span", "", labelText));
    const select = document.createElement("select");
    select.name = name;
    choices.forEach(([optionValue, text]) => {
      select.appendChild(optionElement(optionValue, text, optionValue === value));
    });
    select.dataset.initial = select.value;
    label.appendChild(select);
    form.appendChild(label);
    return select;
  }

  const DRAFT_STATUS_LABELS = {
    generated: ["Draft needs review", "is-soon"],
    approved: ["Draft approved", "is-region"],
  };
  const CANDIDATE_METHOD_LABELS = {
    site_published: "Published on their site",
    site_generic: "Shared inbox on their site",
    site_person: "Named on their site, no address",
    pattern_guess: "Guessed from a name on their site",
    published_elsewhere: "Printed on another site",
    ai_research: "Named in deep search research",
    manual: "Added by you",
  };
  // What the company's mail server said when asked about an address. Asking
  // sends no email, and even an accepted address stays unverified.
  const CANDIDATE_VERIFICATION_LABELS = {
    smtp_accepted: ["Mail server accepted it", "is-region"],
    smtp_rejected: ["Mail server: no such address", "is-warning"],
    catch_all: ["Server accepts any address", "is-soon"],
    smtp_unknown: ["Mail server gave no answer", ""],
  };
  const OUTREACH_EVENT_LABELS = {
    created: "Added",
    draft_edited: "Draft edited",
    follow_up_edited: "Follow-up edited",
    draft_generated: "Draft generated",
    follow_up_generated: "Follow-up generated",
    draft_approved: "Draft approved",
    follow_up_approved: "Follow-up approved",
    approval_withdrawn: "Approval withdrawn",
    reply_logged: "Reply logged",
    contacts_searched: "Searched their site for contacts",
    discovery_follow_through: "Deep search follow-up",
    research_confirmed: "Research confirmed",
    contact_applied: "Contact applied",
    gmail_draft_created: "Draft created in Gmail",
    gmail_sent: "Sent from Gmail",
    bounced: "Bounced",
    partly_bounced: "Partly bounced",
    greeting_updated: "Greeting updated for the new contact",
    auto_reply: "Automatic reply (out of office)",
    possible_reply: "Possible reply found in Gmail",
    possible_reply_dismissed: "Not a reply, you said",
    possible_reply_confirmed: "A reply, you said",
    reply_found: "Found in Gmail",
    contact_recovery: "Looked for another contact after the bounce",
    send_scheduled: "Send scheduled",
    send_cancelled: "Scheduled send cancelled",
    scheduled_send_failed: "Scheduled send stopped",
    follow_up_reviewed: "Follow-up reviewed",
    gmail_scheduled: "Scheduled in Gmail",
    send_moved: "Scheduled send moved to the next morning",
    follow_up_held: "Follow-up held",
    auto_draft_failed: "Automatic draft failed",
    draft_restored: "Earlier draft restored",
    follow_up_restored: "Earlier follow-up restored",
    // Named apart from an ordinary "location recorded" on purpose: this is what
    // an import file claimed and the tracker refused, not what it accepted.
    location_import_claim: "Import file's unverified location claim",
    location_entered: "Location entered",
    location_line_added: "Location line added to the draft",
    location_line_removed: "Location line taken out of the draft",
    not_interested: "Marked not interested",
    applied_directly: "Marked applied directly",
    interested_again: "Moved back into outreach",
    call_prep_queued: "Call prep started",
    call_prep_generated: "Call prep written",
    call_prep_replaced: "Call prep replaced",
    tech_brief_queued: "Company research started",
    tech_brief_written: "Company research written",
    tech_brief_failed: "Company research failed",
    research_cleared: "Company research cleared",
    thank_you_scheduled: "Thank-you scheduled",
    thank_you_reviewed: "Thank-you reviewed",
    thank_you_sent: "Thank-you sent",
    thank_you_cancelled: "Thank-you not sent",
    thank_you_held: "Thank-you held",
    thank_you_failed: "Thank-you stopped",
    thank_you_draft_created: "Thank-you put in Gmail Drafts",
  };
  const DRAFT_PROVIDER_LABELS = {
    openai: "OpenAI",
    anthropic: "Anthropic",
    "claude-code": "Claude Code",
    "codex-cli": "Codex",
  };
  // Beyond this, some browsers and Gmail truncate a prefilled compose URL.
  const COMPOSE_URL_LIMIT = 1800;

  function outreachDraftNeedsReview(item, kind) {
    if (kind === "initial") {
      return item.draft_status === "generated" && !item.sent_at && ["not_started", "drafted"].includes(item.status);
    }
    return item.follow_up_status === "generated" && item.status === "sent";
  }

  function composeHref(compose, to, subject, body, cc = "") {
    const encode = (value) => encodeURIComponent(value || "");
    if (compose?.provider === "gmail" && compose.account) {
      const copied = cc ? `&cc=${encode(cc)}` : "";
      const base = `https://mail.google.com/mail/?authuser=${encode(compose.account)}&view=cm&fs=1&to=${encode(to)}${copied}&su=${encode(subject)}`;
      return body ? `${base}&body=${encode(body)}` : base;
    }
    const params = [];
    if (cc) params.push(`cc=${encode(cc)}`);
    if (subject) params.push(`subject=${encode(subject)}`);
    if (body) params.push(`body=${encode(body)}`);
    return `mailto:${encode(to).replace(/%40/g, "@")}${params.length ? `?${params.join("&")}` : ""}`;
  }

  // Every hand-off (Gmail draft, compose link, copy) sends the approved draft
  // stored on the server, never the text box. If the box holds unsaved edits,
  // the student would get different words than they see, so the hand-off stops.
  function unsavedDraftEdits(control, kind) {
    const card = control.closest("[data-outreach-id]");
    const names = kind === "follow_up" ? ["follow_up_subject", "follow_up_body"] : ["email_subject", "email_body"];
    return names.some((name) => {
      const field = card?.querySelector(`[name="${name}"]`);
      return Boolean(field) && field.value !== field.dataset.initial;
    });
  }

  function refuseUnsavedHandOff(control, kind) {
    if (!unsavedDraftEdits(control, kind)) return false;
    const noun = kind === "follow_up" ? "follow-up" : "draft";
    showError(`The ${noun} text box has unsaved edits, and this would use the approved ${noun} instead. Save your changes, approve them, then try again.`);
    return true;
  }

  // An approved draft opens in the student's own email account. The app never
  // sends; a body too long for a compose URL is copied for pasting instead.
  function composeLink(compose, item, kind) {
    const subject = kind === "follow_up" ? item.follow_up_subject : item.email_subject;
    const body = kind === "follow_up" ? item.follow_up_body : item.email_body;
    const where = compose?.provider === "gmail" ? `Gmail (${compose.account})` : "email app";
    const link = element("a", "primary-button outreach-compose", kind === "follow_up" ? `Open follow-up in ${where} ↗` : `Open in ${where} ↗`);
    let href = composeHref(compose, item.contact_email, subject, body, item.contact_cc);
    const tooLong = href.length > COMPOSE_URL_LIMIT;
    if (tooLong) href = composeHref(compose, item.contact_email, subject, "", item.contact_cc);
    link.href = href;
    link.addEventListener("click", (event) => {
      if (!refuseUnsavedHandOff(link, kind)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    });
    if (compose?.provider === "gmail") {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
    if (tooLong) {
      link.addEventListener("click", async () => {
        try {
          await copyText(body);
          announce("The draft is too long for a compose link, so its body was copied. Paste it into the message.");
        } catch (error) {
          showError(error.message);
        }
      });
    }
    return link;
  }

  // After a 428 the server has named what the student must look for in Gmail.
  // The next confirmed click sends that check, once: whatever that attempt's
  // outcome, a later one has to be vouched for again.
  function sentFolderCheck() {
    let check = "";
    return {
      get pending() { return check; },
      // Adds the remembered check to a request body, and forgets it.
      attach(payload) {
        if (check) payload.sent_folder_check = check;
        check = "";
      },
      note(error) {
        if (error.status === 428 && typeof error.detail?.check === "string") check = error.detail.check;
      },
    };
  }

  function gmailSendButton(gmail, item, kind, { now = false } = {}) {
    const attachment = gmail.attachment ? ` with ${gmail.attachment}` : "";
    const label = now ? "Send now" : kind === "follow_up" ? `Send follow-up${attachment}` : `Send${attachment}`;
    const recipients = item.contact_cc ? `${item.contact_email} (Cc ${item.contact_cc})` : item.contact_email;
    const button = element("button", `${now ? "secondary-button" : "primary-button"} outreach-compose outreach-send`, label);
    button.type = "button";
    if (gmail.attachment_problem) {
      button.disabled = true;
      button.title = gmail.attachment_problem;
    }
    const sentCheck = sentFolderCheck();
    const reset = armConfirm(button, {
      idleLabel: () => (sentCheck.pending ? `Checked Gmail — send again${attachment}` : label),
      armedLabel: () => `Send to ${recipients}?`,
      prompt: () => `Press again to send the ${kind === "follow_up" ? "follow-up" : "email"} to ${recipients} from ${gmail.account || "Gmail"}.`,
      beforeClick: () => refuseUnsavedHandOff(button, kind),
      onConfirm: async () => {
        button.disabled = true;
        button.textContent = "Sending…";
        const payload = { kind, fingerprint: kind === "follow_up" ? item.follow_up_fingerprint : item.draft_fingerprint };
        sentCheck.attach(payload);
        try {
          const sent = await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/gmail-send`, {
            method: "POST",
            body: JSON.stringify(payload),
          });
          state.outreachOpen = item.id;
          watchForBounces();
          if (sent.marked === false) {
            announce(`Sent to ${sent.to}, but ${item.company} could not be marked ${kind === "follow_up" ? "followed up" : "sent"}. Press "I sent it" to catch it up.`);
          } else {
            announce(kind === "follow_up"
              ? `Sent the follow-up to ${sent.to}. ${item.company} is marked followed up.`
              : `Sent to ${sent.to} from ${sent.account || "Gmail"}. ${item.company} is marked sent, with a follow-up set for ${formatCalendarDate(sent.follow_up_at)}.`);
          }
          await loadOutreach();
        } catch (error) {
          // Gmail may already have this email: the message says where to look,
          // and the next confirmed click vouches for having looked.
          sentCheck.note(error);
          reset();
          button.disabled = false;
          // A changed or already-sent draft means the card is stale; reloading
          // clears errors, so the reason is shown after it.
          if (error.status === 409 || error.status === 422) {
            await reloadOutreachAt(item.id);
          }
          showError(error.message);
        }
      },
    });
    return button;
  }

  // A company that publishes no email may still have a contact form on its
  // site. The approved first email goes in through it, as the student, once.
  // Like Send, the first click only asks and a second click sends. Finish in
  // browser opens a window on this computer with the form filled in, for a
  // CAPTCHA that asks a person; the app sends the form once it is solved.
  const FORM_STATE_NOTES = {
    unconfirmed: "The form was sent, but their page did not say it arrived.",
    needs_you: "Nothing was sent.",
    failed: "Nothing was sent.",
  };

  function formHost(item) {
    try {
      return new URL(item.contact_form.page_url).hostname.replace(/^www\./, "");
    } catch (_error) {
      return item.company;
    }
  }

  function outreachReachable(item) {
    return item.contact_email ? !item.contact_bounced : Boolean(item.contact_form);
  }

  function formSendControls(item) {
    const form = item.contact_form;
    const controls = element("div", "outreach-send-controls");
    if (form.note && FORM_STATE_NOTES[form.state]) {
      controls.appendChild(element("p", "outreach-note is-wide", `${FORM_STATE_NOTES[form.state]} ${form.note}`));
    }
    const retry = form.state === "unconfirmed";
    controls.appendChild(formSendButton(item, { retry, inBrowser: false }));
    if (form.state === "needs_you" || form.captcha) controls.appendChild(formSendButton(item, { retry, inBrowser: true }));
    return controls;
  }

  function formOutcomeMessage(item, result) {
    if (result.outcome === "submitted") {
      if (result.marked === false) return `Sent through ${item.company}'s contact form, but it could not be marked sent. Press "It arrived" to catch it up.`;
      const said = result.confirmation ? ` Their page said: "${result.confirmation}"` : "";
      // Finish in browser: a box of the app's the student changed before pressing send is named.
      const changed = result.note ? ` ${result.note}.` : "";
      return `Sent through ${item.company}'s contact form.${said}${changed} ${item.company} is marked sent; replies are read from Gmail.`;
    }
    if (result.outcome === "unconfirmed") {
      return `The form was sent, but ${item.company}'s page did not say it arrived. Look for a confirmation email from them; if it came, press "It arrived".`;
    }
    return `Nothing was sent to ${item.company}. ${result.note}`;
  }

  function formSendButton(item, { retry, inBrowser }) {
    const label = inBrowser ? "Finish in browser" : retry ? "Checked — send the form again" : "Send through contact form";
    const button = element("button", inBrowser ? "secondary-button outreach-compose" : "primary-button outreach-compose outreach-send", label);
    button.type = "button";
    // Finish in browser: the student finishes the form in the window and presses its send button; the app never does.
    const yourTurn = "Fill in the boxes outlined in orange, solve any CAPTCHA, then press the form's own send button. Nothing is sent until you press it.";
    if (inBrowser) button.title = `Opens a browser window on this computer with the form filled in as far as the app can. ${yourTurn}`;
    const reset = armConfirm(button, {
      idleLabel: () => label,
      armedLabel: () => inBrowser ? `Open ${formHost(item)}'s form?` : `Send through ${formHost(item)}'s form?`,
      prompt: () => inBrowser
        ? `Press again to open ${item.company}'s contact form from ${item.contact_form.page_url} in a browser window, filled in with the approved email. You press its send button there.`
        : `Press again to send the approved email through ${item.company}'s contact form as you, from ${item.contact_form.page_url}.`,
      beforeClick: () => refuseUnsavedHandOff(button, "initial"),
      onConfirm: async () => {
        button.disabled = true;
        button.textContent = inBrowser ? "Waiting for you in the browser…" : "Sending…";
        // 10 minutes is PERSON_WAIT_SECONDS in outreach/forms.py.
        if (inBrowser) announce(`A browser window is opening with the form filled in as far as the app can. ${yourTurn} The window waits 10 minutes.`);
        try {
          const result = await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/form-submit`, {
            method: "POST",
            body: JSON.stringify({ fingerprint: item.draft_fingerprint, retry_unconfirmed: retry, in_browser: inBrowser }),
          });
          state.outreachOpen = item.id;
          if (result.outcome === "submitted" || result.outcome === "unconfirmed") watchForBounces();
          announce(formOutcomeMessage(item, result));
          await loadOutreach();
        } catch (error) {
          reset();
          button.disabled = false;
          if ([409, 422, 428].includes(error.status)) {
            await reloadOutreachAt(item.id);
          }
          showError(error.message);
        }
      },
    });
    return button;
  }

  // Where the contact form is, for a company with no email. The URL field has
  // no name attribute, so the pane's Save never sends it.
  function outreachContactFormSection(item) {
    const form = item.contact_form;
    const section = element("section", "tracker-subsection outreach-contact-form");
    section.appendChild(element("h4", "", "Contact form"));
    if (form) {
      const where = element("p", "outreach-note");
      const link = externalLink(form.page_url, form.page_url);
      where.append(document.createTextNode("On their site at "), link, document.createTextNode("."));
      section.appendChild(where);
      if (form.captcha) {
        section.appendChild(element("p", "outreach-note",
          `It has a ${form.captcha === "recaptcha" ? "reCAPTCHA" : form.captcha === "hcaptcha" ? "hCaptcha" : "Cloudflare Turnstile"}. A checkbox is ticked for you; a picture challenge is yours to solve under Finish in browser.`));
      }
      if (form.state === "submitted") section.appendChild(element("p", "outreach-note", `Your first email went through this form${form.attempted_at ? ` on ${formatCalendarDate(form.attempted_at.slice(0, 10))}` : ""}.`));
      else if (form.note && FORM_STATE_NOTES[form.state]) section.appendChild(element("p", "outreach-note outreach-guess", `${FORM_STATE_NOTES[form.state]} ${form.note}`));
    }
    if (item.contact_email) {
      section.appendChild(element("p", "outreach-note", "This company has an email contact, so the draft goes by email, not through a form."));
      return section;
    }
    if (form && ["submitted", "unconfirmed"].includes(form.state)) return section;
    section.appendChild(element("p", "outreach-note", form
      ? "With no email, the approved draft goes through this form: send it from the Draft tab, or turn on sending through contact forms in Settings."
      : "No email anywhere? Paste the page with their contact form, and the approved draft can go through it."));
    const label = element("label", "profile-field");
    label.appendChild(element("span", "", form ? "Use a different page" : "Contact form page"));
    const input = document.createElement("input");
    input.type = "url";
    input.placeholder = "https://company.com/contact";
    input.autocomplete = "off";
    label.appendChild(input);
    const save = element("button", "secondary-button", "Use this page");
    save.type = "button";
    const status = element("p", "form-status");
    status.setAttribute("aria-live", "polite");
    section.append(label, save, status);
    const submit = async () => {
      if (!input.value.trim() || !input.checkValidity()) {
        status.textContent = "Enter the page's web address first.";
        input.focus();
        return;
      }
      save.disabled = true;
      try {
        await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/contact-form`, {
          method: "PUT",
          body: JSON.stringify({ page_url: input.value.trim() }),
        });
        state.outreachOpen = item.id;
        announce(`${item.company}'s contact form is set. Approve the draft to send it through the form.`);
        await loadOutreach();
        refocusOutreach(item.id, ".outreach-contact-form input");
      } catch (error) {
        status.textContent = error.message;
        save.disabled = false;
      }
    };
    save.addEventListener("click", submit);
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      submit();
    });
    return section;
  }

  // A bounce usually lands in Gmail within seconds of a send; a reply can come
  // any time. The app also checks in the background every few minutes; this
  // asks for a look on each load of the list and a few times right after a
  // send. The server spaces its own looks, so asking often costs nothing.
  const BOUNCE_LOOKS_MS = [15000, 45000, 120000, 300000];
  let bounceTimers = [];

  async function checkForBounces() {
    if (!state.userId) return;
    const epoch = state.sessionEpoch;
    try {
      const result = await api("/api/v1/outreach/inbox-check", { method: "POST" });
      // Signed out while Gmail was being read: the news belongs to the session that asked.
      if (state.sessionEpoch !== epoch) return;
      const news = [];
      if (result.bounced?.length) {
        const names = result.bounced.map((entry) => `${entry.company} (${entry.addresses.join(", ")})`).join("; ");
        news.push(`Bounced: ${names}. Moved back to Drafted; pick another contact and send again.`);
      }
      if (result.sent_in_gmail?.length) {
        news.push(`Sent from Gmail: ${result.sent_in_gmail.map((entry) => entry.company).join(", ")}. Marked sent; watching for bounces and replies.`);
      }
      if (result.scheduled_in_gmail?.length) {
        news.push(`Scheduled in Gmail: ${result.scheduled_in_gmail.map((entry) => entry.company).join(", ")}. It is marked sent when Gmail sends it.`);
      }
      if (result.replies?.length) {
        const names = result.replies.map((entry) => `${entry.company} (${entry.from})`).join("; ");
        news.push(`New ${result.replies.length === 1 ? "reply" : "replies"} from ${names}, logged from Gmail.`);
      }
      if (result.possible?.length) {
        // One that more than one company could have sent names them all, never the app's guess.
        const names = result.possible.map((entry) => `${entry.companies?.length > 1 ? entry.companies.join(" or ") : entry.company} (${entry.from})`).join("; ");
        news.push(`Maybe a reply from ${names}. Say whether it is on the company's card; follow-ups wait until you do.`);
      }
      if (!news.length) return;
      if (state.view === "outreach") await loadOutreach();
      // Signed out while the list reloaded: the news is the old session's.
      if (state.sessionEpoch !== epoch) return;
      announce(news.join(" "));
    } catch (_error) {
      // A failed look is retried on the next load; it never blocks the page.
    }
  }

  function watchForBounces() {
    bounceTimers.forEach(clearTimeout);
    bounceTimers = BOUNCE_LOOKS_MS.map((delay) => setTimeout(checkForBounces, delay));
  }

  // The approved draft can also be written into Gmail Drafts with the
  // configured attachment (a compose URL cannot attach a file), for editing
  // there before sending.
  function gmailDraftButton(gmail, item, kind) {
    const attachment = gmail.attachment ? ` with ${gmail.attachment}` : "";
    const label = kind === "follow_up" ? `Open follow-up in Gmail${attachment} ↗` : `Open in Gmail${attachment} ↗`;
    const button = element("button", "secondary-button outreach-compose", label);
    button.type = "button";
    if (gmail.attachment_problem) {
      button.disabled = true;
      button.title = gmail.attachment_problem;
    }
    button.addEventListener("click", async () => {
      if (refuseUnsavedHandOff(button, kind)) return;
      button.disabled = true;
      // Opened during the click so a popup blocker allows it; pointed at the
      // draft once Gmail has created it.
      const tab = window.open("about:blank", "_blank");
      try {
        const draft = await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/gmail-draft`, {
          method: "POST",
          body: JSON.stringify({ kind }),
        });
        if (tab) {
          tab.opener = null;
          tab.location.href = draft.url;
        } else {
          window.open(draft.url, "_blank", "noopener");
        }
        announce(draft.reused
          ? `Reopened the Gmail draft for ${item.company}.`
          : `Created the Gmail draft for ${item.company}${attachment}. Send it in Gmail, or use the arrow next to Send, then Schedule send, to have Google send it later even with this computer off. The app marks it sent when it goes.`);
      } catch (error) {
        if (tab) tab.close();
        showError(error.message);
      } finally {
        button.disabled = false;
      }
    });
    return button;
  }

  // With scheduled sending on, the confirmed click queues the approved email
  // for the recipient's next weekday morning instead of sending it now.
  function gmailScheduleButton(item, kind) {
    const recipients = item.contact_cc ? `${item.contact_email} (Cc ${item.contact_cc})` : item.contact_email;
    const label = kind === "follow_up" ? "Schedule follow-up for their morning" : "Schedule for their morning";
    const button = element("button", "primary-button outreach-compose outreach-schedule", label);
    button.type = "button";
    const reset = armConfirm(button, {
      idleLabel: () => label,
      armedLabel: () => `Schedule to ${recipients}?`,
      prompt: () => `Press again to schedule the ${kind === "follow_up" ? "follow-up" : "email"} to ${recipients} for their next weekday morning.`,
      beforeClick: () => refuseUnsavedHandOff(button, kind),
      onConfirm: async () => {
        button.disabled = true;
        try {
          const scheduled = await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/schedule`, {
            method: "POST",
            body: JSON.stringify({ kind, fingerprint: kind === "follow_up" ? item.follow_up_fingerprint : item.draft_fingerprint }),
          });
          state.outreachOpen = item.id;
          state.outreachKeep.add(item.id);
          await loadOutreach();
          announce(automationPaused()
            ? `Scheduled for ${scheduled.label}. Automation is paused, so it goes out after you resume.`
            : `Scheduled: goes out ${scheduled.label}.`);
        } catch (error) {
          reset();
          button.disabled = false;
          showError(error.message);
        }
      },
    });
    return button;
  }

  // A draft waiting for review that could be scheduled once approved: true when
  // one press can confirm the research, approve the draft and schedule it.
  function canApproveAndSchedule(context, item) {
    const schedule = item.scheduled?.initial;
    return outreachDraftNeedsReview(item, "initial")
      && Boolean(item.email_subject && item.email_body && item.contact_email)
      && !item.contact_bounced && !item.cc_bounced
      && Boolean(context.gmail?.connected && context.gmail.bounce_check && context.automation?.scheduled_sending)
      && !["scheduled", "sending", "transmitting"].includes(schedule?.state);
  }

  // The three steps the separate buttons take, in order, after one confirmed
  // press: Confirm research (when unverified), Approve draft, then Schedule for
  // their morning. Each step is the same request its own button makes, so
  // every check still applies, and a step that stops leaves the earlier ones done.
  function approveAndScheduleButton(item) {
    const recipients = item.contact_cc ? `${item.contact_email} (Cc ${item.contact_cc})` : item.contact_email;
    const unverified = item.research_confidence === "unverified";
    const label = unverified ? "Confirm research, approve and schedule" : "Approve and schedule for their morning";
    const steps = unverified ? "confirm the research, approve the draft, and schedule it" : "approve the draft and schedule it";
    const button = element("button", "secondary-button outreach-approve-schedule", label);
    button.type = "button";
    const id = encodeURIComponent(item.id);
    const reset = armConfirm(button, {
      idleLabel: () => label,
      armedLabel: () => `Schedule to ${recipients}?`,
      prompt: () => `Press again to ${steps} to ${recipients} for their next weekday morning.`,
      beforeClick: () => {
        if (!unsavedDraftEdits(button, "initial")) return false;
        showError("The draft text box has unsaved edits. Save them, then try again, so what is approved is what you see.");
        return true;
      },
      onConfirm: async () => {
        button.disabled = true;
        const done = [];
        try {
          if (unverified) {
            await api(`/api/v1/outreach/${id}/confirm-research`, { method: "POST" });
            done.push("confirmed the research");
          }
          const approve = (acknowledge) => api(`/api/v1/outreach/${id}/approve`, {
            method: "POST",
            body: JSON.stringify({ kind: "initial", fingerprint: item.draft_fingerprint, acknowledge_warnings: acknowledge }),
          });
          let approved;
          try {
            approved = await approve(false);
          } catch (error) {
            if (error.status !== 422 || !String(error.message).startsWith("Review these warnings")) throw error;
            if (!window.confirm(`${error.message}\n\nApprove anyway and schedule it?`)) {
              await reloadOutreachAt(item.id);
              announce(done.length ? `Confirmed the research for ${item.company}. The draft is not approved.` : `The ${item.company} draft is not approved.`);
              return;
            }
            approved = await approve(true);
          }
          done.push("approved the draft");
          const scheduled = await api(`/api/v1/outreach/${id}/schedule`, {
            method: "POST",
            body: JSON.stringify({ kind: "initial", fingerprint: approved.draft_fingerprint }),
          });
          state.outreachKeep.add(item.id);
          await reloadOutreachAt(item.id, ".outreach-next .tracker-exports button");
          announce(`${unverified ? "Confirmed the research, approved" : "Approved"} the ${item.company} draft. ${automationPaused()
            ? `Scheduled for ${scheduled.label}. Automation is paused, so it goes out after you resume.`
            : `Scheduled: goes out ${scheduled.label}.`}`);
        } catch (error) {
          reset();
          button.disabled = false;
          if (!done.length) {
            showError(error.message);
            return;
          }
          // Reload so the card shows what did happen; the message says where it stopped.
          await reloadOutreachAt(item.id).catch(() => {});
          const what = done.join(" and ");
          showError(`${what.charAt(0).toUpperCase()}${what.slice(1)} for ${item.company}, but it is not scheduled: ${error.message}`);
        }
      },
    });
    return button;
  }

  // A follow-up waiting for review that one press can approve and hand to Gmail.
  // Not while an email from them may be a reply: follow-ups wait for that.
  function canApproveFollowUpAndSend(context, item) {
    return outreachDraftNeedsReview(item, "follow_up")
      && Boolean(context.gmail?.connected && item.contact_email && item.follow_up_subject && item.follow_up_body)
      && !item.contact_bounced && !item.cc_bounced && !item.possible_reply_count
      && !["scheduled", "sending", "transmitting"].includes(item.scheduled?.follow_up?.state);
  }

  // The one-press buttons beside Approve follow-up. With scheduled sending on,
  // as with Send, the usual press queues it for their next weekday morning and
  // Approve and send now stays beside it; otherwise one button sends it now.
  function approveFollowUpButtons(context, item, subjectControl, bodyControl) {
    if (!canApproveFollowUpAndSend(context, item)) return [];
    // The check just before a scheduled send reads Gmail, so it needs read access.
    if (context.automation?.scheduled_sending && context.gmail.bounce_check) {
      return [
        approveFollowUpButton(context.gmail, item, subjectControl, bodyControl, "schedule"),
        approveFollowUpButton(context.gmail, item, subjectControl, bodyControl, "now"),
      ];
    }
    return [approveFollowUpButton(context.gmail, item, subjectControl, bodyControl, "send")];
  }

  // Approve follow-up, then Send follow-up (or Schedule follow-up for their
  // morning), after one confirmed press. Each step is the same request its own
  // button makes, so every check still applies. Edits still in the box are saved
  // first, as Approve does, so what goes out is the words on screen. A send or a
  // schedule that stops leaves the follow-up approved, and the bar above can try
  // again.
  function approveFollowUpButton(gmail, item, subjectControl, bodyControl, how) {
    const recipients = item.contact_cc ? `${item.contact_email} (Cc ${item.contact_cc})` : item.contact_email;
    const attachment = gmail.attachment ? ` with ${gmail.attachment}` : "";
    const scheduling = how === "schedule";
    const label = scheduling ? "Approve and schedule for their morning" : how === "now" ? "Approve and send now" : `Approve and send${attachment}`;
    const button = element("button", `${how === "now" ? "secondary-button" : "primary-button"} outreach-approve-send`, label);
    button.type = "button";
    if (scheduling) button.dataset.followUpApproveSchedule = "";
    else button.dataset.followUpApproveSend = "";
    if (!scheduling && gmail.attachment_problem) {
      button.disabled = true;
      button.title = gmail.attachment_problem;
    }
    const verb = scheduling ? "scheduled" : "sent";
    const id = encodeURIComponent(item.id);
    const unsaved = () => subjectControl.value !== subjectControl.dataset.initial || bodyControl.value !== bodyControl.dataset.initial;
    const reset = armConfirm(button, {
      idleLabel: () => label,
      armedLabel: () => (scheduling ? `Schedule to ${recipients}?` : `Approve and send to ${recipients}?`),
      prompt: () => (scheduling
        ? `Press again to approve the follow-up and schedule it to ${recipients} for their next weekday morning.`
        : `Press again to approve the follow-up and send it to ${recipients} from ${gmail.account || "Gmail"}.`),
      onConfirm: async () => {
        button.disabled = true;
        button.textContent = scheduling ? "Scheduling…" : "Sending…";
        let approved = false;
        try {
          let print = item.follow_up_fingerprint;
          const editing = unsaved();
          if (editing) {
            const saved = await api(`/api/v1/outreach/${id}`, {
              method: "PATCH",
              body: JSON.stringify({ follow_up_subject: subjectControl.value, follow_up_body: bodyControl.value }),
            });
            print = saved.follow_up_fingerprint;
          }
          const approve = (acknowledge) => api(`/api/v1/outreach/${id}/approve`, {
            method: "POST",
            body: JSON.stringify({ kind: "follow_up", fingerprint: print, acknowledge_warnings: acknowledge }),
          });
          let result;
          try {
            result = await approve(false);
          } catch (error) {
            if (error.status !== 422 || !String(error.message).startsWith("Review these warnings")) throw error;
            if (!window.confirm(`${error.message}\n\nApprove anyway and ${scheduling ? "schedule" : "send"} it?`)) {
              reset();
              button.disabled = false;
              announce(editing ? `Your edits are saved. The follow-up is not approved or ${verb}.` : `The follow-up is not approved or ${verb}.`);
              return;
            }
            result = await approve(true);
          }
          approved = true;
          const payload = JSON.stringify({ kind: "follow_up", fingerprint: result.follow_up_fingerprint });
          state.outreachOpen = item.id;
          if (scheduling) {
            const scheduled = await api(`/api/v1/outreach/${id}/schedule`, { method: "POST", body: payload });
            state.outreachKeep.add(item.id);
            await loadOutreach();
            announce(`Approved the ${item.company} follow-up. ${automationPaused()
              ? `Scheduled for ${scheduled.label}. Automation is paused, so it goes out after you resume.`
              : `Scheduled: goes out ${scheduled.label}.`}`);
            return;
          }
          const sent = await api(`/api/v1/outreach/${id}/gmail-send`, { method: "POST", body: payload });
          watchForBounces();
          announce(sent.marked === false
            ? `Approved and sent the follow-up to ${sent.to}, but ${item.company} could not be marked followed up. Press "I sent the follow-up" to catch it up.`
            : `Approved and sent the follow-up to ${sent.to}. ${item.company} is marked followed up.`);
          await loadOutreach();
        } catch (error) {
          reset();
          button.disabled = false;
          if (approved) {
            // Reload so the card shows the approved follow-up; the message says where it stopped.
            await reloadOutreachAt(item.id).catch(() => {});
            showError(`Approved the ${item.company} follow-up, but it is not ${verb}: ${error.message}`);
            return;
          }
          if (error.status === 409) {
            state.outreachFlash = { id: item.id, kind: "follow_up", message: error.message };
            await reloadOutreachAt(item.id, '[data-draft-kind="follow_up"] [data-draft-approve]');
            return;
          }
          showError(error.message);
        }
      },
    });
    return button;
  }

  // Why a company in a batch of follow-ups is left out, or "" when it can go.
  // `how` is "queue" or "send". The same conditions as the one-press buttons,
  // except that a follow-up already approved goes as it stands.
  function followUpBatchProblem(item, how) {
    if (!item.follow_up_subject || !item.follow_up_body || !["generated", "approved"].includes(item.follow_up_status)) {
      return "no follow-up is written yet";
    }
    if (item.status !== "sent") return "it is not waiting on a follow-up";
    if (!item.contact_email) return "it has no email address";
    if (item.contact_bounced || item.cc_bounced) return "an address bounced";
    if (item.possible_reply_count) return "an email from them may be a reply";
    const queued = item.scheduled?.follow_up?.state;
    if (["sending", "transmitting"].includes(queued)) return "it is being sent now";
    if (how === "queue" && queued === "scheduled") return "it is already scheduled";
    return "";
  }

  // Approve each follow-up that still needs it, then queue it for the
  // recipient's next weekday morning or send it now, one company at a time,
  // with the same requests the one-press buttons make, so every server check
  // still applies. A batch never accepts warnings on its own: the follow-ups
  // whose approval asks to review warnings wait until the rest have gone, then
  // one question lists every warning, as Approve's own question does, and only
  // a yes approves them. Stops when the session ends. Returns
  // { done, skipped, stopped }.
  async function runFollowUpBatch(items, how, onProgress) {
    const epoch = state.sessionEpoch;
    const ended = () => state.sessionEpoch !== epoch;
    const done = [];
    const skipped = [];
    const held = [];
    const WARNINGS = "Review these warnings, then approve again to accept them: ";
    const deliver = async (item, acknowledge) => {
      const id = encodeURIComponent(item.id);
      let approved = false;
      try {
        let print = item.follow_up_fingerprint;
        if (item.follow_up_status !== "approved") {
          const result = await api(`/api/v1/outreach/${id}/approve`, {
            method: "POST",
            body: JSON.stringify({ kind: "follow_up", fingerprint: print, acknowledge_warnings: acknowledge }),
          });
          approved = true;
          print = result.follow_up_fingerprint;
          if (ended()) return;
        }
        const payload = JSON.stringify({ kind: "follow_up", fingerprint: print });
        if (how === "queue") {
          const scheduled = await api(`/api/v1/outreach/${id}/schedule`, { method: "POST", body: payload });
          done.push({ item, label: scheduled.label });
        } else {
          const sent = await api(`/api/v1/outreach/${id}/gmail-send`, { method: "POST", body: payload });
          done.push({ item, marked: sent.marked !== false });
        }
      } catch (error) {
        const message = String(error.message);
        if (!acknowledge && error.status === 422 && message.startsWith(WARNINGS)) held.push({ item, warnings: message.slice(WARNINGS.length) });
        else skipped.push({ item, reason: approved ? `approved, but not ${how === "queue" ? "scheduled" : "sent"}: ${message}` : message });
      }
    };
    for (const [index, item] of items.entries()) {
      if (ended()) break;
      onProgress(index, items.length, item);
      const problem = followUpBatchProblem(item, how);
      if (problem) skipped.push({ item, reason: problem });
      else await deliver(item, false);
    }
    if (held.length && !ended()) {
      const one = held.length === 1;
      const accept = window.confirm(
        `${one ? "One follow-up has" : `${held.length} follow-ups have`} warnings to review:\n\n`
        + `${held.map(({ item, warnings }) => `• ${item.company}: ${warnings}`).join("\n")}\n\n`
        + `Approve ${one ? "it" : "them"} anyway and ${how === "queue" ? "queue" : "send"} ${one ? "it" : "them"}?`
      );
      for (const [index, { item }] of held.entries()) {
        if (ended()) break;
        if (!accept) {
          skipped.push({ item, reason: "its follow-up has warnings to review, so it is not approved" });
          continue;
        }
        onProgress(index, held.length, item);
        await deliver(item, true);
      }
    }
    if (how === "send" && done.length) watchForBounces();
    return { done, skipped, stopped: ended() };
  }

  function cancelScheduleButton(item, kind, text = "Cancel") {
    const button = element("button", "secondary-button", text);
    button.type = "button";
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const result = await api(`/api/v1/outreach/${encodeURIComponent(item.id)}/schedule?kind=${kind}`, { method: "DELETE" });
        await reloadOutreachAt(item.id);
        if (text !== "Cancel") announce("Dismissed.");
        else announce(result.cancelled
          ? `Cancelled the scheduled send to ${item.contact_email}.`
          : `Too late to cancel: the email to ${item.contact_email} had already gone out. Check the history.`);
      } catch (error) {
        showError(error.message);
        button.disabled = false;
      }
    });
    return button;
  }

  // Words that depend on the pause. The node keeps both versions, so a pause or
  // resume rewrites it in place (repaintPauseWords) without reloading the
  // cards, which would drop focus and unsaved edits.
  function pauseWords(node, running, paused) {
    Object.assign(node.dataset, { whileRunning: running, whilePaused: paused });
    node.textContent = automationPaused() ? paused : running;
    return node;
  }

  function repaintPauseWords() {
    const paused = automationPaused();
    document.querySelectorAll("[data-while-paused]").forEach((node) => {
      node.textContent = paused ? node.dataset.whilePaused : node.dataset.whileRunning;
    });
  }

  // Every promise the Outreach page makes about what sends is built from these, so it can never say "nothing sends"
  // while a switch that sends without a click is on. Each phrase finishes "Automatic sending is on: ...".
  const AUTOMATIC_SENDS = {
    bounce_auto_resend: "resending an approved email after a bounce",
    decline_thank_you: "a thank-you when someone declines",
    form_submission: "sending through contact forms",
  };
  // The switches that email through Gmail; Send through contact forms needs no Gmail.
  const AUTOMATIC_SENDS_NEEDING_GMAIL = ["bounce_auto_resend", "decline_thank_you"];

  function joinPhrases(phrases) {
    return phrases.length < 3
      ? phrases.join(" and ")
      : `${phrases.slice(0, -1).join(", ")}, and ${phrases[phrases.length - 1]}`;
  }

  // The sentence for the switches in `keys` that are on, in both versions (a pause holds all of them): null
  // when none is, so the caller keeps whatever reassurance is then true.
  function automaticSendWords(automation, keys, gmail) {
    const on = keys.filter((key) => automation?.[key]);
    if (!on.length) return null;
    const list = joinPhrases(on.map((key) => AUTOMATIC_SENDS[key]));
    const needsGmail = !gmail?.connected && on.some((key) => AUTOMATIC_SENDS_NEEDING_GMAIL.includes(key));
    return {
      running: `Automatic sending is on: ${list}. These go out without a click${needsGmail ? " (the emails need Gmail connected first)" : ""}.`,
      paused: `Automatic sending is on: ${list}, but automation is paused, so none of it goes out until you resume.`,
    };
  }

  // The Outreach page's status line. It holds whether or not automation is paused, so it never claims a pause.
  // What the student's own click does is part of every version: an approved email goes from their Gmail (or opens in
  // their own email), and an approved contact form goes out through Send through contact form, which needs no Gmail
  // and no switch. The form clause is left out when sending through contact forms is on, which already says it.
  function outreachSendStatus(gmail, automation) {
    const automatic = automaticSendWords(automation, Object.keys(AUTOMATIC_SENDS), gmail);
    const email = gmail?.connected
      ? "approved emails send from your Gmail only when you press Send and confirm the recipient"
      : "approved emails open in your own email, where you press Send";
    const byClick = "only when you press Send through contact form and confirm";
    const form = automation?.form_submission ? "" : gmail?.connected ? `, and a contact form ${byClick}` : `; a contact form goes out ${byClick}`;
    if (!automatic) return `Nothing sends on its own. ${email.charAt(0).toUpperCase()}${email.slice(1)}${form}.`;
    return `${automatic.running.replace(/\.$/, "")}, while automation is running. Anything else goes out only by your own click: ${email}${form}.`;
  }

  // When a scheduled send goes, in words. The worker holds every send while
  // automation is paused, so then it goes after the student resumes, not at
  // its time; the time it had is kept in brackets.
  function scheduleWords(label, { reconnect = false, paused = automationPaused() } = {}) {
    if (paused) {
      return reconnect
        ? `Paused. Goes out after you resume and Gmail is reconnected (was ${label})`
        : `Paused. Goes out after you resume (was ${label})`;
    }
    return reconnect ? `Goes out ${label} once Gmail is reconnected` : `Goes out ${label}`;
  }

  function scheduleText(node, label, { reconnect = false, suffix = "" } = {}) {
    return pauseWords(node, `${scheduleWords(label, { reconnect, paused: false })}${suffix}`, `${scheduleWords(label, { reconnect, paused: true })}${suffix}`);
  }

  function composeControl(context, item, kind) {
    const controls = document.createDocumentFragment();
    const schedule = item.scheduled?.[kind];
    // A scheduled send can always be cancelled, even while Gmail needs reconnecting.
    if (schedule?.state === "transmitting") {
      // Handed to Gmail: too late to cancel, and it will show as sent in a moment.
      controls.append(chip("Sending…", "is-region"));
      return controls;
    }
    if (!context.gmail?.connected) {
      if (schedule?.state === "scheduled" || schedule?.state === "sending") {
        controls.append(scheduleText(chip("", "is-warning"), schedule.label, { reconnect: true }), cancelScheduleButton(item, kind));
        return controls;
      }
      return composeLink(context.compose, item, kind);
    }
    if (schedule?.state === "scheduled" || schedule?.state === "sending") {
      // A Gmail draft made now would stop the scheduled send, so it is not offered.
      controls.append(
        schedule.state === "sending" ? chip("Sending…", "is-region") : scheduleText(chip("", "is-region"), schedule.label),
        cancelScheduleButton(item, kind),
        gmailSendButton(context.gmail, item, kind, { now: true }),
      );
      return controls;
    }
    if (schedule?.state === "failed") {
      controls.append(chip(`Scheduled send stopped: ${schedule.error}`, "is-warning"), cancelScheduleButton(item, kind, "Dismiss"));
    }
    // A paused company that was already written to is never sent the first email again.
    if (kind === "follow_up" || !item.sent_at) {
      // The check just before a scheduled send reads Gmail, so it needs read access.
      if (context.automation?.scheduled_sending && context.gmail.bounce_check) {
        controls.append(gmailScheduleButton(item, kind), gmailSendButton(context.gmail, item, kind, { now: true }));
      } else {
        controls.append(gmailSendButton(context.gmail, item, kind));
      }
    }
    controls.append(gmailDraftButton(context.gmail, item, kind));
    return controls;
  }

  // Re-rendering replaces the card, so return focus to where the action left
  // off instead of dropping keyboard users back at the top of the page.
  function refocusOutreach(id, ...selectors) {
    const card = els.results.querySelector(`[data-outreach-id="${CSS.escape(id)}"]`);
    if (!card) return;
    const target = selectors.map((selector) => card.querySelector(selector)).find((node) => node && !node.disabled && !node.hidden)
      || card.querySelector('[role="tab"][aria-selected="true"]');
    target?.focus();
  }

  // After an action on one company: keep its card open, reload the list from
  // the server, and put focus back on the first of focusSelectors still there.
  async function reloadOutreachAt(id, ...focusSelectors) {
    state.outreachOpen = id;
    await loadOutreach();
    if (focusSelectors.length) refocusOutreach(id, ...focusSelectors);
  }

  // Call prep is written by a background job on the server. While one runs,
  // its company is checked every few seconds and the list reloads when it
  // ends. The job's state is on the server, so a reload or a laptop waking up
  // finds it again; a hidden tab is checked the moment it is shown.
  const CALL_PREP_ACTIVE = ["queued", "running", "retry"];
  const CALL_PREP_WRITING = "Writing call prep in the background. When the company has no recent research, it researches them on the web first, which takes a few minutes. It keeps going if you leave this page, and picks back up if your laptop sleeps or the app restarts.";

  // What this file does as it loads, run once by app.js in load order.
  function installOutreachSend() {
    registerSessionPoller(() => {
      bounceTimers.forEach((timer) => window.clearTimeout(timer));
      bounceTimers = [];
    });
  }

  Object.assign(App, {
    CALL_PREP_ACTIVE, CALL_PREP_WRITING, CANDIDATE_METHOD_LABELS, CANDIDATE_VERIFICATION_LABELS,
    CONTACT_CONFIDENCE_LABELS, DRAFT_PROVIDER_LABELS, DRAFT_STATUS_LABELS, OUTREACH_EVENT_LABELS, OUTREACH_STATUS_LABELS,
    approveAndScheduleButton, approveFollowUpButtons, automaticSendWords, canApproveAndSchedule, checkForBounces, composeControl, formHost, formSendControls, installOutreachSend, outreachChoice,
    outreachContactFormSection, outreachDraftNeedsReview, outreachField, outreachReachable, outreachSendStatus, pauseWords, refocusOutreach,
    refuseUnsavedHandOff, reloadOutreachAt, repaintPauseWords, runFollowUpBatch, scheduleText, scheduleWords, sentFolderCheck,
  });
})();
