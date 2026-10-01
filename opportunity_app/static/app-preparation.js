// Prepare: documents, saved answers, interview practice, and the page loader.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { els, registerViewHandlers, state } = App;

  // From app-ui.js.
  const { chip, commaList, element, formatDate, optionElement, plural, showError } = App;

  // From app-http.js.
  const { api } = App;

  // From app-nav.js.
  const { loadingLine, runViewLoad, sectionSubnav, tagSection } = App;

  function opportunityOptions(select, applications) {
    applications.forEach((application) => {
      select.appendChild(optionElement(application.opportunity_id, `${application.company} — ${application.title}`));
    });
  }

  function preparationDocumentCard(documentRecord) {
    const card = element("article", "preparation-item");
    const heading = element("div", "preparation-heading");
    const identity = element("div");
    identity.appendChild(element("strong", "", `${documentRecord.document_type.replaceAll("_", " ")} v${documentRecord.version}`));
    identity.appendChild(element("span", "", `${documentRecord.company} · ${documentRecord.title}`));
    heading.append(identity, chip(documentRecord.status, documentRecord.status === "approved" ? "is-region" : ""));
    const editor = document.createElement("textarea");
    editor.className = "document-editor";
    editor.value = documentRecord.content;
    editor.setAttribute("aria-label", `Edit ${documentRecord.document_type} version ${documentRecord.version}`);
    const evidence = element("div", "document-evidence");
    evidence.appendChild(element("p", "eyebrow", "Confirmed evidence"));
    const fields = [...new Set((documentRecord.evidence || []).map((item) => item.profile_field))];
    fields.forEach((field) => evidence.appendChild(chip(`profile.${field}`, "is-region")));
    if (!fields.some((field) => field !== "name")) {
      // A grounded draft can only use confirmed facts; say why this one is thin.
      evidence.appendChild(element("p", "score-note", "Add confirmed skills and experience in Profile to fill this draft."));
    }
    const actions = element("div", "preparation-actions");
    const save = element("button", "secondary-button", "Save edit");
    save.type = "button";
    const approve = element("button", "secondary-button", documentRecord.status === "approved" ? "Approved" : "Approve version");
    approve.type = "button";
    approve.disabled = documentRecord.status === "approved";
    const download = element("a", "secondary-button", "Download Markdown");
    download.href = `/api/v1/preparation/documents/${encodeURIComponent(documentRecord.id)}/download`;
    const remove = element("button", "danger-button", "Delete version");
    remove.type = "button";
    const statusLine = element("p", "form-status");
    statusLine.setAttribute("aria-live", "polite");
    // Fetched rather than linked, so a missing browser build shows a message
    // here instead of navigating the tab to an error page.
    const pdf = element("button", "secondary-button", "Download PDF");
    pdf.type = "button";
    pdf.hidden = !state.pdfAvailable;
    pdf.addEventListener("click", async () => {
      pdf.disabled = true;
      statusLine.textContent = "Rendering the PDF…";
      try {
        const response = await fetch(`/api/v1/preparation/documents/${encodeURIComponent(documentRecord.id)}/pdf`, { credentials: "same-origin" });
        if (!response.ok) {
          const detail = await response.json().catch(() => ({}));
          throw new Error(detail.detail || `The PDF could not be made (HTTP ${response.status}).`);
        }
        const url = URL.createObjectURL(await response.blob());
        const link = document.createElement("a");
        link.href = url;
        link.download = `${documentRecord.document_type}-v${documentRecord.version}.pdf`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
        statusLine.textContent = "PDF downloaded. Check it before you send it.";
      } catch (error) {
        statusLine.textContent = error.message;
      } finally {
        pdf.disabled = false;
      }
    });
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        await api(`/api/v1/preparation/documents/${encodeURIComponent(documentRecord.id)}`, {
          method: "PUT",
          body: JSON.stringify({ content: editor.value, evidence_fields: fields }),
        });
        statusLine.textContent = "Saved as an evidence-linked draft.";
        approve.disabled = false;
        approve.textContent = "Approve version";
      } catch (error) {
        statusLine.textContent = error.message;
      } finally {
        save.disabled = false;
      }
    });
    remove.addEventListener("click", async () => {
      if (!window.confirm("Delete this document version and its attachable PDF?")) return;
      await api(`/api/v1/preparation/documents/${encodeURIComponent(documentRecord.id)}`, {method: "DELETE"});
      await loadPreparation();
    });
    approve.addEventListener("click", async () => {
      approve.disabled = true;
      try {
        await api(`/api/v1/preparation/documents/${encodeURIComponent(documentRecord.id)}/approve`, { method: "POST" });
        approve.textContent = "Approved";
        statusLine.textContent = "Approved for your use. Nothing was sent.";
      } catch (error) {
        approve.disabled = false;
        statusLine.textContent = error.message;
      }
    });
    actions.append(save, approve, download, pdf, remove);
    card.append(heading, editor, evidence, actions, statusLine);
    if (documentRecord.diff) {
      const diff = element("details", "document-diff");
      diff.appendChild(element("summary", "", "Compare with previous version"));
      diff.appendChild(element("pre", "", documentRecord.diff));
      card.appendChild(diff);
    }
    return card;
  }

  // The recording streams from the server with the session cookie; nothing
  // leaves the machine, and preload="none" fetches it only when played.
  function answerRecording(answerId, label) {
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.preload = "none";
    audio.src = `/api/v1/preparation/answers/${encodeURIComponent(answerId)}/audio`;
    audio.setAttribute("aria-label", label);
    return audio;
  }

  function earlierAnswers(question, index) {
    const answers = question.answers || [];
    if (!answers.length) return null;
    const details = element("details", "interview-earlier");
    details.appendChild(element("summary", "", plural(answers.length, "earlier answer", "earlier answers")));
    answers.forEach((answer) => {
      const item = element("div", "interview-earlier-answer");
      item.appendChild(element("p", "eyebrow", `${formatDate(answer.created_at)} · ${answer.score}/100`));
      item.appendChild(element("p", "", answer.answer_text));
      if (answer.has_audio) item.appendChild(answerRecording(answer.id, `Recording of your answer to question ${index + 1}, ${formatDate(answer.created_at)}`));
      (answer.feedback || []).forEach((line) => item.appendChild(element("p", "profile-help", line)));
      details.appendChild(item);
    });
    return details;
  }

  function renderInterview(interview, host) {
    host.replaceChildren();
    host.appendChild(element("h3", "", `${interview.company}: ${interview.title}`));
    interview.questions.forEach((question, index) => {
      const card = element("article", "interview-question");
      card.appendChild(element("p", "eyebrow", `Question ${index + 1}`));
      card.appendChild(element("h4", "", question.prompt));
      const promptActions = element("div", "preparation-actions");
      const speak = element("button", "secondary-button", "Read aloud");
      speak.type = "button";
      speak.addEventListener("click", () => {
        if (!("speechSynthesis" in window)) return;
        window.speechSynthesis.cancel();
        window.speechSynthesis.speak(new SpeechSynthesisUtterance(question.prompt));
      });
      promptActions.appendChild(speak);
      card.appendChild(promptActions);
      const earlier = earlierAnswers(question, index);
      if (earlier) card.appendChild(earlier);
      const form = element("form", "interview-answer-form");
      const answer = document.createElement("textarea");
      answer.placeholder = "Type your answer, or use voice transcription and review the text before submitting.";
      answer.required = true;
      const voice = element("button", "secondary-button", "Transcribe voice");
      voice.type = "button";
      voice.setAttribute("aria-pressed", "false");
      const record = element("button", "secondary-button", "Record answer");
      record.type = "button";
      record.setAttribute("aria-pressed", "false");
      const recordingStatus = element("p", "profile-help", "No audio recording saved.");
      recordingStatus.setAttribute("aria-live", "polite");
      let recordingBlob = null;
      let recorder = null;
      let recordingStream = null;
      let stopActiveRecording = null;
      const finishRecording = () => {
        recordingStream?.getTracks().forEach((track) => track.stop());
        recordingStream = null;
        recorder = null;
        if (state.activeRecordingStop === stopActiveRecording) state.activeRecordingStop = null;
        record.textContent = "Record again";
        record.setAttribute("aria-pressed", "false");
      };
      stopActiveRecording = () => {
        if (recorder?.state === "recording") recorder.stop();
        else finishRecording();
      };
      if (!("MediaRecorder" in window) || !navigator.mediaDevices?.getUserMedia) {
        record.disabled = true;
        record.title = "Audio recording is unavailable in this browser; typed and transcribed answers remain available.";
      } else {
        record.addEventListener("click", async () => {
          if (recorder?.state === "recording") {
            recorder.stop();
            return;
          }
          try {
            recordingStream = await navigator.mediaDevices.getUserMedia({ audio: true });
            const chunks = [];
            recorder = new MediaRecorder(recordingStream);
            recorder.addEventListener("dataavailable", (event) => {
              if (event.data.size) chunks.push(event.data);
            });
            recorder.addEventListener("stop", () => {
              recordingBlob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
              recordingStatus.textContent = `Recording ready for private upload (${Math.max(1, Math.ceil(recordingBlob.size / 1024))} KB).`;
              finishRecording();
            }, { once: true });
            recorder.addEventListener("error", () => {
              recordingStatus.textContent = "Recording failed; no audio was saved.";
              finishRecording();
            }, { once: true });
            recorder.start();
            state.activeRecordingStop = stopActiveRecording;
            record.textContent = "Stop recording";
            record.setAttribute("aria-pressed", "true");
            recordingStatus.textContent = "Microphone active — select Stop recording when finished.";
          } catch (_) {
            recordingStatus.textContent = "Microphone permission was not granted; no audio was saved.";
            finishRecording();
          }
        });
      }
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SpeechRecognition) {
        voice.disabled = true;
        voice.title = "Voice transcription is unavailable in this browser; the text path remains fully supported.";
      } else {
        voice.addEventListener("click", () => {
          const recognition = new SpeechRecognition();
          recognition.lang = navigator.language || "en-US";
          recognition.interimResults = false;
          voice.disabled = true;
          voice.textContent = "Microphone active…";
          voice.setAttribute("aria-pressed", "true");
          recognition.addEventListener("result", (event) => {
            const transcript = event.results[0]?.[0]?.transcript || "";
            answer.value = `${answer.value} ${transcript}`.trim();
          });
          recognition.addEventListener("end", () => {
            voice.disabled = false;
            voice.textContent = "Transcribe voice";
            voice.setAttribute("aria-pressed", "false");
          });
          recognition.addEventListener("error", () => {
            voice.disabled = false;
            voice.textContent = "Transcribe voice";
            voice.setAttribute("aria-pressed", "false");
          });
          recognition.start();
        });
      }
      const submit = element("button", "primary-button", "Score reviewed answer");
      submit.type = "submit";
      const feedback = element("div", "interview-feedback");
      form.append(answer, voice, record, recordingStatus, submit, feedback);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        submit.disabled = true;
        try {
          let path = `/api/v1/preparation/questions/${encodeURIComponent(question.id)}/answers`;
          let body = JSON.stringify({ answer_text: answer.value, transcript: "" });
          if (recordingBlob) {
            path = `/api/v1/preparation/questions/${encodeURIComponent(question.id)}/recorded-answers`;
            body = new FormData();
            body.append("answer_text", answer.value);
            body.append("transcript", answer.value);
            body.append("audio", recordingBlob, `mock-answer.${recordingBlob.type.includes("ogg") ? "ogg" : "webm"}`);
          }
          const result = await api(path, { method: "POST", body });
          feedback.replaceChildren(element("strong", "", `${result.score}/100`));
          result.feedback.forEach((item) => feedback.appendChild(element("p", "", item)));
          if (result.has_audio) {
            recordingStatus.textContent = "Recording saved privately with this scored answer.";
            feedback.appendChild(answerRecording(result.id, `Recording of your answer to question ${index + 1}`));
            recordingBlob = null;
          }
          submit.textContent = "Retry and rescore";
        } catch (error) {
          feedback.replaceChildren(element("p", "form-error", error.message));
        } finally {
          submit.disabled = false;
        }
      });
      card.appendChild(form);
      host.appendChild(card);
    });
  }

  async function loadPreparation() {
    await runViewLoad({ views: ["prepare"], placeholder: loadingLine("Loading preparation workspace…") }, async ({ isCurrent }) => {
      const [documents, answers, applications, providersPayload, interviews] = await Promise.all([
        api("/api/v1/preparation/documents"),
        api("/api/v1/preparation/answers"),
        api("/api/v1/applications"),
        api("/api/v1/agent/providers"),
        api("/api/v1/preparation/interviews"),
      ]);
      if (!isCurrent()) return;
      els.results.replaceChildren();
      els.resultCount.textContent = "Preparation workspace";
      els.pageStatus.textContent = "Drafts never send or submit themselves";

      const documentSection = element("section", "profile-card preparation-section");
      documentSection.appendChild(element("p", "eyebrow", "Documents"));
      documentSection.appendChild(element("h3", "", "Resume and cover-letter versions"));
      const generator = element("form", "preparation-create-form");
      const opportunity = document.createElement("select");
      opportunityOptions(opportunity, applications.items || []);
      opportunity.setAttribute("aria-label", "Application opportunity");
      const type = document.createElement("select");
      type.setAttribute("aria-label", "Document type");
      [["resume", "Resume variant"], ["cover_letter", "Cover letter"]].forEach(([value, label]) => {
        type.appendChild(optionElement(value, label));
      });
      const provider = document.createElement("select");
      provider.setAttribute("aria-label", "Draft generation method");
      provider.appendChild(optionElement("", "Grounded template (no AI)"));
      (providersPayload.items || []).filter((item) => item.configured).forEach((item) => {
        provider.appendChild(optionElement(item.id, `${item.display_name} · ${item.model}`));
      });
      const generate = element("button", "primary-button", "Generate grounded draft");
      generate.type = "submit";
      const generatorStatus = element("p", "form-status");
      generator.append(opportunity, type, provider, generate, generatorStatus);
      generator.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!opportunity.value) {
          generatorStatus.textContent = "Start or capture an application first.";
          return;
        }
        generate.disabled = true;
        try {
          await api("/api/v1/preparation/documents", {
            method: "POST",
            body: JSON.stringify({
              opportunity_id: opportunity.value,
              document_type: type.value,
              provider: provider.value || null,
            }),
          });
          await loadPreparation();
        } catch (error) {
          generatorStatus.textContent = error.message;
          generate.disabled = false;
        }
      });
      documentSection.appendChild(generator);
      const documentList = element("div", "preparation-list");
      state.pdfAvailable = Boolean(documents.pdf_available);
      documents.items.forEach((record) => documentList.appendChild(preparationDocumentCard(record)));
      if (!documents.items.length) documentList.appendChild(element("p", "empty-inline", "No generated documents yet."));
      documentSection.appendChild(documentList);

      const answerSection = element("section", "profile-card preparation-section");
      answerSection.appendChild(element("p", "eyebrow", "Reusable answers"));
      answerSection.appendChild(element("h3", "", "Answer library"));
      const answerForm = element("form", "answer-create-form");
      const question = document.createElement("input");
      question.placeholder = "Application question";
      question.required = true;
      const answerText = document.createElement("textarea");
      answerText.placeholder = "Your reviewed answer";
      answerText.required = true;
      const company = document.createElement("input");
      company.placeholder = "Company (optional)";
      const tags = document.createElement("input");
      tags.placeholder = "Tags, comma separated";
      const saveAnswer = element("button", "secondary-button", "Save answer");
      saveAnswer.type = "submit";
      answerForm.append(question, answerText, company, tags, saveAnswer);
      answerForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        saveAnswer.disabled = true;
        try {
          await api("/api/v1/preparation/answers", {
            method: "POST",
            body: JSON.stringify({ question: question.value, answer: answerText.value, company: company.value, tags: commaList(tags.value) }),
          });
          await loadPreparation();
        } catch (error) {
          showError(error.message);
          saveAnswer.disabled = false;
        }
      });
      answerSection.appendChild(answerForm);
      const answerList = element("div", "answer-list");
      answers.items.forEach((item) => {
        const card = element("article", "answer-item");
        card.appendChild(element("strong", "", item.question));
        card.appendChild(element("p", "", item.answer));
        card.appendChild(element("span", "", [item.company, ...(item.tags || [])].filter(Boolean).join(" · ")));
        const remove = element("button", "danger-button", "Delete");
        remove.type = "button";
        remove.addEventListener("click", async () => {
          if (!window.confirm("Delete this saved answer?")) return;
          await api(`/api/v1/preparation/answers/${encodeURIComponent(item.id)}`, { method: "DELETE" });
          await loadPreparation();
        });
        card.appendChild(remove);
        answerList.appendChild(card);
      });
      if (answers.items.length) {
        const deleteAll = element("button", "danger-button", "Delete all answers");
        deleteAll.type = "button";
        deleteAll.addEventListener("click", async () => {
          if (!window.confirm("Permanently delete every saved answer?")) return;
          await api("/api/v1/preparation/answers/all", { method: "DELETE" });
          await loadPreparation();
        });
        answerSection.appendChild(deleteAll);
      }
      answerSection.appendChild(answerList);

      const interviewSection = element("section", "profile-card preparation-section");
      interviewSection.appendChild(element("p", "eyebrow", "Practice"));
      interviewSection.appendChild(element("h3", "", "Mock interview"));
      interviewSection.appendChild(element("p", "profile-help", "Text works everywhere. Voice transcription and optional private audio recording ask for microphone permission and show a visible active state. You review the answer before it is scored."));
      const interviewForm = element("form", "preparation-create-form");
      const interviewOpportunity = document.createElement("select");
      opportunityOptions(interviewOpportunity, applications.items || []);
      interviewOpportunity.setAttribute("aria-label", "Interview opportunity");
      const start = element("button", "primary-button", "Start mock interview");
      start.type = "submit";
      interviewForm.append(interviewOpportunity, start);
      const interviewHost = element("div", "interview-host");
      interviewForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!interviewOpportunity.value) return;
        start.disabled = true;
        try {
          const interview = await api("/api/v1/preparation/interviews", {
            method: "POST",
            body: JSON.stringify({ opportunity_id: interviewOpportunity.value }),
          });
          renderInterview(interview, interviewHost);
        } catch (error) {
          showError(error.message);
        } finally {
          start.disabled = false;
        }
      });
      interviewSection.append(interviewForm);
      if (interviews.items.length) {
        const past = element("details", "interview-past");
        past.appendChild(element("summary", "", `Earlier practice (${interviews.items.length})`));
        const list = element("ul", "interview-past-list");
        interviews.items.forEach((entry) => {
          const row = element("li", "interview-past-row");
          const facts = [plural(entry.answers, "answer", "answers")];
          if (entry.recordings) facts.push(plural(entry.recordings, "recording", "recordings"));
          row.appendChild(element("span", "", `${entry.company}: ${entry.title} · ${formatDate(entry.created_at)} · ${facts.join(", ")}`));
          const open = element("button", "secondary-button", "Open");
          open.type = "button";
          open.setAttribute("aria-label", `Open the ${entry.company} practice from ${formatDate(entry.created_at)}`);
          open.addEventListener("click", async () => {
            open.disabled = true;
            try {
              renderInterview(await api(`/api/v1/preparation/interviews/${encodeURIComponent(entry.id)}`), interviewHost);
              interviewHost.querySelector("h3")?.setAttribute("tabindex", "-1");
              interviewHost.querySelector("h3")?.focus();
            } catch (error) {
              showError(error.message);
            } finally {
              open.disabled = false;
            }
          });
          row.appendChild(open);
          list.appendChild(row);
        });
        past.appendChild(list);
        interviewSection.appendChild(past);
      }
      interviewSection.appendChild(interviewHost);
      els.results.append(
        tagSection(documentSection, "documents", "Documents"),
        tagSection(answerSection, "answers", "Saved answers"),
        tagSection(interviewSection, "interview", "Interview practice"),
      );
      sectionSubnav();
      els.results.setAttribute("aria-busy", "false");
    });
  }

  // What this file does as it loads, run once by app.js in load order.
  function installPreparation() {
    registerViewHandlers("prepare", { load: loadPreparation });
  }

  Object.assign(App, {
    installPreparation,
  });
})();
