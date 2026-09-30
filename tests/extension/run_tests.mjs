// Zero-dependency test runner for the Apply Mode extension.
// Usage: node tests/extension/run_tests.mjs
// Exercises content.js scan/fill behavior against hand-rolled DOM stubs and
// per-ATS fixtures, plus the UTF-8-safe base64 helper.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import vm from "node:vm";
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
  // The rules now live in apply-engine.js, so the positive assertions follow them there;
  // the negative ones cover both files (every click the agent makes lives in Python).
  const engineSource = readFileSync(path.join(ROOT, "apps", "extension", "apply-engine.js"), "utf8");
  assert.match(engineSource, /"submit"/);
  assert.match(engineSource, /SENSITIVE/);
  for (const file of ["content.js", "apply-engine.js", "adapters.js", "field-engine.js"]) {
    const source = readFileSync(path.join(ROOT, "apps", "extension", file), "utf8");
    assert.doesNotMatch(source, /\.click\(/, `${file} must not click`);
    assert.doesNotMatch(source, /requestSubmit/, `${file} must not requestSubmit`);
    assert.doesNotMatch(source, /\.submit\(/, `${file} must not submit`);
    assert.doesNotMatch(source, /new MouseEvent/, `${file} must not build a MouseEvent`);
    assert.doesNotMatch(source, /new PointerEvent/, `${file} must not build a PointerEvent`);
    if (file === "field-engine.js") {
      // The one place events are dispatched: a field the reviewed plan named, input and change only.
      const dispatches = source.match(/dispatchEvent\([^)]*\)?/g) || [];
      assert.ok(dispatches.length > 0);
      for (const call of dispatches) assert.match(call, /dispatchEvent\(new Event\("(?:input|change)"/, `${file}: ${call}`);
    } else {
      assert.doesNotMatch(source, /dispatchEvent/, `${file} must not dispatch events itself`);
    }
  }
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

tests.picked_resume_variant_is_preselected_without_reordering = () => {
  const { documentChoices, documentLabel } = require(path.join(ROOT, "apps", "extension", "lib", "documents.js"));
  const documents = [
    { artifact_id: "resume-general", document_type: "resume", opportunity_id: null, filename: "general.pdf", preferred: false },
    { artifact_id: "resume-hardware", document_type: "resume", opportunity_id: null, filename: "hardware.pdf", variant_label: "Hardware", preferred: true },
    { artifact_id: "cover-other", document_type: "cover_letter", opportunity_id: "opp-other", filename: "other.md" },
    { artifact_id: "cover-this", document_type: "cover_letter", opportunity_id: "opp-1", filename: "this.md" },
  ];
  const choices = documentChoices(documents, "opp-1");
  assert.deepEqual(choices.map((choice) => choice.item.artifact_id), ["resume-general", "resume-hardware", "cover-this"],
    "the tracker's order stays, and another role's documents are left out");
  assert.deepEqual(choices.map((choice) => choice.selected), [false, true, false], "the picked variant starts selected");
  assert.equal(documentLabel(documents[1]), "hardware.pdf (resume, Hardware variant, picked for this role)");
  const none = documentChoices(documents.map((item) => ({ ...item, preferred: false })), "opp-1");
  assert.deepEqual(none.map((choice) => choice.selected), [true, false, false], "with no pick, the first stays selected as before");
  assert.deepEqual(documentChoices(undefined, "opp-1"), []);
};

// --- The shared engine (apps/extension/apply-engine.js), Phase 5 M1 ---

const APPLY_FIXTURES = path.join(ROOT, "tests", "fixtures", "apply");

function loadApplyFixture(name) {
  return JSON.parse(readFileSync(path.join(APPLY_FIXTURES, name), "utf8"));
}

function pageOf(...controls) {
  return { hostname: "job-boards.greenhouse.io", url: "https://job-boards.greenhouse.io/acme/jobs/1", controls };
}

function fieldById(scan, id) {
  const field = scan.fields.find((item) => item.id === id);
  assert.ok(field, `expected a scanned field with id ${id}`);
  return field;
}

tests.engine_loads_without_chrome_and_exposes_the_agent_surface = () => {
  const ext = loadContentScript(pageOf({ tag: "input", type: "text", id: "first_name", label: "First Name" }), { contentScript: false });
  const engine = ext.engine;
  assert.equal(engine.version, "1");
  assert.deepEqual(Object.keys(engine).sort(), ["attachDocumentFromBytes", "contextDependent", "fill", "needsLabelKey", "netTopics", "neverStorable", "possiblySensitive", "questionKey", "questionText", "scan", "version"]);
  assert.ok(Object.isFrozen(engine));
  assert.equal(engine.scan({ name: "Test Student" }, []).fields[0].proposed_value, "Test");
  // A second injection of the same source keeps the first engine.
  const source = readFileSync(path.join(ROOT, "apps", "extension", "apply-engine.js"), "utf8");
  vm.runInContext(source, ext.context, { filename: "apply-engine.js" });
  assert.strictEqual(ext.context.OpportunityApplyEngine, engine);
  assert.equal(ext.context.chrome, undefined, "the agent's page has no chrome.*");
};

tests.clean_question_excludes_name_id_and_placeholder = () => {
  const page = {
    ...pageOf(
      { tag: "input", type: "text", id: "question_4000000101", name: "question_4000000101", placeholder: "Type here", label: "Why do you want to work here? *" },
      { tag: "select", id: "team", name: "team_pick", wrapped: true, label: "Preferred team (required)", text: "Backend Frontend" },
      { tag: "input", type: "text", id: "portfolio", ariaLabelledby: "lbl1 lbl2" },
      { tag: "input", type: "text", id: "nickname", ariaLabel: "Nickname   required" },
      { tag: "input", type: "text", id: "anon", name: "anon_name", placeholder: "Only a placeholder" },
    ),
    texts: { lbl1: "Portfolio", lbl2: "link" },
  };
  const scan = loadContentScript(page).scan(profile);
  const why = fieldById(scan, "question_4000000101");
  assert.equal(why.question, "Why do you want to work here?");
  assert.match(why.label, /question_4000000101/, "label stays exactly as before, name and id included");
  assert.match(why.label, /Type here/);
  assert.equal(fieldById(scan, "team").question, "Preferred team", "a wrapping label without the control's own text");
  assert.equal(fieldById(scan, "portfolio").question, "Portfolio link");
  assert.equal(fieldById(scan, "nickname").question, "Nickname");
  const anon = fieldById(scan, "anon");
  assert.equal(anon.question, "", "name, id and placeholder are never the question");
  assert.match(anon.label, /anon_name/);
  assert.equal(anon.name, "anon_name");
  assert.equal(anon.id, "anon");
};

tests.wrapping_label_question_drops_the_controls_inside_it = () => {
  // The stub's labels clone and remove inner controls as a browser does, so this is the real path.
  const page = pageOf(
    { tag: "select", id: "team", name: "team_pick", wrapped: true, label: "Preferred team", text: "Backend Frontend Platform" },
    { tag: "select", id: "other", name: "other_pick", label: "Other team", text: "Ignored" },
  );
  const scan = loadContentScript(page).scan(profile);
  assert.equal(fieldById(scan, "team").question, "Preferred team", "the options' text is not part of the question");
  assert.match(fieldById(scan, "team").label, /Backend Frontend Platform/, "the label itself still holds it");
  assert.equal(fieldById(scan, "other").question, "Other team");
};

tests.question_text_strips_only_trailing_required_markers = () => {
  const cases = [
    ["Email*", "Email"],
    ["Email *", "Email"],
    ["Email (required)", "Email"],
    ["Email  required", "Email"],
    ["Phone * (required)", "Phone"],
    ["Why  us?\n  *", "Why us?"],
    ["Required documents for review", "Required documents for review"],
    ["Rate 1-5 (5 is best)", "Rate 1-5 (5 is best)"],
    ["*", ""],
  ];
  for (const [text, expected] of cases) {
    const ext = loadContentScript(pageOf({ tag: "input", type: "text", id: "field", label: text }));
    assert.equal(fieldById(ext.scan(profile), "field").question, expected, JSON.stringify(text));
  }
};

tests.required_markers_are_reported_and_required_is_unchanged = () => {
  const scan = loadContentScript(pageOf(
    { tag: "input", type: "text", id: "m_attr", label: "Attr", required: true },
    { tag: "input", type: "text", id: "m_aria", label: "Aria", ariaRequired: true },
    { tag: "input", type: "text", id: "m_group", label: "Group", group: { ariaRequired: true } },
    { tag: "input", type: "text", id: "m_star", label: "Star *" },
    { tag: "input", type: "text", id: "m_mirror", label: "Mirror", container: { hiddenMirror: true } },
    { tag: "input", type: "file", id: "m_span", label: "Resume", container: { spanRequired: true } },
    { tag: "input", type: "text", id: "m_shared", label: "Shared", container: { hiddenMirror: true, spanRequired: true, others: 1 } },
    { tag: "input", type: "text", id: "m_all", label: "All *", required: true, ariaRequired: true, container: { hiddenMirror: true, spanRequired: true } },
    { tag: "input", type: "text", id: "m_none", label: "Optional" },
  )).scan(profile);
  const expected = {
    m_attr: [["attr"], true],
    m_aria: [["aria"], true],
    m_group: [["aria"], false],
    m_star: [["asterisk"], false],
    m_mirror: [["hidden_required_sibling"], false],
    m_span: [["span_required"], false],
    m_shared: [[], false],
    m_all: [["attr", "aria", "asterisk", "hidden_required_sibling", "span_required"], true],
    m_none: [[], false],
  };
  for (const [id, [markers, required]] of Object.entries(expected)) {
    const field = fieldById(scan, id);
    assert.deepEqual([...field.required_markers], markers, id);
    assert.equal(field.required_any, markers.length > 0, id);
    // `required` is still only the attribute and the control's own aria-required.
    assert.equal(field.required, required, `${id}: required must not change`);
  }
};

tests.combobox_input_is_custom_select_and_never_filled = () => {
  const ext = loadContentScript(pageOf(
    { tag: "input", type: "text", role: "combobox", id: "school", label: "School" },
    { tag: "input", type: "text", role: "combobox", id: "candidate-location", label: "Location (City)" },
    { tag: "input", type: "text", role: "combobox", id: "question_3", label: "Which office location do you prefer?" },
    { tag: "input", type: "file", id: "resume", label: "Resume/CV" },
    { tag: "input", type: "text", id: "plain", label: "Plain" },
  ));
  const scan = ext.scan(profile);
  const school = fieldById(scan, "school");
  assert.equal(school.type, "custom_select");
  assert.equal(school.unsupported, true);
  assert.equal(school.widget, "react_select");
  assert.equal(school.proposed_value, "", "no value is proposed for a widget the extension cannot fill");
  assert.match(school.reason, /manual/);
  assert.equal(fieldById(scan, "candidate-location").widget, "location");
  assert.equal(fieldById(scan, "question_3").widget, "react_select", "a fixed list that mentions location is not the location typeahead");
  assert.equal(fieldById(scan, "resume").widget, "file_group");
  assert.equal(fieldById(scan, "plain").widget, "native");
  const result = ext.fill([{ ...school, proposed_value: "Somewhere University", approved: true }]);
  assert.equal(result.results[0].filled, false);
  assert.match(result.results[0].reason, /manual/);
  assert.equal(ext.document.controls.find((item) => item.id === "school").value, "");
};

tests.visible_css_is_reported_not_used_to_filter = () => {
  const scan = loadContentScript(pageOf(
    { tag: "input", type: "text", id: "v_unknown", label: "Unknown" },
    { tag: "input", type: "text", id: "v_ok", label: "Ok", rect: { width: 200, height: 30 } },
    { tag: "input", type: "text", id: "v_tiny", label: "Tiny", rect: { width: 1, height: 30 } },
    { tag: "input", type: "text", id: "v_none", label: "None", rect: { width: 200, height: 30 }, style: { display: "none" } },
    { tag: "input", type: "text", id: "v_hidden", label: "Hidden", rect: { width: 200, height: 30 }, style: { visibility: "hidden" } },
    { tag: "input", type: "text", id: "v_clear", label: "Clear", rect: { width: 200, height: 30 }, style: { opacity: "0" } },
    { tag: "input", type: "text", id: "v_off", label: "Off", rect: { width: 200, height: 30, right: -5 } },
    { tag: "input", type: "text", id: "v_left", label: "Left", rect: { width: 200, height: 30, left: 20000, right: 20200 } },
    { tag: "input", type: "text", id: "v_aria", label: "Aria", rect: { width: 200, height: 30 }, ariaHiddenAncestor: true },
    { tag: "input", type: "text", id: "v_trap", label: "Trap", rect: { width: 4, height: 30 }, tabIndex: -1 },
    { tag: "input", type: "text", id: "v_skip", label: "Skip", rect: { width: 200, height: 30 }, tabIndex: -1 },
  )).scan(profile);
  const seen = Object.fromEntries(scan.fields.map((field) => [field.id, field.visible_css]));
  assert.deepEqual(seen, { v_unknown: null, v_ok: true, v_tiny: false, v_none: false, v_hidden: false, v_clear: false, v_off: false,
    v_left: false, v_aria: false, v_trap: false, v_skip: true });
  assert.equal(scan.fields.length, 11, "a control that fails the CSS test is still listed");
};

tests.tag_option_marks_controls_only_when_asked = () => {
  const ext = loadContentScript(pageOf(
    { tag: "input", type: "text", id: "first_name", label: "First Name" },
    { tag: "input", type: "text", id: "school", role: "combobox", label: "School" },
  ));
  ext.scan(profile);
  ext.engine.scan(profile, []);
  ext.engine.scan(profile, [], { tag: false });
  assert.ok(ext.document.controls.every((control) => !("data-opportunity-field" in control.attrs)), "untagged by default");
  const tagged = ext.engine.scan(profile, [], { tag: true });
  for (const field of tagged.fields) {
    const control = ext.document.controls.find((item) => item.id === field.id);
    assert.equal(control.attrs["data-opportunity-field"], field.key);
  }
};

tests.saved_clean_question_matches_exactly_and_legacy_label_rows_still_do = () => {
  const page = pageOf({ tag: "textarea", id: "question_4000000101", name: "question_4000000101", label: "Why do you want to work here? *" });
  const first = loadContentScript(page).scan(profile);
  const field = first.fields[0];
  const clean = { id: "clean-1", question: field.question, answer: "Because I like the mission.", company: "Acme Robotics" };
  const exact = loadContentScript(page).scan(profile, [clean], "Acme Robotics").fields[0];
  assert.equal(exact.provenance, "answer_library:clean-1");
  assert.equal(exact.confidence, 0.9);
  assert.equal(exact.reason, "Same question saved for this company; verify before filling");
  assert.equal(exact.proposed_value, "Because I like the mission.");

  // A row saved before the side panel kept the clean question holds the whole label.
  const legacy = { id: "legacy-1", question: field.label, answer: "Legacy answer.", company: "Acme Robotics" };
  const old = loadContentScript(page).scan(profile, [legacy], "Acme Robotics").fields[0];
  assert.equal(old.provenance, "answer_library:legacy-1");
  assert.equal(old.confidence, 0.9);

  // The clean question is compared first, so it wins when both kinds of row exist.
  const both = loadContentScript(page).scan(profile, [legacy, clean], "Acme Robotics").fields[0];
  assert.equal(both.provenance, "answer_library:clean-1");

  // The same question on another posting (new name and id) still matches; the legacy row does not.
  const other = pageOf({ tag: "textarea", id: "question_4000000999", name: "question_4000000999", label: "Why do you want to work here? *" });
  const carried = loadContentScript(other).scan(profile, [legacy, clean], "Acme Robotics").fields[0];
  assert.equal(carried.provenance, "answer_library:clean-1");
  assert.equal(carried.confidence, 0.9);
  const notCarried = loadContentScript(other).scan(profile, [legacy], "Acme Robotics").fields[0];
  assert.notEqual(notCarried.confidence, 0.9, "a label-keyed row is not an exact match on another posting");
};

tests.aria_labelledby_and_long_labels_are_screened_like_the_question = () => {
  const felony = "Have you ever been convicted of a felony?";
  const consent = "I consent to the processing of my personal data";
  const filler = "This acknowledgment is long. ".repeat(20);
  const page = {
    ...pageOf(
      { tag: "input", type: "text", id: "question_900", name: "question_900", ariaLabelledby: "q900" },
      { tag: "input", type: "checkbox", id: "question_901", name: "question_901", ariaLabelledby: "q901" },
      { tag: "textarea", id: "long", name: "long", label: `${filler}Please confirm that you consent to contact.` },
      { tag: "input", type: "checkbox", id: "long_box", name: "long_box", label: `${filler}I consent to background screening.` },
    ),
    texts: { q900: felony, q901: consent },
  };
  const answers = [
    { id: "lib-1", question: felony, answer: "No", company: "Acme Robotics" },
    { id: "lib-2", question: consent, answer: "yes", company: "Acme Robotics" },
    { id: "lib-3", question: `${filler}Please confirm that you consent to contact.`, answer: "ok", company: "Acme Robotics" },
  ];
  const ext = loadContentScript(page);
  const scan = ext.scan(profile, answers, "Acme Robotics");
  const text = fieldById(scan, "question_900");
  assert.equal(text.question, felony, "the question comes from aria-labelledby");
  assert.equal(text.requires_review, true, "a felony question named only by aria-labelledby is sensitive");
  assert.equal(text.provenance, "unmapped");
  assert.equal(text.confidence, 0);
  assert.equal(text.proposed_value, "");
  const box = fieldById(scan, "question_901");
  assert.equal(box.prohibited, true, "a consent checkbox named only by aria-labelledby is prohibited");
  assert.equal(box.provenance, "unmapped");
  assert.equal(box.proposed_value, "");
  const long = fieldById(scan, "long");
  assert.ok(long.label.length <= 500 && !/consent/i.test(long.label), "the label is cut before the word");
  assert.equal(long.prohibited, true, "consent past character 500 is still screened");
  assert.equal(long.proposed_value, "");
  const longBox = fieldById(scan, "long_box");
  assert.equal(longBox.question, "", "a lone checkbox reports no question");
  assert.equal(longBox.prohibited, true, "the uncut label is screened even when it is not the question");
  // Even a reviewed field that lost its prohibited flag cannot be filled when the word is only in its question.
  const forged = ext.fill([{ ...box, question: consent, prohibited: false, proposed_value: "yes", approved: true }]);
  assert.equal(forged.results[0].filled, false);
  assert.match(forged.results[0].reason, /Prohibited/);
  assert.equal(ext.document.controls.find((item) => item.id === "question_901").checked, false);
};

tests.radio_and_checkbox_options_are_never_the_question = () => {
  const groupOf = (legend) => ({ legend });
  const page = pageOf(
    { tag: "input", type: "radio", id: "yes_a", name: "question_111", value: "Yes", label: "Yes", groupQuestion: groupOf("Will you require visa sponsorship?") },
    { tag: "input", type: "radio", id: "yes_b", name: "question_222", label: "Yes", groupQuestion: groupOf("Do you like robots?") },
    { tag: "input", type: "radio", id: "yes_c", name: "question_333", label: "Yes" },
    { tag: "input", type: "checkbox", id: "agree_a", name: "question_555[]", label: "I agree", groupQuestion: { labelledby: "g1" } },
    { tag: "input", type: "checkbox", id: "agree_b", name: "question_999[]", label: "I agree" },
    { tag: "input", type: "radio", id: "woman", name: "gender", label: "Woman", groupQuestion: { ariaLabel: "Gender" } },
  );
  page.texts = { g1: "Please review and acknowledge the policy" };
  // What the side panel would have saved from each option: its answer_key.
  const first = loadContentScript(page).scan(profile, []);
  const saved = (id) => fieldById(first, id).answer_key;
  assert.equal(fieldById(first, "yes_a").question, "Will you require visa sponsorship?");
  assert.equal(fieldById(first, "yes_b").question, "Do you like robots?");
  assert.equal(fieldById(first, "yes_c").question, "", "no group text, so no question");
  assert.equal(fieldById(first, "agree_a").question, "Please review and acknowledge the policy");
  assert.equal(fieldById(first, "agree_b").question, "");
  assert.notEqual(saved("yes_c"), "Yes", "with no group text the saved key is the full label");
  assert.notEqual(saved("agree_b"), "I agree");
  // A row whose question is the bare option text must not exact-match or pre-tick anything.
  const answers = [
    { id: "lib-yes", question: "Yes", answer: "Yes", company: "Acme Robotics", tags: ["reusable"] },
    { id: "lib-agree", question: "I agree", answer: "yes", company: "Acme Robotics", tags: ["reusable"] },
  ];
  const scan = loadContentScript(page).scan(profile, answers, "Acme Robotics");
  for (const id of ["yes_b", "yes_c", "agree_b"]) {
    const field = fieldById(scan, id);
    assert.notEqual(field.confidence, 0.9, `${id}: an option's text is not an exact question match`);
    assert.doesNotMatch(field.reason, /Same question/, id);
  }
  // A Yes/No group that asks about sponsorship is sensitive, so it is never proposed.
  const sponsor = fieldById(scan, "yes_a");
  assert.equal(sponsor.requires_review, true);
  assert.equal(sponsor.provenance, "unmapped");
  assert.equal(fieldById(scan, "woman").requires_review, true, "the group's own label is screened");
};

tests.saved_answers_do_not_cross_companies_unless_reusable = () => {
  const page = pageOf(
    { tag: "input", type: "text", id: "worked", name: "question_556", label: "Have you previously worked here?" },
    { tag: "input", type: "text", id: "explain", name: "question_557", label: "If yes, please explain" },
    { tag: "input", type: "text", id: "learn", name: "question_558", label: "Tell us what you would like to learn" },
  );
  const rows = (extra = {}) => [
    { id: "w", question: "Have you previously worked here?", answer: "Yes", company: "Acme Robotics", ...extra },
    { id: "e", question: "If yes, please explain", answer: "Interned", company: "Acme Robotics", ...extra },
    { id: "l", question: "Tell us what you would like to learn", answer: "Controls", company: "Acme Robotics", ...extra },
  ];
  const at = (company, extra) => loadContentScript(page).scan(profile, rows(extra), company);
  // The same company: exact for a question whose truth depends only on the company. An opener
  // takes its meaning from the question above it, so it is never exact on its bare words.
  for (const id of ["worked", "learn"]) {
    assert.equal(fieldById(at("Acme Robotics"), id).confidence, 0.9, `${id} at the company it was saved for`);
  }
  assert.notEqual(fieldById(at("Acme Robotics"), "explain").confidence, 0.9, "an if-yes opener is not exact on its bare words");
  assert.equal(fieldById(at("acme  robotics"), "worked").confidence, 0.9, "company is compared normalized");
  // Another company, or none named: shown, but not exact and never pre-tickable.
  for (const company of ["Orbit Systems", undefined, ""]) {
    for (const id of ["worked", "learn"]) {
      const field = fieldById(at(company), id);
      assert.equal(field.confidence, 0.7, `${id} for ${JSON.stringify(company)}`);
      assert.match(field.reason, /another company/);
    }
    assert.equal(fieldById(at(company), "explain").confidence, 0.7, `explain for ${JSON.stringify(company)}`);
  }
  // Reusable carries an ordinary question, never a context-dependent one.
  const reusable = at("Orbit Systems", { tags: ["reusable"] });
  assert.equal(fieldById(reusable, "learn").confidence, 0.9);
  assert.equal(fieldById(reusable, "worked").confidence, 0.7, "previously worked here never crosses companies");
  assert.equal(fieldById(reusable, "explain").confidence, 0.7, "an if-yes opener never crosses companies");
};

tests.openers_and_short_keys_never_match_another_parent_question = () => {
  const page = pageOf(
    { tag: "textarea", id: "question_601", name: "question_601", label: "If yes, please explain" },
    { tag: "textarea", id: "question_602", name: "question_602", label: "If yes, please explain" },
    { tag: "input", type: "text", id: "question_700", name: "question_700", label: "Other" },
    { tag: "input", type: "text", id: "question_701", name: "question_701", label: "Other" },
  );
  const first = loadContentScript(page).scan(profile, [], "Acme Robotics");
  const explain = fieldById(first, "question_601");
  assert.equal(explain.question, "If yes, please explain", "question keeps the form's words");
  assert.notEqual(explain.answer_key, explain.question, "an opener is saved on its per-posting label");
  assert.equal(explain.answer_key, explain.label);
  assert.equal(fieldById(first, "question_700").answer_key, fieldById(first, "question_700").label, "a one-word key too");
  assert.equal(fieldById(first, "question_602").answer_key, fieldById(first, "question_602").label);
  // What the side panel saves from the first fields is exact for them and for nothing else.
  const rows = [
    { id: "i", question: explain.answer_key, answer: "I interned here in 2025.", company: "Acme Robotics" },
    { id: "o", question: fieldById(first, "question_700").answer_key, answer: "Robotics club", company: "Acme Robotics" },
    // Hand-made rows on the bare words, at the same company and reusable.
    { id: "b", question: "If yes, please explain", answer: "bare", company: "Acme Robotics", tags: ["reusable"] },
    { id: "c", question: "Other", answer: "bare other", company: "Acme Robotics", tags: ["reusable"] },
  ];
  for (const company of ["Acme Robotics", "Orbit Systems"]) {
    const scan = loadContentScript(page).scan(profile, rows, company);
    const one = fieldById(scan, "question_601");
    assert.equal(one.confidence, company === "Acme Robotics" ? 0.9 : 0.7, `the field it was saved from, at ${company}`);
    assert.equal(one.provenance, "answer_library:i");
    for (const id of ["question_602", "question_701"]) {
      const other = fieldById(scan, id);
      assert.notEqual(other.confidence, 0.9, `${id} at ${company}: another parent question is not an exact match`);
      assert.doesNotMatch(other.reason, /Same question/, id);
    }
    assert.equal(fieldById(scan, "question_700").provenance, "answer_library:o");
  }
};

// What the side panel pre-ticks: 0.9 and up with a value, on a field that is not sensitive.
function preTicked(scan, id) {
  const field = fieldById(scan, id);
  return !field.prohibited && !field.requires_review && field.confidence >= 0.9 && field.proposed_value !== "";
}

tests.follow_up_wordings_are_never_exact_across_parent_questions = () => {
  const wordings = [
    "If you answered yes, please explain",
    "If you selected Other, please specify",
    "If applicable, please explain",
    "Please provide more details",
    "Please provide additional information",
    "Could you share further context",
    "Please elaborate on your answer here",
    "Briefly describe your experience",
    "Tell us about it, please specify",
  ];
  for (const wording of wordings) {
    const page = pageOf(
      { tag: "textarea", id: "question_611", name: "question_611", label: wording },
      { tag: "textarea", id: "question_612", name: "question_612", label: wording },
    );
    const first = loadContentScript(page).scan(profile, [], "Acme Robotics");
    const a = fieldById(first, "question_611");
    assert.equal(a.question, wording, "question keeps the form's words");
    assert.equal(a.answer_key, a.label, `${wording}: saved on the per-posting label`);
    assert.notEqual(a.answer_key, fieldById(first, "question_612").answer_key, `${wording}: siblings save on different keys`);
    // The side panel saves field.answer_key from the first field.
    const rows = [{ id: "sv", question: a.answer_key, answer: "Parent A answer", company: "Acme Robotics" }];
    for (const company of ["Acme Robotics", "Orbit Systems"]) {
      const scan = loadContentScript(page).scan(profile, rows, company);
      assert.equal(fieldById(scan, "question_611").confidence, company === "Acme Robotics" ? 0.9 : 0.7, `${wording}: the field it was saved from, at ${company}`);
      const other = fieldById(scan, "question_612");
      assert.notEqual(other.confidence, 0.9, `${wording} at ${company}: the second field is not exact`);
      assert.equal(preTicked(scan, "question_612"), false, `${wording} at ${company}: the second field is not pre-ticked`);
      assert.doesNotMatch(other.reason, /Same question/, wording);
    }
    // A later posting: the same wording under a different parent question, different name and id.
    const later = pageOf({ tag: "textarea", id: "question_9001", name: "question_9001", label: wording });
    const scan = loadContentScript(later).scan(profile, rows, "Acme Robotics");
    assert.notEqual(fieldById(scan, "question_9001").confidence, 0.9, `${wording}: a later posting at the same company`);
  }
  // Ordinary clean questions keep the exact tier.
  const plain = pageOf({ tag: "textarea", id: "q1", name: "question_1", label: "Tell us what you would like to learn" });
  const rows = [{ id: "l", question: "Tell us what you would like to learn", answer: "Controls", company: "Acme Robotics" }];
  assert.equal(fieldById(loadContentScript(plain).scan(profile, rows, "Acme Robotics"), "q1").confidence, 0.9);
};

tests.exact_label_and_legacy_rows_need_the_company_too = () => {
  const only = (scan) => scan.fields[0];
  const boards = (...controls) => ({ hostname: "boards.greenhouse.io", url: "https://boards.greenhouse.io/acme/jobs/1", controls });
  // Repro (a): a Greenhouse textarea whose question is a follow-up or company-specific wording is
  // saved on its per-posting label (which carries the name and id).
  for (const wording of ["Describe your interest in Acme", "Please provide more details"]) {
    const id = "job_application_answers_attributes_3_text_value";
    const page = boards({ tag: "textarea", id, name: "job_application[answers_attributes][3][text_value]", label: wording });
    const saved = fieldById(loadContentScript(page).scan(profile, [], "Acme Robotics"), id);
    assert.equal(saved.answer_key, saved.label, `${wording}: saved on the per-posting label`);
    const rows = [{ id: "sv", question: saved.answer_key, answer: "Acme answer", company: "Acme Robotics" }];
    const home = fieldById(loadContentScript(page).scan(profile, rows, "Acme Robotics"), id);
    assert.equal(home.confidence, 0.9, `${wording}: still exact at the company it was saved for`);
    for (const company of ["Orbit Systems", "", undefined]) {
      const scan = loadContentScript(page).scan(profile, rows, company);
      const away = fieldById(scan, id);
      assert.notEqual(away.confidence, 0.9, `${wording} at ${JSON.stringify(company)}: a label-keyed row is not exact at another company`);
      assert.equal(preTicked(scan, id), false, `${wording} at ${JSON.stringify(company)}: not pre-ticked`);
    }
    const foreign = fieldById(loadContentScript(page).scan(profile, rows, "Orbit Systems"), id);
    assert.equal(foreign.confidence, 0.7);
    assert.match(foreign.reason, /another company/);
    // Reusable never carries a follow-up.
    const reusable = [{ ...rows[0], tags: ["reusable"] }];
    assert.notEqual(fieldById(loadContentScript(page).scan(profile, reusable, "Orbit Systems"), id).confidence, 0.9, `${wording}: reusable does not carry a follow-up`);
  }
  // Repro (b): a field with no name or id, whose label is its question, and a row on those words.
  const bare = pageOf({ tag: "textarea", wrapped: true, label: "Tell us about a project you are proud of" });
  assert.equal(only(loadContentScript(bare).scan(profile, [], "Acme Robotics")).answer_key, "Tell us about a project you are proud of", "the label is the question and there is no name or id");
  const row = { id: "lib", question: "Tell us about a project you are proud of", answer: "The mission.", company: "Acme Robotics" };
  assert.equal(only(loadContentScript(bare).scan(profile, [row], "Acme Robotics")).confidence, 0.9);
  const orbit = only(loadContentScript(bare).scan(profile, [row], "Orbit Systems"));
  assert.equal(orbit.confidence, 0.7, "the same words saved at another company are not exact");
  assert.match(orbit.reason, /another company/);
  assert.equal(only(loadContentScript(bare).scan(profile, [{ ...row, company: "" }], "Orbit Systems")).confidence, 0.7, "no company and not reusable");
  assert.equal(only(loadContentScript(bare).scan(profile, [{ ...row, company: "", tags: ["reusable"] }], "Orbit Systems")).confidence, 0.9, "no company but reusable");
  assert.equal(only(loadContentScript(bare).scan(profile, [{ ...row, tags: ["reusable"] }], "Orbit Systems")).confidence, 0.9, "reusable at another company");
  // A legacy row on the whole label: exact at its own company (spec 12.5), not at another.
  const page = pageOf({ tag: "textarea", id: "question_4000000101", name: "question_4000000101", label: "Why do you want to work here? *" });
  const label = loadContentScript(page).scan(profile, [], "Acme Robotics").fields[0].label;
  const legacy = { id: "legacy", question: label, answer: "Legacy answer.", company: "Acme Robotics" };
  assert.equal(only(loadContentScript(page).scan(profile, [legacy], "Acme Robotics")).confidence, 0.9);
  assert.equal(only(loadContentScript(page).scan(profile, [legacy], "Orbit Systems")).confidence, 0.7);
  assert.equal(only(loadContentScript(page).scan(profile, [{ ...legacy, company: "" }], "Acme Robotics")).confidence, 0.7, "a legacy row with no company is not exact");
  assert.equal(only(loadContentScript(page).scan(profile, [{ ...legacy, company: "" }])).confidence, 0.7);
};

const FOLLOW_UPS = [
  "Please provide more detail", "Please provide additional detail", "Please provide an explanation", "Please provide a brief explanation",
  "Please provide the details", "Please share the details", "Please include details", "Provide more info", "Why or why not?",
  "Please tell us more", "Tell us more about your answer above", "Tell us why", "Please clarify your answer", "Please expand on your answer",
  "Please list them", "Which company was it?", "What was the reason?", "Details (if any)", "Additional details (if applicable)",
  "b. If yes, what was your role?", "1a) If so, when?", "'If other' - list the dates",
  "(ii) Please specify", "- Explain your answer", "When?", "Which one?", "Tell us more about it", "Please give more context",
  "Q4b. Which company was it?", "Q4b. What was the reason?", "Question 3: Which company was it?", "Question 3b. When?",
  "Part B: What was the reason?", "Step 2. When?", "1.2.3 Which company was it?", "Section 2 - When?", "No. 3 What was the reason?",
  "Follow-up: Which company was it?", "Follow up question: When?",
  "3. Follow-up: Which company was it?", "Q4 follow-up: which company was it?", "Question 3 - Follow-up: Which company was it?",
  "1a. Follow-up: When?", "2) Follow-up: What was the reason?", "4. Follow up question: When?", "Follow up (optional): which company?",
  "Follow-up questions: which company was it?", "Question no. 3: When?", "Question number 3: When?",
  "Question 3 (optional): which company was it?", "Follow-on question: which company was it?", "Sub-question: which company was it?",
];

tests.every_reviewed_follow_up_wording_is_saved_on_its_own_label = () => {
  for (const wording of FOLLOW_UPS) {
    const at = (name) => pageOf({ tag: "textarea", id: name, name, label: wording });
    const first = loadContentScript(at("question_71")).scan(profile, [], "Acme Robotics").fields[0];
    assert.notEqual(first.answer_key, first.question, `${wording}: is a follow-up, so it is keyed on its per-posting label`);
    // The side panel saves answer_key from the field under one parent question.
    const rows = [{ id: "sv", question: first.answer_key, answer: "Parent A answer", company: "Acme Robotics" }];
    // The same words under a different parent (another field, another name and id) at the same company.
    const other = loadContentScript(at("question_72")).scan(profile, rows, "Acme Robotics");
    assert.notEqual(other.fields[0].confidence, 0.9, `${wording}: not exact on a different field at the same company`);
    assert.equal(preTicked(other, "question_72"), false, `${wording}: not pre-ticked on a different field`);
    // A row saved on the bare wording (an older panel, or by hand) is no better.
    const bare = [{ id: "bare", question: wording, answer: "bare", company: "Acme Robotics" }];
    assert.notEqual(loadContentScript(at("question_72")).scan(profile, bare, "Acme Robotics").fields[0].confidence, 0.9, `${wording}: a bare row is not exact`);
  }
};

tests.standalone_questions_stay_exact_at_their_company = () => {
  for (const question of ["Why do you want to work here?", "Why are you interested in this role?", "Tell us about yourself", "Describe a time you worked on a team",
    "Number of years of experience with Python", "Part-time availability: when can you start?", "Question 1: Why do you want to work at Acme?",
    "Optional: Tell us about a project you are proud of", "Section 2: What motivates you to apply for this internship?"]) {
    const at = (name) => pageOf({ tag: "textarea", id: name, name, label: question });
    const first = loadContentScript(at("question_81")).scan(profile, [], "Acme Robotics").fields[0];
    assert.equal(first.answer_key, first.question, `${question}: a clean question`);
    const rows = [{ id: "sv", question: first.answer_key, answer: "Answer", company: "Acme Robotics" }];
    const later = loadContentScript(at("question_82")).scan(profile, rows, "Acme Robotics").fields[0];
    assert.equal(later.confidence, 0.9, `${question}: exact on a later posting at the same company`);
    assert.equal(later.reason, "Same question saved for this company; verify before filling");
    const away = loadContentScript(at("question_82")).scan(profile, rows, "Orbit Systems").fields[0];
    assert.equal(away.confidence, 0.7, `${question}: not exact at another company`);
    const reusable = loadContentScript(at("question_82")).scan(profile, [{ ...rows[0], tags: ["reusable"] }], "Orbit Systems").fields[0];
    // Wording about the employer itself ("work here", "this role") never travels, even when reusable.
    if (/work here|this role/.test(question)) {
      assert.equal(reusable.confidence, 0.7, `${question}: about the employer, so it does not travel`);
    } else {
      assert.equal(reusable.confidence, 0.9);
      assert.equal(reusable.reason, "Same question, saved as reusable; verify before filling");
    }
  }
};

tests.a_question_shared_by_two_fields_never_matches_on_the_clean_question = () => {
  const shared = "Describe a project you are proud of";
  const page = pageOf(
    { tag: "textarea", id: "question_301", name: "question_301", label: shared },
    { tag: "textarea", id: "question_302", name: "question_302", label: shared },
    { tag: "textarea", id: "question_303", name: "question_303", label: "Tell us what you would like to learn" },
  );
  const first = loadContentScript(page).scan(profile, [], "Acme Robotics");
  const one = fieldById(first, "question_301");
  assert.equal(one.question, shared);
  assert.equal(one.answer_key, one.label, "a duplicated question is saved on the per-question label");
  assert.notEqual(one.answer_key, fieldById(first, "question_302").answer_key);
  assert.equal(fieldById(first, "question_303").answer_key, "Tell us what you would like to learn", "a unique question keeps its clean key");
  const rows = [
    { id: "sv", question: one.answer_key, answer: "Robot arm", company: "Acme Robotics" },
    // A row on the clean words, at the same company and reusable, must not answer either copy.
    { id: "clean", question: shared, answer: "clean row", company: "Acme Robotics", tags: ["reusable"] },
  ];
  for (const company of ["Acme Robotics", "Orbit Systems"]) {
    const scan = loadContentScript(page).scan(profile, rows, company);
    assert.equal(fieldById(scan, "question_301").provenance, "answer_library:sv");
    const other = fieldById(scan, "question_302");
    assert.notEqual(other.confidence, 0.9, `${company}: the second copy is not exact`);
    assert.equal(preTicked(scan, "question_302"), false);
    assert.notEqual(other.provenance, "answer_library:clean", "the clean row never lands on a duplicated question");
  }
  // Alone on its form, the same question is a clean question again.
  const alone = loadContentScript(pageOf({ tag: "textarea", id: "question_301", name: "question_301", label: shared }))
    .scan(profile, [{ id: "clean", question: shared, answer: "clean row", company: "Acme Robotics" }], "Acme Robotics");
  assert.equal(fieldById(alone, "question_301").confidence, 0.9);
  assert.equal(fieldById(alone, "question_301").answer_key, shared);
};

tests.an_option_with_no_label_source_has_no_stable_key_and_is_not_saved = () => {
  const page = pageOf(
    { tag: "input", type: "radio", groupQuestion: { legend: "Do you enjoy working in small teams?" } },
    { tag: "input", type: "checkbox" },
  );
  const scan = loadContentScript(page).scan(profile, [
    { id: "leg", question: "Do you enjoy working in small teams?", answer: "yes", company: "Acme Robotics", tags: ["reusable"] },
    { id: "un", question: "Unlabelled radio field", answer: "yes", company: "Acme Robotics" },
    { id: "blank", question: "", answer: "yes", company: "Acme Robotics" },
  ], "Acme Robotics");
  for (const field of scan.fields) {
    assert.equal(field.answer_key, "", `${field.type}: nothing stable to save or match on`);
    assert.equal(field.confidence, 0, `${field.type}: no row answers it`);
    assert.equal(field.proposed_value, "");
  }
  // The side panel must not invent a key (the legend or "Unlabelled ...") that matching cannot find.
  const sidepanel = readFileSync(path.join(ROOT, "apps", "extension", "sidepanel.js"), "utf8");
  assert.match(sidepanel, /if \(!field\.answer_key\)/, "the side panel refuses a row with no key");
  assert.match(sidepanel, /no stable question/i, "and says why");
  assert.doesNotMatch(sidepanel, /field\.answer_key \|\| field\.question/, "and never falls back to another text");
};

tests.checkbox_and_radio_siblings_never_share_a_saved_answer = () => {
  const legend = "Which programming languages have you used professionally?";
  const teams = "Do you enjoy working in small teams?";
  const page = pageOf(
    { tag: "input", type: "checkbox", id: "lang_py", name: "question_800[]", value: "Python", label: "Python", groupQuestion: { legend } },
    { tag: "input", type: "checkbox", id: "lang_java", name: "question_800[]", value: "Java", label: "Java", groupQuestion: { legend } },
    { tag: "input", type: "checkbox", id: "lang_c", name: "question_800[]", value: "C++", label: "C++", groupQuestion: { legend } },
    { tag: "input", type: "radio", id: "rel_yes", name: "question_801", value: "Yes", label: "Yes", groupQuestion: { legend: teams } },
    { tag: "input", type: "radio", id: "rel_no", name: "question_801", value: "No", label: "No", groupQuestion: { legend: teams } },
  );
  const first = loadContentScript(page).scan(profile, [], "Acme Robotics");
  const py = fieldById(first, "lang_py");
  assert.equal(py.question, legend, "question stays the group text");
  assert.equal(py.answer_key, py.label, "an option is saved on its own label, not the group text");
  assert.notEqual(fieldById(first, "lang_java").answer_key, py.answer_key);
  // What the side panel saves from Python at Acme, then rescans at Acme and at another company.
  for (const answer of ["yes", "no", "Python"]) {
    for (const tags of [[], ["reusable"]]) {
      const rows = [{ id: "py", question: py.answer_key, answer, company: "Acme Robotics", tags }];
      for (const company of ["Acme Robotics", "Orbit Systems"]) {
        const scan = loadContentScript(page).scan(profile, rows, company);
        // Exact at the company it was saved for. An option row never travels, reusable or not: it
        // does not carry its group question, so nothing could show it is the same question.
        const travels = company === "Acme Robotics";
        assert.equal(fieldById(scan, "lang_py").confidence, travels ? 0.9 : 0.7, `the option it was saved from (${company}, ${tags})`);
        for (const id of ["lang_java", "lang_c", "rel_yes", "rel_no"]) {
          const sibling = fieldById(scan, id);
          assert.notEqual(sibling.confidence, 0.9, `${id} (${answer}, ${company}) is not an exact match`);
          assert.doesNotMatch(sibling.reason, /Same question/, id);
        }
      }
    }
  }
  // Even a group-keyed row from before this change is never exact for an option.
  const legacy = [{ id: "leg", question: legend, answer: "yes", company: "Acme Robotics", tags: ["reusable"] },
    { id: "leg2", question: teams, answer: "No", company: "Acme Robotics" }];
  const scan = loadContentScript(page).scan(profile, legacy, "Acme Robotics");
  for (const id of ["lang_py", "lang_java", "lang_c", "rel_yes", "rel_no"]) {
    assert.notEqual(fieldById(scan, id).confidence, 0.9, `${id}: a group-keyed row is never exact for an option`);
  }
  // Filling everything the side panel would pre-tick (0.9 and up) touches no sibling.
  const rows = [{ id: "py", question: py.answer_key, answer: "no", company: "Acme Robotics" }];
  const ext = loadContentScript(page);
  const pre = ext.scan(profile, rows, "Acme Robotics").fields.filter((field) => field.confidence >= 0.9 && field.proposed_value !== "");
  assert.deepEqual([...pre.map((field) => field.id)], ["lang_py"]);
  const java = ext.document.controls.find((item) => item.id === "lang_java");
  java.checked = true;
  ext.fill(pre.map((field) => ({ ...field, approved: true })));
  assert.equal(java.checked, true, "a box the student ticked by hand stays ticked");
};

tests.extended_sensitive_flags_the_shared_vectors = () => {
  const { vectors } = loadApplyFixture("sensitive_vectors.json");
  assert.ok(vectors.length >= 30);
  for (const vector of vectors) {
    const scan = loadContentScript(pageOf({ tag: "input", type: "text", id: "q", label: vector.question })).scan(profile, [
      { id: "lib", question: vector.question, answer: "should never be proposed" },
    ]);
    const field = scan.fields[0];
    assert.equal(field.requires_review, vector.extension_flags, `${vector.question}: extension_flags`);
    if (vector.extension_flags) {
      assert.equal(field.provenance, "unmapped", `${vector.question}: never filled from the library`);
      assert.equal(field.proposed_value, "");
    }
    // The agent must never be looser than the extension.
    if (vector.extension_flags) assert.notEqual(vector.expected, null, `${vector.question}: flagged by the extension, so never null`);
  }
  // The terms step 6 adds, each on its own.
  for (const term of ["immigration", "clearance", "felony", "criminal", "convicted", "non-compete", "at least 18 years of age", "F-1", "H-1B", "STEM OPT", "visa sponsorship",
    "green card", "permanent resident", "misdemeanor", "arrested", "background check", "ITAR", "U.S. person", "export control",
    "export administration regulations", "religious", "transgender", "OPT in 2027", "OPT in the US"]) {
    const scan = loadContentScript(pageOf({ tag: "input", type: "text", id: "t", label: `Question about ${term}` })).scan(profile);
    assert.equal(scan.fields[0].requires_review, true, term);
  }
  // And what it must not catch.
  for (const text of ["How did you hear about us?", "Would you like to opt in to updates?", "Do you accept the terms and opt out later?", "Why Visa?", "Expected graduation year"]) {
    const scan = loadContentScript(pageOf({ tag: "input", type: "text", id: "t", label: text })).scan(profile);
    assert.equal(scan.fields[0].requires_review, false, text);
  }
};

tests.question_keys_match_the_shared_parity_vectors = () => {
  const { vectors } = loadApplyFixture("question_keys.json");
  const ext = loadContentScript(pageOf({ tag: "input", type: "text", id: "q", label: "Q" }), { contentScript: false });
  assert.ok(vectors.length >= 20);
  for (const { text, key } of vectors) {
    assert.equal(ext.engine.questionKey(text), key, JSON.stringify(text));
  }
};

tests.follow_up_and_context_rules_match_the_shared_vectors = () => {
  // apply_policy.needs_label_key and context_dependent repeat these two rules for the agent's plan.
  const { vectors } = loadApplyFixture("context_keys.json");
  const ext = loadContentScript(pageOf({ tag: "input", type: "text", id: "q", label: "Q" }), { contentScript: false });
  assert.ok(vectors.length >= 50);
  for (const { text, key, needs_label_key: needsLabel, context_dependent: dependent } of vectors) {
    assert.equal(ext.engine.questionKey(text), key, JSON.stringify(text));
    assert.equal(ext.engine.needsLabelKey(key), needsLabel, `${text}: needsLabelKey`);
    assert.equal(ext.engine.contextDependent(key), dependent, `${text}: contextDependent`);
  }
};

tests.the_broad_net_matches_the_shared_vectors = () => {
  // apply_policy.net_topics, possibly_sensitive and never_storable repeat these three rules for the agent's plan.
  const { vectors } = loadApplyFixture("broad_net.json");
  const ext = loadContentScript(pageOf({ tag: "input", type: "text", id: "q", label: "Q" }), { contentScript: false });
  assert.ok(vectors.length >= 120);
  for (const { text, topics, possibly_sensitive: possibly, never_storable: never } of vectors) {
    assert.deepEqual(Array.from(ext.engine.netTopics(text)), topics, `${text}: netTopics`);
    assert.equal(ext.engine.possiblySensitive(text), possibly, `${text}: possiblySensitive`);
    assert.equal(ext.engine.neverStorable(text), never, `${text}: neverStorable`);
  }
  assert.equal(ext.engine.possiblySensitive(undefined), false);
  assert.equal(ext.engine.possiblySensitive(null), false);
};

tests.a_possibly_sensitive_question_never_travels_when_reusable = () => {
  // Wordings the precise SENSITIVE rule does not flag but the broad net does: a reusable row never carries them to another company.
  for (const question of ["Are you permitted to work in the United States?", "Do you have unrestricted work rights in the United States?",
    "US work eligibility", "Will you be able to provide proof of employment eligibility?", "What sentence were you given?",
    "I agree to the arbitration rules", "Do you give permission for us to keep your resume on file?", "Where is your court date?",
    "What is the expiration date of your visa’s status?", "Expected pay"]) {
    const page = pageOf({ tag: "textarea", id: "question_41", name: "question_41", label: question });
    const rows = [{ id: "r", question, answer: "Answer", company: "Acme Robotics", tags: ["reusable"] }];
    const away = loadContentScript(page).scan(profile, rows, "Orbit Systems").fields[0];
    assert.notEqual(away.confidence, 0.9, `${question}: not exact at another company`);
  }
  // A question the net does not touch still travels when reusable.
  for (const question of ["Tell us about yourself", "What programming languages do you know?", "Describe a time you worked on a team"]) {
    const page = pageOf({ tag: "textarea", id: "question_42", name: "question_42", label: question });
    const rows = [{ id: "r", question, answer: "Answer", company: "Acme Robotics", tags: ["reusable"] }];
    const field = loadContentScript(page).scan(profile, rows, "Orbit Systems").fields[0];
    assert.equal(field.confidence, 0.9, `${question}: ${field.reason}`);
  }
};

tests.injection_lists_and_the_answer_save_follow_the_split = () => {
  const files = '["adapters.js", "field-engine.js", "apply-engine.js", "content.js"]';
  const sidepanel = readFileSync(path.join(ROOT, "apps", "extension", "sidepanel.js"), "utf8");
  assert.ok(sidepanel.includes(files), "the side panel injects all four files, in order");
  assert.match(sidepanel, /question: field\.answer_key,/, "the side panel saves the engine's answer key");
  assert.doesNotMatch(sidepanel, /question: field\.label,/);
  const browserTest = readFileSync(path.join(ROOT, "tests", "extension", "browser", "run_browser_tests.mjs"), "utf8");
  assert.ok(browserTest.includes(files), "the MV3 browser test injects the same four files");
  const ci = readFileSync(path.join(ROOT, ".github", "workflows", "ci.yml"), "utf8");
  assert.match(ci, /node --check apps\/extension\/apply-engine\.js/);
};


// --- Round 2 review: what the student sees, and where an exact match can still leak ---

tests.the_match_reason_is_shown_beside_every_non_file_field = () => {
  const notes = require(path.join(ROOT, "apps", "extension", "lib", "field-notes.js"));
  const page = pageOf({ tag: "textarea", id: "question_1", name: "question_1", label: "Why do you want to work here?" });
  const rows = [{ id: "a", question: "Why do you want to work here?", answer: "Robots", company: "Acme Robotics" }];
  const same = loadContentScript(page).scan(profile, rows, "Acme Robotics").fields[0];
  assert.match(notes.reasonLine(same), /verify before filling/i, "a same-company match says to verify");
  const away = loadContentScript(page).scan(profile, rows, "Orbit Systems").fields[0];
  assert.match(notes.reasonLine(away), /another company/i, "a match saved for another company says so");
  const mapped = loadContentScript(pageOf({ tag: "input", type: "text", id: "e", name: "email", label: "Email Address" })).scan(profile).fields[0];
  assert.equal(notes.reasonLine(mapped), mapped.reason);
  assert.equal(notes.reasonLine({ reason: "" }), "");
  assert.equal(notes.reasonLine(null), "");
  const sidepanel = readFileSync(path.join(ROOT, "apps", "extension", "sidepanel.js"), "utf8");
  assert.match(sidepanel, /ApplyModeFieldNotes\.reasonLine\(field\)/, "the side panel renders the reason for each row");
  assert.doesNotMatch(sidepanel, /exact-question/, "and no longer calls a saved match exact");
  const html = readFileSync(path.join(ROOT, "apps", "extension", "sidepanel.html"), "utf8");
  assert.ok(html.indexOf("lib/field-notes.js") > -1 && html.indexOf("lib/field-notes.js") < html.indexOf("sidepanel.js"), "the page loads the helper first");
};

tests.a_follow_up_or_option_with_no_name_or_id_cannot_be_saved = () => {
  for (const wording of ["Please provide more details", "If yes, please explain", "Which company was it?"]) {
    const scan = loadContentScript(pageOf({ tag: "textarea", wrapped: true, label: wording })).scan(profile, [], "Acme Robotics");
    assert.equal(scan.fields[0].answer_key, "", `${wording}: nothing per-posting to key on`);
  }
  const radio = loadContentScript(pageOf({ tag: "input", type: "radio", wrapped: true, label: "Yes", groupQuestion: { legend: "Are you willing to relocate?" } })).scan(profile, [], "Acme Robotics");
  assert.equal(radio.fields[0].answer_key, "", "an option with no name or id has no per-posting key");
  const clean = loadContentScript(pageOf({ tag: "textarea", wrapped: true, label: "Why do you want to work here?" })).scan(profile, [], "Acme Robotics");
  assert.equal(clean.fields[0].answer_key, "Why do you want to work here?", "a clean question is unaffected");
  const named = loadContentScript(pageOf({ tag: "textarea", id: "question_5", name: "question_5", label: "Please provide more details" })).scan(profile, [], "Acme Robotics");
  assert.notEqual(named.fields[0].answer_key, "", "with a name and id it keeps its per-posting key");
};

tests.a_reusable_option_row_never_travels_to_another_company = () => {
  const legend = "Are you willing to relocate?";
  const at = (groupQuestion) => pageOf({ tag: "input", type: "radio", id: "q_9_yes", name: "question_9", value: "Yes", label: "Yes", ...(groupQuestion ? { groupQuestion } : {}) });
  const first = loadContentScript(at({ legend })).scan(profile, [], "Acme Robotics").fields[0];
  const row = { id: "opt", question: first.answer_key, answer: "yes", company: "Acme Robotics", tags: ["reusable"] };
  for (const [name, group] of [["a different legend", { legend: "Do you enjoy working in small teams?" }], ["no legend", null], ["the same legend", { legend }]]) {
    const scan = loadContentScript(at(group)).scan(profile, [row], "Orbit Systems");
    assert.notEqual(scan.fields[0].confidence, 0.9, `a reusable option at Orbit under ${name}`);
    assert.equal(preTicked(scan, "q_9_yes"), false, name);
  }
  const home = loadContentScript(at({ legend })).scan(profile, [row], "Acme Robotics");
  assert.equal(home.fields[0].confidence, 0.9, "still exact at its own company");
  const box = pageOf({ tag: "input", type: "checkbox", id: "lang_py", name: "question_800[]", value: "Python", label: "Python", groupQuestion: { legend: "Languages?" } });
  const boxKey = loadContentScript(box).scan(profile, [], "Acme Robotics").fields[0].answer_key;
  const away = loadContentScript(box).scan(profile, [{ id: "c", question: boxKey, answer: "true", company: "Acme Robotics", tags: ["reusable"] }], "Orbit Systems");
  assert.notEqual(away.fields[0].confidence, 0.9, "a reusable checkbox row does not travel either");
};

tests.employer_relative_questions_never_travel_even_when_reusable = () => {
  for (const question of ["Have you ever worked for this organization?", "Do you have relatives employed by us?", "Have you interviewed with us before?",
    "Are any of your family members employed by this company?", "Were you previously employed by this firm?",
    "Why do you want to work here?", "Why are you interested in our company?", "Why are you interested in this role?", "What interests you about this position?",
    "Why do you want to join us?"]) {
    const page = pageOf({ tag: "textarea", id: "question_31", name: "question_31", label: question });
    const rows = [{ id: "r", question, answer: "No", company: "Acme Robotics", tags: ["reusable"] }];
    const away = loadContentScript(page).scan(profile, rows, "Orbit Systems").fields[0];
    assert.notEqual(away.confidence, 0.9, `${question}: not exact at another company`);
    assert.equal(loadContentScript(page).scan(profile, rows, "Acme Robotics").fields[0].confidence, 0.9, `${question}: exact at its own company`);
  }
  // Questions that are not about the employer still travel when reusable.
  for (const question of ["Tell us about yourself", "Describe a time you worked on a team", "Are you comfortable working in a team environment?"]) {
    const page = pageOf({ tag: "textarea", id: "question_32", name: "question_32", label: question });
    const rows = [{ id: "r", question, answer: "Answer", company: "Acme Robotics", tags: ["reusable"] }];
    assert.equal(loadContentScript(page).scan(profile, rows, "Orbit Systems").fields[0].confidence, 0.9, question);
  }
};

tests.relative_and_family_wordings_never_travel_even_when_reusable = () => {
  // Wordings that only the relatives / family-members alternatives catch.
  for (const question of ["Do you have any relatives at Acme?", "Do you have family members who work here?", "Are you related to anyone who works here?",
    "Is your spouse employed here?", "Are you a current or former employee?", "Have you ever been employed here?"]) {
    const page = pageOf({ tag: "textarea", id: "question_33", name: "question_33", label: question });
    const rows = [{ id: "r", question, answer: "No", company: "Acme Robotics", tags: ["reusable"] }];
    assert.notEqual(loadContentScript(page).scan(profile, rows, "Orbit Systems").fields[0].confidence, 0.9, `${question}: not exact at another company`);
  }
};

tests.the_engine_source_has_no_stray_control_characters = () => {
  const source = readFileSync(new URL("../../apps/extension/apply-engine.js", import.meta.url), "utf8");
  const stray = [...source].map((ch, index) => [ch.charCodeAt(0), index]).filter(([code]) => code < 32 && code !== 9 && code !== 10 && code !== 13);
  assert.deepEqual(stray, [], "a control byte in a regex silently disables that alternative");
};

tests.a_nameless_label_keyed_field_is_never_an_exact_match = () => {
  // A follow-up with no name or id: its label is only its wording, which repeats under any parent.
  const followUp = pageOf({ tag: "textarea", wrapped: true, label: "If yes, please explain" });
  const bare = [{ id: "bare", question: "If yes, please explain", answer: "Parent A answer", company: "Acme Robotics" }];
  const scan = loadContentScript(followUp).scan(profile, bare, "Acme Robotics");
  assert.notEqual(scan.fields[0].confidence, 0.9, "a bare follow-up row is not exact on a nameless field");
  // The same standalone wording twice on one form, with no names: neither copy is exact.
  const shared = "Describe a project you are proud of";
  const twice = pageOf({ tag: "textarea", wrapped: true, label: shared }, { tag: "textarea", wrapped: true, label: shared });
  const clean = [{ id: "clean", question: shared, answer: "Answer", company: "Acme Robotics" }];
  for (const field of loadContentScript(twice).scan(profile, clean, "Acme Robotics").fields) {
    assert.notEqual(field.confidence, 0.9, "a repeated nameless question is not exact");
  }
  // A standalone nameless question is still exact at its company.
  const alone = pageOf({ tag: "textarea", wrapped: true, label: shared });
  assert.equal(loadContentScript(alone).scan(profile, clean, "Acme Robotics").fields[0].confidence, 0.9);
};

// --- Round 2 design change: a reusable row never carries after a question the net finds something in, and never ticks a box ---

tests.a_reusable_row_never_carries_a_question_that_follows_one_the_net_finds_something_in = () => {
  // PR #52 wordings: the follow-up's own words say nothing, so only the question right before it can tell.
  const pairs = [
    ["Have you ever been convicted of a felony?", "Please tell us what happened"],
    ["Have you ever been convicted of a felony?", "What did you learn from that experience?"],
    ["Do you now or will you in the future require visa sponsorship?", "What is the expiration date of your current status?"],
    ["Are you currently on probation or parole?", "How much longer is it expected to last?"],
    ["Have you ever pleaded guilty or no contest to a crime?", "Tell us the circumstances of that"],
  ];
  for (const [parent, child] of pairs) {
    const page = pageOf(
      { tag: "textarea", id: "question_1", name: "question_1", label: parent },
      { tag: "textarea", id: "question_2", name: "question_2", label: child },
    );
    const rows = [{ id: "r", question: child, answer: "Answer", company: "Acme Robotics", tags: ["reusable"] }];
    const away = fieldById(loadContentScript(page).scan(profile, rows, "Orbit Systems"), "question_2");
    assert.notEqual(away.confidence, 0.9, `${child} under ${parent}: a reusable row is not exact at another company`);
    const scan = loadContentScript(page).scan(profile, rows, "Orbit Systems");
    assert.equal(preTicked(scan, "question_2"), false, `${child}: not pre-ticked`);
    // At the company it was saved for it is still that company's own answer.
    const home = fieldById(loadContentScript(page).scan(profile, rows, "Acme Robotics"), "question_2");
    assert.equal(home.confidence, 0.9, `${child}: the same company's own row is still exact`);
  }
  // The same wording after an ordinary question still travels when the row is reusable.
  const ordinary = pageOf(
    { tag: "textarea", id: "question_1", name: "question_1", label: "Tell us about yourself" },
    { tag: "textarea", id: "question_2", name: "question_2", label: "What did you learn from that experience?" },
  );
  const row = { id: "r", question: "What did you learn from that experience?", answer: "Answer", company: "Acme Robotics", tags: ["reusable"] };
  assert.equal(fieldById(loadContentScript(ordinary).scan(profile, [row], "Orbit Systems"), "question_2").confidence, 0.9);
};

tests.a_reusable_row_never_ticks_a_box_at_another_company = () => {
  const box = pageOf({ tag: "input", type: "checkbox", id: "coe", name: "question_900[]", value: "I will adhere to the Code of Ethics at all times", label: "I will adhere to the Code of Ethics at all times", groupQuestion: { legend: "Code of Ethics" } });
  const key = loadContentScript(box).scan(profile, [], "Acme Robotics").fields[0].answer_key;
  const rows = [{ id: "c", question: key, answer: "true", company: "Acme Robotics", tags: ["reusable"] }];
  const away = loadContentScript(box).scan(profile, rows, "Orbit Systems");
  assert.notEqual(away.fields[0].confidence, 0.9);
  assert.equal(preTicked(away, "coe"), false, "a box is never pre-ticked from a row saved at another company");
  assert.equal(preTicked(loadContentScript(box).scan(profile, [{ ...rows[0], company: "" }], "Orbit Systems"), "coe"), false, "nor from a row with no company");
};

tests.a_question_the_net_calls_never_storable_is_marked_so_the_panel_offers_no_save = () => {
  const cases = [["Have you ever pleaded guilty or no contest to a crime?", true], ["What is your desired annual income?", true], ["Tell us about yourself", false]];
  for (const [question, never] of cases) {
    const page = pageOf({ tag: "textarea", id: "question_5", name: "question_5", label: question });
    assert.equal(fieldById(loadContentScript(page).scan(profile, [], "Acme Robotics"), "question_5").never_storable, never, question);
  }
  const sidepanel = readFileSync(path.join(ROOT, "apps", "extension", "sidepanel.js"), "utf8");
  assert.match(sidepanel, /!field\.requires_review && !field\.never_storable/, "the panel hides Save for a never-storable question");
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
