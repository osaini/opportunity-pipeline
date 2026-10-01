// Shared by the extension test suites (suites/*.mjs): the sample profile, the fixture
// loaders, and the small readers the engine tests use on a scan result.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const require = createRequire(import.meta.url);
export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
export const FIXTURES = path.join(ROOT, "tests", "extension", "fixtures");

export const profile = {
  name: "Test Student",
  contact: {
    email: "test@example.com",
    phone: "(512) 555-0123",
    github: "https://github.com/test",
  },
  school: "University of Texas",
};

export function loadFixture(name) {
  return JSON.parse(readFileSync(path.join(FIXTURES, name), "utf8"));
}

export function byLabel(scan, label) {
  // labelFor() appends name/id sources, so match on the primary label prefix.
  const field = scan.fields.find((field) => field.label === label || field.label.startsWith(label));
  assert.ok(field, `expected a scanned field labelled ${label}`);
  return field;
}

export const APPLY_FIXTURES = path.join(ROOT, "tests", "fixtures", "apply");

export function loadApplyFixture(name) {
  return JSON.parse(readFileSync(path.join(APPLY_FIXTURES, name), "utf8"));
}

export function pageOf(...controls) {
  return { hostname: "job-boards.greenhouse.io", url: "https://job-boards.greenhouse.io/acme/jobs/1", controls };
}

export function fieldById(scan, id) {
  const field = scan.fields.find((item) => item.id === id);
  assert.ok(field, `expected a scanned field with id ${id}`);
  return field;
}

// What the side panel pre-ticks: 0.9 and up with a value, on a field that is not sensitive.
export function preTicked(scan, id) {
  const field = fieldById(scan, id);
  return !field.prohibited && !field.requires_review && field.confidence >= 0.9 && field.proposed_value !== "";
}
