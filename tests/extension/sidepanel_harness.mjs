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
const syncDigestCrypto = {
  subtle: { digest: async (_algorithm, bytes) => Uint8Array.from(createHash("sha256").update(bytes).digest()).buffer },
};

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
export async function loadSidepanel({ applications, slowContext = [] }) {
  const elements = pageElements();
  const store = { serverOrigin: "http://127.0.0.1:8765", deviceToken: "device-token", deviceId: "device-1", pendingMetadata: [], unsupportedCounts: {} };
  const requests = [];
  const held = new Map();
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
        documents: [],
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
      query: async () => [{ id: 7, url: "https://jobs.example.com/apply/1" }],
      sendMessage: async (_tabId, message) => {
        if (message.type === "SCAN_FIELDS") {
          return { ats_type: "generic", fields: [{ key: "email", label: "Email", type: "email", provenance: "confirmed_profile.contact.email", confidence: 1, proposed_value: "student@example.com" }] };
        }
        return { results: [] };
      },
      onUpdated: { addListener() {} },
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
    document, chrome: chromeStub, fetch: fetchStub, crypto: syncDigestCrypto, TextEncoder, URL, Blob, btoa,
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
