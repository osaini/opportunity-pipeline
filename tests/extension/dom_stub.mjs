// Minimal hand-rolled DOM stub for exercising apps/extension/content.js and
// apply-engine.js in Node without any dependency. Supports only what the
// scripts use: querySelectorAll("input, textarea, select"), label[for="id"]
// lookup, closest("label"), aria-label/aria-labelledby/name/id/placeholder
// label sources, focus, and dispatched input/change events. Descriptor keys
// beyond the basics (all optional, all absent in the older fixtures):
//   wrapped, text          a wrapping <label>, and the control's own text inside it
//   ariaRequired, group    aria-required on the control, or on a [role=group] ancestor
//   ariaLabelledby         ids resolved through page.texts
//   container              {hiddenMirror, spanRequired, others}: the field container
//   style, rect            computed style and box; without rect, visible_css is unknown
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

// The one ancestor the engine asks about. It answers the exact selector strings the
// engine uses for a hidden required mirror and an upload group's required span, and
// reports `others` extra visible controls so a shared container can be simulated.
class StubContainer {
  constructor(control, descriptor) {
    this.control = control;
    this.descriptor = descriptor;
    this.parentElement = null;
  }

  querySelectorAll(_selector) {
    const others = Array.from({ length: this.descriptor.others || 0 }, () => (
      { disabled: false, hidden: false, type: "text", getAttribute: () => null }
    ));
    return [this.control, ...others];
  }

  querySelector(selector) {
    if (selector === 'input[required][aria-hidden="true"]') return this.descriptor.hiddenMirror ? {} : null;
    if (selector === "span.required") return this.descriptor.spanRequired ? {} : null;
    return null;
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
    this.ariaLabelledby = descriptor.ariaLabelledby || "";
    this.role = descriptor.role || "";
    this.disabled = Boolean(descriptor.disabled);
    this.value = "";
    this.checked = false;
    this.required = Boolean(descriptor.required);
    this.ariaRequired = Boolean(descriptor.ariaRequired);
    this.group = descriptor.group || null;
    this.hidden = Boolean(descriptor.hidden);
    this.accept = descriptor.accept || "";
    this.options = (descriptor.options || []).map((option) => ({ value: option.value, textContent: option.label }));
    this.labelText = descriptor.label || "";
    this.ownText = descriptor.text || "";
    this.wrappedByLabel = Boolean(descriptor.wrapped);
    this.attrs = {};
    this.style = descriptor.style || null;
    if (descriptor.rect) this.getBoundingClientRect = () => ({ right: 1000, bottom: 1000, ...descriptor.rect });
    this.parentElement = descriptor.container ? new StubContainer(this, descriptor.container) : null;
    this.events = [];
    this.focused = false;
  }

  get textContent() {
    return this.ownText;
  }

  get attributes() {
    const self = this;
    return {
      get(name) {
        return self.getAttribute(name);
      },
    };
  }

  setAttribute(name, value) {
    if (name === "aria-label") this.ariaLabel = value;
    else this.attrs[name] = String(value);
  }

  getAttribute(name) {
    if (name === "aria-label") return this.ariaLabel;
    if (name === "aria-labelledby") return this.ariaLabelledby || null;
    if (name === "role") return this.role;
    if (name === "aria-required") return this.ariaRequired ? "true" : null;
    return this.attrs[name] ?? null;
  }

  closest(selector) {
    if (selector === "label" && this.wrappedByLabel) {
      return { textContent: `${this.labelText} ${this.ownText}`.trim() };
    }
    if (selector === '[role="group"]' && this.group) {
      return { getAttribute: (name) => (name === "aria-required" && this.group.ariaRequired ? "true" : null) };
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
      if (control.id && control.labelText && !control.wrappedByLabel) this.labelsById[control.id] = { textContent: control.labelText };
    }
    this.texts = page.texts || {};
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

  getElementById(id) {
    return this.texts[id] === undefined ? null : { textContent: this.texts[id] };
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

export function loadContentScript(page, { contentScript = true } = {}) {
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
    ...(contentScript ? { chrome } : {}),
    CSS: { escape: (value) => String(value).replace(/"/g, '\\"') },
    getComputedStyle: (element) => ({ display: "block", visibility: "visible", opacity: "1", ...element.style }),
    Event: StubEvent,
    console,
    setTimeout,
  });
  // contentScript: false loads only the three files a page gets from the agent, with no chrome.*.
  for (const filename of contentScript ? ["adapters.js", "field-engine.js", "apply-engine.js", "content.js"] : ["adapters.js", "field-engine.js", "apply-engine.js"]) {
    const source = readFileSync(path.join(EXTENSION_DIR, filename), "utf8");
    vm.runInContext(source, context, { filename });
  }
  if (contentScript && !listener) throw new Error("content.js did not register an onMessage listener");
  return {
    document,
    context,
    // The shared engine itself, for options the extension's messages never pass (tag).
    engine: context.OpportunityApplyEngine,
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
