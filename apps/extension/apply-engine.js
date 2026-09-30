(() => {
  "use strict";
  // The shared field engine: everything content.js used to hold, minus the chrome.runtime
  // listener, so the extension and the Apply for me agent read a form with the same rules.
  // It never clicks, submits, or dispatches anything but input/change events on a field the
  // reviewed plan named. Every click the agent makes lives in Python.
  if (globalThis.OpportunityApplyEngine?.version) return;

  // Sensitive questions are never mapped, proposed from the library, or offered for saving.
  // The immigration, clearance, export-control, 18-or-older, criminal-history and non-compete
  // terms mirror the ones the agent's classifier (apply_policy.classify_sensitive) adds.
  // opportunity_app/extension_apply.py keeps a copy for the save guard; a test pins the two.
  // "opt in" and "opt out" are marketing wording, unless a country or year follows ("OPT in 2027").
  const SENSITIVE = /\b(gender|sex|sexual orientation|race|ethnic(?:ity)?|disab(?:ility|led)?|veteran|age|birth|sponsor(?:ship)?|authori[sz](?:ed|ation)|citizen(?:ship)?|salary|compensation|pronoun|marital|religio\w*|genetic|pregnan(?:cy|t)|eeo|transgender|immigration|petition|employment[- ]based|green card|permanent resident|visa[- ](?:sponsor\w*|status|support|type|holder|transfer)|(?:require|need|hold)\w*\s+(?:a\s+)?visa|work visa|student visa|f[- ]?1|j[- ]?1|h[- ]?1[- ]?b|tn|e[- ]?3|stem opt|opt(?!-(?:in|out)\b)(?! (?:in|out)\b(?! (?:the )?(?:us|u\.s\.|usa|united states|20\d\d)(?!\w)))|cpt|practical training|clearance|right to work|eligible to work|legally (?:eligible|authori[sz]ed)|18\+?(?: years)? (?:or older|of age)|over (?:the age of )?18|at least 18|age of 18|u\.? ?s\.? person|itar|export control|export administration regulations|felony|misdemeanor|arrest\w*|criminal|convict\w*|background check|non[- ]?compete)\b/i;
  const PROHIBITED = /\b(submit|next|continue|captcha|consent|send message|contact recruiter)\b/i;
  const NEVER_GENERIC_TYPES = new Set(["submit", "button", "image", "reset", "hidden", "password"]);
  const DOCUMENT_MEDIA = new Set([
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
  ]);
  const MAX_DOCUMENT_BYTES = 5 * 1024 * 1024;
  const TRAILING_REQUIRED = /(?:\s*(?:\*|\(required\)|\brequired\b))+\s*$/i;
  const ADAPTERS = globalThis.OpportunityApplyAdapters;
  const ENGINE = globalThis.OpportunityFieldEngine;
  if (!ADAPTERS || !ENGINE) throw new Error("Apply Mode adapter modules were not loaded");

  function escapeSelector(value) {
    if (globalThis.CSS?.escape) return globalThis.CSS.escape(value);
    return String(value).replace(/[^a-zA-Z0-9_-]/g, (character) => `\\${character}`);
  }

  function labelSources(control, extra = []) {
    const owner = control.ownerDocument || document;
    const explicit = control.id ? owner.querySelector(`label[for="${escapeSelector(control.id)}"]`) : null;
    const wrapping = typeof control.closest === "function" ? control.closest("label") : null;
    return [explicit?.textContent, wrapping?.textContent, control.getAttribute?.("aria-label"),
      control.getAttribute?.("data-automation-id"), control.name, control.id, control.placeholder, ...extra]
      .filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
  }

  function labelFor(control) {
    return labelSources(control).slice(0, 500);
  }

  function collapse(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  // A label's own words, without the text of the controls inside it (a select's options
  // would otherwise read as part of the question).
  function ownLabelText(label, control) {
    if (typeof label.cloneNode === "function" && typeof label.querySelectorAll === "function") {
      const copy = label.cloneNode(true);
      for (const inner of copy.querySelectorAll("input, select, textarea, option")) inner.remove?.();
      return collapse(copy.textContent);
    }
    const own = String(control.textContent || "");
    return collapse(own ? String(label.textContent || "").split(own).join(" ") : label.textContent);
  }

  // Every word the form shows for a control, uncut, for the sensitive and prohibited screens:
  // the label is cut at 500 characters and never reads aria-labelledby, but matching and
  // Save can use both.
  function screenText(control, question) {
    const owner = control.ownerDocument || document;
    return labelSources(control, [textOfIds(owner, control.getAttribute?.("aria-labelledby")), question]);
  }

  function textOfIds(owner, ids) {
    const list = collapse(ids).split(" ").filter(Boolean);
    if (!list.length || typeof owner.getElementById !== "function") return "";
    return collapse(list.map((id) => owner.getElementById(id)?.textContent || "").join(" "));
  }

  // The question a radio or checkbox option belongs to: the enclosing fieldset's legend, else
  // a radiogroup or group's aria-labelledby or aria-label. The option's own label ("Yes",
  // "I agree", "Woman") is an answer, not the question, so it is never used here.
  function groupQuestion(control) {
    if (typeof control.closest !== "function") return "";
    const owner = control.ownerDocument || document;
    let node = control.closest('fieldset, [role="radiogroup"], [role="group"]');
    for (let depth = 0; node && depth < 3; depth += 1) {
      const legend = typeof node.querySelector === "function" ? node.querySelector("legend") : null;
      const text = collapse(legend?.textContent)
        || textOfIds(owner, node.getAttribute?.("aria-labelledby"))
        || collapse(node.getAttribute?.("aria-label"));
      if (text) return text;
      node = typeof node.parentElement?.closest === "function"
        ? node.parentElement.closest('fieldset, [role="radiogroup"], [role="group"]') : null;
    }
    return "";
  }

  // The words the form shows for a control, before the required marker is stripped:
  // <label for>, else the wrapping label, else aria-labelledby, else aria-label.
  // Never name, id or placeholder: those change with every posting. A radio or checkbox
  // reports its group's question instead, or nothing when the group has none.
  function rawQuestion(control) {
    const type = String(control.type || "").toLowerCase();
    if (type === "radio" || type === "checkbox") return groupQuestion(control);
    const owner = control.ownerDocument || document;
    const explicit = control.id ? owner.querySelector?.(`label[for="${escapeSelector(control.id)}"]`) : null;
    const wrapping = typeof control.closest === "function" ? control.closest("label") : null;
    const candidates = [
      () => (explicit ? ownLabelText(explicit, control) : ""),
      () => (wrapping ? ownLabelText(wrapping, control) : ""),
      () => textOfIds(owner, control.getAttribute?.("aria-labelledby")),
      () => collapse(control.getAttribute?.("aria-label")),
    ];
    for (const candidate of candidates) {
      const text = candidate();
      if (text) return text;
    }
    return "";
  }

  function questionText(control) {
    return collapse(rawQuestion(control).replace(TRAILING_REQUIRED, ""));
  }

  function atsType() {
    return ADAPTERS.detect(globalThis.location?.hostname).id;
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
    // An <input role=combobox> is a react-select: typing free text into it does nothing useful.
    if (control.getAttribute?.("role") === "combobox") return "custom_select";
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

  // The key a saved answer is matched on. Python's question_key must give the same result
  // (tests/fixtures/apply/question_keys.json is run by both suites).
  function questionKey(text) {
    return normalizedQuestion(text);
  }

  // Text that takes its meaning from the question above it, not from the company: an opener
  // ("If yes, please explain", "Other"), anything under three words, or a follow-up wording
  // ("Please provide more details"). Such a key is never matched on the clean question at any
  // company, so it falls back to the per-posting label tier. The rules err toward the label
  // tier: a question wrongly sent there only loses an exact match, while a real follow-up left
  // on the clean question would carry parent A's answer into parent B's field.
  const CONTEXT_OPENER = /^(?:if yes|if so|if no|if other|please specify|please explain|please describe|other|explain)\b/;
  const CONTEXT_IF = /^if\b/;
  const CONTEXT_IF_ANY = /\bif (?:yes|so|no|not|other|applicable|any)\b/;
  const CONTEXT_PLEASE = /\bplease (?:explain|specify|describe|elaborate)\b/;
  // provide/give/share/include/add/list, then up to three filler words (an article, "more", "a
  // brief", "the"), then the thing asked for.
  const CONTEXT_DETAILS = /\b(?:provide|give|share|include|add|list)\s+(?:[a-z']+\s+){0,3}?(?:details?|information|info|context|explanations?)\b/;
  const CONTEXT_PRONOUN = /\b(?:list|name|give|provide|share|describe|explain|specify|identify) (?:them|it|those|these|each)\b/;
  const CONTEXT_VERB = /\b(?:explain|explanation|specify|elaborate|clarify|expand)\b/;
  const CONTEXT_DESCRIBE = /\bdescribe\b/;
  const CONTEXT_PHRASE = /\btell us (?:more|why)\b|\bwhy or why not\b|\bif applicable\b|\byour (?:answer|response)s? (?:above|to the previous)\b|\bprevious question\b|\bthe above\b/;
  const CONTEXT_WH = /^(?:which|what|when|where|who|whom|whose|how|why)\b/;

  // The key with a leading enumeration or bullet ("b.", "1a)", "(ii)", "-") and quote marks taken
  // off. The key is already lower-case with punctuation turned into spaces.
  function withoutEnumeration(key) {
    let text = key.replace(/(^|\s)'+/g, "$1").replace(/'+(?=\s|$)/g, "").trim();
    for (let pass = 0; pass < 2; pass += 1) {
      const next = text.replace(/^(?:[a-z]|[ivx]{1,4}|\d{1,2}[a-z]?|[a-z]\d{1,2})\s+(?=\S)/, "");
      if (next === text) break;
      text = next;
    }
    return text;
  }

  // Questions whose truth depends on the employer. A saved answer to one never carries to
  // another company, even when the row is tagged reusable.
  const CONTEXT_WORDING = /previously (?:worked|been employed|applied)|worked (?:here|for us|for this company|at)|applied (?:here|before|previously)|referr|who referred|know (?:anyone|someone)|how did you hear|where did you (?:hear|find)|current(?:ly)? (?:an )?employee/;

  function needsLabelKey(key) {
    const text = withoutEnumeration(key);
    const words = text.split(" ").filter(Boolean).length;
    return key.split(" ").filter(Boolean).length < 3 || words < 3
      || [key, text].some((item) => CONTEXT_OPENER.test(item) || CONTEXT_PLEASE.test(item))
      || CONTEXT_IF.test(text) || CONTEXT_IF_ANY.test(text) || CONTEXT_DETAILS.test(text) || CONTEXT_PRONOUN.test(text)
      || CONTEXT_PHRASE.test(text) || (words < 8 && CONTEXT_VERB.test(text)) || (words < 6 && CONTEXT_DESCRIBE.test(text))
      || (words < 6 && CONTEXT_WH.test(text));
  }

  function contextDependent(key) {
    return needsLabelKey(key) || CONTEXT_WORDING.test(key);
  }

  // Whether a field's saved answer is matched and saved on its label, not its question. A radio
  // or checkbox option shares its group's question with every sibling, and an opener shares its
  // words with every other opener, so both key on the per-posting label (the `answer_key` a
  // scanned field reports), as they did before the clean question existed. `question` stays the
  // group text for anything that joins on it.
  function labelKeyed(type, question, repeated) {
    const key = questionKey(question);
    return !key || type === "radio" || type === "checkbox" || needsLabelKey(key) || Boolean(repeated?.has(key));
  }

  // The form a control belongs to, else its document: the scope in which a repeated question is
  // counted.
  function formScope(control) {
    let form = null;
    try { form = typeof control.closest === "function" ? control.closest("form") : null; } catch (_) { form = null; }
    return form || control.ownerDocument || document;
  }

  // The clean question keys that more than one field on the same form carries. Two fields with the
  // same words can sit under different parents, so neither may use the clean-question tier: each
  // falls back to its own per-question label (name and id).
  function repeatedQuestionKeys(controls) {
    const counts = new Map();
    for (const control of controls) {
      const type = controlType(control);
      if (type === "radio" || type === "checkbox") continue;
      const key = questionKey(questionText(control));
      if (!key) continue;
      const scope = formScope(control);
      if (!counts.has(scope)) counts.set(scope, new Map());
      counts.get(scope).set(key, (counts.get(scope).get(key) || 0) + 1);
    }
    const repeated = new Map();
    for (const [scope, keys] of counts) {
      repeated.set(scope, new Set([...keys].filter(([, count]) => count > 1).map(([key]) => key)));
    }
    return repeated;
  }

  // A saved answer is exact (0.9, pre-tickable) only for a row saved at this company, or tagged
  // reusable when neither the field's question nor the row's key is context-dependent. It holds
  // for every tier: the clean question, the whole label, and rows saved before the clean
  // question existed. An answer saved at another employer is never assumed true here, and a row
  // with no company is exact only when it is reusable.
  function mayUseAtCompany(entry, keys, company) {
    if (company && normalizedQuestion(entry.company || "") === company) return true;
    const reusable = (entry.tags || []).some((tag) => String(tag).toLowerCase() === "reusable");
    return reusable && !keys.some((key) => key && contextDependent(key));
  }

  // A saved question is compared with the clean question first, then with the whole label,
  // so answers saved before the side panel kept the clean question still match. `fieldQuestion`
  // is the field's own question, which a radio, checkbox or opener does not pass as `question`
  // but which still decides whether a reusable row may travel.
  function matchAnswer(question, label, answers, company, fieldQuestion) {
    const cleanKey = questionKey(question);
    const normalizedLabel = normalizedQuestion(label);
    // No words at all (an option with no label source): nothing can be an exact match.
    if (!cleanKey && !normalizedLabel) return null;
    const companyKey = normalizedQuestion(company || "");
    const ownKey = questionKey(fieldQuestion === undefined ? question : fieldQuestion);
    const usable = (entry) => mayUseAtCompany(entry, [normalizedQuestion(entry.question), ownKey], companyKey);
    const cleanMatches = cleanKey && !needsLabelKey(cleanKey) ? (answers || []).filter((entry) => normalizedQuestion(entry.question) === cleanKey) : [];
    const labelMatches = normalizedLabel ? (answers || []).filter((entry) => normalizedQuestion(entry.question) === normalizedLabel) : [];
    const exact = cleanMatches.find(usable) || labelMatches.find(usable);
    if (exact) return { entry: exact, confidence: 0.9, exact: true, sameCompany: Boolean(companyKey) && normalizedQuestion(exact.company || "") === companyKey };
    const elsewhere = cleanMatches[0] || labelMatches[0];
    if (elsewhere) return { entry: elsewhere, confidence: 0.7, exact: false, otherCompany: true };
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

  function visibleCount(container) {
    try {
      return [...container.querySelectorAll("input, textarea, select")].filter(isVisible).length;
    } catch (_) {
      return 2;
    }
  }

  // The widest ancestor (up to four levels) that holds this control and no other visible one:
  // where react-select keeps its hidden required mirror and an upload group its required span.
  function fieldContainer(control) {
    let container = null;
    let node = control.parentElement;
    for (let depth = 0; node && depth < 4; depth += 1, node = node.parentElement) {
      if (typeof node.querySelectorAll !== "function" || visibleCount(node) > 1) break;
      container = node;
    }
    return container;
  }

  function requiredMarkers(control, rawText) {
    const markers = [];
    const group = typeof control.closest === "function" ? control.closest('[role="group"]') : null;
    if (control.required) markers.push("attr");
    if (control.getAttribute?.("aria-required") === "true" || group?.getAttribute?.("aria-required") === "true") markers.push("aria");
    if (String(rawText).includes("*")) markers.push("asterisk");
    const container = fieldContainer(control);
    if (container && typeof container.querySelector === "function") {
      if (container.querySelector('input[required][aria-hidden="true"]')) markers.push("hidden_required_sibling");
      if (container.querySelector("span.required")) markers.push("span_required");
    }
    return markers;
  }

  function widgetKind(control, type) {
    if (type === "file") return "file_group";
    if (control.getAttribute?.("role") === "combobox") {
      return control.id === "candidate-location" ? "location" : "react_select";
    }
    return "native";
  }

  // Computed style and box size, the same test as the contact-form extractor. Reported, never
  // used to filter. null means the DOM here cannot say (a stub, or a detached node).
  function visibleCss(control) {
    const view = control.ownerDocument?.defaultView || globalThis;
    if (typeof view.getComputedStyle !== "function" || typeof control.getBoundingClientRect !== "function") return null;
    try {
      const style = view.getComputedStyle(control);
      const box = control.getBoundingClientRect();
      if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
      if (box.width < 2 || box.height < 2 || box.right < 0 || box.bottom < 0 || box.left > 10000) return false;
      if (typeof control.closest === "function" && control.closest("[aria-hidden='true']")) return false;
      return !(control.tabIndex === -1 && box.width < 5);
    } catch (_) {
      return null;
    }
  }

  function scan(profile, answers, options) {
    profile = profile || {};
    const tag = options?.tag === true;
    const controls = visibleControls();
    const repeated = repeatedQuestionKeys(controls);
    const fields = controls.map((control) => {
      const label = labelFor(control);
      const type = controlType(control);
      const rawText = rawQuestion(control);
      const question = questionText(control);
      const labelKey = labelKeyed(type, question, repeated.get(formScope(control)));
      const screened = screenText(control, question);
      const requiresReview = SENSITIVE.test(screened);
      const prohibited = PROHIBITED.test(screened);
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
        const match = matchAnswer(labelKey ? "" : question, label, answers, options?.company, question);
        if (match) {
          value = String(match.entry.answer || "");
          provenance = `answer_library:${match.entry.id}`;
          confidence = match.confidence;
          reason = match.exact
            ? (match.sameCompany ? "Same question saved for this company; verify before filling"
              : "Same question, saved as reusable; verify before filling")
            : match.otherCompany ? "Saved for another company; direct review required"
              : "Similar saved question; direct review required";
        }
      }
      if (value === null || typeof value === "object") value = "";
      const key = fingerprint(control, label);
      if (tag && typeof control.setAttribute === "function") control.setAttribute("data-opportunity-field", key);
      const markers = requiredMarkers(control, rawText);
      return { key, label: label || `Unlabelled ${type} field`, type,
        provenance, confidence, requires_review: requiresReview, prohibited,
        unsupported: type === "custom_select",
        required: Boolean(control.required || control.getAttribute?.("aria-required") === "true"),
        reason, proposed_value: String(value),
        question, answer_key: labelKey ? label : question, required_markers: markers, required_any: markers.length > 0,
        widget: widgetKind(control, type), visible_css: visibleCss(control),
        name: String(control.name || ""), id: String(control.id || "") };
    });
    return { ats_type: atsType(), page_url: globalThis.location?.href || "", fields };
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
      if (field.prohibited || PROHIBITED.test(`${field.label} ${field.question || ""}`) || NEVER_GENERIC_TYPES.has(String(field.type).toLowerCase())) {
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

  function attachDocumentFromBytes(message) {
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

  globalThis.OpportunityApplyEngine = Object.freeze({
    version: "1",
    scan,
    fill,
    attachDocumentFromBytes,
    questionText,
    questionKey,
  });
})();
