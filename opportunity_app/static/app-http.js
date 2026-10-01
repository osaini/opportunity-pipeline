// Talking to the server: api(), the CSRF header, the error that means the session ended, and the outbox of actions
// queued while offline.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { els, state } = App;

  // From app-ui.js.
  const { announce, plural, showError } = App;

  // Defined in files that load later; looked up when called.
  const invalidateUrgentBadge = (...args) => App.invalidateUrgentBadge(...args);
  const showAuth = (...args) => App.showAuth(...args);

  // FastAPI reports a failed request body as a list of {loc, msg, type}
  // objects. Turn any detail shape into a sentence a person can read; never
  // "[object Object]".
  function errorDetailText(detail) {
    if (detail == null || detail === "") return "";
    if (typeof detail === "string") return detail;
    if (typeof detail === "number" || typeof detail === "boolean") return String(detail);
    if (Array.isArray(detail)) {
      const parts = detail.map((entry) => errorDetailText(entry)).filter(Boolean);
      return [...new Set(parts)].join(" ");
    }
    if (typeof detail === "object") {
      if (typeof detail.msg === "string") {
        const text = detail.msg.replace(/^Value error,\s*/i, "").trim();
        if (!text) return "";
        const sentence = /[.!?]$/.test(text) ? text : `${text}.`;
        // Name the field when the message alone would not say which one.
        const field = Array.isArray(detail.loc)
          ? [...detail.loc].reverse().find((part) => typeof part === "string" && !["body", "query", "path", "header"].includes(part))
          : "";
        if (!field || detail.type === "value_error") return sentence;
        const label = field.replace(/_/g, " ");
        return `${label.charAt(0).toUpperCase()}${label.slice(1)}: ${sentence.charAt(0).toLowerCase()}${sentence.slice(1)}`;
      }
      for (const key of ["detail", "message", "error"]) {
        const text = errorDetailText(detail[key]);
        if (text) return text;
      }
      try {
        return JSON.stringify(detail);
      } catch (_) {
        return "";
      }
    }
    return "";
  }

  // What api() throws after a 401 has raised the sign-in gate. Callers use it to
  // stay quiet about a session that ended, so the gate is the only thing shown.
  const AUTH_REQUIRED = "Authentication required";
  const isAuthError = (error) => error.message === AUTH_REQUIRED;

  async function api(path, options = {}) {
    const isFormData = options.body instanceof FormData;
    const method = (options.method || "GET").toUpperCase();
    // The session this request was sent under: a 401 that answers after it ended is old news.
    const sentUnder = state.sessionEpoch;
    let response;
    try {
      response = await fetch(path, {
        credentials: "same-origin",
        ...options,
        headers: {
          ...(isFormData ? {} : { "Content-Type": "application/json" }),
          ...csrfHeaders(method),
          ...(options.headers || {}),
        },
      });
    } catch (cause) {
      // Only a request that never got an answer is a connection problem. Any
      // HTTP status, 5xx included, is the server's verdict and must be shown.
      const error = new Error("Connection lost.");
      error.network = true;
      error.cause = cause;
      throw error;
    }
    if (response.status === 401) {
      // The gate is already up when the session ended meanwhile (a poll still in
      // flight at sign-out) or a sign-in attempt is on screen; showing it again
      // would blank the sign-in error and move focus.
      const gateShowing = els.authGate.classList.contains("is-visible");
      if (state.sessionEpoch === sentUnder && !gateShowing) showAuth();
      throw new Error(AUTH_REQUIRED);
    }
    if (!response.ok) {
      let detail = `Request failed (${response.status})`;
      let body = null;
      try {
        body = await response.json();
        detail = errorDetailText(body.detail) || detail;
      } catch (_) {
        // The HTTP status remains a useful fallback.
      }
      const error = new Error(detail);
      error.status = response.status;
      error.detail = body?.detail;
      throw error;
    }
    if (!["GET", "HEAD", "OPTIONS"].includes(method)) invalidateUrgentBadge(path);
    if (response.status === 204) return null;
    return response.json();
  }

  function csrfHeaders(method = "POST") {
    const csrf = document.cookie.split("; ").find((entry) => entry.startsWith("pipeline_csrf="))?.split("=")[1];
    return csrf && !["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase())
      ? { "X-CSRF-Token": decodeURIComponent(csrf) }
      : {};
  }

  function requestKey() {
    return globalThis.crypto?.randomUUID?.() || `web-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  // The outbox holds actions made while offline. It is keyed to the signed-in
  // user so a shared browser never replays one person's actions as another's.
  const LEGACY_OUTBOX_KEY = "opportunity-action-outbox";
  const MAX_OUTBOX_ATTEMPTS = 5;

  function outboxKey() {
    return state.userId ? `${LEGACY_OUTBOX_KEY}:${state.userId}` : null;
  }

  function readOutbox() {
    const key = outboxKey();
    if (!key) return [];
    try {
      const value = JSON.parse(localStorage.getItem(key) || "[]");
      return Array.isArray(value) ? value : [];
    } catch (_) {
      return [];
    }
  }

  function writeOutbox(items) {
    const key = outboxKey();
    if (!key) return;
    try {
      if (items.length) localStorage.setItem(key, JSON.stringify(items.slice(-100)));
      else localStorage.removeItem(key);
    } catch (_) {
      // The current action still has a visible error if storage is unavailable.
    }
  }

  function queueAction(entry) {
    writeOutbox([...readOutbox(), { ...entry, attempts: 0 }]);
  }

  function discardLegacyOutbox() {
    try {
      // Written before outboxes were per user; its owner cannot be attributed.
      localStorage.removeItem(LEGACY_OUTBOX_KEY);
    } catch (_) {
      // Storage may be unavailable; there is then nothing to discard.
    }
  }

  async function flushOutbox() {
    const pending = readOutbox();
    if (!pending.length) return;
    const remaining = [];
    let synced = 0;
    const rejected = [];
    for (const action of pending) {
      try {
        await api(action.path, {
          method: "POST",
          headers: { "Idempotency-Key": action.key },
          body: JSON.stringify({ action: action.action }),
        });
        synced += 1;
      } catch (error) {
        // The session ended mid-flush; leave the queue for this user's next sign-in.
        if (isAuthError(error)) return;
        const attempts = (action.attempts || 0) + 1;
        if (error.network && attempts < MAX_OUTBOX_ATTEMPTS) remaining.push({ ...action, attempts });
        else rejected.push(error.network ? "still unreachable after several attempts" : error.message);
      }
    }
    writeOutbox(remaining);
    if (synced) announce(`Synced ${plural(synced, "queued action", "queued actions")}.`);
    if (rejected.length) {
      showError(`${plural(rejected.length, "queued action was", "queued actions were")} not applied: ${rejected[0]}`);
    }
  }

  Object.assign(App, {
    AUTH_REQUIRED, api, csrfHeaders, discardLegacyOutbox, errorDetailText, flushOutbox, isAuthError, queueAction,
    requestKey, writeOutbox,
  });
})();
