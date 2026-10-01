"""The browser app is several ordered classic scripts, not one file.

index.html loads them in dependency order with `defer`. Each is its own IIFE, and they share one namespace,
window.OpportunityApp: a file publishes what other files use with `Object.assign(App, {...})`, takes what earlier files
published with `const {...} = App`, and reaches a function that loads later through a small wrapper that looks it up when
called. These checks read the scripts as text, so a name a file needs but no earlier file publishes fails here, in the
fast suite, instead of as a ReferenceError the first time a button is pressed in a browser.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers_source import STATIC_DIR, static_scripts

try:
    import realdata_guard
except ImportError:  # imported as tests.test_static_scripts, with tests/ not on sys.path
    from tests import realdata_guard
realdata_guard.install()

APP_SCRIPT = re.compile(r"^app(-[a-z-]+)?\.js$")
DEFERRED_SCRIPT = re.compile(r'<script src="/assets/([^"?]+\.js)[^"]*"[^>]*\bdefer\b')
EXPORT_BLOCK = re.compile(r"Object\.assign\(App, \{(.*?)\}\);", re.S)
IMPORT_BLOCK = re.compile(r"const \{([^}]*)\} = App;")
FORWARD_WRAPPER = re.compile(r"^  const (\w+) = \(\.\.\.args\) => App\.(\w+)\(\.\.\.args\);$", re.M)
APP_MEMBER = re.compile(r"\bApp\.(\w+)")


def names(block):
    return [name.strip() for name in block.split(",") if name.strip()]


def page_scripts():
    return DEFERRED_SCRIPT.findall((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


def app_scripts():
    return {name: text for name, text in static_scripts().items() if APP_SCRIPT.match(name)}


class StaticScriptsTest(unittest.TestCase):
    def test_the_page_loads_every_app_script_once_in_order_with_defer(self):
        loaded = page_scripts()
        self.assertEqual(sorted(loaded), sorted(app_scripts()), "index.html must load exactly the app scripts, each with defer")
        self.assertEqual(len(loaded), len(set(loaded)), "a script is loaded twice")
        self.assertEqual(loaded[0], "app-context.js", "the namespace is created by the first script")
        self.assertEqual(loaded[-1], "app.js", "app.js boots the session, so it loads last")

    def test_each_script_is_one_iife_and_only_the_first_creates_the_namespace(self):
        for name, text in app_scripts().items():
            with self.subTest(script=name):
                code = "\n".join(line for line in text.splitlines() if not line.startswith("//"))
                self.assertTrue(code.lstrip().startswith("(() => {"), "wrap the script in an IIFE so nothing leaks into the global scope")
                self.assertTrue(code.rstrip().endswith("})();"))
                self.assertEqual(text.count("window.OpportunityApp = "), 1 if name == "app-context.js" else 0)
                if name != "app-context.js":
                    self.assertIn("const App = window.OpportunityApp;", text)

    def test_every_name_a_script_takes_from_the_namespace_is_published_before_it(self):
        loaded = page_scripts()
        scripts = app_scripts()
        published = {}
        for name in loaded:
            for block in EXPORT_BLOCK.findall(scripts[name]):
                for exported in names(block):
                    self.assertNotIn(exported, published, f"{exported} is published by both {published.get(exported)} and {name}")
                    published[exported] = name
        for position, name in enumerate(loaded):
            earlier = set(loaded[:position])
            for block in IMPORT_BLOCK.findall(scripts[name]):
                for taken in names(block):
                    with self.subTest(script=name, name=taken):
                        self.assertIn(published.get(taken), earlier, f"{name} takes {taken}, which no earlier script publishes")
            for local, target in FORWARD_WRAPPER.findall(scripts[name]):
                with self.subTest(script=name, forward=target):
                    self.assertEqual(local, target)
                    self.assertIn(published.get(target), set(loaded[position + 1:]), f"{name} looks up {target}, which no later script publishes")

    def test_every_published_name_is_used_by_another_script(self):
        loaded = page_scripts()
        scripts = app_scripts()
        for name in loaded:
            for block in EXPORT_BLOCK.findall(scripts[name]):
                for exported in names(block):
                    others = [other for other in loaded if other != name]
                    used = any(
                        re.search(rf"\b{re.escape(exported)}\b", scripts[other]) for other in others
                    )
                    with self.subTest(script=name, name=exported):
                        self.assertTrue(used or exported.startswith("install"), f"{name} publishes {exported}, but no other script uses it")

    def test_a_script_only_touches_namespace_members_that_some_script_publishes(self):
        scripts = app_scripts()
        published = {exported for text in scripts.values() for block in EXPORT_BLOCK.findall(text) for exported in names(block)}
        for name, text in scripts.items():
            for member in sorted(set(APP_MEMBER.findall(text))):
                if name == "app-context.js" and member == "OpportunityApp":
                    continue
                with self.subTest(script=name, member=member):
                    self.assertIn(member, published)

    def test_boot_runs_the_registrations_of_every_script_that_has_them(self):
        scripts = app_scripts()
        boot = scripts["app.js"]
        for name, text in scripts.items():
            for installer in re.findall(r"^  function (install\w+)\(\) \{", text, re.M):
                with self.subTest(script=name, installer=installer):
                    self.assertIn(f"App.{installer}();", boot, f"app.js never runs {name}'s registrations")


if __name__ == "__main__":
    unittest.main()
