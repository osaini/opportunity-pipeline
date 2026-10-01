// Zero-dependency test runner for the Apply Mode extension.
// Usage: node tests/extension/run_tests.mjs
// Each suites/*.mjs file exports a `tests` object of named tests (sync or async). They run here one after
// another, in the order the suites are listed, and a thrown or rejected test is counted as a failure.
//   scan_and_fill          content.js scan/fill against hand-rolled DOM stubs and per-ATS fixtures
//   engine_questions       how the shared engine reads a control's question, markers and kind
//   answer_matching        saved answers: proposed, pre-ticked, matched or refused
//   reusable_and_sensitive the shared sensitive/broad-net vectors and reusable rows that never travel
//   source_guards          checks that read the extension source (never submits, injection list)
//   sidepanel_tests.mjs    sidepanel.js driven through a harness: clicks, requests, stale-response guards
import { tests as scanAndFillTests } from "./suites/scan_and_fill.mjs";
import { tests as engineQuestionsTests } from "./suites/engine_questions.mjs";
import { tests as answerMatchingTests } from "./suites/answer_matching.mjs";
import { tests as reusableAndSensitiveTests } from "./suites/reusable_and_sensitive.mjs";
import { tests as sourceGuardsTests } from "./suites/source_guards.mjs";
import { sidepanelTests } from "./sidepanel_tests.mjs";

const tests = {};
for (const suite of [scanAndFillTests, engineQuestionsTests, answerMatchingTests, reusableAndSensitiveTests, sourceGuardsTests, sidepanelTests]) {
  for (const name of Object.keys(suite)) {
    if (name in tests) throw new Error(`two extension tests are named ${name}`);
  }
  Object.assign(tests, suite);
}

let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
  try {
    await fn();
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
