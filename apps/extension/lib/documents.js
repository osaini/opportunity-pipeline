(() => {
  "use strict";
  // The documents the side panel offers for one application: approved uploads
  // and documents for this role only, in the order the tracker sent them. The
  // résumé variant the tracker picked for this role (preferred) is selected;
  // nothing is reordered, so the list reads the same with or without a pick.
  function documentChoices(documents, opportunityId) {
    const allowed = (Array.isArray(documents) ? documents : [])
      .filter((item) => item && (!item.opportunity_id || item.opportunity_id === opportunityId));
    const preferred = allowed.findIndex((item) => item.preferred === true);
    const selected = preferred >= 0 ? preferred : 0;
    return allowed.map((item, index) => ({ item, selected: allowed.length > 0 && index === selected }));
  }

  function documentLabel(item) {
    const variant = typeof item?.variant_label === "string" && item.variant_label.trim() ? `, ${item.variant_label.trim()} variant` : "";
    const picked = item?.preferred === true ? ", picked for this role" : "";
    return `${item?.filename || "Document"} (${item?.document_type || "document"}${variant}${picked})`;
  }

  const api = { documentChoices, documentLabel };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else globalThis.ApplyModeDocuments = api;
})();
