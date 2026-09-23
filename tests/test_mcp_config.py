"""The Playwright MCP server must start in a checkout that never ran npm install."""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class PlaywrightMcpConfigTests(unittest.TestCase):
    def setUp(self):
        self.server = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["playwright"]

    def test_it_starts_through_the_self_installing_launcher(self):
        # A bare `npx @playwright/mcp` downloads at editor launch in every fresh
        # clone or worktree and times out as "Connection closed".
        self.assertEqual(self.server["command"], "node")
        self.assertEqual(self.server["args"][0], "scripts/playwright-mcp.mjs")
        self.assertTrue((ROOT / "scripts" / "playwright-mcp.mjs").is_file())

    def test_the_browser_stays_on_the_sandbox(self):
        args = self.server["args"]
        origins = args[args.index("--allowed-origins") + 1].split(";")
        self.assertTrue(origins)
        for origin in origins:
            self.assertRegex(origin, r"^http://(127\.0\.0\.1|localhost):8799$")
        self.assertIn("--isolated", args)


if __name__ == "__main__":
    unittest.main()
