// Minimal hand-rolled DOM stub for exercising apps/extension/content.js in
// Node without any dependency. Supports only what the content script uses:
// querySelectorAll("input, textarea, select"), label[for="id"] lookup,
// closest("label"), aria-label/name/id/placeholder label sources, focus,
// and dispatched input/change events.
import vm from "node:vm";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const EXTENSION_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "apps", "extension");

class StubEvent {
  constructor(type, init = {}) {
    this.type = type;
    this.bubbles = Boolean(init.bubbles);
  }
}

class StubElement {
  constructor(descriptor) {
    this.tagName = String(descriptor.tag || "input").toUpperCase();
    this.type = descriptor.type || "";
    this.id = descriptor.id || "";
    this.name = descriptor.name || "";
    this.placeholder = descriptor.placeholder || "";
    this.ariaLabel = descriptor.ariaLabel || "";
    this.role = descriptor.role || "";
    this.disabled = Boolean(descriptor.disabled);
    this.value = "";
    this.checked = false;
    this.required = Boolean(descriptor.required);
    this.hidden = Boolean(descriptor.hidden);
    this.accept = descriptor.accept || "";
    this.options = (descriptor.options || []).map((option) => ({ value: option.value, textContent: option.label }));
    this.labelText = descriptor.label || "";
    this.wrappedByLabel = false;
    this.events = [];
    this.focused = false;
  }

  get attributes() {
    const self = this;
    return {
      get(name) {
        if (name === "aria-label") return self.ariaLabel;
        if (name === "role") return self.role;
        if (name === "aria-required") return self.required ? "true" : null;
        return null;
      },
    };
  }

  setAttribute(name, value) {
    if (name === "aria-label") this.ariaLabel = value;
  }

  getAttribute(name) {
    if (name === "aria-label") return this.ariaLabel;
    if (name === "role") return this.role;
    if (name === "aria-required") return this.required ? "true" : null;
    return null;
  }

  closest(selector) {
    if (selector === "label" && this.wrappedByLabel) {
      return { textContent: this.labelText };
    }
    return null;
  }

  focus() {
    this.focused = true;
  }

  dispatchEvent(event) {
    this.events.push(event);
  }
}

export class StubDocument {
  constructor(page) {
    this.controls = (page.controls || []).map((descriptor) => new StubElement(descriptor));
    this.labelsById = {};
    for (const control of this.controls) {
      if (control.id && control.labelText) this.labelsById[control.id] = { textContent: control.labelText };
    }
    this.removed = new Set();
  }

  // SPA-style mutation used by hostile fixtures.
  remove(control) {
    this.removed.add(control);
  }

  insertBefore(descriptor, _reference) {
    const control = new StubElement(descriptor);
    this.controls.unshift(control);
    return control;
  }

  appendControl(descriptor) {
    const control = new StubElement(descriptor);
    this.controls.push(control);
    if (control.id && control.labelText) this.labelsById[control.id] = { textContent: control.labelText };
    return control;
  }

  querySelectorAll(selector) {
    if (!["input, textarea, select", "input, textarea, select, [role=combobox]"].includes(selector)) return [];
    return this.controls.filter((control) => !this.removed.has(control));
  }

  querySelector(selector) {
    const match = /^label\[for="(.+)"\]$/.exec(selector);
    if (match) return this.labelsById[match[1]] || null;
    return null;
  }
}

export function loadContentScript(page) {
  const document = new StubDocument(page);
  let listener = null;
  const chrome = {
    runtime: {
      onMessage: {
        addListener(callback) {
          listener = callback;
        },
      },
    },
  };
  const context = vm.createContext({
    document,
    location: { hostname: page.hostname, href: page.url },
    chrome,
    CSS: { escape: (value) => String(value).replace(/"/g, '\\"') },
    Event: StubEvent,
    console,
    setTimeout,
  });
  for (const filename of ["adapters.js", "field-engine.js", "content.js"]) {
    const source = readFileSync(path.join(EXTENSION_DIR, filename), "utf8");
    vm.runInContext(source, context, { filename });
  }
  if (!listener) throw new Error("content.js did not register an onMessage listener");
  return {
    document,
    scan(profile, answers) {
      let response = null;
      listener({ type: "SCAN_FIELDS", profile, answers }, null, (value) => {
        response = value;
      });
      return response;
    },
    fill(fields) {
      let response = null;
      listener({ type: "FILL_REVIEWED_FIELDS", fields }, null, (value) => {
        response = value;
      });
      return response;
    },
  };
}
