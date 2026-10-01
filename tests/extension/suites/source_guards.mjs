// Guards that read the extension source: it never submits, the injection list and answer save stay as split, no stray characters.
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { loadContentScript } from "../dom_stub.mjs";
import { EXTENSION_DIR, injectedFiles } from "../engine_files.mjs";
import { ROOT, loadFixture } from "../helpers.mjs";

export const tests = {};

tests.content_script_preserves_no_submit_guarantee = () => {
  // The rules now live in apply-engine.js, so the positive assertions follow them there;
  // the negative ones cover every page script the side panel injects (every click the agent makes lives in Python).
  const engineSource = readFileSync(path.join(ROOT, "apps", "extension", "apply-engine.js"), "utf8");
  assert.match(engineSource, /"submit"/);
  assert.match(engineSource, /SENSITIVE/);
  for (const file of injectedFiles()) {
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

tests.injection_lists_and_the_answer_save_follow_the_split = () => {
  // The panel's executeScript call is the one list; the DOM stub and the MV3 browser test read it from there.
  const injected = injectedFiles();
  const pageScripts = readdirSync(EXTENSION_DIR).filter((name) => name.endsWith(".js") && name !== "sidepanel.js" && name !== "service-worker.js");
  assert.deepEqual([...injected].sort(), [...pageScripts].sort(), "the panel injects every page script in the extension folder, and nothing else");
  assert.equal(injected[0], "adapters.js", "adapters.js first");
  assert.equal(injected.at(-1), "content.js", "content.js last");
  assert.ok(injected.indexOf("field-engine.js") < injected.indexOf("apply-engine.js"), "field-engine.js before apply-engine.js");
  assert.ok(injected.indexOf("apply-engine.js") < injected.indexOf("content.js"), "apply-engine.js before content.js");
  const sidepanel = readFileSync(path.join(EXTENSION_DIR, "sidepanel.js"), "utf8");
  assert.match(sidepanel, /question: field\.answer_key,/, "the side panel saves the engine's answer key");
  assert.doesNotMatch(sidepanel, /question: field\.label,/);
  for (const file of ["dom_stub.mjs", path.join("browser", "run_browser_tests.mjs")]) {
    const source = readFileSync(path.join(ROOT, "tests", "extension", file), "utf8");
    assert.match(source, /injectedFiles\(\)/, `${file} takes the injection list from the panel`);
    assert.doesNotMatch(source, /"apply-engine\.js"/, `${file} keeps no copy of the list`);
  }
  // CI and `npm run check` syntax-check every static and extension script by directory, so a new file is covered.
  const checker = readFileSync(path.join(ROOT, "scripts", "check-js-syntax.mjs"), "utf8");
  assert.match(checker, /"apps\/extension"/);
  assert.match(checker, /"opportunity_app\/static"/);
  const ci = readFileSync(path.join(ROOT, ".github", "workflows", "ci.yml"), "utf8");
  assert.match(ci, /node scripts\/check-js-syntax\.mjs/);
  assert.match(JSON.parse(readFileSync(path.join(ROOT, "package.json"), "utf8")).scripts.check, /scripts\/check-js-syntax\.mjs/);
};

tests.the_engine_source_has_no_stray_control_characters = () => {
  const source = readFileSync(new URL("../../../apps/extension/apply-engine.js", import.meta.url), "utf8");
  const stray = [...source].map((ch, index) => [ch.charCodeAt(0), index]).filter(([code]) => code < 32 && code !== 9 && code !== 10 && code !== 13);
  assert.deepEqual(stray, [], "a control byte in a regex silently disables that alternative");
};
