// Runs `node --check` on every JavaScript file the project ships or tests with
// a plain Node script: the web app's static scripts, the browser extension, the
// extension's Node tests, the repo's Node scripts, and the Claude Code hooks.
// New files are covered by directory, so a file added later cannot be left out
// of the syntax check.
// Usage: node scripts/check-js-syntax.mjs
import { readdirSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const TREES = [
  { dir: "opportunity_app/static", extensions: [".js"], recursive: false },
  { dir: "apps/extension", extensions: [".js"], recursive: true },
  { dir: "tests/extension", extensions: [".mjs"], recursive: true },
  { dir: "scripts", extensions: [".mjs"], recursive: false },
  { dir: ".claude/hooks", extensions: [".mjs"], recursive: false },
];

function collect(dir, extensions, recursive) {
  const found = [];
  for (const entry of readdirSync(path.join(ROOT, dir), { withFileTypes: true })) {
    const relative = `${dir}/${entry.name}`;
    if (entry.isDirectory()) {
      if (recursive && entry.name !== "node_modules") found.push(...collect(relative, extensions, recursive));
    } else if (extensions.includes(path.extname(entry.name))) {
      found.push(relative);
    }
  }
  return found;
}

const files = TREES.flatMap(({ dir, extensions, recursive }) => collect(dir, extensions, recursive)).sort();
let failed = 0;
for (const file of files) {
  const result = spawnSync(process.execPath, ["--check", file], { cwd: ROOT, encoding: "utf8" });
  if (result.status !== 0) {
    failed += 1;
    console.error(`FAIL ${file}\n${result.stderr}`);
  }
}
if (failed > 0) {
  console.error(`${failed} of ${files.length} JavaScript files failed the syntax check`);
  process.exitCode = 1;
} else {
  console.log(`${files.length} JavaScript files pass the syntax check`);
}
