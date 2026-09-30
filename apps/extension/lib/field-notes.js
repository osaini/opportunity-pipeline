(() => {
  "use strict";
  // The line the side panel shows under a field: why the engine proposed what it did. For a saved
  // answer that is "verify before filling" (an exact match saved for this company, or as
  // reusable), or "Saved for another company; direct review required". It is shown for every
  // field so the student sees it where they decide, not only in the synced session record.
  function reasonLine(field) {
    return field && typeof field.reason === "string" ? field.reason : "";
  }

  const api = { reasonLine };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else globalThis.ApplyModeFieldNotes = api;
})();
