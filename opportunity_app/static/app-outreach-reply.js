// Outreach, part 3b: the student's answer to a company's reply, and the note for a revisit date (the Your reply tab).
// The server drafts it (outreach/reply_drafts.py); here the student reads it, edits it, approves it, and sends it with
// two presses, in the company's Gmail thread.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { state } = App;

  // From app-ui.js.
  const { announce, armConfirm, chip, element, externalLink, formatCalendarDate, formatDateTime, showError } = App;

  // From app-http.js.
  const { api } = App;

  // From app-automation.js.
  const { automationFeature } = App;

  // From app-outreach-send.js.
  const { reloadOutreachAt, sentFolderCheck } = App;

  // From app-outreach-drafts.js.
  const { renderDraftChecks } = App;

  // Statuses whose latest reply may be answered (outreach/reply_drafts.py ANSWER_STATUSES).
  const ANSWER_STATUSES = ["sent", "followed_up", "replied", "paused", "call_scheduled", "declined"];
  const OPEN = ["generated", "approved"];
  // How the tab says what their reply was read as.
  const READING_WORDS = {
    paused: "asks you to get back in touch later",
    declined: "says no for now",
    call_scheduled: "offers a call or a meeting",
    replied: "says something else, or asks you something",
  };

  const answerPath = (item, tail = "") => `/api/v1/outreach/${encodeURIComponent(item.id)}/reply-draft${tail}`;

  // The tab is there once a company has written back, or a revisit date has come, or a reply is on record.
  function outreachHasAnswer(item) {
    return Boolean(item.reply_draft) || Boolean(item.revisit_due) || (item.reply_count > 0 && ANSWER_STATUSES.includes(item.status));
  }

  function answerWhat(draft) {
    return draft?.purpose === "revisit" ? "revisit note" : "reply";
  }

  // The next step a waiting reply makes, for the bar and the list row; null when there is none.
  function outreachAnswerNextStep(item) {
    const draft = item.reply_draft;
    if (!draft || !OPEN.includes(draft.state)) return null;
    const what = answerWhat(draft);
    if (draft.behind) return { label: `Rewrite your ${what}`, hint: "They wrote again since it was written. Read their message, then write it again.", tab: "answer", tone: "is-warning" };
    if (draft.state === "approved") return { label: `Send your ${what}`, hint: "It is approved. Send it from Your reply.", tab: "answer", tone: "is-warning" };
    const blanks = draft.checks?.placeholders || [];
    if (blanks.length) return { label: `Finish your ${what}`, hint: `Fill in ${blanks.join(", ")}, then approve it.`, tab: "answer", tone: "is-warning" };
    return { label: `Review your ${what}`, hint: `Your ${what} is drafted. Read it, change anything, then approve and send it.`, tab: "answer", tone: "is-warning" };
  }

  // Every write here reloads the company afterwards, so the card shows what the server now holds.
  async function answerRequest(item, tail, options, done) {
    try {
      await api(answerPath(item, tail), options);
      await reloadOutreachAt(item.id);
      if (done) announce(done);
      return true;
    } catch (error) {
      if (error.status === 409) await reloadOutreachAt(item.id);
      showError(error.message);
      return false;
    }
  }

  function actionButton(text, className, onClick, ariaLabel = "") {
    const button = element("button", className, text);
    button.type = "button";
    if (ariaLabel) button.setAttribute("aria-label", ariaLabel);
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await onClick(button);
      } finally {
        if (button.isConnected) button.disabled = false;
      }
    });
    return button;
  }

  function writeButton(item, text) {
    return actionButton(text, "primary-button", async (button) => {
      button.textContent = "Writing…";
      await answerRequest(item, "", { method: "POST", body: JSON.stringify({}) }, `Wrote a ${text.includes("revisit") ? "revisit note" : "reply"} for ${item.company}. Read it before approving.`);
    });
  }

  function switchHint() {
    const feature = automationFeature(state.automation?.settings, "reply_drafts");
    if (!feature || feature.mode === "on") return null;
    return element("p", "outreach-note", `${feature.label} is off, so replies are written only when you press the button. Turn it on in Profile → Automation to have one waiting whenever a company writes back.`);
  }

  function recipientsLine(draft) {
    const to = draft.to_name ? `${draft.to_name} <${draft.to_email}>` : draft.to_email;
    const where = draft.thread_id ? "in their Gmail thread" : "as a new email";
    return element("p", "outreach-fit", `To ${to}${draft.cc ? `, Cc ${draft.cc}` : ""}, ${where}.`);
  }

  function revisitLine(item, draft) {
    if (!draft.revisit_on) return null;
    const line = element("div", "outreach-answer-revisit");
    line.appendChild(element("p", "outreach-fit", `They named a time to get back in touch: “${draft.revisit_said}”.`));
    if (item.follow_up_at === draft.revisit_on) {
      line.appendChild(chip(`Revisit on ${formatCalendarDate(draft.revisit_on)} is set`, "is-region"));
    } else {
      line.appendChild(actionButton(`Revisit on ${formatCalendarDate(draft.revisit_on)}`, "secondary-button", async () => {
        await answerRequest(item, "/revisit", { method: "POST" }, `${item.company} comes back on ${formatCalendarDate(draft.revisit_on)}.`);
      }, `Set ${item.company}'s revisit date to ${formatCalendarDate(draft.revisit_on)}`));
    }
    return line;
  }

  function claimsList(draft) {
    if (!draft.claims?.length) return null;
    const details = element("details", "outreach-answer-claims");
    details.appendChild(element("summary", "", "What it rests on"));
    const list = element("ul");
    draft.claims.forEach((claim) => list.appendChild(element("li", "", `${claim.text} (${claim.basis})`)));
    details.appendChild(list);
    return details;
  }

  // Like Send on a first email: the first press only asks, and a second press sends. After a 428 the server named what
  // to look for in Gmail, and the next press vouches for it.
  function sendButton(item, draft, unsaved) {
    const label = `Send ${answerWhat(draft)}`;
    const button = element("button", "primary-button outreach-send", label);
    button.type = "button";
    const sentCheck = sentFolderCheck();
    const reset = armConfirm(button, {
      idleLabel: () => (sentCheck.pending ? "Checked Gmail — send again" : label),
      armedLabel: () => `Send to ${draft.to_email}?`,
      prompt: () => `Press again to send your ${answerWhat(draft)} to ${draft.to_email}.`,
      beforeClick: () => {
        if (!unsaved()) return false;
        showError("Save and approve your changes before sending.");
        return true;
      },
      onConfirm: async () => {
        button.disabled = true;
        button.textContent = "Sending…";
        const payload = { fingerprint: draft.fingerprint };
        sentCheck.attach(payload);
        try {
          await api(answerPath(item, "/send"), { method: "POST", body: JSON.stringify(payload) });
          await reloadOutreachAt(item.id);
          announce(`Sent your ${answerWhat(draft)} to ${draft.to_email}.`);
        } catch (error) {
          sentCheck.note(error);
          reset();
          button.disabled = false;
          if (error.status === 409 || error.status === 422) await reloadOutreachAt(item.id);
          showError(error.message);
        }
      },
    });
    return button;
  }

  function editor(item, draft) {
    const wrap = element("div", "outreach-answer-editor");
    const subjectLabel = element("label", "profile-field outreach-answer-field");
    subjectLabel.appendChild(element("span", "", "Subject"));
    const subject = document.createElement("input");
    subject.type = "text";
    subject.value = draft.subject || "";
    subject.className = "outreach-answer-subject";
    subjectLabel.appendChild(subject);
    const bodyLabel = element("label", "profile-field outreach-answer-field");
    bodyLabel.appendChild(element("span", "", "Words"));
    const body = document.createElement("textarea");
    body.rows = 11;
    body.value = draft.body || "";
    body.className = "outreach-answer-body";
    bodyLabel.appendChild(body);
    const checks = element("div", "draft-checks");
    const unsaved = () => subject.value !== (draft.subject || "") || body.value !== (draft.body || "");
    const paint = () => renderDraftChecks(checks, subject.value, body.value);
    subject.addEventListener("input", paint);
    body.addEventListener("input", paint);
    paint();
    wrap.append(subjectLabel, bodyLabel, checks);

    // Saves the words when they changed, and returns the fingerprint the server now holds for them.
    async function saved() {
      if (!unsaved()) return draft.fingerprint;
      const result = await api(answerPath(item), {
        method: "PATCH", body: JSON.stringify({ subject: subject.value, body: body.value, fingerprint: draft.fingerprint }),
      });
      return result.reply_draft?.fingerprint || "";
    }

    const actions = element("div", "outreach-draft-buttons");
    const save = actionButton("Save edits", "secondary-button", async () => {
      if (!unsaved()) return announce("Nothing to save.");
      try {
        await saved();
        await reloadOutreachAt(item.id);
        announce("Saved your changes. Approve it again before sending.");
      } catch (error) {
        if (error.status === 409) await reloadOutreachAt(item.id);
        showError(error.message);
      }
    });
    const approve = actionButton(draft.state === "approved" ? "Approved" : "Approve", "primary-button", async () => {
      try {
        const fingerprint = await saved();
        await api(answerPath(item, "/approve"), { method: "POST", body: JSON.stringify({ fingerprint }) });
        await reloadOutreachAt(item.id);
        announce(`Approved your ${answerWhat(draft)}. Send it when you're ready.`);
      } catch (error) {
        if (error.status === 409) await reloadOutreachAt(item.id);
        showError(error.message);
      }
    });
    approve.disabled = draft.state === "approved";
    const gmail = actionButton("Put in Gmail Drafts", "secondary-button", async () => {
      if (unsaved()) return showError("Save your changes first.");
      await answerRequest(item, "/gmail-draft", { method: "POST" }, `Your ${answerWhat(draft)} is in your Gmail Drafts, in their thread. Send it from there; the app will not send it.`);
    }, `Put your ${answerWhat(draft)} in your Gmail Drafts to finish there`);
    const dismiss = actionButton("Dismiss", "secondary-button", async () => {
      await answerRequest(item, "", { method: "DELETE" }, `Dismissed the ${answerWhat(draft)}. It won't be written again for this message.`);
    });
    if (draft.state === "approved") actions.appendChild(sendButton(item, draft, unsaved));
    actions.append(approve, save, gmail, dismiss);
    wrap.appendChild(actions);

    const rewrite = element("div", "outreach-answer-rewrite");
    const commentsLabel = element("label", "profile-field outreach-answer-field");
    commentsLabel.appendChild(element("span", "", "What to change (optional)"));
    const comments = document.createElement("textarea");
    comments.rows = 2;
    comments.className = "outreach-answer-comments";
    comments.placeholder = "Shorter, mention the visit first…";
    commentsLabel.appendChild(comments);
    const again = actionButton("Rewrite", "secondary-button", async (button) => {
      if (unsaved() && !window.confirm("Rewriting replaces the words in the box, and your unsaved changes with them. Rewrite anyway?")) return;
      button.textContent = "Writing…";
      await answerRequest(item, "", { method: "POST", body: JSON.stringify({ comments: comments.value }) }, "Rewrote it. Read it before approving.");
    });
    rewrite.append(commentsLabel, again);
    wrap.appendChild(rewrite);
    return wrap;
  }

  function outreachAnswerPanel(item) {
    const section = element("section", "tracker-subsection outreach-answer");
    const draft = item.reply_draft;
    section.appendChild(element("h4", "", answerWhat(draft) === "revisit note" ? "Note for your revisit date" : "Your reply"));
    const hint = switchHint();
    if (!draft || !OPEN.includes(draft.state)) {
      if (draft?.state === "sent") {
        section.appendChild(element("p", "outreach-fit", `Your ${answerWhat(draft)} to ${draft.to_email} was sent ${formatDateTime(draft.sent_at)}.`));
        if (draft.thread_url) section.appendChild(externalLink(draft.thread_url, "Open the thread in Gmail ↗", { className: "secondary-button" }));
        section.appendChild(element("pre", "outreach-answer-sent", draft.body));
      } else if (draft?.state === "in_gmail") {
        section.appendChild(element("p", "outreach-fit", `Your ${answerWhat(draft)} is in your Gmail Drafts, in their thread, to finish and send there.`));
        if (/^https:\/\/mail\.google\.com\//.test(draft.note || "")) section.appendChild(externalLink(draft.note, "Open the draft in Gmail ↗", { className: "secondary-button" }));
      } else if (draft?.note && ["dismissed", "stale", "skipped"].includes(draft.state)) {
        section.appendChild(element("p", "outreach-note", `${draft.note}.`.replace(/\.\.$/, ".")));
      }
      const revisit = Boolean(item.revisit_due);
      if (revisit || (item.reply_count > 0 && ANSWER_STATUSES.includes(item.status))) {
        section.appendChild(element("p", "outreach-note", revisit
          ? `Your revisit date (${formatCalendarDate(item.follow_up_at)}) has come. The app can write the note to send them.`
          : "The app writes your answer to their latest reply from your profile and the thread. You read it, approve it, and send it."));
        section.appendChild(writeButton(item, revisit ? "Write the revisit note" : "Write a reply"));
      }
      if (hint) section.appendChild(hint);
      return section;
    }
    section.appendChild(recipientsLine(draft));
    if (draft.purpose === "answer" && READING_WORDS[draft.reading]) {
      section.appendChild(element("p", "outreach-fit", `Their reply ${READING_WORDS[draft.reading]}.`));
    }
    if (draft.behind) section.appendChild(element("p", "outreach-research-warning", "They wrote again since this was written, so it answers an older message. Rewrite it before sending."));
    if (draft.note) section.appendChild(element("p", "outreach-research-warning", draft.note));
    const revisit = revisitLine(item, draft);
    if (revisit) section.appendChild(revisit);
    const tags = element("div", "outreach-answer-tags");
    tags.appendChild(chip(draft.state === "approved" ? "Approved" : "Waiting for your approval", draft.state === "approved" ? "is-region" : "is-soon"));
    tags.appendChild(chip(draft.generated_by === "template" ? "Written from a template" : `Written by ${draft.generated_by}`));
    section.appendChild(tags);
    section.appendChild(editor(item, draft));
    const claims = claimsList(draft);
    if (claims) section.appendChild(claims);
    if (hint) section.appendChild(hint);
    return section;
  }

  Object.assign(App, { outreachAnswerNextStep, outreachAnswerPanel, outreachHasAnswer });
})();
