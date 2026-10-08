// Apply for me: the forms that answer what the app could not, the settings blocks, and the links in them.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { els, registerSessionPoller, state } = App;

  // From app-ui.js.
  const {
    announce, appendLinks, element, externalLink, formatDate, formatWeekdayDateTime, humanizeKey, optionElement, webAddresses,
    whenPresent,
  } = App;

  // From app-http.js.
  const { AUTH_REQUIRED, api, isAuthError } = App;

  // From app-automation.js.
  const { savedAutomationMode } = App;

  // From app-applications.js.
  const { loadApplications } = App;

  // Defined in files that load later; looked up when called.
  const closeDetail = (...args) => App.closeDetail(...args);

  // Apply for me (apply/policy.py, apply/preflight.py): what the app would fill on a saved Greenhouse role, and what it
  // still needs from you. It only reads: opening this section changes nothing in your tracker, and it shows no answer
  // you gave, only the questions, where each answer would come from, and what is missing.
  // About a rehearsal only: a Finish in browser run does write to the tracker (an application and its events), so this never says nothing changed.
  const APPLY_NOTE = "A rehearsal changes nothing in your tracker. It fills the form in a window to check it, and never sends it.";
  const APPLY_API = "/api/v1/apply-agent";
  const REHEARSE_HELP = "Opens a Chromium window and fills this form to check it. Nothing is sent: the app never presses Submit, and it refuses every request it can see that could send the form.";
  const WINDOW_NOTE = "A Chromium window is open. You can watch, but please don't type in it.";
  // Finish in browser (apply/runner.py, docs/assisted-apply.md): the app fills the form in a window and the student presses Submit.
  const HANDOFF_HELP = "Opens a Chromium window and fills the form. You complete what is left and press Submit application yourself. Your application is not sent until you do. To find options for typeahead fields, the app sends what is typed there to Greenhouse's lookup service.";
  const STOP_HELP = "Closes the window. Nothing is sent.";
  const FRONT_HELP = "If it doesn't appear, click Chromium in your taskbar.";
  const TURN_ERROR_HELP = "If the form shows an error, fix that field in the window and press Submit application again. Press Stop only if you want to give up; the app will tell you whether anything was sent.";
  const LEAVING_SOON_MS = 2 * 60 * 1000;
  const NOT_STORED = "Changed since this application was filled; what was sent is not stored.";
  // A run that was never handed over sent nothing, so its answers are "changed", not "not stored".
  const CHANGED_SINCE_FILL = "Changed since the app filled the form.";
  const NOT_ANSWERED_NOW = "Not answered now";
  const RUN_POLL_MS = 1000;

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
    form.append(label, element("p", "profile-help", "You typed this, so the app has not checked it against the form yet. It only uses an option the form really lists, word for word."), applyLookup(problem, input, onSaved), save, status);
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

  // Rehearsals and option lookups (apply/runner.py): a run is started with one POST and read by polling its row. Every poll
  // belongs to the session that started it, so each registers one stop with the session (a poll left running after sign-out
  // would get a 401 each time), and each checks the session and its own page after every await.
  const activeWatches = new Set();
  let watchersRegistered = false;

  // Calls onView(view) with the run's row every second until it is finished or stalled, or until alive() says the page that
  // wanted it is gone. Returns the function that stops it.
  function watchRun(runId, { alive, onView, onFail }) {
    if (!watchersRegistered) {
      watchersRegistered = true;
      registerSessionPoller(() => [...activeWatches].forEach((stopOne) => stopOne()));
    }
    const epoch = state.sessionEpoch;
    let timer = 0;
    let stopped = false;
    let failures = 0;
    const stop = () => {
      stopped = true;
      window.clearTimeout(timer);
      activeWatches.delete(stop);
    };
    activeWatches.add(stop);
    async function tick() {
      if (stopped) return;
      let view;
      try {
        view = await api(`${APPLY_API}/runs/${encodeURIComponent(runId)}`);
      } catch (error) {
        if (stopped) return;
        if (isAuthError(error) || state.sessionEpoch !== epoch || !alive()) {
          stop();
          return;
        }
        failures += 1;
        // A run that is gone, or a server that keeps failing, ends the wait; one lost request does not.
        if (error.status === 404 || failures >= 5) {
          stop();
          onFail(error);
          return;
        }
        timer = window.setTimeout(tick, RUN_POLL_MS * 2);
        return;
      }
      if (stopped) return;
      if (state.sessionEpoch !== epoch || !alive()) {
        stop();
        return;
      }
      failures = 0;
      const done = runIsOver(view);
      if (done) stop();
      onView(view);
      // onView may have replaced this watch (another panel) or ended it; either way no poll is left behind.
      if (!done && !stopped) timer = window.setTimeout(tick, RUN_POLL_MS);
    }
    timer = window.setTimeout(tick, RUN_POLL_MS);
    return stop;
  }

  const runIsOver = (view) => view.status === "finished" || Boolean(view.stalled);

  // The run phases in which a Finish in browser run is the student's (or past their Submit), not the app's filling.
  const TURN_PHASES = ["your_turn", "form_elsewhere", "submitting", "security_code", "code_typed", "code_yours", "challenge"];

  // The result panels whose Answer column is on screen. Signing out takes the column away: it holds the student's answers.
  const valuePanels = new Set();
  let valuePanelsRegistered = false;

  function registerValuePanels() {
    if (valuePanelsRegistered) return;
    valuePanelsRegistered = true;
    registerSessionPoller(() => {
      valuePanels.forEach((panel) => panel.clearValues());
      valuePanels.clear();
    });
  }

  // The Look up options part of a typeahead question's form (a list only Greenhouse knows, such as location): the student types
  // a few letters, the app types them into the form in a window and reads the options it offers, and the student picks theirs.
  // What was found is kept in problem.lookups, so the list being rebuilt (another answer saved) does not lose it.
  function applyLookup(problem, input, onSaved) {
    const action = problem.action;
    const memory = problem.lookups;
    const block = element("div", "apply-lookup");
    const find = element("button", "secondary-button", "Look up options");
    find.type = "button";
    const stopButton = element("button", "secondary-button", "Stop");
    stopButton.type = "button";
    stopButton.hidden = true;
    const actions = element("div", "apply-lookup-actions");
    actions.append(find, stopButton);
    const status = element("p", "form-status apply-lookup-status");
    status.setAttribute("role", "status");
    const choices = element("div", "apply-lookup-choices");
    block.append(
      actions,
      element("p", "profile-help", "This sends what you typed to Greenhouse's lookup service."),
      status,
      choices,
    );
    const alive = () => block.isConnected;
    let stopWatch = null;
    let busy = false;
    let runId = "";

    function setBusy(on) {
      busy = on;
      if (on) find.setAttribute("aria-disabled", "true");
      else find.removeAttribute("aria-disabled");
    }

    function showOptions(view) {
      choices.replaceChildren();
      const options = view.options?.[action.field] || [];
      if (!options.length) return;
      const group = element("fieldset", "apply-options");
      group.appendChild(element("legend", "", "Options the form lists"));
      const name = `apply-lookup-${problem.key}`;
      const radios = options.map((option) => {
        const row = element("label", "confirmation-row");
        const radio = document.createElement("input");
        radio.type = "radio";
        radio.name = name;
        radio.value = option;
        row.append(radio, element("span", "", option));
        group.appendChild(row);
        return radio;
      });
      const use = element("button", "secondary-button", "Use this one from now on");
      use.type = "button";
      let saving = false;
      use.addEventListener("click", async () => {
        if (saving) return;
        const chosen = radios.find((radio) => radio.checked);
        if (!chosen) {
          status.textContent = "Pick one of the options first.";
          return;
        }
        saving = true;
        use.setAttribute("aria-disabled", "true");
        status.textContent = "Saving…";
        const epoch = state.sessionEpoch;
        try {
          await api(`${APPLY_API}/ats-labels/${encodeURIComponent(action.field)}`, { method: "PUT", body: JSON.stringify({ label: chosen.value }) });
          if (state.sessionEpoch !== epoch) return;
          memory?.delete(problem.key);
          onSaved(null, "Saved.");
        } catch (error) {
          saving = false;
          use.removeAttribute("aria-disabled");
          if (!isAuthError(error)) status.textContent = error.message;
        }
      });
      choices.append(group, use);
    }

    function receive(view) {
      runId = view.id;
      memory?.set(problem.key, view);
      status.textContent = view.summary;
      if (!runIsOver(view)) {
        stopButton.hidden = false;
        return;
      }
      stopWatch?.();
      stopWatch = null;
      stopButton.hidden = true;
      setBusy(false);
      showOptions(view);
    }

    function follow(view) {
      receive(view);
      if (runIsOver(view)) return;
      setBusy(true);
      stopWatch?.();
      stopWatch = watchRun(view.id, {
        alive,
        onView: receive,
        onFail: (error) => {
          setBusy(false);
          stopButton.hidden = true;
          status.textContent = error.message;
        },
      });
    }

    find.addEventListener("click", async () => {
      if (busy) return;
      const text = input.value.trim();
      if (!text) {
        status.textContent = "Type a few letters of the option first.";
        return;
      }
      if (text.length > 100) {
        status.textContent = "Type at most 100 letters to look up.";
        return;
      }
      setBusy(true);
      choices.replaceChildren();
      status.textContent = "Starting…";
      const epoch = state.sessionEpoch;
      try {
        const view = await api(`${APPLY_API}/opportunities/${encodeURIComponent(problem.opportunityId)}/lookups`, {
          method: "POST",
          body: JSON.stringify({ key: problem.key, text }),
        });
        if (state.sessionEpoch !== epoch || !alive()) return;
        follow(view);
      } catch (error) {
        setBusy(false);
        if (state.sessionEpoch === epoch && !isAuthError(error)) status.textContent = error.message;
      }
    });

    stopButton.addEventListener("click", async () => {
      if (!runId || stopButton.getAttribute("aria-disabled") === "true") return;
      stopButton.setAttribute("aria-disabled", "true");
      stopButton.textContent = "Stopping…";
      const epoch = state.sessionEpoch;
      try {
        const view = await api(`${APPLY_API}/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
        if (state.sessionEpoch === epoch && alive()) receive(view);
      } catch (error) {
        if (!isAuthError(error) && state.sessionEpoch === epoch) status.textContent = error.message;
      }
      stopButton.removeAttribute("aria-disabled");
      stopButton.textContent = "Stop";
    });

    // The list was rebuilt while a lookup was running or after one finished: pick it up where it was.
    const earlier = memory?.get(problem.key);
    if (earlier) follow(earlier);
    return block;
  }

  // The window and the steps of a rehearsal that is running. update() takes each new row of the run.
  function applyRunPanel(view, onStop) {
    const node = element("div", "apply-run");
    const step = element("p", "apply-run-step");
    step.setAttribute("role", "status");
    step.tabIndex = -1;
    const steps = element("ol", "apply-run-steps");
    const stop = element("button", "secondary-button", "Stop");
    stop.type = "button";
    const note = element("p", "profile-help", WINDOW_NOTE);
    const status = element("p", "form-status");
    status.setAttribute("role", "status");
    node.append(step, steps, note, stop, status);
    stop.addEventListener("click", async () => {
      if (stop.getAttribute("aria-disabled") === "true") return;
      stop.setAttribute("aria-disabled", "true");
      stop.textContent = "Stopping…";
      status.textContent = "";
      try {
        await onStop();
      } catch (error) {
        stop.removeAttribute("aria-disabled");
        stop.textContent = "Stop";
        if (!isAuthError(error)) status.textContent = error.message;
      }
    });
    function update(fresh) {
      step.textContent = fresh.summary;
      // A Finish in browser run past Submit cannot be stopped; a rehearsal or lookup says nothing and keeps its button.
      stop.hidden = fresh.can_cancel === false;
      const texts = (fresh.progress || []).map((entry) => entry.text).filter((text, index, all) => text && text !== all[index - 1]);
      steps.replaceChildren(...texts.map((text, index) => {
        const line = element("li", index === texts.length - 1 ? "is-current" : "", text);
        if (index === texts.length - 1) line.setAttribute("aria-current", "step");
        return line;
      }));
    }
    update(view);
    return { node, update, focus: () => step.focus({ preventScroll: true }), fail: (message) => { status.textContent = message; } };
  }

  // A question the app answers from a statement the student stored (a ticked box, or a Yes/No agreement question answered Yes): the run view
  // marks it, whatever its control. A statement the app ticks or answers is "ticked for you"; in a rehearsal it is only checked (deferred).
  const isStatement = (field) => field.statement === true;
  const isTickedStatement = (field) => isStatement(field) && field.disposition === "fill";
  const isTickBox = (field) => field.control === "checkbox";

  // The addresses a ticked statement links to, as text: they are page text, so they are shown, never made into links.
  const linkText = (links) => webAddresses(links).join(", ");

  // The time the window closes, as the student's clock reads it.
  const clockTime = (stamp) => {
    const date = new Date(stamp);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  };

  // "Left for you" and "Ticked for you": what the student still does in the window, and what the app ticked on their behalf.
  function turnLists(view) {
    const lists = [];
    const left = view.left_for_you || [];
    if (left.length) {
      const block = element("section", "apply-left");
      block.appendChild(element("h5", "", "Left for you"));
      const list = element("ul", "reason-list");
      left.forEach((entry) => {
        const row = element("li", "apply-left-item");
        row.appendChild(element("strong", "", entry.question));
        if (entry.reason) row.appendChild(element("p", "profile-help", entry.reason));
        list.appendChild(row);
      });
      block.appendChild(list);
      lists.push(block);
    }
    const ticked = (view.fields || []).filter(isTickedStatement);
    if (ticked.length) {
      const block = element("section", "apply-ticked");
      block.appendChild(element("h5", "", "Ticked for you"));
      const list = element("ul", "reason-list");
      ticked.forEach((field) => {
        const row = element("li", "apply-ticked-item");
        row.appendChild(element("strong", "", field.question));
        const addresses = linkText(field.links);
        const verb = isTickBox(field) ? "Ticked" : "Answered Yes";
        row.appendChild(element("p", "profile-help", `${verb} from your stored statement${addresses ? ` · links to ${addresses}` : ""}`));
        list.appendChild(row);
      });
      block.appendChild(list);
      lists.push(block);
    }
    return lists;
  }

  // The student's turn in a Finish in browser run: what is left, what was ticked, how long the window stays, and the way out.
  // After Submit is pressed the same panel keeps the step and the window button, and drops Stop (the claim is past stopping).
  // update() takes each new row of the run. The countdown is read from each row's handoff_until at poll time, so no timer outlives the panel.
  function applyTurnPanel(view, { onStop, onFront }) {
    const node = element("div", "apply-turn");
    const step = element("p", "apply-run-step");
    step.setAttribute("role", "status");
    step.tabIndex = -1;
    const until = element("p", "profile-help apply-turn-until");
    const soon = element("p", "apply-limit apply-turn-soon");
    soon.setAttribute("role", "status");
    const lists = element("div", "apply-turn-lists");
    const actions = element("div", "apply-turn-actions");
    const stop = element("button", "secondary-button", "Stop");
    stop.type = "button";
    const stopHelp = element("p", "profile-help apply-stop-help", STOP_HELP);
    stopHelp.id = `apply-stop-help-${view.id}`;
    stop.setAttribute("aria-describedby", stopHelp.id);
    const front = element("button", "secondary-button apply-front", "Bring the window forward");
    front.type = "button";
    const frontHelp = element("p", "profile-help apply-front-help", FRONT_HELP);
    frontHelp.id = `apply-front-help-${view.id}`;
    front.setAttribute("aria-describedby", frontHelp.id);
    const errorHelp = element("p", "profile-help apply-turn-help", TURN_ERROR_HELP);
    const status = element("p", "form-status");
    status.setAttribute("role", "status");
    // Each button sits over its own help, so the two read as two choices.
    const stopBox = element("div", "apply-turn-action");
    stopBox.append(stop, stopHelp);
    const frontBox = element("div", "apply-turn-action");
    frontBox.append(front, frontHelp);
    actions.append(stopBox, frontBox);
    node.append(step, until, soon, lists, actions, errorHelp, status);
    // Each text is set only when it changes, so a screen reader is not told the same thing again at every poll.
    const setText = (target, text) => { if (target.textContent !== text) target.textContent = text; };
    let listed = "";
    const guarded = (button, label, busy, action) => button.addEventListener("click", async () => {
      if (button.getAttribute("aria-disabled") === "true") return;
      button.setAttribute("aria-disabled", "true");
      button.textContent = busy;
      status.textContent = "";
      try {
        await action();
      } catch (error) {
        if (!isAuthError(error)) status.textContent = error.message;
      }
      button.removeAttribute("aria-disabled");
      button.textContent = label;
    });
    guarded(stop, "Stop", "Stopping…", onStop);
    function update(fresh) {
      setText(step, fresh.summary);
      // "form_elsewhere" is the same turn: the form tried to send somewhere the app stopped, and the student goes on or stops.
      const turn = fresh.phase === "your_turn" || fresh.phase === "form_elsewhere";
      const closes = turn && fresh.handoff_until ? clockTime(fresh.handoff_until) : "";
      setText(until, closes ? `The window closes at ${closes} if you haven't pressed Submit application.` : "");
      const left = turn && fresh.handoff_until ? new Date(fresh.handoff_until).getTime() - Date.now() : 0;
      setText(soon, left > 0 && left < LEAVING_SOON_MS ? "About 2 minutes left in the window" : "");
      const signature = JSON.stringify([fresh.left_for_you, (fresh.fields || []).filter(isTickedStatement).map((field) => [field.key, field.links])]);
      if (signature !== listed) {
        listed = signature;
        lists.replaceChildren(...turnLists(fresh));
      }
      const canStop = turn && fresh.can_cancel !== false;
      stopBox.hidden = !canStop;
      frontBox.hidden = !fresh.can_front;
      errorHelp.hidden = !turn;
      actions.hidden = stopBox.hidden && frontBox.hidden;
    }
    guarded(front, "Bring the window forward", "Asking…", onFront);
    update(view);
    return { node, update, focus: () => step.focus({ preventScroll: true }), fail: (message) => { status.textContent = message; } };
  }

  // "Was this rehearsal right?" for a finished rehearsal, or the student's mark once it is given. A mark is the student's own
  // word on whether the app's rehearsal matched the form; it changes nothing else.
  function applyReviewBlock(view, onMarked, stale) {
    const block = element("div", "apply-review");
    block.setAttribute("role", "group");
    if (view.review) {
      const mark = element("p", "apply-review-mark", `You marked this rehearsal ${view.review === "right" ? "right" : "wrong"}.`);
      mark.tabIndex = -1;
      block.appendChild(mark);
      if (view.review_note) block.appendChild(element("p", "profile-help", view.review_note));
      return block;
    }
    if (!view.can_review) return null;
    const question = element("p", "apply-review-question", "Was this rehearsal right?");
    question.id = `apply-review-${view.id}`;
    block.setAttribute("aria-labelledby", question.id);
    const row = element("div", "apply-review-actions");
    const right = element("button", "secondary-button", "Right");
    right.type = "button";
    const wrong = element("button", "secondary-button", "Something's wrong");
    wrong.type = "button";
    row.append(right, wrong);
    const status = element("p", "form-status");
    status.setAttribute("role", "status");
    const noteForm = element("form", "apply-review-note");
    noteForm.hidden = true;
    const noteLabel = element("label", "profile-field");
    noteLabel.appendChild(element("span", "", "What was wrong? (optional)"));
    const note = document.createElement("textarea");
    note.rows = 3;
    note.maxLength = 500;
    noteLabel.appendChild(note);
    const send = element("button", "secondary-button", "Send");
    send.type = "submit";
    noteForm.append(noteLabel, send);
    let sending = false;
    async function mark(verdict, text) {
      if (sending) return;
      sending = true;
      for (const button of [right, wrong, send]) button.setAttribute("aria-disabled", "true");
      status.textContent = "Saving…";
      const epoch = state.sessionEpoch;
      try {
        const fresh = await api(`${APPLY_API}/runs/${encodeURIComponent(view.id)}/review`, {
          method: "POST",
          body: JSON.stringify({ verdict, note: text }),
        });
        if (state.sessionEpoch !== epoch || stale()) return;
        onMarked(fresh);
      } catch (error) {
        sending = false;
        for (const button of [right, wrong, send]) button.removeAttribute("aria-disabled");
        status.textContent = isAuthError(error) ? "" : error.message;
      }
    }
    right.addEventListener("click", () => mark("right", ""));
    wrong.setAttribute("aria-expanded", "false");
    wrong.addEventListener("click", () => {
      noteForm.hidden = false;
      wrong.setAttribute("aria-expanded", "true");
      note.focus();
    });
    noteForm.addEventListener("submit", (event) => {
      event.preventDefault();
      mark("wrong", note.value.trim());
    });
    block.append(question, row, noteForm, status);
    return block;
  }

  // What the student's own tick or press recorded, for a Finish in browser result: the buttons that settle the claim, and a line
  // on the confirmation email. settle(path, body) posts to the claim and reloads the run; it throws on a refusal.
  function applyClaimBlock(view, settle) {
    const claim = view.claim;
    if (!claim) return null;
    const ats = claim.ats_name || "Greenhouse";
    const box = element("div", "apply-claim");
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", "What to do next");
    const lines = [];
    if (claim.status === "may_have_been_sent") lines.push(`This may have been sent. Check your email for ${ats}'s confirmation, then say what happened.`);
    else if (claim.application_stage === "applied" && claim.state === "submitted") lines.push("This application is marked as applied.");
    else if (claim.state === "released" && claim.resolved_by === "student" && claim.handed_over) lines.push("You said it didn't go through.");
    else if (claim.state === "released" && claim.resolved_by === "student") lines.push("A later Finish in browser replaced this attempt.");
    if (claim.status === "watching") lines.push(`Looking for ${ats}'s confirmation email until ${formatDate(claim.watch_until)}.`);
    else if (claim.status === "watch_paused") lines.push(`Looking for ${ats}'s confirmation email: paused, ${claim.paused_reason || "the job-email check isn't running"}.`);
    else if (claim.status === "email_confirmed") lines.push(`${ats}'s confirmation email arrived ${formatDate(claim.email_received_at)}.`);
    else if (claim.status === "no_email") lines.push("No confirmation email yet. Some employers don't send one; check the employer's portal or your spam folder.");
    else if (claim.status === "not_watched") lines.push("The app isn't checking for a confirmation email.");
    lines.forEach((line) => box.appendChild(element("p", "apply-claim-line", line)));
    const row = element("div", "apply-claim-actions");
    const status = element("p", "form-status");
    status.setAttribute("role", "status");
    const buttons = [];
    const act = (label, path, body) => {
      const button = element("button", "secondary-button", label);
      button.type = "button";
      button.addEventListener("click", async () => {
        if (button.getAttribute("aria-disabled") === "true") return;
        buttons.forEach((other) => other.setAttribute("aria-disabled", "true"));
        status.textContent = "Saving…";
        try {
          await settle(path, body);
        } catch (error) {
          buttons.forEach((other) => other.removeAttribute("aria-disabled"));
          status.textContent = isAuthError(error) ? "" : error.message;
        }
      });
      buttons.push(button);
      row.appendChild(button);
      return button;
    };
    if (claim.ask_mark_applied) act("Mark as applied?", "mark-applied");
    if (claim.status === "may_have_been_sent") {
      const yes = act("It went through", "resolve", { went_through: true });
      const no = act("It didn't go through", "resolve", { went_through: false });
      if (!claim.can_resolve) [yes, no].forEach((button) => button.setAttribute("aria-disabled", "true"));
    }
    if (buttons.length) box.append(row, status);
    return box.children.length ? box : null;
  }

  // The table of what the run did with each field. `values` (from GET .../values, the student's browser session only) adds the
  // Answer column once it has arrived; a Finish in browser run shows only what is provably what the app filled.
  function applyPlanTable(view, values) {
    const handoff = view.kind === "handoff";
    const fields = (view.fields || []).filter((field) => field.disposition !== "blank");
    if (!fields.length) return null;
    const caption = handoff ? "What the app did with each field" : "What the rehearsal did with each field";
    const scroll = element("div", "apply-plan-scroll");
    scroll.tabIndex = 0;
    scroll.setAttribute("role", "region");
    scroll.setAttribute("aria-label", caption);
    const table = element("table", "apply-plan");
    table.appendChild(element("caption", "", caption));
    const columns = ["Question"];
    if (values) columns.push(handoff ? "What the app filled" : "Answer");
    columns.push(handoff ? "What the app did" : "What the rehearsal did", "From");
    const head = element("tr");
    columns.forEach((words) => {
      const cell = element("th", "", words);
      cell.scope = "col";
      head.appendChild(cell);
    });
    table.appendChild(element("thead")).appendChild(head);
    const rows = element("tbody");
    fields.forEach((field) => {
      const row = element("tr");
      const question = element("th", "", `${field.question}${field.required ? " (required)" : ""}`);
      question.scope = "row";
      row.appendChild(question);
      if (values) row.appendChild(valueCell(view, field, values));
      const did = element("td", "", field.disposition_text);
      if (field.problem) did.appendChild(element("span", "apply-plan-problem", field.problem));
      const from = element("td", "", field.source_text);
      // A ticked statement shows the address of the document it links to, as text, so the student can see what was agreed to.
      const addresses = !values && isStatement(field) && ["fill", "deferred"].includes(field.disposition) ? linkText(field.links) : "";
      if (addresses) from.append(` · links to ${addresses}`);
      row.append(did, from);
      rows.appendChild(row);
    });
    table.appendChild(rows);
    scroll.appendChild(table);
    return scroll;
  }

  // One Answer cell. Only a field the app fills (or checks, in a rehearsal) has one; a field left for the student or blank never does.
  function valueCell(view, field, values) {
    const handoff = view.kind === "handoff";
    const cell = element("td", "apply-plan-answer");
    if (!(field.disposition === "fill" || (!handoff && field.disposition === "deferred"))) return cell;
    const entry = values[field.key];
    if (!handoff && field.source_kind === "cover_letter") {
      cell.textContent = "Not attached yet: attach it in the window";
      return cell;
    }
    if (isStatement(field)) {
      // In a rehearsal this column is today's value: a statement nothing stored answers now (deleted, reworded, or its links no
      // longer match) is not drawn as ticked, because Finish in browser would leave it for the student.
      if (!handoff && (!entry || entry.available === false)) {
        cell.append(NOT_ANSWERED_NOW);
        if (entry?.changed) cell.appendChild(element("span", "apply-plan-changed", "changed since the rehearsal"));
        return cell;
      }
      cell.append(isTickBox(field) ? "Ticked" : (entry && entry.shown !== false && entry.text ? entry.text : "Answered from your stored statement"));
      const addresses = linkText(field.links);
      if (addresses) cell.append(` · links to ${addresses}`);
      if (!handoff && entry?.changed) cell.appendChild(element("span", "apply-plan-changed", "changed since the rehearsal"));
      return cell;
    }
    if (!entry) return cell;
    if (handoff) {
      cell.textContent = entry.shown ? entry.text : (view.handed_over ? NOT_STORED : CHANGED_SINCE_FILL);
      return cell;
    }
    if (entry.text) cell.append(entry.text);
    if (entry.changed) cell.appendChild(element("span", "apply-plan-changed", "changed since the rehearsal"));
    return cell;
  }

  // Fields the app left blank (with why) and optional ones the page filled in itself, each in a collapsed group.
  function applyPlanGroups(view) {
    const groups = [];
    const blank = (view.fields || []).filter((field) => field.disposition === "blank");
    if (blank.length) {
      const details = element("details", "apply-plan-group apply-blank");
      details.appendChild(element("summary", "", `Left blank (${blank.length})`));
      const list = element("ul", "reason-list");
      blank.forEach((field) => {
        const line = element("li", "", field.question);
        const why = field.note || field.problem;
        if (why) line.appendChild(element("p", "profile-help", why));
        list.appendChild(line);
      });
      details.appendChild(list);
      groups.push(details);
    }
    const defaults = view.page_defaults || [];
    if (defaults.length) {
      const details = element("details", "apply-plan-group apply-page-defaults");
      details.appendChild(element("summary", "", `Left as the page set it (${defaults.length})`));
      const list = element("ul", "reason-list");
      defaults.forEach((entry) => list.appendChild(element("li", "", entry.question || entry.key)));
      details.appendChild(list);
      groups.push(details);
    }
    return groups;
  }

  // What a finished (or stalled) rehearsal or Finish in browser run found: in words, from the run's row. The table says what was
  // done with each question and where the answer came from; the Answer column, when it arrives, is a separate read (setValues).
  function applyResultPanel(view, { startAgain, finish, notNow, onMarked, settle, stale, postingUrl }) {
    const handoff = view.kind === "handoff";
    const node = element("div", "apply-result");
    const title = element("h4", "apply-result-title", view.summary);
    title.tabIndex = -1;
    node.appendChild(title);
    const when = view.finished_at ? formatWeekdayDateTime(view.finished_at) : "";
    if (when) node.appendChild(element("p", "profile-help apply-result-when", `${handoff ? "Finished" : "Rehearsed"} ${when}.`));
    const claim = handoff ? applyClaimBlock(view, settle) : null;
    if (claim) node.appendChild(claim);
    if (view.measured) node.appendChild(element("p", "apply-measured", view.measured));
    if (view.outcome === "rehearsed") {
      node.appendChild(element("p", `apply-clean ${view.clean ? "is-clean" : "is-gaps"}`, view.clean ? "Clean rehearsal" : "Not clean: the gaps below"));
    }
    // A Finish in browser run's gaps are the fields it left for the student: the table says so row by row, and the turn panel listed them.
    const problems = handoff ? [] : (view.problems || []);
    if (problems.length) {
      const list = element("ul", "apply-problems apply-result-problems");
      problems.forEach((problem) => {
        const row = element("li", "apply-result-problem");
        const question = problem.question || problem.key;
        row.appendChild(element("strong", "", problem.required ? question : `${question} (optional)`));
        if (problem.message) row.appendChild(element("p", "profile-help", problem.message));
        list.appendChild(row);
      });
      node.appendChild(list);
    }
    // The table, its notes and the groups sit in one box so the Answer column can replace them when it arrives (or leave on sign-out).
    const plan = element("div", "apply-plan-box");
    node.appendChild(plan);
    function paintPlan(values) {
      const parts = [];
      if (values && handoff && view.handed_over) parts.push(element("p", "profile-help apply-values-note", "You may have changed fields in the window before you pressed Submit application."));
      if (values && !handoff && Object.values(values).some((entry) => entry.changed)) {
        parts.push(element("p", "apply-limit apply-values-note", "Some answers changed since this rehearsal. Rehearse again to see the new plan; Finish in browser uses your current answers."));
      }
      const table = applyPlanTable(view, values);
      if (table) parts.push(table);
      parts.push(...applyPlanGroups(view));
      plan.replaceChildren(...parts);
    }
    paintPlan(null);
    (view.screenshots || []).filter((picture) => picture.available).forEach((picture) => {
      const frame = element("div", "apply-shot");
      const link = document.createElement("a");
      link.href = picture.url;
      link.target = "_blank";
      link.rel = "noopener";
      const image = document.createElement("img");
      image.loading = "lazy";
      image.src = picture.url;
      image.alt = picture.step === "needs-you"
        ? `The form where the ${handoff ? "app" : "rehearsal"} stopped, with sensitive fields covered`
        : picture.step === "final"
          ? "The page after you pressed Submit application, with sensitive fields covered"
          : "The filled form, with sensitive fields covered";
      link.appendChild(image);
      frame.appendChild(link);
      node.appendChild(frame);
    });
    // Sentences the summary does not already say (the first reason is the stop sentence of a run that stopped).
    const reasons = (view.reasons || []).filter((reason) => !view.summary.includes(reason));
    if (reasons.length) {
      const list = element("ul", "reason-list apply-reasons");
      reasons.forEach((reason) => list.appendChild(element("li", "", reason)));
      node.appendChild(list);
    }
    const review = applyReviewBlock(view, onMarked, stale);
    if (review) node.appendChild(review);
    const next = element("div", "apply-result-actions");
    if (handoff) {
      // Nothing more to start for an application that went, or may have; one that was stopped or never sent can be tried again.
      // The claim says it when there is one: a stopped claim (including one the student released with "It didn't go through") can be tried again.
      // A run that stopped on a property of the board (no submit address the app knows, a board that uploads on attach, a hidden field)
      // would stop the same way again, so the posting is offered instead (the server says so in finish_again).
      const sent = view.claim ? view.claim.status !== "stopped" : ["submitted", "unconfirmed"].includes(view.outcome);
      if (!sent && view.finish_again !== false) next.appendChild(finish());
      else if (!sent && postingUrl) next.appendChild(externalLink(postingUrl, "Open the posting ↗", { className: "secondary-button" }));
    } else {
      next.append(finish(), notNow(), startAgain());
    }
    if (next.children.length) node.appendChild(next);
    return { node, focus: () => title.focus({ preventScroll: true }), setValues: paintPlan, clearValues: () => paintPlan(null) };
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
    // The rehearsal (a run in a window, never sent) sits above the questions and keeps its state while they are rebuilt.
    const rehearse = element("div", "apply-rehearse");
    rehearse.hidden = true;
    const body = element("div", "apply-body");
    section.append(summary, rehearse, body);
    const stale = () => state.detailItem !== item || !section.isConnected;
    // What a Look up options asked for, by question, so a rebuilt list keeps it.
    const lookups = new Map();
    // The latest check, the run on screen (a rehearsal or a Finish in browser run), and the poll that follows it while it runs.
    let checked = null;
    let current = null;
    let running = null;
    let runningKind = "";
    let stopWatch = null;
    let resumed = false;
    let other = "";
    const starters = new Set();
    let starterCount = 0;
    // Finish in browser: which "Before you go on" boxes are ticked (by code), and the ones a refused start added.
    const ticked = new Set();
    const askedTicks = new Map();
    // Opened from a timeline event's See the run: that one run, not the latest.
    const requested = state.applyRunRequest && state.applyRunRequest.opportunityId === item.id && Date.now() - state.applyRunRequest.at < 10000
      ? state.applyRunRequest.runId : "";
    state.applyRunRequest = null;

    function handoffTicks() {
      const ticks = [...(checked?.eligibility?.handoff?.ticks || [])];
      askedTicks.forEach((label, code) => {
        if (!ticks.some((tick) => tick.code === code)) ticks.push({ code, label });
      });
      return ticks;
    }

    function syncStarters() {
      starters.forEach((starter) => {
        // A starter is painted once before its box is put on the page (fresh); after that a button that is gone is forgotten.
        if (!starter.button.isConnected && !starter.fresh) {
          starters.delete(starter);
          return;
        }
        if (starter.button.isConnected) starter.fresh = false;
        const eligibility = checked?.eligibility?.[starter.kind];
        const blocked = eligibility && eligibility.allowed === false ? (eligibility.reason || "") : "";
        let waiting = false;
        if (starter.kind === "handoff") {
          const ticks = handoffTicks();
          starter.paintTicks(ticks);
          waiting = ticks.some((tick) => !ticked.has(tick.code));
        }
        const off = Boolean(blocked) || starter.busy || waiting;
        if (off) starter.button.setAttribute("aria-disabled", "true");
        else starter.button.removeAttribute("aria-disabled");
        const note = blocked || (starter.busy ? "" : (other || (waiting ? "Tick the boxes above to go on." : "")));
        starter.reason.textContent = note;
        if (note) starter.button.setAttribute("aria-describedby", starter.reason.id);
        else starter.button.removeAttribute("aria-describedby");
      });
    }

    // The button that starts a rehearsal, with its words. Only one run is allowed at a time, so a busy app says so here.
    function startControls(label, { help = true } = {}) {
      const box = element("div", "apply-start-box apply-rehearse-start");
      const button = element("button", "secondary-button", label);
      button.type = "button";
      const reason = element("p", "apply-limit");
      reason.id = `apply-rehearse-reason-${starterCount += 1}`;
      const status = element("p", "form-status");
      status.setAttribute("role", "status");
      box.appendChild(button);
      if (help) box.appendChild(element("p", "profile-help", REHEARSE_HELP));
      box.append(reason, status);
      const starter = { kind: "rehearse", button, reason, busy: false, fresh: true };
      starters.add(starter);
      button.addEventListener("click", async () => {
        if (button.getAttribute("aria-disabled") === "true") return;
        starter.busy = true;
        other = "";
        syncStarters();
        status.textContent = "Starting…";
        const epoch = state.sessionEpoch;
        try {
          const view = await api(`${APPLY_API}/opportunities/${encodeURIComponent(item.id)}/rehearsals`, {
            method: "POST",
            body: JSON.stringify({ posting_confirmed: postingConfirmed }),
          });
          if (state.sessionEpoch !== epoch || stale()) return;
          show(view, true);
        } catch (error) {
          starter.busy = false;
          syncStarters();
          status.textContent = state.sessionEpoch === epoch && !isAuthError(error) ? error.message : "";
        }
      });
      syncStarters();
      return box;
    }

    // Finish in browser: the app fills the form in a window and the student presses Submit there. Anything the app wants the
    // student to agree to first (a recent application to the company, a role Greenhouse took down) is a box to tick, never a default.
    function handoffControls(label, { help = true } = {}) {
      const box = element("div", "apply-start-box apply-handoff-start");
      const group = element("fieldset", "apply-ticks");
      group.hidden = true;
      group.appendChild(element("legend", "", "Before you go on"));
      const rows = element("div", "apply-ticks-rows");
      group.appendChild(rows);
      // Only when the start itself finds the posting is not the saved role (the check did not): the same tick the check offers.
      let postingBox = null;
      const showPosting = () => {
        if (postingBox) return;
        const posting = element("label", "confirmation-row apply-posting-tick");
        postingBox = document.createElement("input");
        postingBox.type = "checkbox";
        postingBox.addEventListener("change", () => { postingConfirmed = postingBox.checked; });
        posting.append(postingBox, element("span", "", "This is the right posting"));
        box.insertBefore(posting, button);
      };
      const button = element("button", "secondary-button", label);
      button.type = "button";
      const reason = element("p", "apply-limit");
      reason.id = `apply-rehearse-reason-${starterCount += 1}`;
      const status = element("p", "form-status");
      status.setAttribute("role", "status");
      box.append(group, button);
      if (help) box.appendChild(element("p", "profile-help", HANDOFF_HELP));
      box.append(reason, status);
      const starter = {
        kind: "handoff", button, reason, busy: false, fresh: true,
        paintTicks(ticks) {
          group.hidden = !ticks.length;
          const shown = [...rows.querySelectorAll("input")].map((input) => input.value).join("|");
          if (shown !== ticks.map((tick) => tick.code).join("|")) {
            rows.replaceChildren(...ticks.map((tick) => {
              const row = element("label", "confirmation-row");
              const tickBox = document.createElement("input");
              tickBox.type = "checkbox";
              tickBox.value = tick.code;
              tickBox.checked = ticked.has(tick.code);
              tickBox.addEventListener("change", () => {
                if (tickBox.checked) ticked.add(tick.code);
                else ticked.delete(tick.code);
                syncStarters();
              });
              row.append(tickBox, element("span", "", tick.label));
              return row;
            }));
          }
          if (postingBox) postingBox.checked = Boolean(postingConfirmed);
        },
      };
      starters.add(starter);
      button.addEventListener("click", async () => {
        if (button.getAttribute("aria-disabled") === "true") return;
        starter.busy = true;
        other = "";
        syncStarters();
        status.textContent = "Starting…";
        const epoch = state.sessionEpoch;
        try {
          const acknowledged = handoffTicks().map((tick) => tick.code).filter((code) => ticked.has(code));
          const view = await api(`${APPLY_API}/opportunities/${encodeURIComponent(item.id)}/handoffs`, {
            method: "POST",
            body: JSON.stringify({ acknowledged, posting_confirmed: Boolean(postingConfirmed) }),
          });
          if (state.sessionEpoch !== epoch || stale()) return;
          // Each start is its own consent: a box is a box to tick, never a default, so the next Finish in browser control (after a
          // Stop, a closed window or the time limit) offers every box unticked, under whatever words the next check gives it.
          ticked.clear();
          askedTicks.clear();
          show(view, true);
        } catch (error) {
          starter.busy = false;
          if (state.sessionEpoch !== epoch) return;
          const detail = error.detail && typeof error.detail === "object" ? error.detail : {};
          // A refusal that the student can answer with a tick adds the box; a posting that is not the saved role asks for its tick.
          if (detail.ask && detail.code) askedTicks.set(detail.code, detail.message || error.message);
          if (detail.code === "posting") showPosting();
          syncStarters();
          status.textContent = isAuthError(error) ? "" : error.message;
        }
      });
      syncStarters();
      return box;
    }

    function startBlock() {
      const block = element("div", "apply-start-block");
      block.append(startControls("Rehearse in a window"), handoffControls("Finish in browser"));
      return block;
    }

    // Not now: the result folds back into the starters. Nothing is sent to the server.
    function collapse() {
      stopWatch?.();
      stopWatch = null;
      running = null;
      current = null;
      starters.clear();
      rehearse.replaceChildren(startBlock());
    }

    // The Answer column: what today's answers would put in each field, or what the app provably filled. It comes from a read
    // the student's own browser session alone may make, so a refusal just leaves the column out; one that arrives after the
    // student signed out, or after the panel moved on, paints nothing.
    async function loadValues(view, result) {
      if (!["rehearsal", "handoff"].includes(view.kind) || !(view.fields || []).length) return;
      const epoch = state.sessionEpoch;
      try {
        const answer = await api(`${APPLY_API}/runs/${encodeURIComponent(view.id)}/values`);
        if (state.sessionEpoch !== epoch || stale() || current !== view || !result.node.isConnected) return;
        valuePanels.add(result);
        registerValuePanels();
        result.setValues(answer.values || {});
      } catch (error) {
        // A column that did not load leaves the table as it was.
      }
    }

    const panelKind = (view) => (view.kind === "handoff" && TURN_PHASES.includes(view.phase) ? "turn" : "run");

    // A new row of the run on screen: still running, or over.
    function receive(view) {
      current = view;
      if (runIsOver(view)) {
        stopWatch?.();
        stopWatch = null;
        running = null;
        show(view, true);
        // The check read while the run was going said its claim was live; the claim is settled now, so what may start again follows it.
        if (view.kind === "handoff") load().catch(() => {});
        return;
      }
      // The window filled the form and it is now the student's turn (or past it): another panel, not another poll.
      if (panelKind(view) !== runningKind) {
        const toTurn = panelKind(view) === "turn";
        show(view, false);
        // A live region that is put on the page already holding its words is not read out, and focus is left where it was: the one
        // moment that needs the student (and starts the clock) is said once, for a screen reader.
        if (toTurn) announce(view.summary);
        return;
      }
      running?.update(view);
    }

    async function stopRun(view) {
      const epoch = state.sessionEpoch;
      try {
        const fresh = await api(`${APPLY_API}/runs/${encodeURIComponent(view.id)}/cancel`, { method: "POST" });
        if (state.sessionEpoch === epoch && !stale()) receive(fresh);
      } catch (error) {
        // A refusal (it was already handed over, or already over) leaves the run as it now is: read it again and show that.
        if (error.status === 409 && !isAuthError(error)) {
          try {
            const fresh = await api(`${APPLY_API}/runs/${encodeURIComponent(view.id)}`);
            if (state.sessionEpoch === epoch && !stale() && current?.id === view.id) receive(fresh);
          } catch (_) {
            // The next poll shows it.
          }
        }
        throw error;
      }
    }

    async function frontRun(view) {
      const epoch = state.sessionEpoch;
      const fresh = await api(`${APPLY_API}/runs/${encodeURIComponent(view.id)}/front`, { method: "POST" });
      if (state.sessionEpoch === epoch && !stale()) receive(fresh);
    }

    // The student's own word on the claim (Mark as applied?, It went through), then the run as it reads now.
    async function settleClaim(view, path, body) {
      const epoch = state.sessionEpoch;
      await api(`${APPLY_API}/claims/${encodeURIComponent(view.claim.token)}/${path}`, {
        method: "POST", body: body === undefined ? undefined : JSON.stringify(body),
      });
      if (state.sessionEpoch !== epoch || stale()) return;
      const fresh = await api(`${APPLY_API}/runs/${encodeURIComponent(view.id)}`);
      if (state.sessionEpoch !== epoch || stale()) return;
      show(fresh, false);
      // What the app may start, and what it asks first, changed with the claim (a released attempt asks): read the check again.
      load().catch(() => {});
      const company = view.claim.company || "the role";
      announce(path === "mark-applied" ? `Marked ${company} as applied.` : body?.went_through ? `Recorded that ${company} went through.` : `Recorded that ${company} did not go through.`);
      loadApplications().catch(() => {});
    }

    function show(view, focus) {
      stopWatch?.();
      stopWatch = null;
      running = null;
      runningKind = "";
      current = view;
      starters.clear();
      rehearse.replaceChildren();
      rehearse.hidden = false;
      if (runIsOver(view)) {
        const result = applyResultPanel(view, {
          startAgain: () => startControls("Rehearse again", { help: false }),
          finish: () => handoffControls("Finish in browser"),
          notNow: () => {
            const button = element("button", "secondary-button", "Not now");
            button.type = "button";
            button.addEventListener("click", collapse);
            return button;
          },
          onMarked: (fresh) => show(fresh, false),
          settle: (path, body) => settleClaim(view, path, body),
          stale,
          postingUrl: item.url,
        });
        rehearse.appendChild(result.node);
        if (focus) result.focus();
        loadValues(view, result);
        return;
      }
      runningKind = panelKind(view);
      running = runningKind === "turn"
        ? applyTurnPanel(view, { onStop: () => stopRun(view), onFront: () => frontRun(view) })
        : applyRunPanel(view, () => stopRun(view));
      rehearse.appendChild(running.node);
      if (focus) running.focus();
      stopWatch = watchRun(view.id, {
        alive: () => !stale(),
        onView: receive,
        onFail: (error) => running?.fail(error.message),
      });
    }

    // Opened again: a run that is still going is followed, and the last one that finished is shown with its result. A lookup
    // belongs to its question's form, so it is not what this block shows.
    async function resume({ handoffOnly = false } = {}) {
      resumed = true;
      const epoch = state.sessionEpoch;
      try {
        if (requested) {
          const run = await api(`${APPLY_API}/runs/${encodeURIComponent(requested)}`);
          if (state.sessionEpoch !== epoch || stale() || current) return;
          show(run, false);
          return;
        }
        // A lookup is a run too, and every press of Look up options adds one: ask for the kinds this block shows, never the latest few.
        const base = `${APPLY_API}/opportunities/${encodeURIComponent(item.id)}/runs`;
        const kinds = handoffOnly ? ["handoff"] : ["rehearsal", "handoff"];
        const lists = await Promise.all(kinds.map((kind) => api(`${base}?kind=${kind}&limit=3`)));
        if (state.sessionEpoch !== epoch || stale() || current) return;
        const listed = { busy: lists.some((one) => one.busy), runs: lists.flatMap((one) => one.runs || []) };
        const shown = listed.runs.filter((run) => run.kind !== "lookup").sort((a, b) => String(b.started_at || "").localeCompare(String(a.started_at || "")));
        const latest = shown.find((run) => run.status === "running") || shown[0];
        if (latest) {
          show(latest, false);
        } else if (listed.busy && !handoffOnly) {
          other = "Another application is being filled. Wait for it to finish.";
          syncStarters();
        }
      } catch (error) {
        // The buttons work without it: nothing to say about a list that did not load.
      }
    }

    function paintRehearse(result) {
      checked = result;
      const offered = ["ready", "needs_you"].includes(result.status);
      if (!offered && !current && !requested) {
        rehearse.hidden = true;
        // A role Apply for me can no longer start (the application went, or may have) still shows what its Finish in browser run did.
        if (!resumed) resume({ handoffOnly: true });
        return;
      }
      rehearse.hidden = false;
      if (!current && !rehearse.firstChild && offered) rehearse.appendChild(startBlock());
      syncStarters();
      if (!resumed && (offered || requested)) resume();
    }
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
      paintRehearse(result);
      if (result.status === "unavailable" || result.status === "failed") return;
      // When the Finish in browser button is on the page it says what the student must agree to (the boxes) and why it is off itself.
      const buttonShown = ["ready", "needs_you"].includes(result.status);
      if (!buttonShown) (result.asks || []).forEach((ask) => body.appendChild(element("p", "apply-limit", `Before you go on: ${ask.message}`)));
      const stopped = result.eligibility?.handoff;
      if (stopped && !stopped.allowed && stopped.reason && !buttonShown) body.appendChild(element("p", "apply-limit", `Not right now: ${stopped.reason}`));
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
          const control = applyProblemAction({ ...problem, opportunityId: item.id, postingConfirmed: () => postingConfirmed, lookups }, result.company, (fresh, message) => {
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
      const epoch = state.sessionEpoch;
      try {
        const result = await api(`/api/v1/apply-agent/opportunities/${encodeURIComponent(item.id)}/check`);
        if (state.sessionEpoch !== epoch) return;
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
      (settings.ats_statistics || []).forEach((entry) => {
        host.appendChild(element("h5", "", "How Apply for me has gone"));
        const stats = element("ul", "reason-list apply-stats");
        entry.lines.forEach((line) => stats.appendChild(element("li", "", line)));
        host.appendChild(stats);
      });
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
