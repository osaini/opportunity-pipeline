// Behavioral harness for apps/extension/sidepanel.js: the real script runs in a vm with a tiny
// DOM, chrome.* and fetch stubbed, so a test clicks the real buttons and reads the real requests.
// The element ids and their initial hidden/disabled state are read from sidepanel.html, so an id
// the script asks for that the page does not have fails loudly instead of returning a blank stub.
import vm from "node:vm";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const EXTENSION_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "apps", "extension");

class El {
  constructor(tagName) {
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.listeners = {};
    this.dataset = {};
    this.attrs = {};
    this.className = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.type = "";
    this._text = "";
  }

  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }

  append(...items) {
    for (const item of items) this.children.push(typeof item === "string" ? Object.assign(new El("#text"), { _text: item }) : item);
  }

  replaceChildren(...items) {
    this._text = "";
    this.children = [];
    this.append(...items);
  }

  setAttribute(name, value) {
    this.attrs[name] = String(value);
  }

  addEventListener(type, listener) {
    (this.listeners[type] ||= []).push(listener);
  }

  descendants() {
    return this.children.flatMap((child) => [child, ...child.descendants()]);
  }

  querySelectorAll(selector) {
    if (selector === "input:checked") return this.descendants().filter((item) => item.tagName === "INPUT" && item.checked);
    throw new Error(`sidepanel harness: unsupported selector ${selector}`);
  }

  // Run the handlers for an event, as the browser would; the caller decides whether a disabled
  // control may fire (a user cannot click one, but a handler can still be reached by a bug).
  dispatch(type, event = {}) {
    for (const listener of this.listeners[type] || []) listener({ type, preventDefault() {}, ...event });
  }

  click() {
    if (this.disabled) return;
    this.dispatch("click");
  }
}

// Real crypto.subtle.digest finishes on a worker thread, outside the microtask queue settle() drains,
// so it would make a test depend on machine load. This one is the same SHA-256, resolved in a microtask.
// A test can hold the next digest (holdNextDigest) to widen the window between two awaits in the script.
function makeDigestCrypto() {
  let holdNext = false;
  let release = null;
  return {
    holdNextDigest() { holdNext = true; },
    releaseDigest() { release?.(); },
    crypto: {
      subtle: {
        digest: async (_algorithm, bytes) => {
          const value = Uint8Array.from(createHash("sha256").update(bytes).digest()).buffer;
          if (holdNext) {
            holdNext = false;
            await new Promise((resolve) => { release = resolve; });
          }
          return value;
        },
      },
    },
  };
}

function pageElements() {
  const html = readFileSync(path.join(EXTENSION_DIR, "sidepanel.html"), "utf8");
  const elements = new Map();
  for (const match of html.matchAll(/<(\w+)([^>]*)>/g)) {
    const id = /\bid="([^"]+)"/.exec(match[2])?.[1];
    if (!id) continue;
    const element = new El(match[1]);
    element.hidden = /\shidden(\s|$)/.test(match[2]);
    element.disabled = /\sdisabled(\s|$)/.test(match[2]);
    element.type = /\btype="([^"]+)"/.exec(match[2])?.[1] || "";
    element.value = /\bvalue="([^"]*)"/.exec(match[2])?.[1] || "";
    elements.set(id, element);
  }
  return elements;
}

export function settle() {
  return new Promise((resolve) => setImmediate(() => setImmediate(() => setImmediate(resolve))));
}

