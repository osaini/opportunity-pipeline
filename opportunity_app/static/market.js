(() => {
  "use strict";
  const root = document.getElementById("market-issues");
  const line = (label, value) => { const p = document.createElement("p"); p.textContent = `${label}: ${value ?? "unknown"}`; return p; };
  fetch("/api/v1/public/market").then(async (response) => {
    if (!response.ok) throw new Error(`Archive unavailable (${response.status})`);
    return response.json();
  }).then((archive) => {
    root.replaceChildren();
    if (!archive.items.length) { root.appendChild(line("Status", "No issue has passed editorial review yet")); return; }
    archive.items.forEach((issue) => {
      const article = document.createElement("article"); article.className = "panel";
      const title = document.createElement("h2"); title.textContent = issue.title; article.appendChild(title);
      article.append(line("As of", issue.market.as_of), line("Active roles", issue.market.active_roles),
        line("New in seven days", issue.market.new_last_7_days), line("Known compensation", issue.market.known_compensation),
        line("Unknown compensation", issue.market.unknown_compensation), line("Verified deadlines", issue.market.verified_deadlines),
        line("Snapshot", issue.snapshot_id), line("Methodology", issue.methodology));
      root.appendChild(article);
    });
  }).catch((error) => { root.textContent = error.message; });
})();
