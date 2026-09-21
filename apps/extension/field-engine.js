(() => {
  "use strict";
  function hash(value) {
    let result = 2166136261;
    for (let index = 0; index < value.length; index += 1) {
      result ^= value.charCodeAt(index);
      result = Math.imul(result, 16777619);
    }
    return (result >>> 0).toString(16).padStart(8, "0");
  }
  function setNative(control, property, value) {
    let prototype = Object.getPrototypeOf(control);
    while (prototype) {
      const descriptor = Object.getOwnPropertyDescriptor(prototype, property);
      if (descriptor?.set) { descriptor.set.call(control, value); return; }
      prototype = Object.getPrototypeOf(prototype);
    }
    control[property] = value;
  }
  function dispatchValueEvents(control) {
    control.dispatchEvent(new Event("input", { bubbles: true }));
    control.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function normalized(value) { return String(value).trim().toLowerCase(); }
  globalThis.OpportunityFieldEngine = Object.freeze({ hash, setNative, dispatchValueEvents, normalized });
})();
