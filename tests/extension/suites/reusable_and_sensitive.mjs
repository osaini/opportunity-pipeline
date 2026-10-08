// Sensitive topics and the broad net (the shared vectors), and why a reusable row never travels past one.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { loadContentScript } from "../dom_stub.mjs";
import { ROOT, require, profile, loadApplyFixture, pageOf, fieldById, preTicked } from "../helpers.mjs";

export const tests = {};

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
  // apply.policy.needs_label_key and context_dependent repeat these two rules for the agent's plan.
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
  // apply.policy.net_topics, possibly_sensitive and never_storable repeat these three rules for the agent's plan.
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

tests.a_reusable_select_whose_options_or_help_text_agree_never_travels_or_pre_ticks = () => {
  const selects = [
    ["Keep my details in the talent pool for future roles", ["I consent", "I do not consent"], "I consent"],
    ["Interview recording for training purposes", ["I accept", "I decline"], "I accept"],
    ["Our arbitration agreement for disputes", ["Opt in", "Opt out"], "Opt in"],
    ["Do you certify that your answers are true?", ["Yes I do", "No I do not"], "Yes I do"],
  ];
  for (const [question, labels, answer] of selects) {
    const page = pageOf({ tag: "select", id: "question_7", name: "question_7", label: question, options: labels.map((label) => ({ value: label, label })) });
    const rows = [{ id: "r", question, answer, company: "Acme Robotics", tags: ["reusable"] }];
    for (const company of ["Orbit Systems", "", undefined]) {
      const scan = loadContentScript(page).scan(profile, rows, company);
      assert.notEqual(fieldById(scan, "question_7").confidence, 0.9, `${question} at ${JSON.stringify(company)}: not exact`);
      assert.equal(preTicked(scan, "question_7"), false, `${question} at ${JSON.stringify(company)}: not pre-ticked`);
    }
    // At the company it was saved for it is still that company's own answer.
    assert.equal(fieldById(loadContentScript(page).scan(profile, rows, "Acme Robotics"), "question_7").confidence, 0.9, `${question}: own company`);
  }
  // A help text that carries the real question (aria-describedby) is read like the question itself.
  const help = "Please list any criminal convictions here";
  const page = Object.assign(pageOf({ tag: "textarea", id: "question_8", name: "question_8", label: "Anything else we should know about you?", ariaDescribedby: "help_8" }), { texts: { help_8: help } });
  const rows = [{ id: "r", question: "Anything else we should know about you?", answer: "Answer", company: "Acme Robotics", tags: ["reusable"] }];
  const away = loadContentScript(page).scan(profile, rows, "Orbit Systems");
  assert.equal(preTicked(away, "question_8"), false, "a criminal ask in the help text: not pre-ticked at another company");
  assert.equal(fieldById(away, "question_8").never_storable, true, "and the panel offers no Save for it");
  // A plain select, and a plain textarea with harmless help text, still travel when reusable.
  const plain = pageOf({ tag: "select", id: "question_9", name: "question_9", label: "Which team are you most interested in?", options: [{ value: "a", label: "Perception" }, { value: "b", label: "Security" }] });
  const plainRow = [{ id: "p", question: "Which team are you most interested in?", answer: "Perception", company: "Acme Robotics", tags: ["reusable"] }];
  assert.equal(fieldById(loadContentScript(plain).scan(profile, plainRow, "Orbit Systems"), "question_9").confidence, 0.9, "a plain choice list is not read as a clearance question");
  const harmless = Object.assign(pageOf({ tag: "textarea", id: "question_10", name: "question_10", label: "Tell us about yourself", ariaDescribedby: "help_10" }), { texts: { help_10: "A few lines is plenty" } });
  const harmlessRow = [{ id: "h", question: "Tell us about yourself", answer: "Answer", company: "Acme Robotics", tags: ["reusable"] }];
  assert.equal(fieldById(loadContentScript(harmless).scan(profile, harmlessRow, "Orbit Systems"), "question_10").confidence, 0.9);
};

