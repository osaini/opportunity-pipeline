// The opportunity detail panel: the posting, the match review, the résumé pick, and opening and closing the panel.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { els, state } = App;

  // From app-ui.js.
  const {
    autoSaveSelect, deadlineState, element, externalLink, formatCalendarDate, formatDate, inertClaims, optionElement,
    setBackgroundInert,
  } = App;

  // From app-http.js.
  const { api, isAuthError } = App;

  // From app-nav.js.
  const { loadCurrentView } = App;

  // From app-opportunities.js.
  const { baseScoreReason, companyTagsSection } = App;

  // From app-apply.js.
  const { applyForMeSection } = App;

  // The student's own deadline for a role. It is labelled as theirs, feeds the
  // Urgent queue, and never changes the posting or the purge.
  function userDeadlineSection(item) {
    if (!item.can_set_user_deadline && !item.user_deadline) return null;
    const section = element("section", "detail-section user-deadline");
    section.appendChild(element("p", "eyebrow", "Your deadline (you entered)"));
    const current = element("p", "user-deadline-current");
    current.setAttribute("tabindex", "-1");
    const status = element("p", "form-status");
    status.setAttribute("role", "status");

    const form = element("form", "user-deadline-form");
    const dateLabel = element("label", "profile-field");
    dateLabel.appendChild(element("span", "", "Deadline"));
    const dateInput = document.createElement("input");
    dateInput.type = "date";
    dateInput.required = true;
    dateInput.min = "2020-01-01";
    dateInput.max = "2100-12-31";
    dateLabel.appendChild(dateInput);
    const noteLabel = element("label", "profile-field");
    noteLabel.appendChild(element("span", "", "Where you saw it (optional)"));
    const noteInput = document.createElement("input");
    noteInput.type = "text";
    noteInput.maxLength = 200;
    noteInput.placeholder = "Careers page, recruiter email…";
    noteLabel.appendChild(noteInput);
    const save = element("button", "primary-button", "Save deadline");
    save.type = "submit";
    form.append(dateLabel, noteLabel, save);

    const clear = element("button", "secondary-button", "Clear deadline");
    clear.type = "button";

    function paint() {
      const value = item.user_deadline;
      current.textContent = value
        ? `You entered ${formatCalendarDate(value.deadline_on)}${value.note ? ` · ${value.note}` : ""}.`
        : "No deadline entered. Most postings list none; add the one you find on the employer's site.";
      dateInput.value = value?.deadline_on || "";
      noteInput.value = value?.note || "";
      form.hidden = !item.can_set_user_deadline;
      clear.hidden = !value;
    }

    function refreshBehind() {
      if (["discover", "saved", "urgent"].includes(state.view)) loadCurrentView();
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      save.disabled = true;
      status.textContent = "";
      try {
        item.user_deadline = await api(`/api/v1/opportunities/${encodeURIComponent(item.id)}/deadline`, {
          method: "PUT",
          body: JSON.stringify({ deadline_on: dateInput.value, note: noteInput.value }),
        });
        paint();
        status.textContent = "Saved. It now appears in Urgent as a date you entered.";
        refreshBehind();
      } catch (error) {
        status.textContent = error.message;
      } finally {
        save.disabled = false;
      }
    });
    clear.addEventListener("click", async () => {
      clear.disabled = true;
      status.textContent = "";
      try {
        await api(`/api/v1/opportunities/${encodeURIComponent(item.id)}/deadline`, { method: "DELETE" });
        item.user_deadline = null;
        paint();
        status.textContent = "Cleared.";
        (item.can_set_user_deadline ? dateInput : current).focus();
        refreshBehind();
      } catch (error) {
        status.textContent = error.message;
      } finally {
        clear.disabled = false;
      }
    });

    paint();
    section.append(current, form, clear, status);
    return section;
  }

  function detailLine(label, value) {
    const row = element("div", "detail-line");
    row.appendChild(element("span", "", label));
    row.appendChild(element("strong", "", value || "Not provided"));
    return row;
  }

  function jevProbabilityDetails(answer) {
    const details = element("details", "jev-probabilities");
    details.appendChild(element("summary", "", "Probability distribution"));
    const list = element("ul", "reason-list");
    Object.entries(answer.probabilities || {})
      .sort((left, right) => Number(right[1]) - Number(left[1]))
      .forEach(([label, probability]) => {
        list.appendChild(element("li", "", `${label.replaceAll("_", " ")} · ${Math.round(Number(probability) * 100)}%`));
      });
    details.appendChild(list);
    return details;
  }

  function renderJevReview(host, result) {
    const cards = element("div", "jev-answer-grid");
    Object.values(result.answers || {}).forEach((answer) => {
      const card = element("article", "jev-answer");
      card.appendChild(element("h4", "", answer.name));
      const value = answer.type === "score"
        ? `${answer.score}/${answer.maximum}`
        : String(answer.choice || "unclear").replaceAll("_", " ");
      card.appendChild(element("strong", "jev-answer-value", value));
      card.appendChild(element("p", "", answer.label));
      if (Number.isFinite(answer.confidence)) {
        card.appendChild(element("small", "", `Jev confidence · ${Math.round(answer.confidence * 100)}%`));
      }
      card.appendChild(jevProbabilityDetails(answer));
      cards.appendChild(card);
    });
    const metadata = element("p", "jev-meta",
      `Unconfirmed AI suggestion · ${result.model_resolved} · ${result.usage?.input_tokens || 0} input tokens · score unchanged`);
    const disclosure = element("details", "jev-disclosure");
    disclosure.appendChild(element("summary", "", "Data sent for this review"));
    disclosure.appendChild(element("p", "", `Posting fields: ${(result.opportunity_fields_sent || []).join(", ") || "none"}.`));
    disclosure.appendChild(element("p", "", `Profile fields: ${(result.profile_fields_sent || []).join(", ") || "none"}.`));
    host.replaceChildren(cards, metadata, element("p", "score-note", result.notice), disclosure);
  }

  // Which of the student's résumés goes with a role (student/resume_variants.py).
  function resumePickText(pick) {
    if (!pick) return "";
    const label = pick.label || "your résumé";
    if (pick.picked_by === "student") return `Résumé: ${label} (your choice)`;
    if (pick.status === "unsure") return `Résumé: ${label}, the default: couldn't tell which variant fits`;
    const matched = Array.isArray(pick.matched) && pick.matched.length ? ` (matched ${pick.matched.join(", ")})` : "";
    return `Résumé: ${label}${matched}`;
  }

  function resumeOptionText(option) {
    if (option.label) return `${option.label} (${option.original_name})`;
    return option.original_name || "Résumé";
  }

  // The résumé for this role: the pick in force (or what the words suggest),
  // a dropdown to change it, and the confirmed skills the posting names that
  // the résumé never mentions. Informational; nothing is edited.
  function resumePickSection(item) {
    const section = element("section", "detail-section resume-pick");
    section.appendChild(element("p", "eyebrow", "Résumé for this role"));
    const current = element("p", "resume-pick-current", "Checking your résumés…");
    const control = element("div", "resume-pick-control");
    const status = element("p", "form-status");
    status.setAttribute("role", "status");
    const check = element("div", "resume-check");
    section.append(current, control, status, check);
    let view = null;
    const stale = () => state.detailItem !== item || !section.isConnected;

    function paintCheck(result) {
      check.replaceChildren();
      const terms = Array.isArray(result?.terms) ? result.terms : [];
      if (!terms.length) {
        // A check that could not run says so, so it is not mistaken for one that found nothing missing.
        // With no confirmed résumé, the line above already says so.
        if (result?.note && result.resume_file_id) check.appendChild(element("p", "resume-check-note", `${result.note}.`));
        return;
      }
      const list = element("ul", "reason-list is-gap");
      terms.forEach((term) => {
        list.appendChild(element("li", "", `This posting asks for ${term}. Your profile lists ${term}, but your ${result.label} résumé doesn't mention it.`));
      });
      check.append(element("p", "resume-check-note", "From your confirmed skills only. Nothing is changed."), list);
    }

    async function refreshCheck() {
      try {
        const result = await api(`/api/v1/opportunities/${encodeURIComponent(item.id)}/resume-check`);
        if (!stale()) paintCheck(result);
      } catch (error) {
        if (!stale() && !isAuthError(error)) check.replaceChildren(element("p", "form-error", `The skills check could not run: ${error.message}`));
      }
    }

    function paint() {
      const pick = view.pick;
      const suggestion = view.suggestion || {};
      if (pick) {
        current.textContent = resumePickText(pick);
      } else if (!view.options.length) {
        current.textContent = "No confirmed résumé yet. Confirm one on your Profile page to use it here.";
      } else if (!view.configured) {
        current.textContent = "No résumé variants are listed under resume_variants in your profile file, so the app uses your confirmed résumé, as before.";
      } else if (suggestion.resume_file_id) {
        const suggested = resumePickText({ ...suggestion, picked_by: "automatic" }).replace(/^Résumé: /, "");
        // A pick is made only when a save changes the role, with the switch on and automation not paused.
        let next;
        if (view.saved) next = "Nothing was picked when this role was saved. Choose a résumé below to set one.";
        else if (!view.enabled) next = "Turn on the résumé variant switch under Automation to have it picked when you save.";
        else if (view.paused) next = "Automation is paused, so nothing is picked when you save. Choose a résumé below to set one.";
        else next = "It is picked when you save this role.";
        current.textContent = `Suggested: ${suggested}. ${next}`;
      } else {
        current.textContent = suggestion.reason ? `${suggestion.reason}.` : "No variant fits this role yet.";
      }
      control.replaceChildren();
      if (!view.options.length) return;
      const label = element("label", "profile-field");
      label.appendChild(element("span", "", "Change résumé"));
      const select = document.createElement("select");
      if (!pick) {
        select.appendChild(optionElement("", "Choose a résumé for this role"));
      }
      view.options.forEach((option) => {
        select.appendChild(optionElement(option.resume_file_id, resumeOptionText(option)));
      });
      select.value = pick?.resume_file_id || "";
      label.appendChild(select);
      control.appendChild(label);
      autoSaveSelect(select, {
        saved: () => view.pick?.resume_file_id || "",
        commit: async (value, trigger) => {
          if (!value) {
            select.value = view.pick?.resume_file_id || "";
            return;
          }
          select.disabled = true;
          status.textContent = "Saving…";
          try {
            view = await api(`/api/v1/opportunities/${encodeURIComponent(item.id)}/resume-pick`, {
              method: "PUT",
              body: JSON.stringify({ resume_file_id: value }),
            });
            if (stale()) return;
            item.resume_pick = view.pick;
            paint();
            status.textContent = `Using ${view.pick?.label || "that résumé"} for this role. The app will not change it.`;
            // A save made by leaving the select leaves focus where the student went.
            const focused = document.activeElement;
            if (trigger !== "blur" || !focused || focused === document.body || !focused.isConnected) {
              control.querySelector("select")?.focus();
            }
            refreshCheck();
            // The card behind the panel shows the pick too, so it is drawn again with the new one.
            if (["discover", "saved", "urgent"].includes(state.view)) loadCurrentView();
          } catch (error) {
            if (stale()) return;
            select.value = view.pick?.resume_file_id || "";
            select.disabled = false;
            if (!isAuthError(error)) status.textContent = error.message;
          }
        },
      });
    }

    api(`/api/v1/opportunities/${encodeURIComponent(item.id)}/resume-pick`).then((payload) => {
      if (stale()) return;
      view = { ...payload, options: Array.isArray(payload?.options) ? payload.options : [] };
      paint();
      refreshCheck();
    }).catch((error) => {
      if (stale() || isAuthError(error)) return;
      current.textContent = `Your résumé choice could not be loaded: ${error.message}`;
    });
    return section;
  }

  function jevReviewSection(item) {
    const section = element("section", "detail-section jev-review");
    section.appendChild(element("p", "eyebrow", "Jev second opinion"));
    section.appendChild(element("p", "jev-intro",
      "Run an on-demand semantic review. It sends the posting plus your degree, graduation, skills and interests, location and term preferences, and any authorization, citizenship, or sponsorship answers to TypeSafe. It returns probabilities and never changes this score."));
    const status = element("p", "form-status", "Checking Jev setup…");
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    const button = element("button", "secondary-button", "Run Jev review");
    button.type = "button";
    button.disabled = true;
    const output = element("div", "jev-output");
    section.append(status, button, output);

    api("/api/v1/typesafe").then((configuration) => {
      if (!configuration.configured) {
        status.textContent = `Jev is optional. ${configuration.setup_hint || "Set TYPESAFE_API_KEY in .env and restart the app to enable it."}`;
        return;
      }
      status.textContent = `Ready · ${configuration.model} · nothing is sent until you click.`;
      button.disabled = false;
    }).catch((error) => {
      status.textContent = `Jev setup could not be checked: ${error.message}`;
    });

    button.addEventListener("click", async () => {
      button.disabled = true;
      status.textContent = "Jev is reviewing eight narrow questions…";
      output.replaceChildren();
      try {
        const result = await api(`/api/v1/opportunities/${encodeURIComponent(item.id)}/jev-review`, {
          method: "POST",
        });
        renderJevReview(output, result);
        status.textContent = "Review complete. Treat every result as a suggestion to verify.";
      } catch (error) {
        status.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    });
    return section;
  }

  async function openDetail(id, { updateHistory = true } = {}) {
    if (!state.selectedId) {
      const opener = document.activeElement;
      state.detailReturnFocus = opener && opener !== document.body && els.appShell.contains(opener) ? opener : null;
    }
    state.selectedId = id;
    setBackgroundInert("detail", true);
    els.detail.hidden = false;
    els.detail.classList.add("is-open");
    els.detail.setAttribute("aria-hidden", "false");
    els.detail.removeAttribute("inert");
    els.detailScrim.hidden = false;
    els.detailContent.replaceChildren(element("p", "detail-loading", "Loading opportunity…"));
    document.body.classList.add("has-panel");
    if (updateHistory) {
      state.detailReturnPath = window.location.pathname;
      window.history.pushState({ opportunityId: id }, "", `/opportunities/${encodeURIComponent(id)}`);
    }
    try {
      const item = await api(`/api/v1/opportunities/${encodeURIComponent(id)}`);
      renderDetail(item);
      els.detailClose.focus();
    } catch (error) {
      els.detailContent.replaceChildren(element("p", "form-error", error.message));
    }
  }

  function renderDetail(item) {
    state.detailItem = item;
    const content = document.createDocumentFragment();
    content.appendChild(element("p", "detail-company", item.company));
    const title = element("h2", "", item.title);
    title.id = "detail-title";
    content.appendChild(title);

    const scoreBlock = element("div", "detail-score");
    scoreBlock.appendChild(element("strong", "", `${item.score}/100`));
    const scoreCopy = element("div");
    scoreCopy.appendChild(element("span", "", "Transparent fit score"));
    const progress = document.createElement("progress");
    progress.max = 100;
    progress.value = item.score;
    progress.setAttribute("aria-label", `Fit score ${item.score} out of 100`);
    scoreCopy.appendChild(progress);
    scoreBlock.appendChild(scoreCopy);
    content.appendChild(scoreBlock);

    const facts = element("div", "detail-facts");
    const compensation = item.compensation?.known
      ? `${item.compensation.currency} ${item.compensation.minimum}${item.compensation.maximum !== item.compensation.minimum ? `–${item.compensation.maximum}` : ""} per ${item.compensation.period}`
      : "Not listed";
    facts.append(
      detailLine("Location", item.location),
      detailLine("Region", item.region),
      detailLine("Work mode", item.remote_mode),
      detailLine("Role type", item.role_type),
      detailLine("Term", item.terms?.join(", ") || "Not detected"),
      detailLine("Compensation", compensation),
      detailLine("Posted", formatCalendarDate(item.posted_at)),
      detailLine(
        "Deadline in posting text",
        `${formatCalendarDate(item.deadline_at)}${deadlineState(item.deadline_at) === "passed" ? " (passed)" : ""}`
      ),
      detailLine("Last checked", formatDate(item.last_seen_at)),
      detailLine("Source", item.source_name),
      detailLine("Score version", `${item.score_version || "unknown"} · ${formatDate(item.score_created_at)}`)
    );
    content.appendChild(facts);
    const deadlineSection = userDeadlineSection(item);
    if (deadlineSection) content.appendChild(deadlineSection);
    if (item.company_sort_key) content.appendChild(companyTagsSection(item));

    const why = element("section", "detail-section");
    why.appendChild(element("p", "eyebrow", "Why it ranked here"));
    const reasons = element("ul", "reason-list");
    // Show the starting points too, so the listed adjustments account for the
    // whole score instead of leaving part of it unexplained.
    const adjustments = (item.reasons || []).map((reason) => /^([+-])(\d+)\s/.exec(reason)).filter(Boolean);
    if (Number.isInteger(item.score_base) && adjustments.length) {
      reasons.appendChild(element("li", "", `Starting score +${item.score_base}`));
    }
    (item.reasons?.length ? item.reasons : [baseScoreReason()]).forEach((reason) => {
      reasons.appendChild(element("li", "", reason));
    });
    why.appendChild(reasons);
    if (Number.isInteger(item.score_base) && adjustments.length) {
      const listed = adjustments.reduce((total, [, sign, points]) => total + (sign === "+" ? 1 : -1) * Number(points), item.score_base);
      if (listed !== item.score) {
        const note = listed > 100 || listed < 0
          ? `These add up to ${listed}; scores are capped between 0 and 100.`
          : `These add up to ${listed}; some smaller adjustments are not listed.`;
        why.appendChild(element("p", "score-note", note));
      }
    }
    const evidence = element("div", "evidence-list");
    (item.score_evidence || []).forEach((entry) => {
      evidence.appendChild(element(
        "p",
        "",
        `Evidence: profile.${entry.profile_field} ↔ posting ${entry.opportunity_fields.join(", ")}`
      ));
    });
    why.appendChild(evidence);
    content.appendChild(why);

    if (item.gaps?.length) {
      const gaps = element("section", "detail-section");
      gaps.appendChild(element("p", "eyebrow", "Potential gaps to verify"));
      const gapList = element("ul", "reason-list is-gap");
      item.gaps.forEach((gap) => gapList.appendChild(element("li", "", gap)));
      gaps.appendChild(gapList);
      content.appendChild(gaps);
    }

    content.appendChild(resumePickSection(item));
    const applySection = applyForMeSection(item);
    if (applySection) content.appendChild(applySection);
    content.appendChild(jevReviewSection(item));

    const overview = element("section", "detail-section");
    overview.appendChild(element("p", "eyebrow", "Role overview"));
    overview.appendChild(element("p", "detail-description", item.description || "No description was captured. Use the original posting below."));
    content.appendChild(overview);

    const sourceLink = externalLink(item.url, "Open original posting ↗", { className: "primary-link" });
    content.appendChild(sourceLink);

    els.detailContent.replaceChildren(content);
  }

  function closeDetail({ updateHistory = true } = {}) {
    const closedId = state.selectedId;
    const wasOpen = !els.detail.hidden;
    state.selectedId = null;
    els.detail.classList.remove("is-open");
    els.detail.setAttribute("aria-hidden", "true");
    els.detail.setAttribute("inert", "");
    els.detail.hidden = true;
    els.detailScrim.hidden = true;
    document.body.classList.remove("has-panel");
    setBackgroundInert("detail", false);
    if (updateHistory && window.location.pathname !== state.detailReturnPath) {
      window.history.pushState({}, "", state.detailReturnPath || "/");
    }
    const opener = state.detailReturnFocus;
    state.detailReturnFocus = null;
    if (!wasOpen || inertClaims.size) return;
    // Return focus to what opened the panel. A re-render may have replaced that
    // card, so fall back to the same opportunity's card, then the results.
    const target = opener?.isConnected
      ? opener
      : els.results.querySelector(`[data-opportunity-id="${CSS.escape(closedId || "")}"] .card-button`) || els.results;
    if (target === els.results && !els.results.hasAttribute("tabindex")) els.results.setAttribute("tabindex", "-1");
    target.focus();
  }

  Object.assign(App, {
    closeDetail, openDetail, resumePickText,
  });
})();
