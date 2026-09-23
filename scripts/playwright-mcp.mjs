#!/usr/bin/env node
// Starts the Playwright MCP server from this checkout's pinned install.
//
// `.mcp.json` used to run `npx @playwright/mcp`, which only works once someone
// has run `npm install` in this exact directory. Every fresh clone and every new
// git worktree starts without node_modules, so npx fell back to downloading the
// package at editor launch, overran the MCP startup timeout, and the server
// showed as "Connection closed". This launcher installs what is missing first:
// the pinned packages from package-lock.json (about a second), and the Chromium
// build Playwright drives, which lives in a per-user cache shared by every
// checkout, so it downloads once per machine.
//
// stdout carries the MCP protocol, so everything else writes to stderr.

import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const cli = join(root, "node_modules", "@playwright", "mcp", "cli.js");

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

function run(command) {
  process.stderr.write(`playwright-mcp: ${command}\n`);
  // shell: Node refuses to spawn npm.cmd and npx.cmd directly on Windows.
  const result = spawnSync(command, { cwd: root, shell: true, stdio: ["ignore", 2, 2] });
  if (result.status !== 0) {
    process.stderr.write(`playwright-mcp: "${command}" failed; run it in ${root} to see why.\n`);
    process.exit(result.status || 1);
  }
}

const pinned = readJson(join(root, "package-lock.json"))?.packages?.["node_modules/@playwright/mcp"]?.version;
const installed = readJson(join(root, "node_modules", "@playwright", "mcp", "package.json"))?.version;
if (!installed || (pinned && installed !== pinned)) run("npm ci --no-audit --no-fund");

const { chromium } = createRequire(join(root, "package.json"))("playwright-core");
if (!existsSync(chromium.executablePath())) run("npx --no-install playwright install chromium");

// Hand over to the server in this process, as if its own bin had been run.
process.argv = [process.argv[0], cli, ...process.argv.slice(2)];
createRequire(import.meta.url)(cli);