tests.a_race_or_pay_range_select_is_never_storable_and_a_typed_signature_never_travels = () => {
  const race = pageOf({ tag: "select", id: "question_12", name: "question_12", label: "How do you describe yourself?", options: [{ value: "a", label: "Asian" }, { value: "w", label: "White" }, { value: "o", label: "Other" }] });
  assert.equal(fieldById(loadContentScript(race).scan(profile, [], "Acme Robotics"), "question_12").never_storable, true, "a race list");
  const pay = pageOf({ tag: "select", id: "question_13", name: "question_13", label: "Range", options: [{ value: "a", label: "$40,000-$50,000" }, { value: "b", label: "$50,000-$60,000" }] });
  assert.equal(fieldById(loadContentScript(pay).scan(profile, [], "Acme Robotics"), "question_13").never_storable, true, "a pay-range list");
  for (const question of ["Type your initials to agree", "Initials (to show you accept the terms above)", "Your initials"]) {
    const page = pageOf({ tag: "input", type: "text", id: "question_11", name: "question_11", label: question });
    const rows = [{ id: "r", question, answer: "SR", company: "Acme Robotics", tags: ["reusable"] }];
    assert.equal(preTicked(loadContentScript(page).scan(profile, rows, "Orbit Systems"), "question_11"), false, question);
  }
};

tests.a_reusable_row_never_carries_a_field_two_or_more_below_a_sensitive_question = () => {
  const felony = { legend: "Have you ever been convicted of a felony?" };
  const yesNo = [{ value: "y", label: "Yes" }, { value: "n", label: "No" }];
  const chains = [
    [
      { tag: "input", type: "radio", id: "f_yes", name: "question_1", value: "Yes", label: "Yes", groupQuestion: felony },
      { tag: "input", type: "radio", id: "f_no", name: "question_1", value: "No", label: "No", groupQuestion: felony },
      { tag: "input", type: "text", id: "question_2", name: "question_2", label: "Year it happened" },
      { tag: "textarea", id: "question_3", name: "question_3", label: "Please tell us what happened" },
    ],
    [
      { tag: "select", id: "question_1", name: "question_1", label: "Do you now or will you in the future require visa sponsorship?", options: yesNo },
      { tag: "input", type: "text", id: "question_2", name: "question_2", label: "Which type?" },
      { tag: "input", type: "text", id: "question_3", name: "question_3", label: "What is the expiration date of your current status?" },
    ],
    [
      { tag: "select", id: "question_1", name: "question_1", label: "Are you currently on probation or parole?", options: yesNo },
      { tag: "input", type: "text", id: "question_2", name: "question_2", label: "When does it end?" },
      { tag: "textarea", id: "question_3", name: "question_3", label: "What conditions were imposed on you by the judge" },
    ],
  ];
  for (const controls of chains) {
    const page = pageOf(...controls);
    const last = controls[controls.length - 1];
    const rows = [{ id: "r", question: last.label, answer: "2027", company: "Acme Robotics", tags: ["reusable"] }];
    const away = loadContentScript(page).scan(profile, rows, "Orbit Systems");
    assert.notEqual(fieldById(away, "question_3").confidence, 0.9, `${last.label}: not exact at another company`);
    assert.equal(preTicked(away, "question_3"), false, `${last.label}: not pre-ticked`);
    const home = loadContentScript(page).scan(profile, rows, "Acme Robotics");
    assert.equal(fieldById(home, "question_3").confidence, 0.9, `${last.label}: the same company's own row is still exact`);
  }
  // An ordinary chain is left alone.
  const ordinary = pageOf(
    { tag: "textarea", id: "question_1", name: "question_1", label: "Tell us about yourself" },
    { tag: "input", type: "text", id: "question_2", name: "question_2", label: "Which languages?" },
    { tag: "textarea", id: "question_3", name: "question_3", label: "What did you learn from that experience?" },
  );
  const row = { id: "r", question: "What did you learn from that experience?", answer: "Answer", company: "Acme Robotics", tags: ["reusable"] };
  assert.equal(fieldById(loadContentScript(ordinary).scan(profile, [row], "Orbit Systems"), "question_3").confidence, 0.9);
};

