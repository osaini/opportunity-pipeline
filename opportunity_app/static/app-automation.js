// Automation on the Profile page: the switches, health list, waiting and recent lists, and the controls that follow
// the automation settings.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { state } = App;

  // From app-ui.js.
  const {
    CLOCK_FORMAT, announce, applicationPicker, atsName, autoSaveSelect, chip, element, formatDate, formatDateTime, humanizeKey,
    optionElement, plural, showError, timeAgo,
  } = App;

  // From app-http.js.
  const { api, isAuthError } = App;

  // From app-status.js.
  const {
    AUTOMATION_BANNER_LEVELS, applyAutomationRead, automationStatus, automationWrite, loadStats, refreshAutomationStatus,
    setAutomationPaused, startAutomationRead,
  } = App;

  // Defined in files that load later; looked up when called.
  const applyAgentSettingsBlock = (...args) => App.applyAgentSettingsBlock(...args);
  const loadApplications = (...args) => App.loadApplications(...args);
  const openCaptureDraft = (...args) => App.openCaptureDraft(...args);

  // Work the app does on its own. Each switch is the student's, stored in the
  // database, and off until they turn it on. Only scheduled sending, the
  // resend after a bounce, and contact forms send anything, and only a draft
  // the student approved (a resend: approved before the bounce, greeting aside).
  const AUTOMATION_SWITCHES = [
    ["auto_drafts", "Write drafts automatically", "Every company you have not contacted gets a draft written, whether a deep search found it or you added it, including those that still need a contact or a location. Without a contact it greets the company's team, and the greeting changes when a contact turns up; without a checked location it says nothing about where you live, and the \"(live in ...)\" line goes in on its own once the location is checked and is where you live. Companies ready to send are drafted first. Each one waits for your approval."],
    ["bounce_recovery", "Find a new contact after a bounce", "When an email bounces, the app searches the company's site again, picks the best address that has not bounced, and updates the greeting. You review the draft and send it again, unless Resend automatically after a bounce is on."],
    ["bounce_auto_resend", "Resend automatically after a bounce", "Works with Find a new contact after a bounce. When the only change to the email you approved is the greeting for the new contact, it goes out again right away, without a click. A greeting written to someone else, or no greeting at all, waits for you. A guessed address goes only when an inbox listed on the company's site is in Cc, so a wrong guess still reaches them. Gmail is checked for a reply first, and it happens once per company; after a second bounce the draft waits for you. Needs Gmail connected."],
    ["scheduled_sending", "Send on their weekday morning", "Your confirmed Send queues the approved email for 9 to 9:40 AM on the recipient's next weekday, in their timezone (from the company's US state, or yours when it names none). Editing the draft cancels it; Send now and Cancel stay on the card. Turning this off does not cancel emails already scheduled; cancel them on their cards, or pause automation to hold them. Needs Gmail connected. Just before it goes, Gmail is checked again for a reply or a bounce; a follow-up never goes to a company that replied."],
    ["form_submission", "Send through contact forms", "For a company that publishes no email but has a contact form on its site, the approved first email goes in through the form as you, once, with replies going to your outreach Gmail. Only what your confirmed profile says is filled in; a form that asks something else, or a CAPTCHA that wants a picture challenge, waits for you on the card (Finish in browser). The app says Sent only when their page confirms it; otherwise it asks you to check. Needs Playwright's Chromium on this computer."],
    ["follow_up_review", "Have a second model check each follow-up", "Before a scheduled follow-up goes out, a second model reads it with the whole thread: your first email, every reply and out-of-office, and your facts. Pick it under \"Who reviews follow-ups\" below; on Automatic it is a model from a different company than the follow-up writer when one is set up. It goes only on a clean pass. An out-of-office with a return date holds it until then; any other problem stops it and shows the reason here. If the reviewer cannot run, the follow-up waits."],
  ];
  // The Automation section on the Profile page shows the same help for these
  // switches, so neither place leaves out what turning one off does not stop.
  const AUTOMATION_SWITCH_HELP = Object.fromEntries(AUTOMATION_SWITCHES.map(([key, , help]) => [key, help]));
  // Longer help for automation features with no switch on the Outreach tab.
  const AUTOMATION_FEATURE_HELP = {
    decline_thank_you: "When a contact writes back with a plain no, and both the keyword rules and Jev read it that way, the app writes a few lines of thanks and sends them in the same thread, with no approval step. Before 5 PM on a weekday in their time zone it goes after a normal delay the same day; otherwise the next weekday morning. Anything about a call, a question, a referral, an offer, or \"maybe later\" stays yours. Just before it goes, Gmail is read again, a new message from them (or one from you) stops it, and a second model reads it; anything unclear holds it for you on the card. The card shows it with Cancel and Edit (which moves it to your Gmail Drafts instead). Turning this off, or turning Jev inbox suggestions off, holds a waiting thank-you at its time for you to send or dismiss. Needs Jev inbox suggestions on, Gmail connected, and a model set up to review it (Outreach → Settings); without a reviewer every thank-you is held for you. There is no shadow period.",
    application_mail: "Reads job-system and assessment emails in Gmail (and mail from company domains you trust below). An email that clearly confirms, rejects, or invites you moves that application forward and adds a task or a deadline; the email is shown on the application. Anything unclear, an offer, or an email from before you turned this on waits under Waiting for you, with the reason. Start it in shadow: for 48 hours it only logs what it would do, and you mark each one right or wrong before it can act. Needs Gmail connected.",
  };

  // onChange, when given, is called with the saved switches after each write, so a page that states what sends
  // (the Outreach page's status line) can follow the switch.
  async function automationFields({ onChange } = {}) {
    const field = element("div", "automation-settings");
    const status = element("p", "profile-help automation-settings-status");
    status.setAttribute("aria-live", "polite");
    let current;
    try {
      current = await api("/api/v1/outreach/automation");
    } catch (error) {
      field.appendChild(element("p", "form-error", `Automation settings could not be loaded: ${error.message}`));
      return field;
    }
    AUTOMATION_SWITCHES.forEach(([key, text, help]) => {
      const row = element("div", "settings-field settings-toggle");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.className = "settings-switch";
      box.setAttribute("role", "switch");
      box.id = `settings-automation-${key}`;
      box.checked = Boolean(current[key]);
      const label = element("label", "", text);
      label.htmlFor = box.id;
      const description = element("p", "profile-help", help);
      description.id = `${box.id}-help`;
      box.setAttribute("aria-describedby", description.id);
      row.append(label, box, description);
      box.addEventListener("change", async () => {
        box.disabled = true;
        status.textContent = "Saving…";
        try {
          // The same switches as the Automation section's, so the app-wide state is read again after.
          current = await automationWrite(() => api("/api/v1/outreach/automation", { method: "PUT", body: JSON.stringify({ [key]: box.checked }) }));
          refreshAutomationStatus();
          box.checked = Boolean(current[key]);
          status.textContent = `${text}: ${box.checked ? "on" : "off"}.`;
          if (onChange) onChange(current);
        } catch (error) {
          box.checked = !box.checked;
          status.textContent = error.message;
        } finally {
          box.disabled = false;
        }
      });
      field.appendChild(row);
    });
    field.appendChild(status);
    return field;
  }

  // Automation on the Profile page: the switches, the master pause, health,
  // notices, and the ledger of what the app did, proposed, or would have done.
  // Every value that came from a record (summaries, evidence, notices, company
  // names) is set as text, never as HTML.
  const AUTOMATION_GROUPS = [
    ["outreach", "Outreach"],
    ["applications", "Applications"],
    ["discovery", "Discovery"],
    ["notifications", "Notifications"],
  ];
  const AUTOMATION_MODE_LABELS = { off: "Off", shadow: "Shadow (log what it would do)", on: "On" };
  const AUTOMATION_MODE_WORDS = { off: "off", shadow: "shadow, logging what it would do", on: "on" };
  // Action types whose change Undo can take back: every type the ledger has
  // today. A sent email or a submitted application will never be one.
  const UNDOABLE_ACTION_TYPES = new Set([
    "application.stage", "opportunity.intent", "application.task", "application.deadline",
    "outreach.status", "outreach.follow_up_draft", "resume.pick",
  ]);

  // Whether Undo can take this action back: the server says so (undoable), else by its type.
  function canUndo(action) {
    return typeof action?.undoable === "boolean" ? action.undoable : UNDOABLE_ACTION_TYPES.has(action?.action_type);
  }
  const AUTOMATION_STATUS_CHIPS = {
    applied: ["Applied", "is-good"],
    undone: ["Undone", ""],
    superseded: ["Left as it was", "is-soon"],
    rejected: ["Rejected", ""],
    failed: ["Failed", "is-warning"],
    expired: ["Expired", ""],
  };
  // Each list is its own request with its own status filter, so a queue is
  // never crowded out of view by newer rows of another status: the badge and
  // the shadow gate count every row, so every row must be reachable here.
  const AUTOMATION_LISTS = {
    waiting: { query: "status=proposed&limit=200", heading: "Waiting for you", empty: "Nothing is waiting for you." },
    shadow: { query: "status=shadow&limit=200", heading: "Would have done", empty: "Nothing has run in shadow yet." },
    recent: { query: `status=${Object.keys(AUTOMATION_STATUS_CHIPS).join(",")}&limit=50`, heading: "Recent activity", empty: "Nothing has happened automatically yet." },
  };
  const AUTOMATION_LIST_KEYS = Object.keys(AUTOMATION_LISTS);
  // What Pause does, in the words of automation/ledger.py's pause.
  const AUTOMATION_PAUSE_HELP = "Stops everything the app does on its own. Replies and bounces are still recorded, and notices still appear.";
  const AUTOMATION_PAUSE_TEXT = {
    true: "Paused. Nothing is sent and no switch acts on its own until you resume. Replies and bounces are still recorded.",
    false: "Running. Each switch below decides what the app does on its own.",
  };
  const GMAIL_STATE_WORDS = {
    not_connected: "Not connected.",
    connected: "Connected.",
    needs_reconnect: "Needs reconnecting. Reply and bounce checks have stopped.",
    disconnected: "Disconnected.",
    throttled: "Gmail asked the app to slow down for a while.",
  };

  function automationFeature(settings, key) {
    return (settings?.features || []).find((feature) => feature.key === key) || null;
  }

  function automationFeatureLabel(key) {
    return automationFeature(state.automation?.settings, key)?.label || humanizeKey(key);
  }

  function savedAutomationMode(key, fallback = "off") {
    return automationFeature(state.automation?.settings, key)?.mode ?? fallback;
  }

  function paintAutomationControl(control, feature) {
    if (control.type === "checkbox") {
      control.checked = feature.mode === "on";
    } else if (control.tagName === "SELECT") {
      const on = control.querySelector('option[value="on"]');
      if (on) on.disabled = !feature.can_turn_on && feature.mode !== "on";
      control.value = feature.mode;
    }
    const reason = control.closest(".automation-feature")?.querySelector(".automation-reason");
    if (reason) {
      const blocked = !feature.can_turn_on && feature.mode !== "on" && Boolean(feature.can_turn_on_reason);
      // A switch left on can lose what it needs later (a threshold taken out of the profile).
      const stuck = feature.mode === "on" && Boolean(feature.requirement);
      // Some reasons end in a full stop of their own; the sentence gets exactly one.
      const sentence = (text) => `${String(text).replace(/[.]+$/, "")}.`;
      reason.textContent = blocked
        ? `On is not available yet: ${sentence(feature.can_turn_on_reason)}`
        : stuck ? `On, but it can't act yet: ${sentence(feature.requirement)}` : "";
      reason.hidden = !(blocked || stuck);
    }
  }

  function paintAutomationPause(paused, button = document.getElementById("automation-pause"), status = document.getElementById("automation-pause-status")) {
    if (!button) return;
    const value = String(Boolean(paused));
    if (button.dataset.paused === value) return;
    button.dataset.paused = value;
    button.textContent = paused ? "Resume automation" : "Pause all automation";
    if (status) status.textContent = AUTOMATION_PAUSE_TEXT[value];
  }

  function syncAutomationControls(settings) {
    (settings?.features || []).forEach((feature) => {
      document.querySelectorAll(`[data-automation-key="${CSS.escape(feature.key)}"]`).forEach((control) => paintAutomationControl(control, feature));
    });
    if (settings) paintAutomationPause(settings.paused);
  }

  async function saveAutomationMode(key, value) {
    const payload = await automationWrite(() => api("/api/v1/automation/settings", { method: "PUT", body: JSON.stringify({ modes: { [key]: value } }) }));
    return automationFeature(payload.settings, key);
  }

  // What the circuit breaker did, in the notice the server wrote when it
  // fired (breaker_notice), so the counts are the ones it actually used.
  function automationBreakerMessage(featureKey, notice) {
    const title = typeof notice?.title === "string" ? notice.title.trim().replace(/[.!?]+$/, "") : "";
    const body = typeof notice?.body === "string" ? notice.body.trim() : "";
    if (title) return `${title}.${body ? ` ${body}` : ""}`;
    return `Turned off ${automationFeatureLabel(featureKey)}: you undid or rejected too many of its recent actions.`;
  }

  // An undo's own words (undo_note), such as whether a follow-up reminder came back.
  function withUndoNote(message, result) {
    const note = typeof result?.undo_note === "string" ? result.undo_note.trim() : "";
    return note ? `${message} ${note}` : message;
  }

  // A time today reads as "9:12 AM"; any other day adds the date.
  function automationWhen(stamp) {
    const date = new Date(stamp);
    if (!stamp || Number.isNaN(date.getTime())) return "";
    if (date.toDateString() === new Date().toDateString()) {
      return CLOCK_FORMAT.format(date);
    }
    return formatDateTime(stamp);
  }

  // One thing a pause could not stop, by its action: a 'send' is an email
  // Gmail already has, a 'form' a contact form whose button is being
  // pressed, and an 'application' one handed to Greenhouse (Apply for me).
  // Nothing else is past stopping (a Gmail draft being saved sends
  // nothing), so any other action is left out rather than called an email.
  // A Finish in browser window (action 'window') is not on its way anywhere: the student's own Submit is what sends, so it says
  // what it is in its own words, from the server's label.
  const WINDOW_OPEN = "A Finish in browser window is open. Pausing doesn't stop your own Submit; press Stop to end it.";

  function inFlightItem(item) {
    if (item?.action === "window") return { window: true, label: String(item.label || WINDOW_OPEN) };
    if (item?.action !== "send" && item?.action !== "form" && item?.action !== "application") return null;
    const form = item.action === "form";
    const application = item.action === "application";
    const how = application ? `handed to ${atsName(item)}` : form ? "submission started" : item.source === "scheduled_send" ? "handed to Gmail" : "sending started";
    const when = automationWhen(item.at);
    const to = item.company ? ` to ${item.company}` : "";
    return {
      noun: application ? "application" : form ? "contact form" : "email",
      article: application ? "an application" : form ? "a contact form" : "an email",
      to,
      detail: when ? `${how} at ${when}` : how,
    };
  }

  // What a pause could not stop, said plainly: an email Gmail already has goes.
  function inFlightSentence(items) {
    const all = (Array.isArray(items) ? items : []).map(inFlightItem).filter(Boolean);
    const windows = [...new Set(all.filter((entry) => entry.window).map((entry) => entry.label))].join(" ");
    const sent = inFlightSentenceOf(all.filter((entry) => !entry.window));
    return [sent, windows].filter(Boolean).join(" ");
  }

  function inFlightSentenceOf(described) {
    if (!described.length) return "";
    if (described.length === 1) {
      const [only] = described;
      return `1 ${only.noun}${only.to} was already on its way (${only.detail}) and can't be stopped.`;
    }
    const emails = described.filter((entry) => entry.noun === "email").length;
    const applications = described.filter((entry) => entry.noun === "application").length;
    const forms = described.length - emails - applications;
    const counted = [
      emails ? plural(emails, "email", "emails") : "",
      forms ? plural(forms, "contact form", "contact forms") : "",
      applications ? plural(applications, "application", "applications") : "",
    ].filter(Boolean).join(" and ");
    return `${counted} were already on their way and can't be stopped: ${described.map((entry) => `${entry.article}${entry.to} (${entry.detail})`).join("; ")}.`;
  }

  // A send or form submission whose outcome is unknown, and where to look.
  // A Gmail draft can be left unconfirmed too (made, but not recorded); it sent nothing.
  function unconfirmedSentence(item) {
    const to = item.company ? ` to ${item.company}` : "";
    const when = automationWhen(item.at);
    const started = when ? ` (started ${when})` : "";
    if (item.action === "draft") {
      return `A Gmail draft${item.company ? ` for ${item.company}` : ""} may have been saved without the app recording it${started}. Check your Gmail Drafts; a draft sends nothing.`;
    }
    if (item.action === "application") {
      const ats = atsName(item);
      return `The application${to} may or may not have reached ${ats}${started}. Look for ${ats}'s confirmation email or check the company's page, then say whether it went through.`;
    }
    const form = item.action === "form";
    const what = form ? "The contact form message" : item.kind === "follow_up" ? "The follow-up" : item.kind === "thank_you" ? "The thank-you" : "The email";
    const look = form ? "Check the company's page." : item.action === "send" ? "Check your Gmail Sent folder." : "Check your Gmail Sent folder or the company's page.";
    return `${what}${to} may or may not have gone out${started}. ${look}`;
  }

  function automationEvidence(evidence) {
    const excerpt = typeof evidence?.excerpt === "string" ? evidence.excerpt.trim() : "";
    const subject = typeof evidence?.subject === "string" ? evidence.subject.trim() : "";
    return excerpt || subject;
  }

  // A checkbox for a two-mode feature. Saves at once; a refusal puts it back
  // and shows the server's reason in the status line.
  // A switch and its label, kept apart so the row can put the switch at its end.
  function automationCheckbox(feature, id, text, status) {
    const label = element("label", "", text);
    label.htmlFor = id;
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "settings-switch";
    box.setAttribute("role", "switch");
    box.id = id;
    box.dataset.automationKey = feature.key;
    paintAutomationControl(box, feature);
    box.addEventListener("change", async () => {
      const wanted = box.checked ? "on" : "off";
      box.disabled = true;
      status.textContent = "Saving…";
      try {
        const updated = await saveAutomationMode(feature.key, wanted);
        status.textContent = `${feature.label}: ${AUTOMATION_MODE_WORDS[updated?.mode ?? wanted] || wanted}.`;
      } catch (error) {
        box.checked = savedAutomationMode(feature.key) === "on";
        if (!isAuthError(error)) status.textContent = error.message;
      } finally {
        box.disabled = false;
      }
    });
    return [label, box];
  }

  function automationFeatureField(feature, status) {
    const field = element("div", "settings-field automation-feature");
    field.dataset.automationFeature = feature.key;
    const id = `automation-mode-${feature.key}`;
    const head = element("div", "automation-feature-head");
    const help = element("p", "profile-help", AUTOMATION_SWITCH_HELP[feature.key] || AUTOMATION_FEATURE_HELP[feature.key] || feature.description);
    help.id = `${id}-help`;
    const external = feature.risk === "external" ? chip("External", "is-soon") : null;
    if ((feature.modes || []).includes("shadow")) {
      const label = element("label", "", feature.label);
      label.htmlFor = id;
      head.appendChild(label);
      if (external) head.appendChild(external);
      const select = document.createElement("select");
      select.id = id;
      select.dataset.automationKey = feature.key;
      feature.modes.forEach((mode) => {
        select.appendChild(optionElement(mode, AUTOMATION_MODE_LABELS[mode] || mode));
      });
      const reason = element("p", "profile-help automation-reason");
      reason.id = `${id}-reason`;
      select.setAttribute("aria-describedby", `${reason.id} ${help.id}`);
      field.append(head, select, reason, help);
      paintAutomationControl(select, feature);
      autoSaveSelect(select, {
        saved: () => savedAutomationMode(feature.key, feature.mode),
        commit: async (value) => {
          select.disabled = true;
          status.textContent = "Saving…";
          try {
            const updated = await saveAutomationMode(feature.key, value);
            status.textContent = `${feature.label}: ${AUTOMATION_MODE_WORDS[updated?.mode ?? value] || value}.`;
          } catch (error) {
            select.value = savedAutomationMode(feature.key, feature.mode);
            if (!isAuthError(error)) status.textContent = error.message;
          } finally {
            select.disabled = false;
          }
        },
      });
    } else {
      const reason = element("p", "profile-help automation-reason");
      reason.id = `${id}-reason`;
      reason.hidden = true;
      const [label, box] = automationCheckbox(feature, id, feature.label, status);
      box.setAttribute("aria-describedby", `${reason.id} ${help.id}`);
      head.appendChild(label);
      if (external) head.appendChild(external);
      field.append(head, box, reason, help);
      paintAutomationControl(box, feature);
    }
    return field;
  }

  function automationHealthList(health) {
    const list = element("ul", "automation-health");
    const gmail = health?.gmail || {};
    const gmailWords = [GMAIL_STATE_WORDS[gmail.state] || "Not known yet."];
    if (gmail.last_ok_at) gmailWords.push(`Last successful check ${timeAgo(gmail.last_ok_at)}.`);
    if (gmail.state === "throttled" && gmail.backoff_until) gmailWords.push(`Checks resume after ${automationWhen(gmail.backoff_until)}.`);
    // A success clears last_error on the server, so one that is still there
    // came after the last successful check. A connection that is off has none worth showing.
    const lastError = typeof gmail.last_error === "string" ? gmail.last_error.trim().replace(/[.]+$/, "") : "";
    if (lastError && !["not_connected", "disconnected"].includes(gmail.state)) gmailWords.push(`Last problem: ${lastError}.`);
    if (gmail.likely_expires_at) {
      // Once the estimated date has passed there is no "by" date left to promise.
      gmailWords.push(gmail.estimate_passed
        ? "It may ask you to reconnect soon (an estimate)."
        : `It will likely ask you to reconnect by ${formatDate(gmail.likely_expires_at)} (an estimate).`);
    }
    const gmailRow = element("li");
    gmailRow.append(element("strong", "", "Gmail"), element("span", "", gmailWords.join(" ")));
    list.appendChild(gmailRow);
    (health?.components || []).forEach((component) => {
      const failing = Boolean(component.last_error_at) && (!component.last_ok_at || String(component.last_error_at) > String(component.last_ok_at));
      const row = element("li");
      row.append(element("strong", "", humanizeKey(component.component)), chip(failing ? "Error" : "OK", failing ? "is-warning" : "is-good"));
      const words = [];
      if (component.last_ok_at) words.push(`Last worked ${timeAgo(component.last_ok_at)}.`);
      if (component.last_error) words.push(`Last error${component.last_error_at ? ` (${timeAgo(component.last_error_at)})` : ""}: ${component.last_error}`);
      if (words.length) row.appendChild(element("span", "", words.join(" ")));
      list.appendChild(row);
    });
    (health?.in_flight || []).forEach((item) => {
      const described = inFlightItem(item);
      if (!described) return;
      const row = element("li");
      if (described.window) {
        row.append(element("strong", "", "Window open"), element("span", "", described.label));
        list.appendChild(row);
        return;
      }
      row.append(
        element("strong", "", "On its way"),
        element("span", "", `${described.article[0].toUpperCase()}${described.article.slice(1)}${described.to}: ${described.detail}. It can't be stopped.`),
      );
      list.appendChild(row);
    });
    (health?.unconfirmed || []).forEach((item) => {
      const row = element("li");
      row.append(element("strong", "", "Not confirmed"), chip("Check", "is-warning"), element("span", "", unconfirmedSentence(item)));
      list.appendChild(row);
    });
    // Told apart from a switch that was simply never turned on, even after its notice is read.
    (health?.breaker_off || []).forEach((item) => {
      const row = element("li");
      const when = automationWhen(item.at);
      row.append(
        element("strong", "", "Turned off"),
        element("span", "", `${item.label || automationFeatureLabel(item.feature)} was turned off by the app${when ? ` (${when})` : ""} after you undid or rejected its actions. Turn it back on above when you want it.`),
      );
      list.appendChild(row);
    });
    return list;
  }

  // One list's answer from /api/v1/automation/actions: its rows, how many the
  // server holds in all, and the error when it could not be loaded.
  function automationList(payload) {
    return {
      items: Array.isArray(payload?.items) ? payload.items : [],
      total: Number(payload?.total) || 0,
      error: payload?.error ? payload.error.message : "",
    };
  }

  function automationListRequests() {
    return AUTOMATION_LIST_KEYS.map((key) => api(`/api/v1/automation/actions?${AUTOMATION_LISTS[key].query}`));
  }

  function automationSection(payload, actionsPayload, applicationsPayload = null) {
    const section = element("section", "profile-card automation-section");
    section.setAttribute("aria-labelledby", "automation-heading");
    const title = element("h3", "", "What the app does on its own");
    title.id = "automation-heading";
    section.append(title, element("p", "profile-help", "Nothing here is on until you turn it on."));
    if (!payload?.settings) {
      section.appendChild(element("p", "form-error", `Automation could not be loaded${payload?.error ? `: ${payload.error.message}` : "."}`));
      return section;
    }
    const overview = payload;
    let notices = Array.isArray(payload.notices) ? payload.notices : [];
    const data = Object.fromEntries(AUTOMATION_LIST_KEYS.map((key) => [key, automationList(actionsPayload?.[key])]));

    function block(className, heading, id) {
      const wrap = element("div", `automation-block ${className}`.trim());
      const headingNode = element("h4", "", heading);
      headingNode.id = id;
      headingNode.tabIndex = -1;
      wrap.appendChild(headingNode);
      return [wrap, headingNode];
    }

    function liveStatus(className = "") {
      const status = element("p", `form-status ${className}`.trim());
      status.setAttribute("aria-live", "polite");
      return status;
    }

    // The master pause.
    const pauseRow = element("div", "automation-pause");
    const pause = element("button", "secondary-button");
    pause.type = "button";
    pause.id = "automation-pause";
    const pauseHelp = element("p", "profile-help automation-pause-help", AUTOMATION_PAUSE_HELP);
    pauseHelp.id = "automation-pause-help";
    pause.setAttribute("aria-describedby", pauseHelp.id);
    const pauseStatus = liveStatus("automation-pause-status");
    pauseStatus.id = "automation-pause-status";
    pauseRow.append(pause, pauseStatus, pauseHelp);
    paintAutomationPause(overview.settings.paused, pause, pauseStatus);
    pause.addEventListener("click", async () => {
      const wanted = pause.dataset.paused !== "true";
      pause.disabled = true;
      pauseStatus.textContent = wanted ? "Pausing…" : "Resuming…";
      try {
        // The answer repaints the pause, the switches, Health, and the banner (automationWrite).
        const result = await setAutomationPaused(wanted);
        const flight = wanted ? inFlightSentence(result.in_flight) : "";
        pauseStatus.textContent = wanted
          ? `${AUTOMATION_PAUSE_TEXT.true}${flight ? ` ${flight}` : ""}`
          : "Resumed. Each switch below decides what runs again.";
      } catch (error) {
        if (!isAuthError(error)) pauseStatus.textContent = error.message;
      } finally {
        pause.disabled = false;
        if (!document.activeElement || document.activeElement === document.body) pause.focus();
      }
    });
    section.appendChild(pauseRow);

    // The switches, by group. A group with no features is left out.
    const features = element("div", "automation-features");
    AUTOMATION_GROUPS.forEach(([group, heading]) => {
      const members = overview.settings.features.filter((feature) => feature.group === group);
      if (!members.length) return;
      const [wrap] = block("automation-group", heading, `automation-group-${group}`);
      const status = liveStatus();
      members.forEach((feature) => wrap.appendChild(automationFeatureField(feature, status)));
      wrap.appendChild(status);
      features.appendChild(wrap);
    });
    section.appendChild(features);
    if (overview.settings.features.some((feature) => feature.key === "apply_agent")) section.appendChild(applyAgentSettingsBlock());

    // Roles auto_pass passed on in the last week, each with Restore (the ledger's undo).
    const [passedBlock, passedHeading] = block("automation-auto-passed", "Auto-passed this week", "automation-auto-passed-heading");
    passedBlock.appendChild(element("p", "profile-help", "Roles the app passed on for you in the last 7 days. Restore puts one back as if it had never been passed."));
    const passedHost = element("div");
    const passedStatus = liveStatus();
    passedBlock.append(passedHost, passedStatus);
    let autoPassed = null;
    const restoring = new Set();

    function paintAutoPassed() {
      passedHost.replaceChildren();
      if (!autoPassed) {
        passedHost.appendChild(element("p", "empty-inline", "Loading…"));
        return;
      }
      if (autoPassed.error) {
        passedHost.appendChild(element("p", "form-error", `Auto-passed roles could not be loaded: ${autoPassed.error}`));
        return;
      }
      if (!autoPassed.items.length) {
        passedHost.appendChild(element("p", "empty-inline", "Nothing was passed on automatically in the last 7 days."));
        return;
      }
      const list = element("ul", "automation-actions");
      autoPassed.items.forEach((item) => {
        const row = element("li", "automation-action");
        row.dataset.actionId = item.action_id;
        const name = [item.title, item.company].filter(Boolean).join(" at ") || "A role";
        row.appendChild(element("p", "automation-action-summary", name));
        const why = Number.isFinite(item.score) && Number.isFinite(item.threshold)
          ? `Score ${item.score}, below your ${item.threshold}`
          : "";
        const meta = [why, item.applied_at ? `Passed ${formatDateTime(item.applied_at)}` : ""].filter(Boolean).join(" · ");
        if (meta) row.appendChild(element("p", "automation-meta", meta));
        if (Array.isArray(item.reasons) && item.reasons.length) row.appendChild(element("p", "automation-evidence", `Why it scored so: ${item.reasons.join("; ")}`));
        const buttons = element("div", "automation-action-buttons");
        const restore = element("button", "secondary-button", "Restore");
        restore.type = "button";
        restore.disabled = restoring.has(item.action_id);
        restore.setAttribute("aria-label", `Restore ${name}`);
        restore.addEventListener("click", async () => {
          if (restoring.has(item.action_id)) return;
          restoring.add(item.action_id);
          restore.disabled = true;
          passedStatus.textContent = "Saving…";
          let message;
          try {
            const result = await automationWrite(() => api(`/api/v1/automation/actions/${encodeURIComponent(item.action_id)}/undo`, { method: "POST" }));
            message = withUndoNote(result.feature_paused ? automationBreakerMessage("auto_pass", result.breaker_notice) : `Restored ${name}.`, result);
          } catch (error) {
            if (isAuthError(error)) {
              restoring.delete(item.action_id);
              return;
            }
            // As decide(): a 409 or 404 says it is no longer open to Restore; anything else can be tried again.
            if (error.status !== 409 && error.status !== 404) restoring.delete(item.action_id);
            message = error.message;
          }
          await Promise.all([reload(), loadAutoPassed()]);
          passedStatus.textContent = message;
          if (!document.activeElement || document.activeElement === document.body || !document.activeElement.isConnected) passedHeading.focus();
        });
        buttons.appendChild(restore);
        row.appendChild(buttons);
        list.appendChild(row);
      });
      passedHost.appendChild(list);
    }

    async function loadAutoPassed() {
      try {
        const payload = await api("/api/v1/automation/auto-passed");
        autoPassed = { items: Array.isArray(payload?.items) ? payload.items : [], error: "" };
      } catch (error) {
        if (isAuthError(error)) return;
        autoPassed = { items: [], error: error.message };
      }
      paintAutoPassed();
    }
    paintAutoPassed();
    loadAutoPassed();
    section.appendChild(passedBlock);

    const applicationList = Array.isArray(applicationsPayload?.items) ? applicationsPayload.items : [];
    let mailState = payload.application_mail || null;

    // Update applications from job emails: how reading stands, and what the first look back found.
    // Painted only when what it shows changed, so a poll or a window focus never takes a button
    // from under the keyboard; a button whose request is on its way stays disabled across a
    // repaint, and focus comes back to it (or to the heading once it is gone).
    const [mailBlock, mailHeading] = block("automation-application-mail", "Job emails", "automation-application-mail-heading");
    const mailHost = element("div");
    const mailStatus = liveStatus();
    mailBlock.append(mailHost, mailStatus);
    const mailBusy = { check: false, all: false };
    let mailPainted = null;
    const refocusMail = (id) => {
      const active = document.activeElement;
      if (!active || active === document.body || !active.isConnected) (document.getElementById(id) || mailHeading).focus();
    };
    function paintMail() {
      const mail = mailState;
      const key = JSON.stringify([mail, mailBusy]);
      if (key === mailPainted) return;
      mailPainted = key;
      const focusedId = mailHost.contains(document.activeElement) ? document.activeElement.id : "";
      mailHost.replaceChildren();
      const on = Boolean(mail && mail.mode && mail.mode !== "off");
      mailBlock.hidden = !mail || (!on && !mail.backfill_found);
      if (mailBlock.hidden) return;
      const words = [];
      if (on) words.push(mail.enabled_at ? `Reading job emails since ${formatDate(mail.enabled_at)}${mail.mode === "shadow" ? ", in shadow" : ""}.` : "Starts reading job emails at the next check.");
      if (mail.last_ok_at) words.push(`Last checked ${timeAgo(mail.last_ok_at)}.`);
      if (mail.awaiting_resume) words.push(`${plural(mail.awaiting_resume, "email waits", "emails wait")} for you to resume automation.`);
      if (mail.last_error) words.push(`Last problem: ${String(mail.last_error).replace(/[.]+$/, "")}.`);
      if (mail.domain_check === false) words.push("Sender domains cannot be checked on this computer (publicsuffixlist is not installed), so every job email only proposes until it is installed.");
      mailHost.appendChild(element("p", "profile-help", words.join(" ")));
      const buttons = element("div", "automation-action-buttons");
      if (mail.backfill_found) {
        const found = Number(mail.backfill_found) || 0;
        const approvable = Math.min(Number(mail.backfill_approvable) || 0, found);
        const rest = found - approvable;
        const others = "an offer, a sender Gmail could not verify, a guessed application, or a role not in your tracker";
        let text = `Found ${plural(found, "update", "updates")} from the last 60 days. Each is under Waiting for you.`;
        if (approvable && rest) {
          text += ` Approve all approves the ${plural(approvable, "one", "ones")} that waited only because ${approvable === 1 ? "it" : "they"} came before you turned this on; the other ${rest} need${rest === 1 ? "s" : ""} a look one by one (${others}).`;
        } else if (approvable) {
          text += " Approve them one by one, or all at once.";
        } else {
          text += ` Each needs a look one by one (${others}).`;
        }
        mailHost.appendChild(element("p", "automation-backfill", text));
        if (approvable) {
          const all = element("button", "secondary-button", "Approve all");
          all.type = "button";
          all.id = "automation-backfill-approve";
          all.disabled = mailBusy.all;
          all.addEventListener("click", async () => {
            if (mailBusy.all) return;
            mailBusy.all = true;
            all.disabled = true;
            mailStatus.textContent = "Approving…";
            let message = "";
            try {
              const result = await automationWrite(() => api("/api/v1/automation/application-mail/backfill/approve-all", { method: "POST" }));
              await reload();
              const kept = result.superseded ? `; ${plural(result.superseded, "was", "were")} left as ${result.superseded === 1 ? "it was" : "they were"}, because the application changed since` : "";
              const left = result.left ? `. ${plural(result.left, "update waits", "updates wait")} for you to look at one by one` : "";
              message = `Approved ${plural(result.approved, "update", "updates")}${kept}${left}.`;
            } catch (error) {
              if (!isAuthError(error)) message = error.message;
            } finally {
              mailBusy.all = false;
              paintMail();
              refocusMail("automation-backfill-approve");
            }
            if (message) mailStatus.textContent = message;
          });
          buttons.appendChild(all);
        }
      }
      if (on) {
        const check = element("button", "secondary-button", "Check now");
        check.type = "button";
        check.id = "automation-application-mail-check";
        check.disabled = mailBusy.check;
        check.addEventListener("click", async () => {
          if (mailBusy.check) return;
          mailBusy.check = true;
          check.disabled = true;
          mailStatus.textContent = "Checking Gmail…";
          let message = "";
          try {
            const result = await api("/api/v1/automation/application-mail/check", { method: "POST" });
            const read = Number(result.detail?.read) || 0;
            const errors = Number(result.detail?.error) || 0;
            await reload();
            if (result.state === "off") {
              message = "Update applications from job emails is off, so nothing was read.";
            } else if (result.skipped) {
              // Another check (the background one) holds the reader: this one read nothing.
              message = "A check is already running; what it finds will show here shortly.";
            } else if (result.state === "ok" || result.state === "message_errors") {
              const setAside = errors ? `; ${plural(errors, "email", "emails")} could not be read and ${errors === 1 ? "was" : "were"} set aside` : "";
              message = `Checked: ${plural(read, "new email", "new emails")} read${setAside}.`;
            } else if (result.state === "database_busy") {
              message = "The database was busy, so the check stopped. It tries again at the next check.";
            } else {
              message = `Could not check Gmail (${String(result.state).replaceAll("_", " ")}).`;
            }
          } catch (error) {
            if (!isAuthError(error)) message = error.message;
          } finally {
            mailBusy.check = false;
            paintMail();
            refocusMail("automation-application-mail-check");
          }
          if (message) mailStatus.textContent = message;
        });
        buttons.appendChild(check);
      }
      if (buttons.childElementCount) mailHost.appendChild(buttons);
      if (focusedId) document.getElementById(focusedId)?.focus();
    }

    // Trusted company mail domains: only the student's yes lets mail from one act.
    const [domainBlock, domainHeading] = block("automation-domains", "Trusted company mail domains", "automation-domains-heading");
    domainBlock.appendChild(element("p", "profile-help", "Mail from a company's own domain can update that company's applications only after you trust the domain here. Suggestions come from your applications' job links, your outreach records, and emails Gmail vouched for."));
    const domainHost = element("div");
    const domainStatus = liveStatus();
    domainBlock.append(domainHost, domainStatus);
    let domains = null;
    function paintDomains() {
      domainHost.replaceChildren();
      if (domains === null) {
        domainHost.appendChild(element("p", "empty-inline", "Loading…"));
        return;
      }
      if (domains.error) {
        domainHost.appendChild(element("p", "form-error", `Company domains could not be loaded: ${domains.error}`));
        return;
      }
      if (!domains.items.length) {
        domainHost.appendChild(element("p", "empty-inline", "No company domains to review yet."));
        return;
      }
      const list = element("ul", "automation-actions automation-domain-list");
      domains.items.forEach((item) => {
        const row = element("li", "automation-action automation-domain");
        row.dataset.domainId = item.id;
        const trusted = item.status === "trusted";
        row.appendChild(element("p", "automation-action-summary", trusted ? `Trusted: mail from @${item.domain} for ${item.company}` : `Trust mail from @${item.domain} for ${item.company}?`));
        if (item.evidence) row.appendChild(element("p", "automation-evidence", item.evidence));
        const buttons = element("div", "automation-action-buttons");
        if (!trusted) {
          const trust = element("button", "secondary-button", "Trust");
          trust.type = "button";
          trust.setAttribute("aria-label", `Trust mail from ${item.domain} for ${item.company}`);
          trust.addEventListener("click", () => decideDomain(item, "trust"));
          buttons.appendChild(trust);
        }
        // Stop trusting puts it back to a suggestion (still read, only proposing); Dismiss stops reading its mail.
        const dismiss = element("button", trusted ? "secondary-button" : "danger-button", trusted ? "Stop trusting" : "Dismiss");
        dismiss.type = "button";
        dismiss.setAttribute("aria-label", `${trusted ? "Stop trusting" : "Dismiss"} ${item.domain} for ${item.company}`);
        dismiss.addEventListener("click", () => decideDomain(item, trusted ? "untrust" : "dismiss"));
        buttons.appendChild(dismiss);
        row.appendChild(buttons);
        list.appendChild(row);
      });
      domainHost.appendChild(list);
    }
    async function loadDomains() {
      try {
        const answer = await api("/api/v1/automation/employer-domains");
        domains = { items: Array.isArray(answer.items) ? answer.items : [], error: "" };
      } catch (error) {
        if (isAuthError(error)) return;
        domains = { items: [], error: error.message };
      }
      paintDomains();
    }
    async function decideDomain(item, verb) {
      domainHost.querySelectorAll("button").forEach((button) => { button.disabled = true; });
      domainStatus.textContent = "Saving…";
      try {
        await api(`/api/v1/automation/employer-domains/${encodeURIComponent(item.id)}/${verb}`, { method: "POST" });
        const what = `@${item.domain} for ${item.company}`;
        if (verb === "trust") {
          // In shadow nothing acts on its own, and even on, Gmail must vouch for the sender first.
          domainStatus.textContent = mailState?.mode === "on"
            ? `Trusted ${what}. Its emails can now update ${item.company} applications on their own when Gmail vouches for the sender.`
            : `Trusted ${what}. While this is in shadow, its emails are logged under Would have done until you turn it on.`;
        } else if (verb === "untrust") {
          domainStatus.textContent = `Stopped trusting ${what}. Its emails are still read, and only propose changes for you to approve.`;
        } else {
          domainStatus.textContent = `Dismissed ${what}. Its emails are no longer read, unless a job system sends them.`;
        }
      } catch (error) {
        if (isAuthError(error)) return;
        domainStatus.textContent = error.message;
      }
      await loadDomains();
      domainHeading.focus();
    }
    // Shown, and asked for, only while the job-email switch is not off: trusting a domain matters only to it.
    const mailOn = () => Boolean(mailState && mailState.mode && mailState.mode !== "off");
    function syncDomains() {
      domainBlock.hidden = !mailOn();
      if (mailOn()) loadDomains();
    }
    paintDomains();
    syncDomains();
    automationStatus.onMail = (mail) => {
      if (!section.isConnected) return;
      const wasOn = mailOn();
      mailState = mail;
      paintMail();
      if (wasOn !== mailOn()) syncDomains();
    };

    // Every newer answer repaints this host by its id (applyAutomationStatus).
    const [healthBlock] = block("", "Health", "automation-health-heading");
    const healthHost = element("div");
    healthHost.id = "automation-health-host";
    healthBlock.appendChild(healthHost);
    healthHost.appendChild(automationHealthList(state.automation?.health || overview.health));

    const [noticeBlock, noticeHeading] = block("", "Notices", "automation-notices-heading");
    const noticeHost = element("div");
    const markAll = element("button", "secondary-button", "Mark all read");
    markAll.type = "button";
    markAll.id = "automation-notices-read";
    const noticeStatus = liveStatus();
    noticeBlock.append(noticeHost, markAll, noticeStatus);
    const unreadNotices = () => notices.filter((notice) => !notice.read_at);
    function paintNotices() {
      const unread = unreadNotices();
      noticeHost.replaceChildren();
      markAll.hidden = !unread.length;
      if (!unread.length) {
        noticeHost.appendChild(element("p", "empty-inline", "No unread notices."));
        return;
      }
      const list = element("ul", "automation-notices");
      unread.forEach((notice) => {
        const level = AUTOMATION_BANNER_LEVELS.has(notice.level) ? notice.level : "info";
        const row = element("li", `automation-notice is-${level}`);
        row.appendChild(element("strong", "", notice.title || ""));
        if (notice.body) row.appendChild(element("p", "", notice.body));
        row.appendChild(element("span", "automation-meta", formatDateTime(notice.created_at)));
        list.appendChild(row);
      });
      noticeHost.appendChild(list);
    }
    markAll.addEventListener("click", async () => {
      markAll.disabled = true;
      noticeStatus.textContent = "Saving…";
      try {
        // Every unread notice, not only the ones this page was sent (the overview holds at most 20).
        const result = await automationWrite(() => api("/api/v1/automation/notices/read", { method: "POST", body: JSON.stringify({ all: true }) }));
        await reload();
        noticeStatus.textContent = `Marked ${plural(result.marked, "notice", "notices")} read.`;
        noticeHeading.focus();
      } catch (error) {
        if (!isAuthError(error)) noticeStatus.textContent = error.message;
      } finally {
        markAll.disabled = false;
      }
    });

    const lists = Object.fromEntries(AUTOMATION_LIST_KEYS.map((key) => {
      const entry = { ...AUTOMATION_LISTS[key] };
      const [wrap, heading] = block(`automation-${key}`, entry.heading, `automation-${key}-heading`);
      entry.wrap = wrap;
      entry.headingNode = heading;
      if (key === "shadow") wrap.appendChild(element("p", "profile-help", "A switch in shadow changes nothing; it logs what it would have done. Your calls here decide whether it can be turned on."));
      entry.host = element("div");
      entry.status = liveStatus();
      wrap.append(entry.host, entry.status);
      return [key, entry];
    }));

    // Decisions still on their way (by action), and decisions already made
    // here (by list and action). Every repaint, whichever reload drew it,
    // draws their buttons disabled, so a row is never offered a second
    // decision while its first is pending or after it has been made. A
    // decision closes only the list it was made in: an action approved in
    // Waiting lands in Recent as Applied, and its Undo there is a new
    // decision the student can make at once.
    const deciding = new Set();
    const decided = new Set();
    const decidedIn = (listKey, id) => decided.has(`${listKey}:${id}`);
    const busy = (listKey, id) => deciding.has(id) || decidedIn(listKey, id);

    function syncRowButtons() {
      Object.entries(lists).forEach(([listKey, entry]) => {
        entry.host.querySelectorAll(".automation-action[data-action-id]").forEach((row) => {
          const disabled = busy(listKey, row.dataset.actionId);
          row.querySelectorAll(".automation-action-buttons button").forEach((button) => { button.disabled = disabled; });
        });
      });
    }

    function actionButton(text, className, onClick) {
      const button = element("button", className, text);
      button.type = "button";
      button.addEventListener("click", onClick);
      return button;
    }

    function actionRow(action, metaParts) {
      const row = element("li", "automation-action");
      row.dataset.actionId = action.id;
      row.appendChild(element("p", "automation-action-summary", action.summary || "An automatic change"));
      const evidence = automationEvidence(action.evidence);
      if (evidence) row.appendChild(element("p", "automation-evidence", `Evidence: ${evidence}`));
      const reasons = Array.isArray(action.evidence?.why_proposal) ? action.evidence.why_proposal.filter((reason) => typeof reason === "string" && reason) : [];
      if (action.status === "proposed" && reasons.length) {
        row.appendChild(element("p", "automation-reasons", `Waiting for you because ${reasons.join("; ")}.`));
      }
      const meta = metaParts.filter(Boolean).join(" · ");
      if (meta) row.appendChild(element("p", "automation-meta", meta));
      return row;
    }

    function reasoning(action) {
      const confidence = typeof action.confidence === "number" ? `${Math.round(action.confidence * 100)}% confidence` : "";
      return [automationFeatureLabel(action.feature), action.basis ? `Basis: ${action.basis}` : "", confidence, formatDateTime(action.created_at)];
    }

    function decisionMessage(verb, action, result, body) {
      const summary = String(action.summary || "the change").replace(/[.!?]+$/, "");
      if (verb === "approve" && action.action_type === "application.capture_proposal") return "Opened a capture draft. Check the fields and confirm it to add the role.";
      if (verb === "approve" && body?.subject_id && body.subject_id !== action.subject_id) {
        const chosen = applicationList.find((application) => application.id === body.subject_id);
        return `Approved for ${chosen ? `${chosen.company} (${chosen.title})` : "the application you chose"} instead: ${summary}.`;
      }
      if (verb === "approve") return `Approved: ${summary}.`;
      if (verb === "reject") return result.feature_paused ? automationBreakerMessage(action.feature, result.breaker_notice) : `Rejected: ${summary}. Nothing was changed.`;
      if (verb === "undo") return withUndoNote(result.feature_paused ? automationBreakerMessage(action.feature, result.breaker_notice) : `Undone: ${summary}.`, result);
      return `Marked as the ${body.verdict} call.`;
    }

    async function decide(listKey, action, verb, body = null) {
      if (busy(listKey, action.id)) return;
      const entry = lists[listKey];
      deciding.add(action.id);
      syncRowButtons();
      entry.status.textContent = "Saving…";
      let message;
      try {
        const result = await automationWrite(() => api(`/api/v1/automation/actions/${encodeURIComponent(action.id)}/${verb}`, {
          method: "POST",
          ...(body ? { body: JSON.stringify(body) } : {}),
        }));
        decided.add(`${listKey}:${action.id}`);
        message = decisionMessage(verb, action, result, body);
        const captureId = verb === "approve" ? result?.after?._result?.capture_id : null;
        if (captureId) {
          try {
            await openCaptureDraft(captureId, async () => {
              announce("Added to your tracker.");
              await reload();
            });
          } catch (error) {
            if (!isAuthError(error)) {
              message = `The capture draft was made, but it could not be opened (${error.message}). Open it from Recent.`;
            }
          }
        }
      } catch (error) {
        if (isAuthError(error)) {
          deciding.delete(action.id);
          syncRowButtons();
          return;
        }
        // A 409 or 404 says the action is no longer open to this decision
        // (decided somewhere else, or superseded by a later change), so it
        // stays decided. Anything else (a lost connection) can be tried again.
        if (error.status === 409 || error.status === 404) decided.add(`${listKey}:${action.id}`);
        message = error.message;
      }
      deciding.delete(action.id);
      // An auto-pass undone here also leaves Auto-passed this week, which has its own list.
      const [refreshed] = await Promise.all([reload(), action.feature === "auto_pass" ? loadAutoPassed() : null]);
      // Whether or not the lists were redrawn, the buttons follow what is known.
      syncRowButtons();
      entry.status.textContent = refreshed ? message : `${message} The lists could not be refreshed, so they may be out of date.`;
      const verdict = entry.host.querySelector(`[data-action-id="${CSS.escape(action.id)}"] .automation-verdict`);
      if (!document.activeElement || document.activeElement === document.body || !document.activeElement.isConnected) {
        (verdict || entry.headingNode).focus();
      }
    }

    // Unreviewed shadow actions first, since each one stands between the
    // switch and On (can_turn_on); newest first within each.
    function ordered(key, items) {
      if (key !== "shadow") return items;
      return [...items.filter((action) => !action.review), ...items.filter((action) => action.review)];
    }

    // Application choices made in Waiting but not approved yet, so a repaint after another
    // row's decision does not put the proposed application back under the student's Approve.
    function unsavedPickers() {
      return [...lists.waiting.host.querySelectorAll(".automation-action[data-action-id] .automation-picker")]
        .map((picker) => ({ id: picker.closest("[data-action-id]").dataset.actionId, value: picker.value, proposed: picker.dataset.proposed, focused: picker === document.activeElement }))
        .filter((choice) => choice.value !== choice.proposed || choice.focused);
    }

    function restorePickers(choices) {
      choices.forEach(({ id, value, focused }) => {
        const picker = lists.waiting.host.querySelector(`[data-action-id="${CSS.escape(id)}"] .automation-picker`);
        if (!picker) return;
        if ([...picker.options].some((option) => option.value === value)) picker.value = value;
        if (focused) picker.focus();
      });
    }

    async function openDraftFromRecent(entry, captureId) {
      try {
        await openCaptureDraft(captureId, async () => {
          announce("Added to your tracker.");
          await reload();
        });
      } catch (error) {
        if (!isAuthError(error)) entry.status.textContent = error.message;
      }
    }

    function paintLists() {
      const carried = unsavedPickers();
      Object.entries(lists).forEach(([key, entry]) => {
        const { items, total, error } = data[key];
        entry.headingNode.textContent = total ? `${entry.heading} (${total.toLocaleString()})` : entry.heading;
        entry.host.replaceChildren();
        if (error) {
          entry.host.appendChild(element("p", "form-error", `Automatic actions could not be loaded: ${error}`));
          return;
        }
        if (!items.length) {
          entry.host.appendChild(element("p", "empty-inline", entry.empty));
          return;
        }
        const list = element("ul", "automation-actions");
        ordered(key, items).forEach((action) => {
          if (key === "waiting") {
            const row = actionRow(action, reasoning(action));
            let picker = null;
            if (action.subject_kind === "application" && applicationList.length) {
              // Approve applies to the application chosen here: the proposed one, unless the student picks another.
              const candidates = Array.isArray(action.evidence?.match?.candidates) ? action.evidence.match.candidates : [action.subject_id];
              picker = applicationPicker(applicationList, candidates, action.subject_id);
              picker.classList.add("automation-picker");
              picker.dataset.proposed = action.subject_id;
              picker.setAttribute("aria-label", `Application for: ${action.summary || "this change"}`);
              row.appendChild(picker);
            }
            const buttons = element("div", "automation-action-buttons");
            const capture = action.action_type === "application.capture_proposal";
            buttons.append(
              actionButton(capture ? "Capture this role" : "Approve", "secondary-button", () => {
                const chosen = picker?.value;
                if (picker && !chosen) {
                  entry.status.textContent = "Choose the application first.";
                  picker.focus();
                  return;
                }
                decide("waiting", action, "approve", chosen && chosen !== action.subject_id ? { subject_id: chosen } : null);
              }),
              actionButton("Reject", "danger-button", () => decide("waiting", action, "reject")),
            );
            row.appendChild(buttons);
            list.appendChild(row);
          } else if (key === "shadow") {
            const row = actionRow(action, reasoning(action));
            if (action.review) {
              const verdict = element("p", "automation-verdict", `You marked this the ${action.review} call.`);
              verdict.tabIndex = -1;
              row.appendChild(verdict);
            } else {
              const buttons = element("div", "automation-action-buttons");
              buttons.append(
                actionButton("Right call", "secondary-button", () => decide("shadow", action, "review", { verdict: "right" })),
                actionButton("Wrong call", "secondary-button", () => decide("shadow", action, "review", { verdict: "wrong" })),
              );
              row.appendChild(buttons);
            }
            list.appendChild(row);
          } else {
            const when = action.applied_at || action.decided_at || action.created_at;
            const row = actionRow(action, [automationFeatureLabel(action.feature), formatDateTime(when)]);
            const [text, tone] = AUTOMATION_STATUS_CHIPS[action.status] || [humanizeKey(action.status), ""];
            const buttons = element("div", "automation-action-buttons");
            buttons.appendChild(chip(text, tone));
            if (action.status === "applied" && canUndo(action)) {
              const undo = actionButton("Undo", "secondary-button", () => decide("recent", action, "undo"));
              undo.setAttribute("aria-label", `Undo: ${action.summary || "this automatic change"}`);
              buttons.appendChild(undo);
            }
            const captureId = action.action_type === "application.capture_proposal" && action.status === "applied" ? action.after?._result?.capture_id : null;
            if (captureId) {
              // The draft an approved capture opened, for when its dialog was closed before confirming.
              const open = actionButton("Open capture draft", "secondary-button", () => openDraftFromRecent(entry, captureId));
              open.setAttribute("aria-label", `Open capture draft: ${action.summary || "this role"}`);
              buttons.appendChild(open);
            }
            row.appendChild(buttons);
            if (action.note) row.appendChild(element("p", "automation-meta", action.note));
            list.appendChild(row);
          }
        });
        entry.host.appendChild(list);
        if (items.length < total) {
          entry.host.appendChild(element("p", "automation-meta automation-more", `Showing the newest ${items.length.toLocaleString()} of ${total.toLocaleString()}.`));
        }
      });
      restorePickers(carried);
      syncRowButtons();
    }

    // Reads everything again. Only the newest reload paints, whichever answer
    // arrives first. True when the section shows the server's answer.
    let reloads = 0;
    async function reload() {
      const ticket = ++reloads;
      const read = startAutomationRead();
      try {
        const [nextOverview, ...nextLists] = await Promise.all([api("/api/v1/automation"), ...automationListRequests()]);
        if (ticket !== reloads) return true;
        // A pause or switch saved while this was on its way is newer than this
        // answer, and has already been shown (applyAutomationRead drops it). The
        // job-email block follows this answer only when it is still the latest
        // word: onMail painted it then.
        applyAutomationRead(read, nextOverview);
        notices = Array.isArray(nextOverview.notices) ? nextOverview.notices : [];
        AUTOMATION_LIST_KEYS.forEach((key, index) => { data[key] = automationList(nextLists[index]); });
        paintNotices();
        paintLists();
        syncDomains();
        syncEmailCards();
        return true;
      } catch (error) {
        if (ticket === reloads && !isAuthError(error)) showError(error.message);
        return false;
      }
    }

    // An email card whose proposals were all decided here is settled on the server; it leaves the page.
    async function syncEmailCards() {
      const cards = [...document.querySelectorAll(".monitored-event[data-event-id]")];
      if (!cards.length) return;
      try {
        const events = await api("/api/v1/monitored-events");
        const pending = new Set((events.items || []).filter((item) => item.status === "pending").map((item) => item.id));
        cards.forEach((card) => { if (!pending.has(card.dataset.eventId)) card.remove(); });
        document.querySelectorAll(".monitored-event-list").forEach((list) => { if (!list.childElementCount) list.remove(); });
      } catch (_) {
        // The cards stay; a decision on one that was settled says so.
      }
    }

    // An automatic change announced while this section is showing refreshes its lists,
    // Auto-passed this week included (a role auto_pass passed, or its Undo from the announcement).
    automationStatus.reloadLists = () => (section.isConnected ? Promise.all([reload(), loadAutoPassed()]).then(([shown]) => shown) : null);

    paintNotices();
    paintLists();
    paintMail();
    section.append(mailBlock, healthBlock, noticeBlock, lists.waiting.wrap, lists.shadow.wrap, lists.recent.wrap, domainBlock);
    return section;
  }

  // Who made each change in an application's timeline, from the source its
  // event recorded. Nothing automatic is ever labelled as the student's own.
  // An automatic change reads "Automatic" until its action is looked up
  // (labelAutomaticChanges), which can tell one the student approved.
  function changeAuthor(source, ats = atsName()) {
    const value = typeof source === "string" ? source : "";
    if (!value || value === "user") return "You";
    if (value.startsWith("automation-undo:")) return "Undone by you";
    if (value.startsWith("automation:")) return "Automatic";
    if (value === "extension_user_confirmed" || value === "explicit_user_confirmation") return "Extension (you confirmed)";
    if (value.startsWith("monitored_event:")) return "From an email (you confirmed)";
    if (value.startsWith("agent_proposal:")) return "Agent (you approved)";
    if (value === "application_import") return "Imported";
    if (value === "apply_agent:confirmation_email") return "The confirmation email";
    if (value === "apply_agent:student_confirmed") return "You confirmed";
    if (value === "apply_agent:confirmation_page") return `${ats}'s confirmation page`;
    if (value === "apply_agent:watch") return "The app, on its own";
    return humanizeKey(value.split(":")[0]);
  }

  // Whether the student approved this action before it was applied. decided_by
  // alone cannot say: an undo records decided_by 'student' too, and would
  // credit the student with approving a change they only took back. A change
  // the app made itself is applied the moment it is recorded (perform writes
  // created_at and applied_at from one timestamp); an approved one later.
  function approvedByStudent(action) {
    return action?.decided_by === "student" && Boolean(action.applied_at) && action.applied_at !== action.created_at;
  }

  // The actions behind a timeline's automatic changes. One is looked up on its
  // own; several come from one list of the actions that can have written an
  // event, and only those not in it are looked up one by one.
  async function automationActionsById(ids) {
    const found = new Map();
    if (ids.length > 1) {
      try {
        const payload = await api("/api/v1/automation/actions?status=applied,undone,superseded&limit=200");
        (payload.items || []).forEach((action) => { if (ids.includes(action.id)) found.set(action.id, action); });
      } catch (_) {
        // Each is looked up on its own below.
      }
    }
    await Promise.all(ids.filter((id) => !found.has(id)).map(async (id) => {
      try {
        found.set(id, await api(`/api/v1/automation/actions/${encodeURIComponent(id)}`));
      } catch (_) {
        // Left as "Automatic", with no Undo.
      }
    }));
    return found;
  }

  // Labels each automatic change by its action, and gives one still in place
  // an Undo beside its newest event. ``automatic`` maps an action id to the
  // author lines of its events, newest first.
  async function labelAutomaticChanges(applicationId, automatic) {
    const actions = await automationActionsById([...automatic.keys()]);
    automatic.forEach((hosts, actionId) => {
      const action = actions.get(actionId);
      if (!action) return;
      const domain = typeof action.evidence?.sender_domain === "string" ? action.evidence.sender_domain : "";
      // A sender Gmail did not vouch for is only ever "claiming to be" that domain.
      const verified = action.evidence?.auth?.ok !== false;
      const from = !domain ? "Automatic"
        : verified ? `Automatic: from an email by ${domain}` : `Automatic: from an email claiming to be from ${domain} (sender not verified)`;
      hosts.forEach((host) => {
        const author = host.querySelector(".timeline-author");
        if (author) author.textContent = approvedByStudent(action) ? `${from}, approved by you` : from;
      });
      const [host] = hosts;
      if (!host.isConnected || action.status !== "applied" || !canUndo(action)) return;
      const undo = element("button", "text-button timeline-undo", "Undo");
      undo.type = "button";
      undo.setAttribute("aria-label", `Undo this automatic change: ${action.summary || action.action_type}`);
      undo.addEventListener("click", async () => {
        undo.disabled = true;
        let message;
        try {
          const result = await automationWrite(() => api(`/api/v1/automation/actions/${encodeURIComponent(action.id)}/undo`, { method: "POST" }));
          message = withUndoNote(result.feature_paused ? automationBreakerMessage(action.feature, result.breaker_notice) : "Undid the automatic change.", result);
        } catch (error) {
          if (isAuthError(error)) return;
          message = error.message;
        }
        announce(message);
        refreshAutomationStatus();
        if (state.view !== "applications") return;
        state.trackerStatus = { id: applicationId, message };
        state.applicationFocus = applicationId;
        try {
          await Promise.all([loadApplications(), loadStats()]);
        } catch (error) {
          showError(error.message);
        }
      });
      host.append(" ", undo);
    });
  }

  Object.assign(App, {
    AUTOMATION_LIST_KEYS, automationBreakerMessage, automationCheckbox, automationFeature, automationFields,
    automationHealthList, automationListRequests, automationSection, changeAuthor, labelAutomaticChanges,
    savedAutomationMode, syncAutomationControls, withUndoNote,
  });
})();
