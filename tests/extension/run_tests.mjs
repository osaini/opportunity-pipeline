// Zero-dependency test runner for the Apply Mode extension.
// Usage: node tests/extension/run_tests.mjs
// Exercises content.js scan/fill behavior against hand-rolled DOM stubs and
// per-ATS fixtures, plus the UTF-8-safe base64 helper.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { loadContentScript } from "./dom_stub.mjs";

const require = createRequire(import.meta.url);
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const FIXTURES = path.join(ROOT, "tests", "extension", "fixtures");

const profile = {
  name: "Test Student",
  contact: {
    email: "test@example.com",
    phone: "(512) 555-0123",
    github: "https://github.com/test",
  },
  school: "University of Texas",
};

function loadFixture(name) {
  return JSON.parse(readFileSync(path.join(FIXTURES, name), "utf8"));
}

function byLabel(scan, label) {
  // labelFor() appends name/id sources, so match on the primary label prefix.
  const field = scan.fields.find((field) => field.label === label || field.label.startsWith(label));
  assert.ok(field, `expected a scanned field labelled ${label}`);
  return field;
}

const tests = {};
tests.base64_utf8_safe = () => {
  const base64 = require(path.join(ROOT, "apps", "extension", "lib", "base64.js"));
  const url = "https://jobs.example.com/apply/東京-role?q=café";
  const encoded = base64.utf8SafeBtoa(url); // plain btoa would throw here
  const decoded = Buffer.from(encoded, "base64").toString("utf8");
  assert.equal(decoded, url);
  assert.equal(base64.utf8SafeBtoa("plain ascii"), btoa("plain ascii"));
};

tests.greenhouse_scan_maps_and_flags_sensitive_fields = () => {
  const page = loadFixture("greenhouse.json");
  const ext = loadContentScript(page);
  const scan = ext.scan(profile);
  assert.equal(scan.ats_type, "greenhouse");

  const email = byLabel(scan, "Email Address");
  assert.equal(email.provenance, "confirmed_profile.contact.email");
  assert.equal(email.proposed_value, profile.contact.email);
  assert.ok(email.confidence >= 0.9);

  const first = byLabel(scan, "First Name");
  assert.equal(first.provenance, "confirmed_profile.name.first");
  assert.equal(first.proposed_value, "Test");

  const workAuth = byLabel(scan, "Are you legally authorized to work in this country?");
  assert.ok(workAuth.requires_review, "work authorization must require review");
  assert.ok(workAuth.confidence <= 0.5);

  const unmapped = byLabel(scan, "Favorite ice cream flavor");
  assert.equal(unmapped.provenance, "unmapped");
  assert.equal(unmapped.confidence, 0);
  assert.equal(unmapped.proposed_value, "");
};

tests.workday_scan_excludes_never_fill_and_disabled_controls = () => {
  const page = loadFixture("workday.json");
  const ext = loadContentScript(page);
  const scan = ext.scan(profile);
  assert.equal(scan.ats_type, "workday");
  const labels = scan.fields.map((field) => field.label);
  assert.ok(!labels.includes("Submit Application"), "submit controls must never be inventoried");
  assert.ok(!labels.includes("Recruiter Comments"), "disabled controls must be skipped");

  const citizenship = byLabel(scan, "Country of Citizenship");
  assert.ok(citizenship.requires_review, "citizenship is sensitive");

  // An accessibility-name-only field still appears with its aria-label text,
  // but "Electronic Mail Address" carries no mappable keyword, so it must
  // conservatively stay unmapped.
  const email = byLabel(scan, "Electronic Mail Address");
  assert.equal(email.provenance, "unmapped");
  assert.equal(email.proposed_value, "");
};

tests.fill_only_touches_approved_fields_and_verifies_values = () => {
  const page = loadFixture("greenhouse.json");
  const ext = loadContentScript(page);
  const scan = ext.scan(profile);
  const approvedKeys = new Set(
    scan.fields
      .filter((field) => field.label.startsWith("Email Address") || field.label.startsWith("First Name"))
      .map((field) => field.key)
  );
  const reviewed = scan.fields.map((field) => ({ ...field, approved: approvedKeys.has(field.key) }));
  const result = ext.fill(reviewed);

  const filledKeys = new Set(result.results.filter((r) => r.filled).map((r) => r.key));
  assert.deepEqual([...filledKeys].sort(), [...approvedKeys].sort());
  const controls = ext.document.controls;
  assert.equal(controls.find((c) => c.id === "email").value, profile.contact.email);
  assert.equal(controls.find((c) => c.id === "first_name").value, "Test");
  assert.equal(controls.find((c) => c.id === "last_name").value, "", "unapproved fields stay untouched");
  const emailEvents = controls.find((c) => c.id === "email").events.map((e) => e.type);
  assert.deepEqual(emailEvents, ["input", "change"]);
};

