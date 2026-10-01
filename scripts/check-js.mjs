// Syntax-checks every script the project ships with `node --check`: the browser scripts in
// opportunity_app/static/ and the extension's scripts under apps/extension/. It globs the folders, so a new file is
// covered without editing a list in CI or package.json. `node --check` only proves a file parses; the browser and
// extension suites are what run it.
//
//   node scripts/check-js.mjs
import { spawnSync } from "node:child_process";
import { readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function scriptsUnder(directory) {
  const found = [];
  for (const entry of readdirSync(path.join(root, directory), { withFileTypes: true })) {
    const relative = path.posix.join(directory, entry.name);
    if (entry.isDirectory()) found.push(...scriptsUnder(relative));
    else if (/\.(js|mjs)$/.test(entry.name)) found.push(relative);
  }
  return found;
}

const files = [...scriptsUnder("opportunity_app/static"), ...scriptsUnder("apps/extension")].sort();
if (!files.length) {
  console.error("check-js: found no scripts to check");
  process.exit(1);
}

let failed = 0;
for (const file of files) {
  const result = spawnSync(process.execPath, ["--check", path.join(root, file)], { encoding: "utf8" });
  if (result.status !== 0) {
    failed += 1;
    console.error(`FAIL ${file}\n${result.stderr}`);
  }
}
console.log(`check-js: ${files.length - failed} of ${files.length} scripts parse`);
process.exit(failed ? 1 : 0);
