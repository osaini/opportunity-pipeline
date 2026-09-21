// Runs before first paint (loaded without defer) so a saved theme never flashes.
// "system" leaves data-theme unset and the stylesheet follows the OS setting.
(() => {
  "use strict";
  let saved = "system";
  try {
    saved = window.localStorage.getItem("pipeline-theme") || "system";
  } catch (error) {
    saved = "system";
  }
  if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
})();