tests.hostile_dom_mutation_fills_by_label_not_stale_index = () => {
  const page = loadFixture("lever.json");
  const ext = loadContentScript(page);
  const scan = ext.scan(profile);
  const reviewed = scan.fields.map((field) => ({
    ...field,
    approved: field.proposed_value !== "",
  }));

  // SPA-style mutation between scan and fill: the first control is removed
  // and a new unrelated field shifts every index by one.
  const nameControl = ext.document.controls.find((c) => c.id === "name");
  ext.document.remove(nameControl);
  ext.document.insertBefore({ tag: "input", type: "text", id: "referral", label: "Referral Code" });

  const result = ext.fill(reviewed);
  const statuses = Object.fromEntries(result.results.map((r) => [r.key, r]));

  const emailField = reviewed.find((f) => f.label.startsWith("Email"));
  const githubField = reviewed.find((f) => f.label.startsWith("GitHub"));
  assert.equal(statuses[emailField.key].filled, true, "email should resolve by label despite index shift");
  assert.equal(ext.document.controls.find((c) => c.id === "email").value, profile.contact.email);
  assert.equal(statuses[githubField.key].filled, true);
  assert.equal(ext.document.controls.find((c) => c.id === "referral").value, "", "index shift must not write into the wrong field");
  assert.equal(statuses[nameFieldKey(reviewed)].filled, false, "removed field reports failure instead of misfiring");
  if (statuses[nameFieldKey(reviewed)].reason) {
    assert.match(statuses[nameFieldKey(reviewed)].reason, /no longer present/i);
  }
};

function nameFieldKey(reviewed) {
  return reviewed.find((f) => f.label.startsWith("Name")).key;
}

tests.fill_reports_when_framework_reverts_value = () => {
  const page = loadFixture("generic.json");
  const ext = loadContentScript(page);
  const scan = ext.scan(profile);
  const fullName = byLabel(scan, "Full Legal Name");
  const reviewed = scan.fields.map((field) => ({ ...field, approved: field.key === fullName.key }));
  // Simulate a framework normalizing/reverting programmatic writes.
  const control = ext.document.controls.find((c) => c.id === "fullName");
  Object.defineProperty(control, "value", {
    get() {
      return "";
    },
    set() {},
  });
  const result = ext.fill(reviewed);
  const status = result.results.find((r) => r.key === fullName.key);
  assert.equal(status.filled, false, "reverted values must not be reported as filled");
  assert.ok(status.reason);
};

