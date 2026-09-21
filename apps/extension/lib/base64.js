(() => {
  "use strict";
  // btoa throws on non-Latin-1 strings; job URLs frequently contain them.
  function utf8SafeBtoa(value) {
    const bytes = new TextEncoder().encode(String(value));
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary);
  }
  const api = { utf8SafeBtoa };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else globalThis.ApplyModeBase64 = api;
})();
