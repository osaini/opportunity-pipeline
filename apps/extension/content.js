(() => {
  "use strict";
  if (globalThis.__opportunityApplyModeLoaded) return;
  const ENGINE = globalThis.OpportunityApplyEngine;
  if (!globalThis.OpportunityApplyAdapters || !globalThis.OpportunityFieldEngine || !ENGINE) throw new Error("Apply Mode adapter modules were not loaded");
  globalThis.__opportunityApplyModeLoaded = true;

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.type === "SCAN_FIELDS") sendResponse(ENGINE.scan(message.profile || {}, message.answers || [], { company: message.company }));
    if (message.type === "FILL_REVIEWED_FIELDS") sendResponse({ results: ENGINE.fill(message.fields || []), final_submit_available: false });
    if (message.type === "ATTACH_REVIEWED_FILE") sendResponse({ result: ENGINE.attachDocumentFromBytes(message), final_submit_available: false });
  });
})();
