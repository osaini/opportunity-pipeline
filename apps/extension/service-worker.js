"use strict";

function enableActionPanel() {
  return chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
}

chrome.runtime.onInstalled.addListener(() => {
  enableActionPanel().catch(() => undefined);
});

chrome.runtime.onStartup.addListener(() => {
  enableActionPanel().catch(() => undefined);
});

enableActionPanel().catch(() => undefined);
