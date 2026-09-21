import assert from "node:assert/strict";
import { cpSync, existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "node:http";
import { chromium } from "playwright";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const EXTENSION = path.join(ROOT, "apps", "extension");
const sandboxDir = mkdtempSync(path.join(tmpdir(), "opportunity-extension-browser-"));
const profileDir = path.join(sandboxDir, "profile");
const extensionUnderTest = path.join(sandboxDir, "extension");
const defects = [];
let context;
let server;

function installedChromium() {
  const base = process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, "ms-playwright");
  if (!base || !existsSync(base)) return undefined;
  const versions = readdirSync(base).filter((name) => /^chromium-\d+$/.test(name)).sort().reverse();
  for (const version of versions) {
    for (const folder of ["chrome-win64", "chrome-win"]) {
      const executable = path.join(base, version, folder, "chrome.exe");
      if (existsSync(executable)) return executable;
    }
  }
  return undefined;
}

function gate(page, name) {
  page.on("pageerror", (error) => defects.push(`${name} pageerror: ${error.message}`));
  page.on("console", (message) => { if (message.type() === "error") defects.push(`${name} console: ${message.text()}`); });
  page.on("requestfailed", (request) => defects.push(`${name} request failed: ${request.url()} ${request.failure()?.errorText}`));
  page.on("response", (response) => { if (response.status() >= 400) defects.push(`${name} HTTP ${response.status()}: ${response.url()}`); });
}

try {
  server = createServer((request, response) => {
    const extensionOrigin = request.headers.origin || "*";
    response.setHeader("Access-Control-Allow-Origin", extensionOrigin);
    response.setHeader("Access-Control-Allow-Headers", "Authorization, Content-Type");
    response.setHeader("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS");
    if (request.method === "OPTIONS") { response.writeHead(204); response.end(); return; }
    if (request.url === "/favicon.ico") { response.writeHead(204); response.end(); return; }
    if (request.url === "/api/v1/extension/pairings/redeem") {
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify({ device_token: "browser-device-token", device_id: "browser-device" }));
      return;
    }
    if (request.url === "/ats") {
      response.setHeader("Content-Type", "text/html; charset=utf-8");
      response.end(`<!doctype html><html><body>
        <form id="application"><label for="candidate">Candidate Name</label><input id="candidate" name="candidate_name">
        <label for="email">Email</label><input id="email" name="email" type="email" required>
        <label for="company">Company Name</label><input id="company" name="company_name">
        <label for="consent">Consent to data processing</label><input id="consent" type="checkbox">
        <button id="next" type="button">Next</button><button id="submit" type="submit">Submit application</button></form>
        <script>window.__nextClicks=0;window.__submitted=false;
          document.querySelector('#next').addEventListener('click',()=>window.__nextClicks++);
          document.querySelector('#application').addEventListener('submit',(event)=>{event.preventDefault();window.__submitted=true;});
        </script></body></html>`);
      return;
    }
    response.writeHead(404); response.end("not found");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const trackerOrigin = `http://127.0.0.1:${server.address().port}`;
  cpSync(EXTENSION, extensionUnderTest, { recursive: true });
  const testManifestPath = path.join(extensionUnderTest, "manifest.json");
  const testManifest = JSON.parse(readFileSync(testManifestPath, "utf8"));
  // The production extension keeps this as an optional permission. Browser UI
  // permission prompts cannot be driven in headless mode, so the unpacked test
  // copy pre-grants only the same loopback pattern.
  testManifest.host_permissions = ["http://127.0.0.1/*"];
  delete testManifest.optional_host_permissions;
  writeFileSync(testManifestPath, JSON.stringify(testManifest));

  context = await chromium.launchPersistentContext(profileDir, {
    channel: "chromium",
    executablePath: installedChromium(),
    headless: true,
    args: [`--disable-extensions-except=${extensionUnderTest}`, `--load-extension=${extensionUnderTest}`],
  });
  let worker = context.serviceWorkers()[0];
  if (!worker) worker = await context.waitForEvent("serviceworker");
  const extensionId = new URL(worker.url()).hostname;
  assert.match(extensionId, /^[a-p]{32}$/);

  const ats = await context.newPage();
  gate(ats, "ats");
  await ats.goto(`${trackerOrigin}/ats`);

  const panel = await context.newPage();
  gate(panel, "panel");
  await panel.goto(`chrome-extension://${extensionId}/sidepanel.html`);
  await panel.waitForSelector("#pairing-code");
  assert.equal(await panel.getByLabel("Pairing code").getAttribute("type"), "password");
  assert.equal(await panel.locator("body").innerText().then((text) => text.includes("Local API token")), false);
  assert.equal(await panel.locator("body").innerText().then((text) => text.includes("profile JSON")), false);
  await panel.getByLabel("Local server").fill(trackerOrigin);
  await panel.getByLabel("Pairing code").fill("browser-pairing-code-123456");
  await panel.getByRole("button", { name: "Pair extension" }).click();
  await panel.getByText("Paired. Open an application page", { exact: false }).waitFor();

  await ats.bringToFront();
  const scan = await panel.evaluate(async () => {
    const [target] = await chrome.tabs.query({ active: true, currentWindow: true });
    await chrome.scripting.executeScript({ target: { tabId: target.id }, files: ["adapters.js", "field-engine.js", "content.js"] });
    return chrome.tabs.sendMessage(target.id, {
      type: "SCAN_FIELDS",
      profile: { name: "Confirmed Student", "contact.email": "confirmed@example.com" },
      answers: [],
    });
  });
  const company = scan.fields.find((field) => field.label.startsWith("Company Name"));
  const consent = scan.fields.find((field) => field.label.startsWith("Consent"));
  const email = scan.fields.find((field) => field.label.startsWith("Email"));
  assert.equal(company.proposed_value, "");
  assert.equal(consent.prohibited, true);
  assert.equal(scan.fields.some((field) => field.type === "submit" || field.label.startsWith("Next")), false);

  await ats.bringToFront();
  const fill = await panel.evaluate(async ({ fields }) => {
    const [target] = await chrome.tabs.query({ active: true, currentWindow: true });
    return chrome.tabs.sendMessage(target.id, { type: "FILL_REVIEWED_FIELDS", fields });
  }, { fields: scan.fields.map((field) => ({ ...field, approved: field.key === email.key })) });
  assert.equal(fill.results.find((item) => item.key === email.key).filled, true);
  assert.equal(await ats.locator("#email").inputValue(), "confirmed@example.com");
  assert.equal(await ats.locator("#company").inputValue(), "");
  assert.equal(await ats.locator("#consent").isChecked(), false);
  assert.deepEqual(await ats.evaluate(() => ({ next: window.__nextClicks, submitted: window.__submitted })), { next: 0, submitted: false });

  await ats.locator("#email").evaluate((element) => element.remove());
  await ats.bringToFront();
  const stale = await panel.evaluate(async ({ field }) => {
    const [target] = await chrome.tabs.query({ active: true, currentWindow: true });
    return chrome.tabs.sendMessage(target.id, { type: "FILL_REVIEWED_FIELDS", fields: [{ ...field, approved: true }] });
  }, { field: email });
  assert.equal(stale.results[0].filled, false);
  assert.match(stale.results[0].reason, /no longer present|ambiguous/);

  assert.deepEqual(defects, [], defects.join("\n"));
  console.log("4 extension browser checks passed");
} finally {
  if (context) await context.close();
  if (server) await new Promise((resolve) => server.close(resolve));
  rmSync(sandboxDir, { recursive: true, force: true });
}
