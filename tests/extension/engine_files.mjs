// The one list of page scripts the side panel injects, in order, read from the
// executeScript call in apps/extension/sidepanel.js. The DOM stub and the MV3
// browser test take their list from here, so neither can drift from the panel;
// the engine_files drift test checks the list itself against the extension folder.
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const EXTENSION_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "apps", "extension");

let cached;
export function injectedFiles() {
  if (cached) return [...cached];
  const source = readFileSync(path.join(EXTENSION_DIR, "sidepanel.js"), "utf8");
  const call = /executeScript\([^;]*?\bfiles:\s*(\[[^\]]*\])/.exec(source);
  if (!call) throw new Error("sidepanel.js has no executeScript({ ..., files: [...] }) call to read the injection list from");
  cached = JSON.parse(call[1]);
  return [...cached];
}