tests.content_script_preserves_no_submit_guarantee = () => {
  const source = readFileSync(path.join(ROOT, "apps", "extension", "content.js"), "utf8");
  assert.match(source, /"submit"/);
  assert.doesNotMatch(source, /\.click\(/);
  assert.doesNotMatch(source, /requestSubmit/);
  assert.doesNotMatch(source, /\.submit\(/);
  // Fill results may never include a submit-type target even if forged.
  const page = loadFixture("lever.json");
  const ext = loadContentScript(page);
  const result = ext.fill([
    {
      key: "forged",
      label: "Submit Application",
      type: "submit",
      provenance: "forged",
      confidence: 1,
      requires_review: false,
      reason: "",
      proposed_value: "x",
      elementIndex: 0,
      approved: true,
    },
  ]);
  assert.equal(result.results.length, 1);
  assert.equal(result.results[0].filled, false);
  assert.match(result.results[0].reason, /prohibited/i);
  assert.equal(
    ext.document.controls.find((c) => c.id === "name").value,
    "",
    "the forged submit field must not redirect its value into another control"
  );
};

tests.answer_library_proposes_with_provenance = () => {
  const page = loadFixture("workday.json");
  const ext = loadContentScript(page);
  const answers = [
    {
      id: "ans-1",
      question: "Why do you want to work at this company?",
      answer: "Because Acme builds robots I admire.",
    },
  ];
  const scan = ext.scan(profile, answers);

  const motivation = scan.fields.find((field) => field.label.startsWith("Why do you want this role?"));
  assert.ok(motivation, "expected the motivation textarea");
  assert.equal(motivation.provenance, "answer_library:ans-1");
  assert.ok(motivation.proposed_value.includes("Acme"), "library answer becomes the proposal");
  assert.equal(motivation.confidence, 0.7);

  const name = byLabel(scan, "Given Name");
  assert.match(name.provenance, /^confirmed_profile/), "profile mapping wins over library";

  // Without library answers the same field stays unmapped.
  const bareScan = ext.scan(profile);
  const bareMotivation = bareScan.fields.find((field) => field.label.startsWith("Why do you want this role?"));
  assert.equal(bareMotivation.provenance, "unmapped");
};

tests.answer_library_skips_sensitive_fields = () => {
  const page = loadFixture("greenhouse.json");
  const ext = loadContentScript(page);
  const answers = [
    {
      id: "ans-9",
      question: "What is your country of citizenship or work authorization status?",
      answer: "US citizen",
    },
  ];
  const scan = ext.scan(profile, answers);
  const workAuth = scan.fields.find((field) => field.label.includes("authorized to work"));
  assert.ok(workAuth, "expected the work-authorization field");
  assert.equal(workAuth.provenance, "unmapped", "sensitive fields must not receive library proposals");
  assert.equal(workAuth.proposed_value, "");
  assert.match(workAuth.reason, /[Ss]ensitive/);
};

tests.six_supported_families_are_detected_with_conservative_mapping = () => {
  const fixtures = [
    ["workday.json", "workday"], ["greenhouse.json", "greenhouse"],
    ["lever.json", "lever"], ["ashby.json", "ashby"],
    ["smartrecruiters.json", "smartrecruiters"], ["generic.json", "generic"],
  ];
  for (const [fixture, expected] of fixtures) {
    assert.equal(loadContentScript(loadFixture(fixture)).scan(profile).ats_type, expected);
  }
  const ashby = loadContentScript(loadFixture("ashby.json")).scan(profile);
  const company = byLabel(ashby, "Company Name");
  assert.equal(company.provenance, "unmapped", "company name must never receive the applicant's name");
  assert.equal(company.proposed_value, "");
  assert.equal(byLabel(ashby, "Consent").prohibited, true, "consent controls must be visibly prohibited");
  assert.ok(!ashby.fields.some((field) => field.label.startsWith("Next")), "navigation controls are excluded from fill inventory");
};

tests.flat_confirmed_fact_contract_maps_without_profile_drafts = () => {
  const flatFacts = {
    name: "Confirmed Student",
    "contact.email": "confirmed@example.com",
    "contact.phone": "+15125550123",
  };
  const scan = loadContentScript(loadFixture("greenhouse.json")).scan(flatFacts);
  assert.equal(byLabel(scan, "Email Address").proposed_value, "confirmed@example.com");
  assert.equal(byLabel(scan, "Phone Number").proposed_value, "+15125550123");
};

tests.duplicate_fingerprints_fail_closed = () => {
  const ext = loadContentScript(loadFixture("greenhouse.json"));
  const scan = ext.scan(profile);
  const email = byLabel(scan, "Email Address");
  ext.document.appendControl({ tag: "input", type: "email", id: "email", label: "Email Address" });
  const result = ext.fill(scan.fields.map((field) => ({ ...field, approved: field.key === email.key })));
  const outcome = result.results.find((item) => item.key === email.key);
  assert.equal(outcome.filled, false);
  assert.match(outcome.reason, /ambiguous/);
  assert.equal(ext.document.controls.filter((item) => item.id === "email").every((item) => item.value === ""), true);
};

tests.document_fields_are_separate_and_never_generically_filled = () => {
  const ext = loadContentScript(loadFixture("smartrecruiters.json"));
  const scan = ext.scan(profile);
  const resume = byLabel(scan, "Resume or CV");
  assert.equal(resume.type, "file");
  assert.equal(resume.proposed_value, "");
  assert.match(resume.reason, /approved local document/i);
  const forged = ext.fill([{ ...resume, proposed_value: "C:\\private\\resume.pdf", approved: true }]);
  assert.equal(forged.results[0].filled, false);
  assert.match(forged.results[0].reason, /explicit document attachment/i);
};

tests.unsupported_custom_widgets_are_visible_but_never_mutated = () => {
  const ext = loadContentScript(loadFixture("ashby.json"));
  const scan = ext.scan(profile);
  const custom = byLabel(scan, "Preferred location");
  assert.equal(custom.type, "custom_select");
  assert.equal(custom.unsupported, true);
  const result = ext.fill([{ ...custom, proposed_value: "Austin", approved: true }]);
  assert.equal(result.results[0].filled, false);
  assert.match(result.results[0].reason, /manual/);
  assert.equal(ext.document.controls.find((item) => item.id === "custom-location").value, "");
};

let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (error) {
    failed += 1;
    console.error(`FAIL - ${name}\n${error.stack}`);
  }
}
if (failed > 0) {
  console.error(`${failed} extension test(s) failed`);
  process.exitCode = 1;
} else {
  console.log(`${Object.keys(tests).length} extension tests passed`);
}
