(() => {
  "use strict";
  if (globalThis.__opportunityApplyModeLoaded) return;
  globalThis.__opportunityApplyModeLoaded = true;

  const SENSITIVE = /\b(gender|sex|sexual orientation|race|ethnic(?:ity)?|disab(?:ility|led)?|veteran|age|birth|sponsor(?:ship)?|authori[sz](?:ed|ation)|citizen(?:ship)?|salary|compensation|pronoun|marital|religion|genetic|pregnan(?:cy|t)|eeo)\b/i;
  const PROHIBITED = /\b(submit|next|continue|captcha|consent|send message|contact recruiter)\b/i;
  const NEVER_GENERIC_TYPES = new Set(["submit", "button", "image", "reset", "hidden", "password"]);
  const DOCUMENT_MEDIA = new Set([
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
  ]);
  const MAX_DOCUMENT_BYTES = 5 * 1024 * 1024;
  const ADAPTERS = globalThis.OpportunityApplyAdapters;
  const ENGINE = globalThis.OpportunityFieldEngine;
  if (!ADAPTERS || !ENGINE) throw new Error("Apply Mode adapter modules were not loaded");

  function escapeSelector(value) {
    if (globalThis.CSS?.escape) return globalThis.CSS.escape(value);
    return String(value).replace(/[^a-zA-Z0-9_-]/g, (character) => `\\${character}`);
  }

  function labelFor(control) {
    const owner = control.ownerDocument || document;
    const explicit = control.id ? owner.querySelector(`label[for="${escapeSelector(control.id)}"]`) : null;
    const wrapping = typeof control.closest === "function" ? control.closest("label") : null;
    return [explicit?.textContent, wrapping?.textContent, control.getAttribute?.("aria-label"),
      control.getAttribute?.("data-automation-id"), control.name, control.id, control.placeholder]
      .filter(Boolean).join(" ").replace(/\s+/g, " ").trim().slice(0, 500);
  }

  function atsType() {
    return ADAPTERS.detect(location.hostname).id;
  }

  function nested(value, path) {
    return path.split(".").reduce((current, key) => current && current[key], value);
  }

  function lookup(profile, path) {
    const direct = profile[path];
    if (path === "name.full") return profile.name || direct || "";
    if (path === "name.first") return direct || String(profile.name || "").trim().split(/\s+/)[0] || "";
    if (path === "name.last") return direct || String(profile.name || "").trim().split(/\s+/).slice(1).join(" ");
    return direct ?? nested(profile, path) ?? "";
  }

  function controlType(control) {
    const tag = String(control.tagName || "").toLowerCase();
    const type = String(control.type || "").toLowerCase();
    if (control.getAttribute?.("role") === "combobox" && tag !== "input") return "custom_select";
    if (["file", "radio", "checkbox"].includes(type)) return type;
    if (tag === "select") return "select";
    if (tag === "textarea") return "textarea";
    return type || tag || "text";
  }

  function isVisible(control) {
    if (control.disabled || control.hidden || control.getAttribute?.("aria-hidden") === "true") return false;
    return !NEVER_GENERIC_TYPES.has(String(control.type || "").toLowerCase());
  }

  function sameOriginDocuments(root = document, depth = 0) {
    const roots = [root];
    if (depth >= 3) return roots;
    for (const frame of root.querySelectorAll?.("iframe") || []) {
      try {
        if (frame.contentDocument) roots.push(...sameOriginDocuments(frame.contentDocument, depth + 1));
      } catch (_) { /* cross-origin frames are intentionally manual */ }
    }
    return roots;
  }

  function visibleControls() {
    return sameOriginDocuments().flatMap((root) => [...root.querySelectorAll("input, textarea, select, [role=combobox]")]).filter(isVisible);
  }

  function optionSignature(control) {
    try {
      return [...(control.options || [])].map((option) => `${option.value}:${option.textContent}`).join("|");
    } catch (_) {
      return "";
    }
  }

  function fingerprint(control, label) {
    return `field-${ENGINE.hash([atsType(), label.toLowerCase(), controlType(control),
      String(control.name || "").toLowerCase(), String(control.id || "").toLowerCase(),
      optionSignature(control).toLowerCase()].join("|"))}`;
  }

  function normalizedQuestion(value) {
    return String(value).toLowerCase().replace(/[^a-z0-9']+/g, " ").trim();
  }

  function matchAnswer(label, answers) {
    const normalizedLabel = normalizedQuestion(label);
    const exact = (answers || []).find((entry) => normalizedQuestion(entry.question) === normalizedLabel);
    if (exact) return { entry: exact, confidence: 0.9, exact: true };
    const labelWords = new Set(normalizedLabel.split(" ").filter((word) => word.length >= 4));
    let best = null;
    let bestScore = 0;
    for (const entry of answers || []) {
      const words = normalizedQuestion(entry.question).split(" ").filter((word) => word.length >= 4);
      if (!words.length) continue;
      const score = words.filter((word) => labelWords.has(word)).length / Math.min(words.length, 3);
      if (score > bestScore) { best = entry; bestScore = score; }
    }
    return bestScore >= 0.5 ? { entry: best, confidence: 0.7, exact: false } : null;
  }

  function scan(profile, answers) {
    const fields = visibleControls().map((control) => {
      const label = labelFor(control);
      const type = controlType(control);
      const requiresReview = SENSITIVE.test(label);
      const prohibited = PROHIBITED.test(label);
      const mapping = ADAPTERS.mappings.find((candidate) => candidate.pattern.test(label));
      let value = "";
      let provenance = "unmapped";
      let confidence = 0;
      let reason = "No safe mapping found";
      if (type === "file") {
        reason = "Choose an approved local document before attaching";
      } else if (type === "custom_select") {
        reason = "Unsupported custom widget requires manual completion";
      } else if (prohibited) {
        reason = "Navigation, CAPTCHA, consent, messaging, and submit controls are prohibited";
      } else if (requiresReview) {
        reason = "Sensitive or consequential field requires direct review";
      } else if (mapping) {
        value = lookup(profile, mapping.field);
        provenance = `confirmed_profile.${mapping.field}`;
        confidence = value !== "" ? 0.95 : 0;
        reason = value !== "" ? "Mapped from an explicit label" : "Confirmed profile value is unavailable";
      } else {
        const match = matchAnswer(label, answers);
        if (match) {
          value = String(match.entry.answer || "");
          provenance = `answer_library:${match.entry.id}`;
          confidence = match.confidence;
          reason = match.exact ? "Exact saved-question match; verify before filling" : "Similar saved question; direct review required";
        }
      }
      if (value === null || typeof value === "object") value = "";
      return { key: fingerprint(control, label), label: label || `Unlabelled ${type} field`, type,
        provenance, confidence, requires_review: requiresReview, prohibited,
        unsupported: type === "custom_select",
        required: Boolean(control.required || control.getAttribute?.("aria-required") === "true"),
        reason, proposed_value: String(value) };
    });
    return { ats_type: atsType(), page_url: location.href, fields };
  }

  function resolveControl(field, controls = visibleControls()) {
    const matches = controls.filter((control) => {
      const label = labelFor(control);
      return controlType(control) === field.type && fingerprint(control, label) === field.key && label === field.label;
    });
    return matches.length === 1 ? matches[0] : null;
  }

  function fillOne(control, field) {
    if (field.type === "file") return { filled: false, reason: "File fields require explicit document attachment" };
    if (field.type === "custom_select") return { filled: false, reason: "Unsupported custom widget requires manual completion" };
    if (field.type === "checkbox") {
      const desired = /^(true|yes|1|on)$/i.test(String(field.proposed_value));
      ENGINE.setNative(control, "checked", desired); ENGINE.dispatchValueEvents(control);
      return control.checked === desired ? { filled: true } : { filled: false, reason: "Checkbox state did not persist" };
    }
    if (field.type === "radio") {
      const desired = ENGINE.normalized(field.proposed_value);
      if (![ENGINE.normalized(control.value), ENGINE.normalized(field.label)].includes(desired)) return { filled: false, reason: "No unambiguous radio option matched the reviewed value" };
      ENGINE.setNative(control, "checked", true); ENGINE.dispatchValueEvents(control);
      return control.checked ? { filled: true } : { filled: false, reason: "Radio selection did not persist" };
    }
    if (field.type === "select") {
      const desired = ENGINE.normalized(field.proposed_value);
      const matches = [...(control.options || [])].filter((option) => [ENGINE.normalized(option.value), ENGINE.normalized(option.textContent)].includes(desired));
      if (matches.length !== 1) return { filled: false, reason: "No unambiguous select option matched the reviewed value" };
      ENGINE.setNative(control, "value", matches[0].value); ENGINE.dispatchValueEvents(control);
      return ENGINE.normalized(control.value) === ENGINE.normalized(matches[0].value) ? { filled: true } : { filled: false, reason: "Select value did not persist" };
    }
    control.focus(); ENGINE.setNative(control, "value", String(field.proposed_value)); ENGINE.dispatchValueEvents(control);
    return String(control.value) === String(field.proposed_value)
      ? { filled: true } : { filled: false, reason: "Value did not persist after fill", observed_value: String(control.value || "") };
  }

  function fill(reviewed) {
    const results = [];
    for (const field of reviewed || []) {
      if (field.approved !== true || field.proposed_value === "") continue;
      if (field.prohibited || PROHIBITED.test(field.label) || NEVER_GENERIC_TYPES.has(String(field.type).toLowerCase())) {
        results.push({ key: field.key, filled: false, reason: "Prohibited controls cannot be filled" }); continue;
      }
      const control = resolveControl(field);
      if (!control) { results.push({ key: field.key, filled: false, reason: "Field is no longer present or is ambiguous after the page changed" }); continue; }
      results.push({ key: field.key, ...fillOne(control, field) });
    }
    return results;
  }

  function decodeBase64(value) {
    const binary = atob(value);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
    return bytes;
  }

  function attachDocument(message) {
    const field = message.field || {};
    const artifact = message.artifact || {};
    if (message.approved !== true || field.type !== "file") return { filled: false, reason: "Document attachment requires explicit approval" };
    const mediaType = String(artifact.media_type || "");
    if (!DOCUMENT_MEDIA.has(mediaType)) return { filled: false, reason: "Only approved PDF or DOCX artifacts can be attached" };
    const filename = String(artifact.filename || "");
    if (!filename || filename.length > 180 || /[\\/\u0000-\u001f]/.test(filename)) return { filled: false, reason: "Document filename is unsafe" };
    if (!Number.isSafeInteger(Number(artifact.byte_size)) || Number(artifact.byte_size) <= 0 || Number(artifact.byte_size) > MAX_DOCUMENT_BYTES) return { filled: false, reason: "Document size is outside the allowed range" };
    const control = resolveControl(field);
    if (!control) return { filled: false, reason: "File field is missing or ambiguous" };
    const accept = String(control.accept || "").toLowerCase();
    const extension = filename.toLowerCase().endsWith(".pdf") ? ".pdf" : filename.toLowerCase().endsWith(".docx") ? ".docx" : "";
    if (accept && !accept.split(",").map((item) => item.trim()).some((item) => item === mediaType.toLowerCase() || item === extension || item === "*/*")) {
      return { filled: false, reason: "ATS file field does not accept this document type" };
    }
    try {
      const bytes = decodeBase64(String(message.data_base64 || ""));
      if (bytes.byteLength !== Number(artifact.byte_size)) return { filled: false, reason: "Artifact size changed before attachment" };
      const file = new File([bytes], filename, { type: mediaType, lastModified: Date.now() });
      const transfer = new DataTransfer(); transfer.items.add(file); control.files = transfer.files; ENGINE.dispatchValueEvents(control);
      const selected = control.files?.[0];
      if (!selected || selected.name !== file.name || selected.size !== file.size || selected.type !== file.type) return { filled: false, reason: "ATS rejected or rewrote the selected document" };
      return { filled: true, filename: selected.name, byte_size: selected.size };
    } catch (_) {
      return { filled: false, reason: "Browser or ATS rejected programmatic attachment" };
    }
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.type === "SCAN_FIELDS") sendResponse(scan(message.profile || {}, message.answers || []));
    if (message.type === "FILL_REVIEWED_FIELDS") sendResponse({ results: fill(message.fields || []), final_submit_available: false });
    if (message.type === "ATTACH_REVIEWED_FILE") sendResponse({ result: attachDocument(message), final_submit_available: false });
  });
})();