// options.applications: [{id, company, title}] the tracker knows about for this page.
// options.slowContext: application ids whose apply-context response waits for release().
// options.withDocument: every context offers one approved document and the scan finds a file field;
//   the next attach request to the page waits for releaseAttach() after holdNextAttach().
export async function loadSidepanel({ applications, slowContext = [], withDocument = false }) {
  const digests = makeDigestCrypto();
  const emptySha256 = createHash("sha256").update(Buffer.alloc(0)).digest("hex");
  let holdAttach = false;
  let releaseAttach = null;
  const elements = pageElements();
  const store = { serverOrigin: "http://127.0.0.1:8765", deviceToken: "device-token", deviceId: "device-1", pendingMetadata: [], unsupportedCounts: {} };
  const requests = [];
  const held = new Map();
  // The tab the panel is looking at; navigate() moves it and tells the panel, as Chrome would.
  const tab = { id: 7, url: "https://jobs.example.com/apply/1" };
  const tabUpdateListeners = [];
  const json = (payload) => ({ ok: true, status: 200, json: async () => payload, arrayBuffer: async () => new ArrayBuffer(0) });
  const fetchStub = async (url, options = {}) => {
    const parsed = new URL(url);
    const method = options.method || "GET";
    requests.push({ method, path: parsed.pathname, search: parsed.search, body: options.body ? JSON.parse(options.body) : undefined });
    if (parsed.pathname === "/api/v1/extension/application-candidates") {
      return json({
        preselected_application_id: null,
        items: applications.map((item) => ({ application_id: item.id, company: item.company, title: item.title, match_kind: "same_host", stage: "saved" })),
      });
    }
    if (parsed.pathname === "/api/v1/extension/apply-context") {
      const id = parsed.searchParams.get("application_id");
      const item = applications.find((candidate) => candidate.id === id);
      const payload = {
        application: { company: item.company, title: item.title, opportunity_status: "open", opportunity_updated_at: "2026-09-30", opportunity_id: `opp-${id}` },
        match: { score: 80, explanation: [] },
        confirmed_profile: {},
        answers: [],
        documents: withDocument ? [{ artifact_id: "doc-1", filename: `resume-${id}.pdf`, media_type: "application/pdf", byte_size: 0, sha256: emptySha256, document_type: "resume" }] : [],
      };
      if (slowContext.includes(id)) await new Promise((resolve) => held.set(id, resolve));
      return json(payload);
    }
    if (parsed.pathname.endsWith("/confirm-submitted")) return json({ inferred: false, stage: "applied" });
    return json({});
  };
  const chromeStub = {
    storage: {
      local: {
        get: async (defaults = {}) => ({ ...defaults, ...store }),
        set: async (values) => { Object.assign(store, values); },
        remove: async (keys) => { for (const key of keys) delete store[key]; },
      },
    },
    permissions: { request: async () => true },
    tabs: {
      query: async () => [{ ...tab }],
      sendMessage: async (_tabId, message) => {
        if (message.type === "SCAN_FIELDS") {
          const fields = [{ key: "email", label: "Email", type: "email", provenance: "confirmed_profile.contact.email", confidence: 1, proposed_value: "student@example.com" }];
          if (withDocument) fields.push({ key: "resume", label: "Resume", type: "file", provenance: "page", confidence: 1 });
          return { ats_type: "generic", fields };
        }
        if (message.type === "ATTACH_REVIEWED_FILE") {
          if (holdAttach) {
            holdAttach = false;
            await new Promise((resolve) => { releaseAttach = resolve; });
          }
          return { result: { filled: false, reason: "The page would not take the file" } };
        }
        return { results: [] };
      },
      onUpdated: { addListener: (listener) => { tabUpdateListeners.push(listener); } },
    },
    scripting: { executeScript: async () => {} },
  };
  const document = {
    getElementById(id) {
      const element = elements.get(id);
      if (!element) throw new Error(`sidepanel harness: sidepanel.html has no element #${id}`);
      return element;
    },
    createElement: (tag) => new El(tag),
  };
  const context = vm.createContext({
    document, chrome: chromeStub, fetch: fetchStub, crypto: digests.crypto, TextEncoder, URL, Blob, btoa,
    window: { addEventListener() {} },
    console,
  });
  for (const lib of ["lib/documents.js", "lib/field-notes.js"]) {
    vm.runInContext(readFileSync(path.join(EXTENSION_DIR, lib), "utf8"), context, { filename: lib });
  }
  vm.runInContext(readFileSync(path.join(EXTENSION_DIR, "sidepanel.js"), "utf8"), context, { filename: "sidepanel.js" });
  await settle();

  const $ = (id) => document.getElementById(id);
  const candidateButton = (id) => {
    const button = $("candidates").children.find((child) => child.dataset.applicationId === id);
    if (!button) throw new Error(`no candidate button for ${id}`);
    return button;
  };
  return {
    $,
    requests,
    release: (id) => held.get(id)?.(),
    holdNextDigest: () => digests.holdNextDigest(),
    releaseDigest: () => digests.releaseDigest(),
    holdNextAttach: () => { holdAttach = true; },
    releaseAttach: () => releaseAttach?.(),
    // Press Attach without waiting for the page to answer.
    // (The stub select has no options to pick from, so it is set to what the script put in them.)
    startAttach: () => {
      $("document-select").value = "doc-1";
      $("file-field-select").value = "resume";
      $("attach").click();
    },
    async findApplications() {
      $("find-context").click();
      await settle();
    },
    // The candidate buttons are rendered by the script; click the one for this application.
    async chooseApplication(id) {
      candidateButton(id).click();
      await settle();
    },
    // Start choosing an application without waiting for its context to arrive.
    startChoosing: (id) => candidateButton(id).click(),
    async scan() {
      $("scan").click();
      await settle();
    },
    // The student opens another page in the tab the panel is attached to.
    async navigate(url) {
      tab.url = url;
      for (const listener of tabUpdateListeners) listener(tab.id, { url });
      await settle();
    },
    confirmRequests: () => requests.filter((item) => item.path.endsWith("/confirm-submitted")),
    // What a user does: tick the box, press the button. A disabled button cannot be pressed.
    async tickAndMarkSubmitted() {
      $("submitted-confirm").checked = true;
      $("submitted-confirm").dispatch("change");
      $("mark-submitted").click();
      await settle();
    },
    // What a stale handler could do: reach the handler even though the button is disabled.
    async forceMarkSubmitted() {
      $("submitted-confirm").checked = true;
      $("mark-submitted").dispatch("click");
      await settle();
    },
  };
}
