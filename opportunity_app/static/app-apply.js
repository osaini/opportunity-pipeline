// Apply for me: the forms that answer what the app could not, the settings blocks, and the links in them.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { els, state } = App;

  // From app-ui.js.
  const {
    appendLinks, element, externalLink, formatWeekdayDateTime, humanizeKey, optionElement, webAddresses, whenPresent,
  } = App;

  // From app-http.js.
  const { AUTH_REQUIRED, api, isAuthError } = App;

  // From app-automation.js.
  const { savedAutomationMode } = App;

  // Defined in files that load later; looked up when called.
  const closeDetail = (...args) => App.closeDetail(...args);

  // Apply for me (apply/policy.py, apply/preflight.py): what the app would fill on a saved Greenhouse role, and what it
  // still needs from you. It only reads: opening this section changes nothing in your tracker, and it shows no answer
  // you gave, only the questions, where each answer would come from, and what is missing.
  const APPLY_NOTE = "Nothing in your tracker has changed. Filling the form in a window comes in a later step.";

  function applyAnswerForm(problem, company, onSaved) {
    const action = problem.action;
    const form = element("form", "apply-answer-form");
    const id = `apply-answer-${problem.key}`;
    const label = element("label", "profile-field");
    label.appendChild(element("span", "", "Your answer"));
    let read;
    let write;
    if (action.control === "select" || action.control === "checkbox") {
      const select = document.createElement("select");
      select.id = id;
      select.appendChild(optionElement("", action.control === "select" ? "Choose an option" : "Choose yes or no"));
      (action.control === "select" ? action.options : ["Yes", "No"]).forEach((option) => {
        select.appendChild(optionElement(option, option));
      });
      label.appendChild(select);
      form.appendChild(label);
      read = () => select.value;
      write = (value) => { select.value = value; };
    } else if (action.control === "multiselect") {
      const group = element("fieldset", "apply-options");
      group.appendChild(element("legend", "", "Your answer (choose every option that applies)"));
      const boxes = action.options.map((option) => {
        const row = element("label", "confirmation-row");
        const box = document.createElement("input");
        box.type = "checkbox";
        box.value = option;
        row.append(box, element("span", "", option));
        group.appendChild(row);
        return box;
      });
      form.appendChild(group);
      read = () => boxes.filter((box) => box.checked).map((box) => box.value);
      write = (value) => boxes.forEach((box) => { box.checked = value.includes(box.value); });
    } else {
      const control = action.control === "text" ? document.createElement("input") : document.createElement("textarea");
      if (action.control === "text") control.type = "text";
      control.id = id;
      control.maxLength = 10000;
      label.appendChild(control);
      form.appendChild(label);
      read = () => control.value;
      write = (value) => { control.value = value; };
    }
    // Apply for me keeps an answer for one company: nothing here carries an answer to another employer.
    form.appendChild(element("p", "profile-help", "This answer is saved for this company only."));
    const save = element("button", "secondary-button", "Save and use for this question");
    save.type = "submit";
    const status = element("p", "form-status");
    status.setAttribute("role", "status");
    form.append(save, status);
    // What is typed and not yet saved, so the list can be rebuilt after another answer is saved without losing it.
    form.applyDraft = {
      read() {
        const answer = read();
        return (Array.isArray(answer) ? answer.length : answer) ? { answer } : null;
      },
      write(draft) {
        write(draft.answer);
      },
    };
    // Busy is aria-disabled, not disabled: a disabled button that has focus drops it to the page, outside the open panel.
    let saving = false;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (saving) return;
      const answer = read();
      if (!answer || (Array.isArray(answer) && !answer.length)) {
        status.textContent = "Give an answer first.";
        return;
      }
      saving = true;
      save.setAttribute("aria-disabled", "true");
      status.textContent = "Saving…";
      try {
        const saved = await api(`/api/v1/apply-agent/opportunities/${encodeURIComponent(problem.opportunityId)}/answers`, {
          method: "POST",
          body: JSON.stringify({ key: problem.key, answer, posting_confirmed: Boolean(problem.postingConfirmed?.()) }),
        });
        onSaved(saved.check, `Saved for ${company}.`);
      } catch (error) {
        saving = false;
        save.removeAttribute("aria-disabled");
        if (!isAuthError(error)) status.textContent = error.message;
      }
    });
    return form;
  }

  function applyLabelForm(problem, onSaved) {
    const action = problem.action;
    const form = element("form", "apply-answer-form");
    const label = element("label", "profile-field");
    label.appendChild(element("span", "", "Exact option, as the form lists it"));
    const input = document.createElement("input");
    input.type = "text";
    input.maxLength = 200;
    input.value = action.suggestion || "";
    label.appendChild(input);
    const save = element("button", "secondary-button", "Save this option");
    save.type = "submit";
    const status = element("p", "form-status");
    status.setAttribute("role", "status");
    form.append(label, element("p", "profile-help", "You typed this, so the app has not checked it against the form yet. It only uses an option the form really lists, word for word."), save, status);
    // Kept across a rebuild of the list, but only when the student changed it from the suggestion.
    form.applyDraft = {
      read: () => (input.value !== (action.suggestion || "") ? { answer: input.value } : null),
      write: (draft) => { input.value = draft.answer; },
    };
    let saving = false;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (saving) return;
      if (!input.value.trim()) {
        status.textContent = "Type the option first.";
        return;
      }
      saving = true;
      save.setAttribute("aria-disabled", "true");
      status.textContent = "Saving…";
      try {
        await api(`/api/v1/apply-agent/ats-labels/${encodeURIComponent(action.field)}`, { method: "PUT", body: JSON.stringify({ label: input.value }) });
        onSaved(null, "Saved.");
      } catch (error) {
        saving = false;
        save.removeAttribute("aria-disabled");
        if (!isAuthError(error)) status.textContent = error.message;
      }
    });
    return form;
  }

  // The addresses a statement points to, as links the student can read before agreeing. Only web addresses become links.
  function applyLinkList(links) {
    const safe = webAddresses(links);
    if (!safe.length) return null;
    const line = element("p", "profile-help");
    appendLinks(line, safe, "This statement links to ");
    return line;
  }

  // Needs you, for a sensitive question the student allowed the app to answer (apply/sensitive.py). Only the answer and the
  // consent tick go to the server: it takes the category, the wording and the options from the form it read.
  function applySensitiveForm(problem, company, onSaved) {
    const action = problem.action;
    const form = element("form", "apply-answer-form apply-sensitive-form");
    const id = `apply-sensitive-${problem.key}`;
    let read;
    let write;
    // A statement is stored only as ticked. It sits on a box, or on a Yes/No question whose question and description are the
    // statement: either way the student reads the statement and its links first, and ticks once, never picks between Yes and No.
    const isBox = action.control === "checkbox";
    const yesNo = action.control === "select" && ["acknowledgment", "consent"].includes(action.category);
    const ticked = isBox || yesNo;
    if (ticked) {
      form.appendChild(element("p", "apply-statement", action.statement || problem.question));
      const links = applyLinkList(action.links);
      if (links) form.appendChild(links);
      const row = element("label", "confirmation-row");
      const agree = document.createElement("input");
      agree.type = "checkbox";
      const yes = (action.options || []).find((option) => option.trim().toLowerCase() === "yes") || "Yes";
      row.append(agree, element("span", "", isBox ? "Yes, tick this statement for me on the form" : `Yes, choose ${yes} for me on the form`));
      form.appendChild(row);
      read = () => (agree.checked ? (isBox ? true : yes) : "");
      write = (value) => { agree.checked = Boolean(value); };
    } else if (action.control === "select" || action.control === "multiselect") {
      const group = element("fieldset", "apply-options");
      const legend = action.decline_only ? "The app stores only a decline answer here" : "Your answer";
      group.appendChild(element("legend", "", legend));
      // Several questions on one form each have a group of options: the name says which question this one answers.
      group.setAttribute("aria-label", `${legend}: ${problem.question}`);
      const boxes = action.options.map((option) => {
        const row = element("label", "confirmation-row");
        const box = document.createElement("input");
        box.type = action.control === "select" ? "radio" : "checkbox";
        box.name = id;
        box.value = option;
        row.append(box, element("span", "", option));
        group.appendChild(row);
        return box;
      });
      form.appendChild(group);
      read = () => {
        const chosen = boxes.filter((box) => box.checked).map((box) => box.value);
        return action.control === "select" ? (chosen[0] || "") : chosen;
      };
      write = (value) => boxes.forEach((box) => { box.checked = [].concat(value).includes(box.value); });
    } else {
      const label = element("label", "profile-field");
      label.appendChild(element("span", "", "Your answer"));
      const control = document.createElement("input");
      control.type = "text";
      control.id = id;
      control.maxLength = 500;
      control.setAttribute("aria-label", `Your answer: ${problem.question}`);
      label.appendChild(control);
      form.appendChild(label);
      read = () => control.value;
      write = (value) => { control.value = value; };
    }
    let everyone = null;
    if (action.company_only) {
      form.appendChild(element("p", "profile-help", `This is saved for ${company} only.`));
    } else {
      const row = element("label", "confirmation-row");
      everyone = document.createElement("input");
      everyone.type = "checkbox";
      row.append(everyone, element("span", "", "Use for any company"));
      form.appendChild(row);
    }
    // The consent is its own tick, never on by default, and it says what the answer may be used for.
    const consent = element("label", "confirmation-row apply-consent");
    const consentBox = document.createElement("input");
    consentBox.type = "checkbox";
    consent.append(consentBox, element("span", "", action.consent_text));
    form.appendChild(consent);
    const save = element("button", "secondary-button", "Save this answer");
    save.type = "submit";
    const status = element("p", "form-status");
    status.setAttribute("role", "status");
    form.append(save, status);
    form.applyDraft = {
      read() {
        const answer = read();
        return (Array.isArray(answer) ? answer.length : answer) ? { answer, everyone: Boolean(everyone?.checked), consent: consentBox.checked } : null;
      },
      write(draft) {
        write(draft.answer);
        if (everyone) everyone.checked = draft.everyone;
        consentBox.checked = draft.consent;
      },
    };
    // Busy is aria-disabled, not disabled: a disabled button that has focus drops it to the page, outside the open panel.
    let saving = false;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (saving) return;
      const answer = read();
      if (!answer || (Array.isArray(answer) && !answer.length)) {
        status.textContent = ticked ? "Tick the statement first." : "Give an answer first.";
        return;
      }
      if (!consentBox.checked) {
        status.textContent = "Tick the box that says how the app may use this answer.";
        return;
      }
      saving = true;
      save.setAttribute("aria-disabled", "true");
      status.textContent = "Saving…";
      try {
        const saved = await api(`/api/v1/apply-agent/opportunities/${encodeURIComponent(problem.opportunityId)}/sensitive-answers`, {
          method: "POST",
          body: JSON.stringify({
            key: problem.key, answer, consent: true, any_company: Boolean(everyone?.checked),
            posting_confirmed: Boolean(problem.postingConfirmed?.()),
          }),
        });
        onSaved(saved.check, "Saved.");
      } catch (error) {
        saving = false;
        save.removeAttribute("aria-disabled");
        if (!isAuthError(error)) status.textContent = error.message;
      }
    });
    return form;
  }

  function applyProblemAction(problem, company, onSaved) {
    const action = problem.action || {};
    if (action.type === "answer") return applyAnswerForm(problem, company, onSaved);
    if (action.type === "sensitive") return applySensitiveForm(problem, company, onSaved);
    if (action.type === "ats_label" && action.field) return applyLabelForm(problem, onSaved);
    if (action.type === "profile") {
      const open = element("button", "secondary-button", action.field === "name_parts" ? "Add your name for applications" : "Open your profile");
      open.type = "button";
      // The Profile page opens behind the role, so leave the role first, then land on the box that is missing.
      const target = { name_parts: 'input[name="name_parts_first"]', contact: 'input[name="contact_email"]' }[action.field];
      open.addEventListener("click", () => {
        closeDetail();
        els.profileNav.click();
        // The page loads its form after it opens, so look for the box for a few seconds.
        whenPresent(() => (target ? document.querySelector(`.profile-form ${target}`) : null), (field) => {
          field.scrollIntoView({ block: "center" });
          field.focus({ preventScroll: true });
        });
      });
      return open;
    }
    if (action.type === "library") {
      // Two saved answers disagree, and only the student can say which one is right: the answer library is where they are kept.
      const open = element("button", "secondary-button", "Open your saved answers");
      open.type = "button";
      open.addEventListener("click", () => {
        // The answer library lives on the Prepare page, behind the open role, so leave the role first.
        closeDetail();
        els.prepareNav.click();
        // The page loads its sections after it opens, so look for the heading for a few seconds.
        whenPresent(() => [...document.querySelectorAll("h3")].find((node) => node.textContent === "Answer library"), (heading) => {
          heading.tabIndex = -1;
          heading.scrollIntoView({ block: "start" });
          heading.focus({ preventScroll: true });
        });
      });
      return open;
    }
    if (action.type === "resume") {
      const open = element("button", "secondary-button", action.chooser ? "Choose a résumé for this role" : "Go to the résumé section");
      open.type = "button";
      open.addEventListener("click", () => {
        const target = document.querySelector(".resume-pick");
        target?.scrollIntoView({ block: "center" });
        target?.querySelector("select")?.focus();
      });
      return open;
    }
    return null;
  }

  // Which Greenhouse posting the check read and when, and a warning (with a tick to go on) when it does not look like the saved role.
  function applyPostingLine(body, result, remember, confirmed) {
    const posting = result.posting;
    if (!posting || !(posting.title || posting.company)) return;
    const line = element("p", "apply-source profile-help");
    const words = [posting.title, posting.company].filter(Boolean).join(" at ");
    line.append("Read from ");
    if (posting.url) {
      const link = externalLink(posting.url, words);
      line.appendChild(link);
    } else {
      line.append(words);
    }
    const when = result.from_cache ? "a copy kept for up to an hour" : formatWeekdayDateTime(result.checked_at);
    line.append(` on Greenhouse${when ? `, ${when}` : ""}.`);
    body.appendChild(line);
    if (!posting.differs) return;
    const warning = element("div", "apply-mismatch");
    warning.appendChild(element("p", "apply-limit", `This may not be your role. ${posting.difference}.`));
    const row = element("label", "confirmation-row");
    const tick = document.createElement("input");
    tick.type = "checkbox";
    tick.checked = confirmed;
    tick.addEventListener("change", () => remember(tick.checked));
    row.append(tick, element("span", "", "This is the right posting"));
    warning.appendChild(row);
    body.appendChild(warning);
  }

  function applyForMeSection(item) {
    // Only a saved role, and only for a student who turned Apply for me on: nobody else's page asks Greenhouse anything.
    if (item.intent_state !== "saved" || savedAutomationMode("apply_agent") !== "on") return null;
    const section = element("section", "detail-section apply-for-me");
    section.dataset.applyForMe = "";
    // Shown once there is something to say. A role that is not on Greenhouse says so; the switch turned off meanwhile says nothing.
    section.hidden = true;
    section.appendChild(element("p", "eyebrow", "Apply for me"));
    const summary = element("p", "apply-summary", "Checking the Greenhouse form…");
    summary.setAttribute("role", "status");
    const body = element("div", "apply-body");
    section.append(summary, body);
    const stale = () => state.detailItem !== item || !section.isConnected;
    let settled = false;
    const slow = setTimeout(() => { if (!settled && !stale()) section.hidden = false; }, 400);
    // The student's word that the form Greenhouse returned is this role's, when it did not look like it.
    let postingConfirmed = false;

    function paint(result, saved = "") {
      settled = true;
      section.hidden = false;
      summary.textContent = saved ? `${saved} ${result.message}` : result.message;
      // The button that was pressed is gone after a save: keep the place, and let the status be read out.
      if (saved) {
        summary.tabIndex = -1;
        summary.focus({ preventScroll: true });
      }
      // Whatever is typed into another question and not yet saved comes back after the list is rebuilt below.
      const drafts = new Map();
      body.querySelectorAll("li[data-apply-key]").forEach((row) => {
        const draft = row.querySelector("form")?.applyDraft?.read();
        if (draft) drafts.set(row.dataset.applyKey, draft);
      });
      body.replaceChildren();
      if (result.status === "unavailable" || result.status === "failed") return;
      (result.asks || []).forEach((ask) => body.appendChild(element("p", "apply-limit", `Before you go on: ${ask.message}`)));
      const stopped = result.eligibility?.handoff;
      if (stopped && !stopped.allowed && stopped.reason) body.appendChild(element("p", "apply-limit", `Not right now: ${stopped.reason}`));
      applyPostingLine(body, result, (confirmed) => { postingConfirmed = confirmed; }, postingConfirmed);
      // Questions the app has a control for come first. The ones it never answers for the student are grouped apart as hers to do on the form.
      const problems = result.problems || [];
      const groups = [[problems.filter((problem) => problem.action?.type !== "manual"), ""], [problems.filter((problem) => problem.action?.type === "manual"), "Left for you"]];
      groups.forEach(([group, heading]) => {
        if (!group.length) return;
        if (heading) body.appendChild(element("p", "apply-group", `${heading}: the app leaves these to you on the Greenhouse form.`));
        const list = element("ul", "apply-problems");
        group.forEach((problem) => {
          const row = element("li", "apply-problem");
          row.dataset.applyKey = problem.key;
          row.appendChild(element("strong", "", problem.required ? problem.question : `${problem.question} (optional)`));
          row.appendChild(element("p", "profile-help", problem.message));
          // A kind of question the student could let the app answer says where, so it is not mistaken for a never.
          if (problem.action?.allowable) row.appendChild(element("p", "profile-help", "You can let the app answer this kind of question, once you add the answer yourself, in Apply for me settings under Automation."));
          const control = applyProblemAction({ ...problem, opportunityId: item.id, postingConfirmed: () => postingConfirmed }, result.company, (fresh, message) => {
            if (fresh) paint(fresh, message);
            else load(message);
          });
          if (control) {
            if (drafts.has(problem.key)) control.applyDraft?.write(drafts.get(problem.key));
            row.appendChild(control);
          }
          list.appendChild(row);
        });
        body.appendChild(list);
      });
      // Optional sensitive questions (voluntary self-identification, mostly) the student allowed the app to answer and has not yet.
      const optional = result.optional_sensitive || [];
      if (optional.length) {
        const details = element("details", "apply-fields apply-optional");
        details.open = optional.some((entry) => drafts.has(entry.key));
        details.appendChild(element("summary", "", `${optional.length} optional question${optional.length === 1 ? "" : "s"} the app could answer for you`));
        const list = element("ul", "apply-problems");
        optional.forEach((entry) => {
          const row = element("li", "apply-problem");
          row.dataset.applyKey = entry.key;
          row.appendChild(element("strong", "", `${entry.question} (optional)`));
          row.appendChild(element("p", "profile-help", "The app leaves this blank unless you add an answer here."));
          const control = applySensitiveForm({ ...entry, opportunityId: item.id, postingConfirmed: () => postingConfirmed }, result.company, (fresh, message) => {
            if (fresh) paint(fresh, message);
            else load(message);
          });
          if (drafts.has(entry.key)) control.applyDraft?.write(drafts.get(entry.key));
          row.appendChild(control);
          list.appendChild(row);
        });
        details.appendChild(list);
        body.appendChild(details);
      }
      const fields = result.fields || [];
      if (fields.length) {
        const details = element("details", "apply-fields");
        details.appendChild(element("summary", "", `What the app would do with each of the ${fields.length} fields`));
        const list = element("ul", "reason-list");
        fields.forEach((field) => {
          const words = field.source ? `from ${field.source.charAt(0).toLowerCase()}${field.source.slice(1)}` : (field.note || "left blank");
          const line = element("li", "", `${field.question}${field.required ? "" : " (optional)"}: ${words}`);
          // A ticked statement shows the address of the document it links to, so the student can see what is agreed to.
          appendLinks(line, webAddresses(field.links), " · links to ");
          list.appendChild(line);
        });
        details.appendChild(list);
        body.appendChild(details);
      }
      body.appendChild(element("p", "apply-note", APPLY_NOTE));
    }

    async function load(saved = "") {
      try {
        const result = await api(`/api/v1/apply-agent/opportunities/${encodeURIComponent(item.id)}/check`);
        if (!stale()) paint(result, typeof saved === "string" ? saved : "");
      } catch (error) {
        clearTimeout(slow);
        if (stale()) return;
        // Turned off since the page opened (409), or not runnable in this app (503): nothing to show.
        if ([409, 503].includes(error.status)) {
          section.remove();
          return;
        }
        if (isAuthError(error)) return;
        settled = true;
        section.hidden = false;
        summary.textContent = `The Apply for me check could not run: ${error.message}`;
      }
    }

    load();
    return section;
  }

  // The answers the app may give on sensitive questions (apply/sensitive.py): which kinds the student switched on, what they
  // stored, and a form to add one. Nothing here is answered for the student until they switch a kind on and add the answer.
  const APPLY_NO_ROLE_MATCH = "None of your roles is at a company with this name, so no role will use this answer yet. Check the name against the one on the role.";

  function applySensitiveSettings() {
    const host = element("div", "apply-sensitive-settings");
    const status = element("p", "form-status");
    status.setAttribute("role", "status");
    // Whatever the student is in the middle of survives a repaint: the kinds they have switched (their changes go to the server
    // one after another, each built from the switches as they stand, so a quick second click cannot undo the first), the Add an
    // answer fields, and the control that had focus (found again by its data-focus name).
    const desired = new Map();
    let pending = 0;
    let queue = Promise.resolve();
    const draft = { kind: "", question: "", answer: "", company: "", links: "", consent: false, tick: false };

    function paint(data) {
      const focusName = host.contains(document.activeElement) ? document.activeElement.dataset.focus || "" : "";
      build(data);
      if (!focusName) return;
      const target = host.querySelector(`[data-focus="${CSS.escape(focusName)}"]`)
        // A removed answer's button is gone: land on the list's heading rather than the top of the page.
        || (focusName.startsWith("remove-") ? host.querySelector('[data-focus="entries-heading"]') : null);
      if (target) target.focus({ preventScroll: true });
    }

    // A change to the kinds is sent after the ones before it, and the page shows the server's answer once the last one is back.
    function switchKinds(groups) {
      pending += 1;
      queue = queue.then(async () => {
        const categories = groups.filter((group) => desired.get(group.key)).flatMap((group) => group.categories);
        let failure = "";
        try {
          const fresh = await api("/api/v1/apply-agent/sensitive-categories", { method: "PUT", body: JSON.stringify({ categories }) });
          if (pending === 1) paint(fresh);
        } catch (error) {
          failure = error.message;
        }
        pending -= 1;
        if (failure && failure !== AUTH_REQUIRED) status.textContent = failure;
        if (failure && !pending) await load();
      });
    }

    function build(data) {
      if (!pending) data.groups.forEach((group) => desired.set(group.key, group.on));
      host.replaceChildren();
      host.appendChild(element("h5", "", "Answers for sensitive questions"));
      host.appendChild(element("p", "profile-help", "Some questions ask about work authorization, visa sponsorship, being 18 or older, or voluntary self-identification (EEO), or ask you to agree to a legal statement. The app never answers these on its own. Switch a kind on, then add the answer yourself. The app uses an answer only to fill in application forms, and never sends it anywhere else."));
      host.appendChild(element("p", "profile-help", "It never answers export control, citizenship, security clearance or salary questions, or any other personal question such as age or birth date. For voluntary self-identification it keeps only a decline answer, never a real one."));
      const kinds = element("fieldset", "apply-kinds");
      kinds.appendChild(element("legend", "", "Kinds of answer the app may give"));
      data.groups.forEach((group) => {
        const row = element("label", "confirmation-row");
        const box = document.createElement("input");
        box.type = "checkbox";
        box.checked = Boolean(desired.get(group.key));
        box.dataset.focus = `kind-${group.key}`;
        box.addEventListener("change", () => {
          desired.set(group.key, box.checked);
          switchKinds(data.groups);
        });
        row.append(box, element("span", "", group.label));
        kinds.appendChild(row);
      });
      host.appendChild(kinds);
      const entriesHeading = element("h5", "", "Answers you added");
      entriesHeading.tabIndex = -1;
      entriesHeading.dataset.focus = "entries-heading";
      host.appendChild(entriesHeading);
      if (!data.entries.length) host.appendChild(element("p", "empty-inline", "No answers added yet."));
      const list = element("ul", "reason-list apply-sensitive-list");
      data.entries.forEach((entry) => {
        const row = element("li", "apply-sensitive-entry");
        row.appendChild(element("strong", "", entry.question));
        const shown = entry.answer_kind === "checkbox" ? "Ticked" : entry.answer.split("\n").join(", ");
        const when = entry.consented_at ? entry.consented_at.slice(0, 10) : "";
        row.appendChild(element("span", "", `${entry.words}. ${shown}. For ${entry.any_company ? "any company" : entry.company}. You agreed to its use on ${when}.${entry.switched_on ? "" : " Switched off, so the app is not using it."}`));
        // A stored company that names none of the student's roles is never used on one: say so rather than leave it looking in force.
        if (entry.matched_roles === 0) row.appendChild(element("p", "profile-help", APPLY_NO_ROLE_MATCH));
        const links = applyLinkList(entry.links);
        if (links) row.appendChild(links);
        const remove = element("button", "secondary-button", "Remove");
        remove.type = "button";
        remove.dataset.focus = `remove-${entry.id}`;
        remove.setAttribute("aria-label", `Remove the stored answer for ${entry.question}, ${entry.any_company ? "for any company" : `for ${entry.company}`}`);
        let removing = false;
        remove.addEventListener("click", async () => {
          if (removing) return;
          removing = true;
          remove.setAttribute("aria-disabled", "true");
          try {
            await api(`/api/v1/apply-agent/sensitive-answers/${encodeURIComponent(entry.id)}`, { method: "DELETE" });
            status.textContent = "Removed.";
            await load();
          } catch (error) {
            removing = false;
            remove.removeAttribute("aria-disabled");
            if (!isAuthError(error)) status.textContent = error.message;
          }
        });
        row.appendChild(remove);
        list.appendChild(row);
      });
      host.appendChild(list);
      const on = data.categories.filter((kind) => data.groups.some((group) => group.on && group.categories.includes(kind.category)));
      if (!on.length) {
        host.append(element("p", "profile-help", "Switch a kind on to add an answer."), status);
        return;
      }
      host.appendChild(element("h5", "", "Add an answer"));
      host.appendChild(element("p", "profile-help", "Type the question exactly as the form shows it. The app fills an answer only when the words are the same. For a statement or a tick box, type its heading and then its text, as the form shows them. Or open a role and answer the question from its list."));
      const form = element("form", "apply-answer-form");
      const kindLabel = element("label", "profile-field");
      kindLabel.appendChild(element("span", "", "Kind"));
      const kind = document.createElement("select");
      on.forEach((entry) => {
        kind.appendChild(optionElement(entry.category, entry.label));
      });
      kindLabel.appendChild(kind);
      kind.dataset.focus = "add-kind";
      if ([...kind.options].some((option) => option.value === draft.kind)) kind.value = draft.kind;
      const questionLabel = element("label", "profile-field");
      const questionCaption = element("span", "", "Question, or the statement word for word");
      const question = document.createElement("textarea");
      question.rows = 2;
      question.maxLength = 4000;
      questionLabel.append(questionCaption, question);
      question.dataset.focus = "add-question";
      question.value = draft.question;
      const answerLabel = element("label", "profile-field");
      answerLabel.appendChild(element("span", "", "Answer"));
      const answer = document.createElement("input");
      answer.type = "text";
      answer.maxLength = 500;
      answer.setAttribute("list", "apply-decline-examples");
      const examples = document.createElement("datalist");
      examples.id = "apply-decline-examples";
      answerLabel.append(answer, examples);
      answer.dataset.focus = "add-answer";
      answer.value = draft.answer;
      const companyLabel = element("label", "profile-field");
      const companyCaption = element("span", "", "Company (leave empty for any company)");
      const company = document.createElement("input");
      company.type = "text";
      company.maxLength = 200;
      companyLabel.append(companyCaption, company);
      company.dataset.focus = "add-company";
      company.value = draft.company;
      const linksLabel = element("label", "profile-field");
      linksLabel.appendChild(element("span", "", "Addresses the statement links to, one per line (optional)"));
      const links = document.createElement("textarea");
      links.rows = 2;
      linksLabel.appendChild(links);
      links.dataset.focus = "add-links";
      links.value = draft.links;
      // An answer the form shows as a tick box (work authorization, sponsorship, 18 or older) is stored as ticked, or it can
      // never be used: the plan reads a box only as a stored statement.
      const tickRow = element("label", "confirmation-row");
      const tickBox = document.createElement("input");
      tickBox.type = "checkbox";
      tickBox.dataset.focus = "add-tick";
      tickBox.checked = draft.tick;
      tickRow.append(tickBox, element("span", "", "The form shows this as a tick box (type its heading and the box's text as one statement, word for word)"));
      const help = element("p", "profile-help");
      const consent = element("label", "confirmation-row apply-consent");
      const consentBox = document.createElement("input");
      consentBox.type = "checkbox";
      consentBox.dataset.focus = "add-consent";
      consentBox.checked = draft.consent;
      consent.append(consentBox, element("span", "", data.consent_text));
      const add = element("button", "secondary-button", "Save this answer");
      add.type = "submit";
      add.dataset.focus = "add-save";
      // Remember what is typed, so a repaint (another kind switched, an answer removed) does not empty the form.
      const remember = () => Object.assign(draft, {
        kind: kind.value, question: question.value, answer: answer.value, company: company.value, links: links.value, consent: consentBox.checked,
        tick: tickBox.checked,
      });
      form.addEventListener("input", remember);
      form.addEventListener("change", remember);
      function adapt() {
        const chosen = data.categories.find((entry) => entry.category === kind.value) || {};
        const ticked = Boolean(chosen.tickable && tickBox.checked);
        tickRow.hidden = !chosen.tickable;
        answerLabel.hidden = Boolean(chosen.statement) || ticked;
        linksLabel.hidden = !chosen.statement && !ticked;
        companyCaption.textContent = chosen.statement || ticked
          ? "Company (required: a statement or a tick box is kept for one company only)"
          : "Company (leave empty for any company; only a choice from the form's own list is kept for any company)";
        examples.replaceChildren(...(chosen.decline_only ? data.decline_examples : []).map((label) => {
          const option = document.createElement("option");
          option.value = label;
          return option;
        }));
        help.textContent = chosen.decline_only ? "For this kind the app keeps only a decline answer, such as Decline To Self Identify. Use the words the form uses." : "";
      }
      kind.addEventListener("change", adapt);
      tickBox.addEventListener("change", adapt);
      adapt();
      form.append(kindLabel, questionLabel, tickRow, answerLabel, linksLabel, companyLabel, help, consent, add);
      // Busy is aria-disabled, not disabled: a disabled button that has focus drops it to the top of the page.
      let adding = false;
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (adding) return;
        if (!question.value.trim()) {
          status.textContent = "Type the question first.";
          return;
        }
        if (!consentBox.checked) {
          status.textContent = "Tick the box that says how the app may use this answer.";
          return;
        }
        adding = true;
        add.setAttribute("aria-disabled", "true");
        try {
          const chosen = data.categories.find((entry) => entry.category === kind.value) || {};
          const ticked = Boolean(chosen.tickable && tickBox.checked);
          const saved = await api("/api/v1/apply-agent/sensitive-answers", {
            method: "POST",
            body: JSON.stringify({
              category: kind.value, question: question.value, answer: chosen.statement || ticked ? "checked" : answer.value,
              answer_kind: ticked ? "checkbox" : "", company: company.value,
              links: links.value.split("\n").map((line) => line.trim()).filter(Boolean), consent: true,
            }),
          });
          Object.assign(draft, { question: "", answer: "", company: "", links: "", consent: false, tick: false });
          status.textContent = saved.matched_roles === 0 ? `Saved. ${APPLY_NO_ROLE_MATCH}` : "Saved.";
          await load();
        } catch (error) {
          adding = false;
          add.removeAttribute("aria-disabled");
          if (!isAuthError(error)) status.textContent = error.message;
        }
      });
      host.append(form, status);
    }

    async function load() {
      try {
        paint(await api("/api/v1/apply-agent/sensitive-answers"));
      } catch (error) {
        host.replaceChildren(element("p", "form-error", `Answers for sensitive questions could not be loaded: ${error.message}`));
      }
    }

    host.appendChild(element("p", "empty-inline", "Loading…"));
    load();
    return host;
  }

  // The Apply for me settings, in the Automation panel: the limits in force (read only), and the exact option labels
  // for the lists only the form knows (school, location, degree).
  function applyAgentSettingsBlock() {
    const wrap = element("div", "automation-block automation-apply-agent");
    const heading = element("h4", "", "Apply for me settings");
    heading.id = "automation-apply-agent-heading";
    heading.tabIndex = -1;
    const host = element("div", "apply-settings");
    wrap.append(heading, host, applySensitiveSettings());
    const status = element("p", "form-status");
    status.setAttribute("role", "status");
    const LIMIT_WORDS = {
      spacing_minutes: ["Minutes between two applications", "minutes"],
      daily_cap: ["Applications a day", ""],
      company_days: ["Days before applying again to the same company", "days"],
      rehearsals_per_day: ["Rehearsals and option lookups a day", ""],
      rehearsals_before_submit: ["Clean rehearsals before a one click submit", ""],
      unattended_per_hour: ["Unattended applications an hour", ""],
      unattended_daily_cap: ["Unattended applications a day", ""],
    };
    const FIELD_WORDS = {
      location: "Location", school: "School", degree: "Degree", discipline: "Discipline", phone_country: "Phone country",
      education_start_month: "Education start month", education_start_year: "Education start year",
      education_end_month: "Education end month", education_end_year: "Education end year",
    };

    function paint(settings) {
      host.replaceChildren();
      if (settings.requirement) host.appendChild(element("p", "profile-help", `To turn it on: ${settings.requirement}.`));
      const limits = element("ul", "reason-list apply-limits");
      settings.limits.forEach((limit) => {
        const [words, unit] = LIMIT_WORDS[limit.key] || [humanizeKey(limit.key), ""];
        limits.appendChild(element("li", "", `${words}: ${limit.value}${unit ? ` ${unit}` : ""}${limit.overridden ? ` (yours; the default is ${limit.default})` : ""}`));
      });
      host.append(
        element("p", "profile-help", "These limits are in force now. To change one, add it under apply_agent in your profile file (config/profile.json)."),
        limits,
        element("p", "profile-help", `Screenshots of a filled form would be kept for ${settings.evidence_days} days, then deleted. Their fingerprints stay.`),
      );
      host.appendChild(element("h5", "", "Exact options for lists the form owns"));
      host.appendChild(element("p", "profile-help", "Some fields, such as school and location, are lists whose wording only the form knows. Save the exact option once and the app uses it word for word."));
      const labels = Object.entries(settings.ats_labels);
      if (!labels.length) host.appendChild(element("p", "empty-inline", "No options saved yet."));
      const list = element("ul", "reason-list apply-labels");
      labels.forEach(([field, entry]) => {
        const row = element("li", "apply-label");
        row.append(element("span", "", `${FIELD_WORDS[field] || field}: ${entry.label} `));
        const remove = element("button", "secondary-button", "Remove");
        remove.type = "button";
        remove.setAttribute("aria-label", `Remove the saved ${FIELD_WORDS[field] || field} option`);
        remove.addEventListener("click", async () => {
          remove.disabled = true;
          try {
            await api(`/api/v1/apply-agent/ats-labels/${encodeURIComponent(field)}`, { method: "DELETE" });
            status.textContent = "Removed.";
            await load();
          } catch (error) {
            remove.disabled = false;
            if (!isAuthError(error)) status.textContent = error.message;
          }
        });
        row.appendChild(remove);
        list.appendChild(row);
      });
      host.appendChild(list);
      const form = element("form", "apply-answer-form");
      const pick = element("label", "profile-field");
      pick.appendChild(element("span", "", "List"));
      const select = document.createElement("select");
      settings.label_fields.forEach((field) => {
        select.appendChild(optionElement(field, FIELD_WORDS[field] || field));
      });
      pick.appendChild(select);
      const typed = element("label", "profile-field");
      typed.appendChild(element("span", "", "Exact option"));
      const input = document.createElement("input");
      input.type = "text";
      input.maxLength = 200;
      typed.appendChild(input);
      const add = element("button", "secondary-button", "Save this option");
      add.type = "submit";
      form.append(pick, typed, add);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!input.value.trim()) {
          status.textContent = "Type the option first.";
          return;
        }
        add.disabled = true;
        try {
          await api(`/api/v1/apply-agent/ats-labels/${encodeURIComponent(select.value)}`, { method: "PUT", body: JSON.stringify({ label: input.value }) });
          status.textContent = "Saved.";
          await load();
        } catch (error) {
          add.disabled = false;
          if (!isAuthError(error)) status.textContent = error.message;
        }
      });
      host.append(form, status);
    }

    async function load() {
      try {
        paint(await api("/api/v1/apply-agent/settings"));
      } catch (error) {
        host.replaceChildren(element("p", "form-error", `Apply for me settings could not be loaded: ${error.message}`));
      }
    }

    host.appendChild(element("p", "empty-inline", "Loading…"));
    load();
    return wrap;
  }

  Object.assign(App, {
    applyAgentSettingsBlock, applyForMeSection,
  });
})();
