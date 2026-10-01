// Saved answers: when one is proposed, pre-ticked, matched on its clean question or its label, and when it is not.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { loadContentScript } from "../dom_stub.mjs";
import { ROOT, require, profile, loadFixture, byLabel, pageOf, fieldById, preTicked } from "../helpers.mjs";

export const tests = {};

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
