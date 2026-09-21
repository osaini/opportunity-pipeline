(() => {
  "use strict";
  const status = document.getElementById("oauth-status");
  const provider = location.pathname.split("/")[3];
  const params = new URLSearchParams(location.search);
  // Gmail drafts are connected from the Outreach tab, so that is where to return.
  const [returnPath, returnName] = provider === "gmail_drafts" ? ["/outreach", "Outreach"] : ["/profile", "Profile"];
  const returnLink = document.querySelector("main a");
  if (returnLink) {
    returnLink.href = returnPath;
    returnLink.textContent = `Return to ${returnName}`;
  }
  const csrf = document.cookie.split("; ").find((entry) => entry.startsWith("pipeline_csrf="))?.split("=")[1];
  if (params.get("error")) {
    status.textContent = `Connection was not approved: ${params.get("error")}`;
    return;
  }
  fetch(`/api/v1/connections/oauth/${encodeURIComponent(provider)}/complete`, {
    method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json", ...(csrf ? {"X-CSRF-Token": decodeURIComponent(csrf)} : {})},
    body: JSON.stringify({state: params.get("state"), code: params.get("code")}),
  }).then(async (response) => {
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "Connection failed");
    status.textContent = `${provider === "gmail_drafts" ? "Gmail drafts" : body.provider} connected. Returning to ${returnName}…`;
    setTimeout(() => location.assign(returnPath), 800);
  }).catch((error) => { status.textContent = error.message; });
})();
