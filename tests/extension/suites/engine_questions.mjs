// The shared engine (apps/extension/apply-engine.js): how a control's question, markers and kind are read.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { loadContentScript } from "../dom_stub.mjs";
import { ROOT, require, profile, pageOf, fieldById } from "../helpers.mjs";

export const tests = {};

tests.engine_loads_without_chrome_and_exposes_the_agent_surface = () => {
  const ext = loadContentScript(pageOf({ tag: "input", type: "text", id: "first_name", label: "First Name" }), { contentScript: false });
  const engine = ext.engine;
  assert.equal(engine.version, "1");
  assert.deepEqual(Object.keys(engine).sort(), ["attachDocumentFromBytes", "contextDependent", "fill", "needsLabelKey", "netTopics", "neverStorable", "possiblySensitive", "questionKey", "questionText", "scan", "sectionNeverText", "version"]);
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