tests.a_follow_up_of_a_never_storable_question_offers_no_save_and_an_independent_one_does = () => {
  const yesNo = [{ value: "y", label: "Yes" }, { value: "n", label: "No" }];
  const probation = { tag: "select", id: "question_1", name: "question_1", label: "Are you currently on probation or parole?", options: yesNo };
  const scanned = (...rest) => loadContentScript(pageOf(probation, ...rest)).scan(profile, [], "Acme Robotics");
  assert.equal(fieldById(scanned({ tag: "textarea", id: "question_2", name: "question_2", label: "Please tell us what happened" }), "question_2").never_storable, true, "a direct follow-up");
  const chain = scanned(
    { tag: "input", type: "text", id: "question_2", name: "question_2", label: "When does it end?" },
    { tag: "textarea", id: "question_3", name: "question_3", label: "What conditions were imposed on you by the judge" },
  );
  assert.equal(fieldById(chain, "question_3").never_storable, true, "a follow-up two fields down");
  const independent = scanned({ tag: "textarea", id: "question_2", name: "question_2", label: "Describe your experience with distributed systems in detail" });
  assert.equal(fieldById(independent, "question_2").never_storable, false, "an independent question after one is still offered for saving");
};

tests.the_never_storable_chains_match_the_shared_vectors_the_python_plan_also_runs = () => {
  // apply_classify (field_net through build_plan) reads the same chains: tests/fixtures/apply/net_chains.json.
  const yesNo = [{ value: "y", label: "Yes" }, { value: "n", label: "No" }];
  const { chains } = loadApplyFixture("net_chains.json");
  assert.ok(chains.length >= 4);
  for (const chain of chains) {
    const controls = [{ tag: "select", id: "question_0", name: "question_0", label: chain.parent, options: yesNo }];
    chain.children.forEach((label, index) => controls.push({ tag: "textarea", id: `question_${index + 1}`, name: `question_${index + 1}`, label }));
    const scanned = loadContentScript(pageOf(...controls)).scan(profile, [], "Acme Robotics");
    chain.children.forEach((label, index) => {
      assert.equal(fieldById(scanned, `question_${index + 1}`).never_storable, chain.never[index], `${chain.parent} / ${label}`);
    });
  }
};

tests.a_one_option_select_and_an_agreement_in_other_words_never_carry_a_reusable_row_to_another_company = () => {
  // apply.classify.field_net marks the same fields (tick or agreement) so the plan never fills them from the answer library.
  const cases = [
    { label: "Work arrangement", options: [{ value: "h", label: "Hybrid, three days on site" }] },
    // A long label travels between companies, so only the one-option rule stops its reusable row; a placeholder is not a choice.
    { label: "Which arrangement would suit you best during the summer internship", options: [{ value: "h", label: "Hybrid, three days on site" }] },
    { label: "Which arrangement would suit you best during the summer internship", options: [{ value: "", label: "Select..." }, { value: "h", label: "Hybrid, three days on site" }] },
    { label: "Which arrangement would suit you best during the summer internship", options: [{ value: "", label: "--" }, { value: "h", label: "Hybrid, three days on site" }] },
    { label: "Code of conduct", options: [{ value: "y", label: "I will comply" }, { value: "n", label: "I will not comply" }] },
    { label: "Handbook", options: [{ value: "y", label: "I will abide by it" }, { value: "n", label: "I will not" }] },
  ];
  for (const { label, options } of cases) {
    const page = pageOf({ tag: "select", id: "question_20", name: "question_20", label, options });
    const rows = [{ id: "r", question: label, answer: options[options.length - 1].label, company: "Acme Robotics", tags: ["reusable"] }];
    assert.equal(preTicked(loadContentScript(page).scan(profile, rows, "Orbit Systems"), "question_20"), false, label);
  }
  for (const label of ["Signed by", "Sign below", "Countersignature", "Name of signatory"]) {
    const page = pageOf({ tag: "input", type: "text", id: "question_21", name: "question_21", label });
    const rows = [{ id: "r", question: label, answer: "SR", company: "Acme Robotics", tags: ["reusable"] }];
    assert.equal(preTicked(loadContentScript(page).scan(profile, rows, "Orbit Systems"), "question_21"), false, label);
  }
  const plain = pageOf({ tag: "select", id: "question_22", name: "question_22", label: "Which team are you most interested in?", options: [{ value: "p", label: "Perception" }, { value: "c", label: "Controls" }, { value: "x", label: "Planning" }] });
  const row = [{ id: "r", question: "Which team are you most interested in?", answer: "Controls", company: "Acme Robotics", tags: ["reusable"] }];
  assert.equal(loadContentScript(plain).scan(profile, row, "Acme Robotics").fields[0].confidence, 0.9, "an ordinary choice still matches at its own company");
};

