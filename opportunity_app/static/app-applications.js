// The application tracker: cards and their history, the capture dialog, and the board.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { els, registerViewHandlers, state } = App;

  // From app-ui.js.
  const {
    HTTP_ADDRESS, announce, autoSaveSelect, browserTimeZone, chip, element, externalLink, formatCalendarDate, formatDate,
    gmailOpenLink, optionElement, plural, profileField, revealRequested, showError, skeletons, toLocalInputValue,
    uniqueLabels,
  } = App;

  // From app-http.js.
  const { api, errorDetailText } = App;

  // From app-status.js.
  const { loadStats } = App;

  // From app-automation.js.
  const { changeAuthor, labelAutomaticChanges } = App;

  // From app-nav.js.
  const { renderSubnav, runViewLoad } = App;

  // Defined in files that load later; looked up when called.
  const openDetail = (...args) => App.openDetail(...args);

  // What the timeline calls the events Apply for me writes; any other event keeps its own name with the underscores taken out.
  const EVENT_WORDS = {
    apply_agent_started: "Apply for me started",
    apply_agent_submitted: "Submitted with Apply for me",
    apply_agent_verification: "Confirmation email watch",
    apply_agent_resolved: "Apply for me attempt settled",
    apply_agent_unconfirmed: "Apply for me: may have been sent",
  };

  // Finish in browser's events say whose act each step was: the app only filled the form, the student pressed Submit.
  function applyEventWords(event) {
    const detail = event.detail || {};
    if (detail.mode === "handoff" && event.event_type === "apply_agent_started") return "Finish in browser started";
    if (detail.mode === "handoff" && event.event_type === "apply_agent_submitted") {
      return `You submitted in the window${detail.confirmation_path ? " · Greenhouse showed its confirmation page" : ""}`;
    }
    return EVENT_WORDS[event.event_type] || event.event_type.replaceAll("_", " ");
  }

  // Opens the role with the run an event names, so the student can read what that run did.
  function seeRunButton(opportunityId, runId) {
    const button = element("button", "text-button apply-see-run", "See the run");
    button.type = "button";
    button.addEventListener("click", () => {
      state.applyRunRequest = { opportunityId, runId, at: Date.now() };
      openDetail(opportunityId);
    });
    return button;
  }

  // 10.5: what Apply for me did for this application, from item.apply (null when it never touched it). Wording is
  // careful on purpose: only a seen confirmation page, a confirmation email or the student's own word says "applied",
  // and an attempt that may have reached Greenhouse never does.
  function applyBadge(item) {
    const apply = item.apply;
    const ats = apply.ats_name || "Greenhouse";
    const submitted = ["watching", "watch_paused", "email_confirmed", "no_email", "not_watched"].includes(apply.status);
    const uncertain = apply.status === "may_have_been_sent" || apply.status === "no_email";
    const box = element("section", `apply-badge${uncertain ? " is-uncertain" : ""}`);
    box.dataset.applyStatus = apply.status;
    box.setAttribute("aria-label", `Apply for me: ${item.title} at ${item.company}`);
    let title = "";
    if (submitted) title = apply.application_stage === "applied" ? "Applied with Apply for me" : "Submitted with Apply for me";
    else if (apply.status === "submitting") title = `Submitting to ${ats}…`;
    else if (apply.status === "may_have_been_sent") title = `May have been sent. Check your email or the ${ats} portal`;
    else if (apply.status === "filling") title = "Apply for me is filling the form in a window";
    else if (apply.status === "your_turn") title = "Your turn: finish the form in the Chromium window and press Submit application";
    else title = apply.note || "Apply for me stopped before anything was sent";
    box.appendChild(element("p", "apply-badge-title", title));
    const lines = [];
    if (submitted) {
      const confirmed = apply.resolved_by === "student" ? "You said it went through"
        : apply.resolved_by === "email" ? "" : `${ats} showed its confirmation page`;
      if (apply.status === "email_confirmed") {
        lines.push(`${ats}'s confirmation email arrived ${formatDate(apply.email_received_at)}`);
      } else if (apply.status === "watching") {
        lines.push([confirmed, `Looking for its email until ${formatDate(apply.watch_until)}`].filter(Boolean).join(" · "));
      } else if (apply.status === "watch_paused") {
        lines.push([confirmed, `Looking for its email: paused, ${apply.paused_reason || "the job-email check isn't running"}`].filter(Boolean).join(" · "));
      } else if (apply.status === "no_email") {
        lines.push([confirmed, "No confirmation email yet"].filter(Boolean).join(" · "));
        lines.push("Some employers don't send one. If you want to be sure, check the employer's portal or your spam folder.");
      } else if (apply.status === "not_watched") {
        lines.push([confirmed, "The app isn't checking for a confirmation email"].filter(Boolean).join(" · "));
      }
    }
    if (apply.possible_email_at && apply.status !== "email_confirmed" && (submitted || apply.status === "may_have_been_sent")) {
      lines.push(`An email from ${apply.company || "the company"} arrived on ${formatDate(apply.possible_email_at)}; it may be for this application.`);
    }
    lines.forEach((line) => box.appendChild(element("p", "apply-badge-line", line)));
    const actions = element("div", "apply-badge-actions");
    const settle = async (path, body, said) => {
      try {
        await api(`/api/v1/apply-agent/claims/${encodeURIComponent(apply.token)}/${path}`, {
          method: "POST", body: body === undefined ? undefined : JSON.stringify(body),
        });
        await Promise.all([loadApplications(), loadStats()]);
        announce(said);
      } catch (error) {
        showError(error.message);
        await loadApplications().catch(() => {});
      }
    };
    if (apply.ask_mark_applied) {
      const mark = element("button", "secondary-button", "Mark as applied?");
      mark.type = "button";
      mark.addEventListener("click", () => {
        mark.disabled = true;
        settle("mark-applied", undefined, `Marked ${item.title} as applied.`);
      });
      actions.appendChild(mark);
    }
    if (apply.status === "may_have_been_sent") {
      [["It went through", true, `Recorded that ${item.title} went through.`], ["It didn't go through", false, `Recorded that ${item.title} did not go through.`]]
        .forEach(([text, wentThrough, said]) => {
          const button = element("button", "secondary-button", text);
          button.type = "button";
          button.disabled = !apply.can_resolve;
          button.addEventListener("click", () => {
            actions.querySelectorAll("button").forEach((other) => { other.disabled = true; });
            settle("resolve", { went_through: wentThrough }, said);
          });
          actions.appendChild(button);
        });
    }
    // A run in a window is read in the role it belongs to.
    if ((apply.status === "filling" || apply.status === "your_turn") && item.opportunity_id) {
      const open = element("button", "secondary-button", "Open");
      open.type = "button";
      open.setAttribute("aria-label", `Open ${item.title} at ${item.company}`);
      open.addEventListener("click", () => openDetail(item.opportunity_id));
      actions.appendChild(open);
    }
    if (actions.children.length) box.appendChild(actions);
    return box;
  }

  function createApplicationCard(item, { board = false } = {}) {
    const card = element("article", "application-card");
    card.dataset.applicationId = item.id;
    if (board) {
      card.draggable = true;
      card.addEventListener("dragstart", (event) => {
        event.dataTransfer.setData("text/plain", item.id);
        event.dataTransfer.effectAllowed = "move";
      });
    }
    const heading = element("div", "application-heading");
    const identity = element("div");
    identity.appendChild(element("p", "company-name", item.company));
    identity.appendChild(element("h3", "", item.title));
    heading.appendChild(identity);
    heading.appendChild(chip(`${item.score} fit`, "is-region"));

    const facts = element("div", "application-facts");
    uniqueLabels([item.region || "Unknown", item.location || "Location unknown"]).forEach((label) => facts.appendChild(chip(label)));
    if (item.follow_up_at) {
      const followUpDay = formatCalendarDate(item.follow_up_on || item.follow_up_at);
      facts.appendChild(item.follow_up_overdue
        ? chip(`Overdue follow-up · ${followUpDay}`, "is-warning")
        : chip(`Follow up ${followUpDay}`, "is-region"));
    }

    const controls = element("div", "application-controls");
    const label = element("label", "");
    label.appendChild(element("span", "", "Stage"));
    const select = document.createElement("select");
    select.dataset.savedStage = item.stage;
    APPLICATION_STAGES.forEach((stage) => {
      select.appendChild(optionElement(stage, stageLabel(stage), item.stage === stage));
    });
    // Every stage change is audited (and "applied" stamps a date), so keyboard
    // browsing through the list must not save each stage it passes.
    autoSaveSelect(select, {
      saved: () => item.stage,
      commit: async (stage, trigger) => {
        select.disabled = true;
        let moved = false;
        try {
          await api(`/api/v1/applications/${encodeURIComponent(item.id)}`, {
            method: "PATCH",
            body: JSON.stringify({ stage }),
          });
          moved = true;
        } catch (error) {
          select.value = item.stage;
          showError(error.message);
        } finally {
          select.disabled = false;
        }
        if (!moved) return;
        try {
          await Promise.all([loadApplications(), loadStats()]);
        } catch (error) {
          showError(error.message);
        }
        // The board re-renders into a new column; keep keyboard users on this
        // card, unless they already left the select for somewhere else.
        if (trigger !== "blur") els.results.querySelector(`[data-application-id="${CSS.escape(item.id)}"] select`)?.focus();
        announce(`Moved ${item.title} to ${stage}.`);
      },
    });
    label.appendChild(select);
    const source = externalLink(item.url, "Open posting ↗", { className: "application-link" });
    controls.append(label, source);
    const trackerFields = element("form", "tracker-fields");
    const notesLabel = element("label", "");
    notesLabel.appendChild(element("span", "", "Notes"));
    const notes = document.createElement("textarea");
    notes.value = item.notes || "";
    notes.placeholder = "Add private notes";
    notesLabel.appendChild(notes);
    const followLabel = element("label", "");
    followLabel.appendChild(element("span", "", "Follow up"));
    const followUp = document.createElement("input");
    followUp.type = "datetime-local";
    followUp.value = toLocalInputValue(item.follow_up_at);
    const initialFollowUp = followUp.value;
    followLabel.appendChild(followUp);
    const saveTracker = element("button", "secondary-button", "Save details");
    saveTracker.type = "submit";
    const trackerStatus = element("p", "form-status");
    trackerStatus.setAttribute("aria-live", "polite");
    if (state.trackerStatus?.id === item.id) {
      trackerStatus.textContent = state.trackerStatus.message;
      state.trackerStatus = null;
    }
    trackerFields.append(notesLabel, followLabel, saveTracker, trackerStatus);
    trackerFields.addEventListener("submit", async (event) => {
      event.preventDefault();
      saveTracker.disabled = true;
      const body = { notes: notes.value };
      // Omitting follow_up_at means "unchanged" server-side; sending it only on
      // a real edit keeps an untouched save from clearing the reminder.
      if (followUp.value !== initialFollowUp) {
        body.follow_up_at = followUp.value || "";
        body.timezone = browserTimeZone() || "UTC";
      }
      try {
        await api(`/api/v1/applications/${encodeURIComponent(item.id)}`, {
          method: "PATCH",
          body: JSON.stringify(body),
        });
        state.trackerStatus = { id: item.id, message: "Saved." };
        await Promise.all([loadApplications(), loadStats()]);
      } catch (error) {
        trackerStatus.textContent = error.message;
      } finally {
        saveTracker.disabled = false;
      }
    });

    const detail = element("details", "tracker-detail");
    detail.appendChild(element("summary", "", "Tasks, contacts, and timeline"));
    const detailBody = element("div", "tracker-detail-body");
    detail.appendChild(detailBody);
    detail.dataset.opportunityId = item.opportunity_id || "";
    detail.addEventListener("toggle", () => {
      if (detail.open && !detail.dataset.loaded) loadApplicationDetail(item.id, detailBody, detail);
    });
    card.append(heading, facts);
    if (item.apply) card.appendChild(applyBadge(item));
    card.append(controls, trackerFields, detail);
    return card;
  }

  async function loadApplicationDetail(applicationId, container, owner) {
    container.replaceChildren(element("p", "detail-loading", "Loading tracker history…"));
    try {
      const payload = await api(`/api/v1/applications/${encodeURIComponent(applicationId)}`);
      container.replaceChildren();

      const tasks = element("section", "tracker-subsection");
      tasks.appendChild(element("h4", "", "Tasks"));
      const taskList = element("div", "tracker-item-list");
      (payload.tasks || []).forEach((task) => {
        const label = element("label", "tracker-check");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = task.status === "done";
        checkbox.addEventListener("change", async () => {
          checkbox.disabled = true;
          try {
            await api(`/api/v1/application-tasks/${encodeURIComponent(task.id)}`, {
              method: "PATCH",
              body: JSON.stringify({ status: checkbox.checked ? "done" : "open" }),
            });
          } catch (error) {
            checkbox.checked = !checkbox.checked;
            showError(error.message);
          } finally {
            checkbox.disabled = false;
          }
        });
        const taskCopy = element("span", "", task.title);
        if (task.due_at) taskCopy.appendChild(element("small", "", `Due ${formatDate(task.due_at)}`));
        if (task.origin === "email") taskCopy.appendChild(element("small", "", "From an email"));
        label.append(checkbox, taskCopy);
        const safeLink = typeof task.link === "string" && HTTP_ADDRESS.test(task.link) ? task.link : "";
        if (safeLink) {
          // The assessment or scheduling page, kept only here in the app.
          const row = element("div", "tracker-task-row");
          const open = externalLink(safeLink, "Open", { className: "text-button tracker-task-link", ariaLabel: `Open the page for ${task.title}` });
          row.append(label, open);
          taskList.appendChild(row);
          return;
        }
        taskList.appendChild(label);
      });
      if (!payload.tasks?.length) taskList.appendChild(element("p", "empty-inline", "No tasks yet."));
      const taskForm = element("form", "inline-create-form");
      const taskTitle = document.createElement("input");
      taskTitle.placeholder = "New task";
      taskTitle.required = true;
      taskTitle.setAttribute("aria-label", "New task");
      const taskDue = document.createElement("input");
      taskDue.type = "datetime-local";
      taskDue.setAttribute("aria-label", "Task due date");
      const addTask = element("button", "secondary-button", "Add task");
      addTask.type = "submit";
      taskForm.append(taskTitle, taskDue, addTask);
      taskForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        addTask.disabled = true;
        try {
          await api(`/api/v1/applications/${encodeURIComponent(applicationId)}/tasks`, {
            method: "POST",
            body: JSON.stringify({
              title: taskTitle.value,
              due_at: taskDue.value || null,
              timezone: browserTimeZone() || null,
            }),
          });
          owner.dataset.loaded = "";
          await loadApplicationDetail(applicationId, container, owner);
        } catch (error) {
          showError(error.message);
          addTask.disabled = false;
        }
      });
      tasks.append(taskList, taskForm);

      const contacts = element("section", "tracker-subsection");
      contacts.appendChild(element("h4", "", "Contacts"));
      const contactList = element("div", "tracker-item-list");
      (payload.contacts || []).forEach((contact) => {
        const row = element("p", "tracker-contact", contact.name);
        row.appendChild(element("span", "", [contact.role, contact.email, contact.phone].filter(Boolean).join(" · ")));
        contactList.appendChild(row);
      });
      if (!payload.contacts?.length) contactList.appendChild(element("p", "empty-inline", "No contacts yet."));
      const contactForm = element("form", "inline-create-form is-contact");
      const contactName = document.createElement("input");
      contactName.placeholder = "Contact name";
      contactName.required = true;
      const contactRole = document.createElement("input");
      contactRole.placeholder = "Role";
      const contactEmail = document.createElement("input");
      contactEmail.type = "email";
      contactEmail.placeholder = "Email";
      const addContact = element("button", "secondary-button", "Add contact");
      addContact.type = "submit";
      contactForm.append(contactName, contactRole, contactEmail, addContact);
      contactForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        addContact.disabled = true;
        try {
          await api(`/api/v1/applications/${encodeURIComponent(applicationId)}/contacts`, {
            method: "POST",
            body: JSON.stringify({ name: contactName.value, role: contactRole.value, email: contactEmail.value }),
          });
          owner.dataset.loaded = "";
          await loadApplicationDetail(applicationId, container, owner);
        } catch (error) {
          showError(error.message);
          addContact.disabled = false;
        }
      });
      contacts.append(contactList, contactForm);

      // The job emails linked to this application (Update applications from job emails).
      const emails = Array.isArray(payload.emails) ? payload.emails : [];
      let emailSection = null;
      if (emails.length) {
        emailSection = element("section", "tracker-subsection tracker-emails");
        emailSection.appendChild(element("h4", "", "Emails"));
        const list = element("ul", "tracker-email-list");
        emails.forEach((mail) => {
          const row = element("li", "tracker-email");
          row.appendChild(element("span", "tracker-email-subject", mail.subject || "(no subject)"));
          // Only the company matched (its one open application): a guess until the student confirms it.
          const guessed = mail.matched_by === "company_single" ? "Matched by the company name only, not confirmed" : "";
          row.appendChild(element("small", "", [mail.sender_domain, formatDate(mail.received_at), guessed].filter(Boolean).join(" · ")));
          const open = gmailOpenLink(mail.gmail_url, mail.subject || "this email");
          if (open) row.appendChild(open);
          list.appendChild(row);
        });
        emailSection.appendChild(list);
      }

      const timeline = element("section", "tracker-subsection");
      timeline.appendChild(element("h4", "", "Activity timeline"));
      const eventList = element("ol", "timeline-list");
      // Newest first, so the first event an automatic action wrote carries its Undo.
      // Each action id maps to the author line of every event it wrote.
      const automatic = new Map();
      (payload.events || []).forEach((event) => {
        const row = element("li", "");
        row.appendChild(element("strong", "", applyEventWords(event)));
        row.appendChild(element("span", "", formatDate(event.created_at)));
        if (event.from_stage || event.to_stage) row.appendChild(element("p", "", `${event.from_stage || "start"} → ${event.to_stage || "unchanged"}`));
        const source = typeof event.detail?.source === "string" ? event.detail.source : "";
        const who = element("p", "timeline-who");
        who.appendChild(element("span", "timeline-author", changeAuthor(source)));
        row.appendChild(who);
        const runId = event.event_type.startsWith("apply_agent_") ? event.detail?.run_id : "";
        if (typeof runId === "string" && runId && owner.dataset.opportunityId) row.appendChild(seeRunButton(owner.dataset.opportunityId, runId));
        const actionId = /^automation:(.+)$/.exec(source)?.[1];
        if (actionId) automatic.set(actionId, [...(automatic.get(actionId) || []), who]);
        eventList.appendChild(row);
      });
      timeline.appendChild(eventList);
      container.append(...[tasks, contacts, emailSection, timeline].filter(Boolean));
      owner.dataset.loaded = "true";
      if (automatic.size) labelAutomaticChanges(applicationId, automatic);
    } catch (error) {
      container.replaceChildren(element("p", "form-error", error.message));
    }
  }

  function openCaptureDialog() {
    let dialog = document.getElementById("capture-dialog");
    if (!dialog) {
      dialog = element("dialog", "capture-dialog");
      dialog.id = "capture-dialog";
      const close = element("button", "icon-button capture-close", "Ã—");
      close.type = "button";
      close.setAttribute("aria-label", "Close manual capture");
      close.addEventListener("click", () => dialog.close());
      const heading = element("div", "");
      heading.appendChild(element("p", "eyebrow", "Manual capture"));
      heading.appendChild(element("h2", "", "Add a role from elsewhere"));
      heading.appendChild(element("p", "profile-help", "Paste a public job URL or upload a PDF/PNG/JPEG. Nothing enters your tracker until you review and confirm it."));
      const draftHost = element("div", "capture-draft-host");
      const sourceForm = element("form", "capture-source-form");
      const url = document.createElement("input");
      url.type = "url";
      url.placeholder = "https://company.example/jobs/role";
      url.setAttribute("aria-label", "Public job URL");
      const file = document.createElement("input");
      file.type = "file";
      file.accept = ".pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg";
      const fileLabel = element("label", "file-input-label", "Job PDF or screenshot");
      fileLabel.appendChild(file);
      const extract = element("button", "primary-button", "Create review draft");
      extract.type = "submit";
      const sourceStatus = element("p", "form-status");
      sourceStatus.setAttribute("aria-live", "polite");
      sourceForm.append(url, fileLabel, extract, sourceStatus);
      sourceForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!url.value.trim() && !file.files.length) {
          sourceStatus.textContent = "Enter a URL or choose a file.";
          return;
        }
        extract.disabled = true;
        sourceStatus.textContent = "Creating a private draft…";
        try {
          let draft;
          if (file.files.length) {
            const body = new FormData();
            body.append("capture", file.files[0]);
            draft = await api("/api/v1/opportunity-captures/file", { method: "POST", body });
          } else {
            draft = await api("/api/v1/opportunity-captures/url", {
              method: "POST",
              body: JSON.stringify({ url: url.value.trim() }),
            });
          }
          renderCaptureDraft(draft, draftHost, dialog);
          sourceForm.hidden = true;
        } catch (error) {
          sourceStatus.textContent = error.message;
          extract.disabled = false;
        }
      });
      dialog.append(close, heading, sourceForm, draftHost);
      document.body.appendChild(dialog);
    }
    const oldHost = dialog.querySelector(".capture-draft-host");
    oldHost.replaceChildren();
    const sourceForm = dialog.querySelector(".capture-source-form");
    sourceForm.hidden = false;
    sourceForm.reset();
    sourceForm.querySelector("button[type=submit]").disabled = false;
    sourceForm.querySelector(".form-status").textContent = "";
    dialog.showModal();
  }

  // A capture draft made from an email proposal (application.capture_proposal), in
  // the ordinary capture form: nothing enters the tracker until the student confirms.
  async function openCaptureDraft(captureId, onConfirmed) {
    const draft = await api(`/api/v1/opportunity-captures/${encodeURIComponent(captureId)}`);
    if (draft.status && draft.status !== "draft") throw new Error("This role was already added from its capture draft.");
    openCaptureDialog();
    const dialog = document.getElementById("capture-dialog");
    dialog.querySelector(".capture-source-form").hidden = true;
    renderCaptureDraft(draft, dialog.querySelector(".capture-draft-host"), dialog, onConfirmed);
  }

  function renderCaptureDraft(draft, host, dialog, onConfirmed = null) {
    const parsed = draft.parsed || {};
    const form = element("form", "capture-confirm-form");
    form.appendChild(element("p", "missing-note", parsed.extraction_note || "Review every extracted field before confirmation."));
    const company = profileField(form, "Company", "company", parsed.company);
    const title = profileField(form, "Title", "title", parsed.title);
    const url = profileField(form, "Source URL", "url", parsed.url || draft.source_url, { type: "url" });
    const location = profileField(form, "Location", "location", parsed.location);
    const roleType = profileField(form, "Role type", "role_type", "internship");
    const description = profileField(form, "Posting text", "description", parsed.description || draft.extracted_text, { multiline: true });
    company.required = true;
    title.required = true;
    url.required = true;
    const confirm = element("button", "primary-button", "Confirm and add to tracker");
    confirm.type = "submit";
    const statusLine = element("p", "form-status");
    statusLine.setAttribute("aria-live", "polite");
    form.append(confirm, statusLine);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      confirm.disabled = true;
      try {
        await api(`/api/v1/opportunity-captures/${encodeURIComponent(draft.id)}/confirm`, {
          method: "POST",
          body: JSON.stringify({
            company: company.value,
            title: title.value,
            url: url.value,
            location: location.value,
            role_type: roleType.value,
            description: description.value,
          }),
        });
        dialog.close();
        if (onConfirmed) await onConfirmed();
        else await Promise.all([loadApplications(), loadStats()]);
      } catch (error) {
        statusLine.textContent = error.message;
        confirm.disabled = false;
      }
    });
    host.replaceChildren(form);
  }

  // Arriving from Urgent: bring the application the row was about into view.
  function focusRequestedApplication() {
    const id = state.applicationFocus;
    state.applicationFocus = null;
    if (!id) return;
    const card = els.results.querySelector(`.application-card[data-application-id="${CSS.escape(id)}"]`);
    if (!card) return;
    revealRequested(card);
  }

  const APPLICATION_STAGES = ["applying", "applied", "interview", "offer", "rejected", "withdrawn", "archived"];

  function stageLabel(stage) {
    return stage[0].toUpperCase() + stage.slice(1);
  }

  const APPLICATION_TABS = [
    { id: "all", label: "All applications", test: () => true },
    { id: "active", label: "In progress", test: (item) => ["applying", "applied", "interview", "offer"].includes(item.stage) },
    ...APPLICATION_STAGES.map((stage) => ({
      id: stage,
      label: stageLabel(stage),
      group: "Stages",
      tone: stage === "interview" || stage === "offer" ? "is-good" : "",
      test: (item) => item.stage === stage,
    })),
  ];

  // Stage choices browsed with the keyboard but not saved yet, so a reload
  // triggered by another card's save does not throw them away.
  function unsavedStageChoices() {
    return [...els.results.querySelectorAll("[data-application-id] select[data-saved-stage]")]
      .filter((select) => select.value !== select.dataset.savedStage)
      .map((select) => ({
        id: select.closest("[data-application-id]").dataset.applicationId,
        value: select.value,
        focused: select === document.activeElement,
      }));
  }

  function restoreStageChoices(choices) {
    choices.forEach(({ id, value, focused }) => {
      const select = els.results.querySelector(`[data-application-id="${CSS.escape(id)}"] select[data-saved-stage]`);
      if (!select || select.dataset.savedStage === value) return;
      if (![...select.options].some((option) => option.value === value)) return;
      select.value = value;
      if (focused) select.focus();
    });
  }

  async function loadApplications() {
    // The stage choices are read before the skeletons wipe the board.
    await runViewLoad({ views: ["applications"], before: unsavedStageChoices, placeholder: skeletons, tracksLoading: true }, async ({ carried, isCurrent }) => {
      const [payload, analytics] = await Promise.all([
        api("/api/v1/applications"),
        api("/api/v1/applications/analytics"),
      ]);
      if (!isCurrent()) return;
      state.total = payload.total;
      const tab = APPLICATION_TABS.find((entry) => entry.id === state.subtabs.applications) || APPLICATION_TABS[0];
      const shown = payload.items.filter(tab.test);
      renderSubnav(APPLICATION_TABS.map((entry) => ({ ...entry, count: payload.items.filter(entry.test).length })));
      els.results.replaceChildren();
      const toolbar = element("div", "tracker-toolbar");
      const modes = element("div", "display-toggle");
      modes.setAttribute("role", "group");
      modes.setAttribute("aria-label", "Tracker display");
      const boardButton = element("button", state.applicationMode === "board" ? "is-active" : "", "Board");
      boardButton.type = "button";
      boardButton.setAttribute("aria-pressed", String(state.applicationMode === "board"));
      const trackerListButton = element("button", state.applicationMode === "list" ? "is-active" : "", "List");
      trackerListButton.type = "button";
      trackerListButton.setAttribute("aria-pressed", String(state.applicationMode === "list"));
      boardButton.addEventListener("click", () => { state.applicationMode = "board"; loadApplications(); });
      trackerListButton.addEventListener("click", () => { state.applicationMode = "list"; loadApplications(); });
      modes.append(boardButton, trackerListButton);
      const exports = element("div", "tracker-exports");
      const capture = element("button", "secondary-button", "Capture role");
      capture.type = "button";
      capture.addEventListener("click", openCaptureDialog);
      const csvExport = element("a", "secondary-button", "Export CSV");
      csvExport.href = "/api/v1/applications/export?format=csv";
      const jsonExport = element("a", "secondary-button", "Export JSON");
      jsonExport.href = "/api/v1/applications/export?format=json";
      const importInput = document.createElement("input");
      importInput.type = "file";
      importInput.accept = ".csv,.json,text/csv,application/json";
      importInput.className = "sr-only";
      importInput.setAttribute("aria-label", "Import applications from CSV or JSON");
      const importButton = element("button", "secondary-button", "Import");
      importButton.type = "button";
      importButton.addEventListener("click", () => importInput.click());
      importInput.addEventListener("change", async () => {
        if (!importInput.files.length) return;
        importButton.disabled = true;
        const body = new FormData();
        body.append("upload", importInput.files[0]);
        try {
          const result = await api("/api/v1/applications/import", { method: "POST", body });
          // The reload rewrites the page status, so report the import after it.
          await loadApplications();
          if (state.view !== "applications") return;
          const summary = `Imported ${result.imported}; skipped ${result.skipped}`;
          els.pageStatus.textContent = summary;
          announce(`${summary}.`);
          const errors = Array.isArray(result.errors) ? result.errors : [];
          if (errors.length) {
            const report = element("div", "import-report");
            report.setAttribute("role", "status");
            report.appendChild(element("strong", "", `${plural(result.skipped || errors.length, "row was", "rows were")} not imported`));
            const list = element("ul", "");
            errors.forEach((entry) => {
              const detail = errorDetailText(entry && typeof entry === "object" ? entry.detail : entry) || "Not imported.";
              list.appendChild(element("li", "", entry && entry.row ? `Row ${entry.row}: ${detail}` : detail));
            });
            report.appendChild(list);
            if (result.skipped > errors.length) {
              report.appendChild(element("p", "", `Only the first ${errors.length} are listed.`));
            }
            const anchor = els.results.querySelector(".tracker-summary");
            if (anchor) anchor.after(report);
            else els.results.prepend(report);
          }
        } catch (error) {
          showError(error.message);
        } finally {
          importButton.disabled = false;
          importInput.value = "";
        }
      });
      exports.append(capture, csvExport, jsonExport, importButton, importInput);
      toolbar.append(modes, exports);
      els.results.appendChild(toolbar);
      const summary = element("div", "tracker-summary");
      summary.append(
        chip(plural(analytics.open_tasks, "open task", "open tasks"), "is-region"),
        chip(
          `${analytics.overdue_tasks + (analytics.overdue_follow_ups || 0)} overdue`,
          analytics.overdue_tasks + (analytics.overdue_follow_ups || 0) ? "is-warning" : ""
        ),
        chip(plural(analytics.scheduled_follow_ups, "follow-up", "follow-ups")),
        chip(`${plural(analytics.activity_last_30_days, "update", "updates")} / 30 days`)
      );
      summary.title = analytics.interpretation;
      els.results.appendChild(summary);
      if (!shown.length) {
        const empty = element("div", "empty-state");
        empty.appendChild(element("strong", "", payload.items.length ? `Nothing in ${tab.label}` : "No applications yet"));
        empty.appendChild(element("p", "", payload.items.length
          ? "Pick another stage in the list beside the page."
          : "Open Apply on an opportunity and it will appear here."));
        els.results.appendChild(empty);
      } else if (state.applicationMode === "list" || tab.id !== "all") {
        // One stage has no columns to spread across, so it reads as a list.
        shown.forEach((item) => els.results.appendChild(createApplicationCard(item)));
      } else {
        const board = element("div", "application-board");
        APPLICATION_STAGES.forEach((stage) => {
          const column = element("section", "board-column");
          const items = payload.items.filter((item) => item.stage === stage);
          const heading = element("div", "board-column-heading");
          heading.appendChild(element("h3", "", stageLabel(stage)));
          heading.appendChild(chip(String(items.length)));
          column.appendChild(heading);
          const list = element("div", "board-column-list");
          list.dataset.stage = stage;
          list.addEventListener("dragover", (event) => {
            event.preventDefault();
            event.dataTransfer.dropEffect = "move";
          });
          list.addEventListener("drop", async (event) => {
            event.preventDefault();
            const applicationId = event.dataTransfer.getData("text/plain");
            if (!applicationId) return;
            try {
              await api(`/api/v1/applications/${encodeURIComponent(applicationId)}`, {
                method: "PATCH",
                body: JSON.stringify({ stage }),
              });
              await Promise.all([loadApplications(), loadStats()]);
            } catch (error) {
              showError(error.message);
            }
          });
          items.forEach((item) => list.appendChild(createApplicationCard(item, { board: true })));
          if (!items.length) list.appendChild(element("p", "board-empty", "Drop an application here"));
          column.appendChild(list);
          board.appendChild(column);
        });
        els.results.appendChild(board);
      }
      els.resultCount.textContent = plural(payload.total, "application", "applications");
      els.pageStatus.textContent = "Every stage change is kept in the audit history";
      els.results.setAttribute("aria-busy", "false");
      focusRequestedApplication();
      restoreStageChoices(carried);
    });
  }

  // What this file does as it loads, run once by app.js in load order.
  function installApplications() {
    registerViewHandlers("applications", { tabs: () => APPLICATION_TABS, load: loadApplications });
  }

  Object.assign(App, {
    installApplications, loadApplications, openCaptureDraft,
  });
})();
