(() => {
  "use strict";

  const DEFAULT_ORIGIN = "http://127.0.0.1:8765";
  const $ = (id) => document.getElementById(id);
  const pairing = $("pairing");
  const workspace = $("workspace");
  const status = $("status");
  const candidatesHost = $("candidates");
  const contextHost = $("context");
  const fieldsHost = $("fields");
  const reviewForm = $("review-form");
  const documentsHost = $("documents");
  const documentSelect = $("document-select");
  const fileFieldSelect = $("file-field-select");
  let activeTabId = null;
  let activePageUrl = "";
  let selectedApplicationId = "";
  let applyContext = null;
  let scanResult = null;
  let sessionId = "";
  let attachmentUrl = "";

  function node(tag, value, className) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (value !== undefined) item.textContent = String(value);
    return item;
  }

  function normalizeOrigin(value) {
    try {
      const parsed = new URL(String(value || DEFAULT_ORIGIN).trim());
      if (parsed.protocol !== "http:" || parsed.hostname !== "127.0.0.1" || parsed.username || parsed.password) return null;
      return parsed.origin;
    } catch (_) {
      return null;
    }
  }

  async function storedAuth() {
    return chrome.storage.local.get({ serverOrigin: DEFAULT_ORIGIN, deviceToken: "", deviceId: "", pendingMetadata: [], unsupportedCounts: {} });
  }

  async function api(path, options = {}, allowUnauthenticated = false) {
    const auth = await storedAuth();
    const origin = normalizeOrigin(options.origin || auth.serverOrigin);
    if (!origin) throw new Error("The tracker must use http://127.0.0.1 on a local port.");
    const granted = await chrome.permissions.request({ origins: [`${origin}/*`] });
    if (!granted) throw new Error("Local tracker access was not granted.");
    const headers = { ...(options.headers || {}) };
    if (!allowUnauthenticated) {
      if (!auth.deviceToken) throw new Error("Pair this extension first.");
      headers.Authorization = `Bearer ${auth.deviceToken}`;
    }
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(`${origin}${path}`, { ...options, origin: undefined, headers });
    if (response.status === 401 || response.status === 403) {
      throw new Error("This paired device is no longer authorized. Pair it again from the tracker.");
    }
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const error = new Error(payload.detail || `Local tracker request failed (${response.status}).`);
      error.retryable = response.status >= 500;
      throw error;
    }
    return response;
  }

  async function activeTab() {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id || !/^https?:/.test(tab.url || "")) throw new Error("Open an external application page first.");
    activeTabId = tab.id;
    activePageUrl = tab.url;
    return tab;
  }

  async function send(message) {
    try {
      return await chrome.tabs.sendMessage(activeTabId, message);
    } catch (_) {
      await chrome.scripting.executeScript({ target: { tabId: activeTabId }, files: ["adapters.js", "field-engine.js", "content.js"] });
      return chrome.tabs.sendMessage(activeTabId, message);
    }
  }

  async function digestHex(bytes) {
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(digest)].map((part) => part.toString(16).padStart(2, "0")).join("");
  }

  function bytesToBase64(bytes) {
    let binary = "";
    const chunk = 0x8000;
    for (let index = 0; index < bytes.length; index += chunk) {
      binary += String.fromCharCode(...bytes.subarray(index, index + chunk));
    }
    return btoa(binary);
  }

  async function stableId(prefix, value) {
    const digest = await digestHex(new TextEncoder().encode(value));
    return `${prefix}-${digest.slice(0, 32)}`;
  }

  function renderAuth(auth) {
    const paired = Boolean(auth.deviceToken);
    pairing.hidden = paired;
    workspace.hidden = !paired;
    $("server-origin").value = auth.serverOrigin || DEFAULT_ORIGIN;
  }

  async function pairDevice() {
    const origin = normalizeOrigin($("server-origin").value);
    const code = $("pairing-code").value.trim();
    if (!origin || !code) throw new Error("Enter the loopback tracker origin and one-time pairing code.");
    const response = await api("/api/v1/extension/pairings/redeem", {
      method: "POST", origin, body: JSON.stringify({ code, device_name: "Chrome Apply Mode" })
    }, true);
    const payload = await response.json();
    await chrome.storage.local.set({ serverOrigin: origin, deviceToken: payload.device_token, deviceId: payload.device_id });
    $("pairing-code").value = "";
    renderAuth(await storedAuth());
    status.textContent = "Paired. Open an application page to choose its tracker context.";
  }

  function matchLabel(kind) {
    return ({ exact_url: "Exact URL", canonical_url: "Canonical URL", same_host: "Same ATS host", recent_apply: "Recently opened" })[kind] || kind;
  }

  async function findContext() {
    const tab = await activeTab();
    status.textContent = "Matching this page to your pipeline…";
    const response = await api(`/api/v1/extension/application-candidates?page_url=${encodeURIComponent(tab.url)}`);
    const payload = await response.json();
    candidatesHost.replaceChildren();
    if (!payload.items.length) {
      candidatesHost.append(node("p", "No matching applications. Add or open this opportunity in the tracker first."));
      contextHost.hidden = true;
      return;
    }
    for (const candidate of payload.items) {
      const button = node("button", undefined, "candidate");
      button.type = "button";
      button.dataset.applicationId = candidate.application_id;
      button.append(node("strong", `${candidate.company} — ${candidate.title}`));
      button.append(node("small", `${matchLabel(candidate.match_kind)} · ${candidate.stage}`));
      button.addEventListener("click", () => selectContext(candidate.application_id));
      candidatesHost.append(button);
    }
    status.textContent = payload.preselected_application_id
      ? "One exact match found. Confirm it before scanning."
      : "Choose the application that belongs to this page.";
  }

  async function selectContext(applicationId) {
    selectedApplicationId = applicationId;
    const response = await api(`/api/v1/extension/apply-context?application_id=${encodeURIComponent(applicationId)}`);
    applyContext = await response.json();
    const app = applyContext.application;
    const match = applyContext.match;
    $("match-card").replaceChildren(
      node("strong", `${app.company} — ${app.title}`),
      node("p", `Match score ${match.score}. ${app.opportunity_status}; evidence refreshed ${app.opportunity_updated_at}.`),
      node("small", (match.explanation || []).map((part) => typeof part === "string" ? part : JSON.stringify(part)).join(" · ") || "No score explanation available.")
    );
    contextHost.hidden = false;
    reviewForm.hidden = true;
    documentsHost.hidden = true;
    status.textContent = "Context loaded from confirmed local facts. Review it, then scan this step.";
  }

  function fieldValue(field) {
    return field.proposed_value === undefined || field.proposed_value === null ? "" : String(field.proposed_value);
  }

  function renderFields() {
    fieldsHost.replaceChildren();
    for (const field of scanResult.fields) {
      const row = node("label", undefined, `field${field.requires_review ? " sensitive" : ""}`);
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.dataset.key = field.key;
      checkbox.checked = !field.prohibited && !field.requires_review && field.confidence >= .9 && fieldValue(field) !== "";
      checkbox.disabled = field.prohibited || fieldValue(field) === "";
      const copy = node("span");
      copy.append(node("strong", field.label));
      copy.append(node("small", `${field.provenance} · ${Math.round(field.confidence * 100)}% confidence${field.required ? " · required" : ""}`));
      if (field.type === "file" || field.prohibited) {
        copy.append(node("output", field.reason || "Manual action required"));
      } else {
        const editor = document.createElement(field.type === "textarea" ? "textarea" : "input");
        if (editor.tagName === "INPUT") editor.type = "text";
        editor.value = fieldValue(field);
        editor.setAttribute("aria-label", `Reviewed value for ${field.label}`);
        editor.addEventListener("input", () => {
          field.proposed_value = editor.value;
          field.provenance = "reviewed_inline";
          field.confidence = 1;
          checkbox.disabled = editor.value === "";
        });
        copy.append(editor);
        if (!field.requires_review) {
          const saveAnswer = node("button", "Save this answer", "quiet");
          saveAnswer.type = "button";
          saveAnswer.disabled = editor.value === "";
          editor.addEventListener("input", () => { saveAnswer.disabled = editor.value === ""; });
          saveAnswer.addEventListener("click", async () => {
            try {
              await api("/api/v1/extension/answers", {
                method: "POST",
                body: JSON.stringify({ question: field.label, answer: editor.value, company: applyContext.application.company, tags: [scanResult.ats_type] })
              });
              status.textContent = "Reviewed answer saved for future exact-question matches.";
            } catch (error) { status.textContent = error.message; }
          });
          copy.append(saveAnswer);
        }
      }
      if (field.requires_review) copy.append(node("em", "Direct review required; this value can never be saved for reuse."));
      row.append(checkbox, copy);
      fieldsHost.append(row);
    }
    reviewForm.hidden = false;
  }

  function renderDocuments() {
    documentSelect.replaceChildren();
    fileFieldSelect.replaceChildren();
    const allowedDocs = (applyContext.documents || []).filter((item) => !item.opportunity_id || item.opportunity_id === applyContext.application.opportunity_id);
    const fileFields = scanResult.fields.filter((item) => item.type === "file" && !item.prohibited);
    for (const item of allowedDocs) {
      const option = node("option", `${item.filename} (${item.document_type})`);
      option.value = item.artifact_id;
      documentSelect.append(option);
    }
    for (const item of fileFields) {
      const option = node("option", item.label);
      option.value = item.key;
      fileFieldSelect.append(option);
    }
    documentsHost.hidden = !(allowedDocs.length && fileFields.length);
  }

  function safeFields(fields, outcomes = new Map()) {
    return fields.map((field) => ({
      key: field.key, label: field.label, type: field.type, provenance: field.provenance,
      confidence: field.confidence, requires_review: Boolean(field.requires_review), required: Boolean(field.required),
      unsupported: Boolean(field.unsupported),
      filled: Boolean(outcomes.get(field.key)?.filled), reason: outcomes.get(field.key)?.reason || field.reason || ""
    }));
  }

  async function syncStep(fields, result, stepStatus) {
    const outcomes = new Map((result?.results || []).map((item) => [item.key, item]));
    const safe = safeFields(fields, outcomes);
    const summary = {
      filled: [...outcomes.values()].filter((item) => item.filled).length,
      failed: [...outcomes.values()].filter((item) => !item.filled).length,
      manual: fields.filter((item) => item.requires_review || !fieldValue(item)).length,
      required_unresolved: fields.filter((item) => item.required && !outcomes.get(item.key)?.filled).length
    };
    const sessionPayload = { application_id: selectedApplicationId, page_url: activePageUrl, ats_type: scanResult.ats_type, fields: safe, status: stepStatus === "filled" ? "reviewed" : "draft" };
    const stepKey = await stableId("step", `${activePageUrl}|${scanResult.ats_type}`);
    try {
      await api(`/api/v1/extension/sessions/${encodeURIComponent(sessionId)}`, { method: "PUT", body: JSON.stringify(sessionPayload) });
      await api(`/api/v1/extension/sessions/${encodeURIComponent(sessionId)}/steps/${encodeURIComponent(stepKey)}`, {
        method: "PUT", body: JSON.stringify({ page_url: activePageUrl, ats_type: scanResult.ats_type, fields: safe, summary, status: stepStatus })
      });
      await flushPendingMetadata();
    } catch (error) {
      const auth = await storedAuth();
      if (error.retryable || error instanceof TypeError) {
        const pending = [...auth.pendingMetadata, { session_id: sessionId, step_key: stepKey, session: sessionPayload, step: { page_url: activePageUrl, ats_type: scanResult.ats_type, fields: safe, summary, status: stepStatus } }].slice(-20);
        await chrome.storage.local.set({ pendingMetadata: pending });
      }
      throw error;
    } finally {
      $("progress").hidden = false;
      $("progress-copy").textContent = `${summary.filled} filled · ${summary.failed} failed · ${summary.manual} manual · ${summary.required_unresolved} required unresolved`;
    }
  }

  async function flushPendingMetadata() {
    const auth = await storedAuth();
    if (!auth.pendingMetadata.length) return;
    const remaining = [];
    for (const item of auth.pendingMetadata) {
      try {
        await api(`/api/v1/extension/sessions/${encodeURIComponent(item.session_id)}`, { method: "PUT", body: JSON.stringify(item.session) });
        await api(`/api/v1/extension/sessions/${encodeURIComponent(item.session_id)}/steps/${encodeURIComponent(item.step_key)}`, { method: "PUT", body: JSON.stringify(item.step) });
      } catch (error) {
        if (error.retryable || error instanceof TypeError) remaining.push(item);
      }
    }
    await chrome.storage.local.set({ pendingMetadata: remaining.slice(-20) });
  }

  async function scan() {
    await activeTab();
    if (!selectedApplicationId || !applyContext) throw new Error("Choose and confirm an application context first.");
    status.textContent = "Scanning visible controls…";
    scanResult = await send({ type: "SCAN_FIELDS", profile: applyContext.confirmed_profile, answers: applyContext.answers });
    const auth = await storedAuth();
    const unsupportedCounts = { ...auth.unsupportedCounts };
    for (const field of scanResult.fields.filter((item) => item.unsupported)) {
      const key = `${scanResult.ats_type}:${field.reason || "unsupported control"}`;
      unsupportedCounts[key] = Math.min(1000000, Number(unsupportedCounts[key] || 0) + 1);
    }
    await chrome.storage.local.set({ unsupportedCounts });
    sessionId = await stableId("extension", selectedApplicationId);
    renderFields();
    renderDocuments();
    await syncStep(scanResult.fields, null, "scanned");
    status.textContent = `${scanResult.fields.length} controls inventoried on ${scanResult.ats_type}. Nothing has been filled.`;
  }

  async function fillReviewed(event) {
    event.preventDefault();
    const checked = new Set([...fieldsHost.querySelectorAll("input:checked")].map((item) => item.dataset.key));
    const reviewed = scanResult.fields.map((field) => ({ ...field, approved: checked.has(field.key) }));
    const result = await send({ type: "FILL_REVIEWED_FIELDS", fields: reviewed });
    await syncStep(reviewed, result, "filled");
    const count = result.results.filter((item) => item.filled).length;
    status.textContent = `${count} reviewed fields filled. Verify the page; advance and submit manually.`;
  }

  async function attachDocument() {
    const artifact = (applyContext.documents || []).find((item) => item.artifact_id === documentSelect.value);
    if (!artifact) throw new Error("Choose an approved document.");
    const response = await api(`/api/v1/extension/artifacts/${encodeURIComponent(artifact.artifact_id)}/file?application_id=${encodeURIComponent(selectedApplicationId)}`);
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.byteLength !== artifact.byte_size || await digestHex(bytes) !== artifact.sha256.toLowerCase()) throw new Error("Document verification failed; no file was attached.");
    const field = scanResult.fields.find((item) => item.key === fileFieldSelect.value);
    const responsePayload = await send({
      type: "ATTACH_REVIEWED_FILE", approved: true, field,
      artifact: { filename: artifact.filename, media_type: artifact.media_type, byte_size: artifact.byte_size, sha256: artifact.sha256 },
      data_base64: bytesToBase64(bytes)
    });
    const result = responsePayload.result;
    if (!result.filled) {
      if (attachmentUrl) URL.revokeObjectURL(attachmentUrl);
      attachmentUrl = URL.createObjectURL(new Blob([bytes], { type: artifact.media_type }));
      $("download-fallback").href = attachmentUrl;
      $("download-fallback").download = artifact.filename;
      $("download-fallback").hidden = false;
      throw new Error(`${result.reason}. Use the manual download fallback.`);
    }
    $("download-fallback").hidden = true;
    status.textContent = `${artifact.filename} attached. Confirm it remains selected before continuing.`;
  }

  async function markSubmitted() {
    if (!$("submitted-confirm").checked || !sessionId) throw new Error("Scan this application and confirm that you personally submitted it.");
    const response = await api(`/api/v1/extension/sessions/${encodeURIComponent(sessionId)}/confirm-submitted`, { method: "POST", body: JSON.stringify({}) });
    const payload = await response.json();
    if (payload.inferred !== false || payload.stage !== "applied") throw new Error("The tracker did not confirm the explicit transition.");
    status.textContent = "Marked applied from your explicit confirmation.";
    $("mark-submitted").disabled = true;
  }

  $("pair").addEventListener("click", () => pairDevice().catch((error) => { status.textContent = error.message; }));
  $("disconnect").addEventListener("click", async () => {
    await chrome.storage.local.remove(["deviceToken", "deviceId", "pendingMetadata"]);
    selectedApplicationId = ""; applyContext = null; scanResult = null;
    renderAuth(await storedAuth()); status.textContent = "Device credential removed from this browser.";
  });
  $("find-context").addEventListener("click", () => findContext().catch((error) => { status.textContent = error.message; }));
  $("scan").addEventListener("click", () => scan().catch((error) => { status.textContent = error.message; }));
  $("refresh").addEventListener("click", () => selectedApplicationId && selectContext(selectedApplicationId).catch((error) => { status.textContent = error.message; }));
  reviewForm.addEventListener("submit", (event) => fillReviewed(event).catch((error) => { status.textContent = error.message; }));
  $("attach").addEventListener("click", () => attachDocument().catch((error) => { status.textContent = error.message; }));
  $("submitted-confirm").addEventListener("change", () => { $("mark-submitted").disabled = !$("submitted-confirm").checked; });
  $("mark-submitted").addEventListener("click", () => markSubmitted().catch((error) => { status.textContent = error.message; }));
  window.addEventListener("unload", () => { if (attachmentUrl) URL.revokeObjectURL(attachmentUrl); });

  chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
    if (tabId !== activeTabId || !changeInfo.url) return;
    activePageUrl = changeInfo.url;
    scanResult = null;
    fieldsHost.replaceChildren();
    reviewForm.hidden = true;
    documentsHost.hidden = true;
    status.textContent = "Application page changed. Scan the new step before filling anything.";
  });

  storedAuth().then((auth) => { renderAuth(auth); if (auth.deviceToken) flushPendingMetadata(); });
})();