tests.the_section_headings_match_the_shared_vectors_the_python_plan_also_runs = () => {
  // apply.classify.section_never repeats this rule; tests/fixtures/apply/broad_net.json ("sections").
  const { sections } = loadApplyFixture("broad_net.json");
  const ext = loadContentScript(pageOf({ tag: "input", type: "text", id: "q", label: "Q" }), { contentScript: false });
  assert.ok(sections.length >= 15);
  for (const { text, never } of sections) assert.equal(ext.engine.sectionNeverText(text), never, text);
  assert.equal(ext.engine.sectionNeverText(undefined), false);
};

tests.every_question_under_a_demographic_compliance_or_background_heading_is_never_storable = () => {
  // A wording no list knows, under three headings: the question is left for the student, nothing carries onto it, and the panel offers no Save.
  const question = "What do you enjoy most about robotics projects";
  const underHeading = (section) => pageOf(
    { tag: "input", type: "text", id: "question_1", name: "question_1", label: "Tell us about your best project" },
    { tag: "input", type: "text", id: "question_2", name: "question_2", label: question, section },
  );
  const row = [{ id: "r", question, answer: "Answer", company: "Acme Robotics", tags: ["reusable"] }];
  for (const section of [{ heading: "Voluntary Self-Identification" }, { heading: "Demographic information" }, { id: "eeoc_fields" }, { ariaLabel: "Background check disclosure" }, { heading: "Compliance" }]) {
    const scanned = loadContentScript(underHeading(section)).scan(profile, row, "Orbit Systems");
    assert.equal(fieldById(scanned, "question_2").never_storable, true, JSON.stringify(section));
    assert.equal(preTicked(scanned, "question_2"), false, JSON.stringify(section));
    assert.equal(fieldById(scanned, "question_1").never_storable, false, "a question outside it is untouched");
  }
  for (const section of [{ heading: "Your information" }, { heading: "Application questions" }, { heading: "Your background in robotics" }, undefined]) {
    const scanned = loadContentScript(underHeading(section)).scan(profile, row, "Acme Robotics");
    assert.equal(fieldById(scanned, "question_2").never_storable, false, JSON.stringify(section));
    assert.equal(fieldById(scanned, "question_2").confidence, 0.9, "an ordinary section leaves the same company's row exact");
  }
  // The standard profile fields are filled from the profile whatever the heading says; only their Save is hidden.
  const standard = loadContentScript(pageOf({ tag: "input", type: "text", id: "first_name", name: "first_name", label: "First Name", section: { heading: "Demographic information" } })).scan(profile, [], "Acme Robotics");
  assert.equal(fieldById(standard, "first_name").proposed_value, "Test");
};
