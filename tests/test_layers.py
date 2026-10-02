"""Layer map for opportunity_app/ and pipeline_core/: imports may only point down, and cycles may only hide in function bodies.

The package grew as ~100 flat modules whose top-level imports happen to be acyclic while ~100 function-level imports close
a strongly connected component of about thirty modules (automation, outreach, profile, the apply_* family, the outreach mail
and workflow modules, ...). Nothing stated the intended shape, so every new feature was free to add one more lazy import.
This test states it and ratchets toward it.

Every module is classified into exactly one layer below. An import of a LOWER-or-equal layer is always fine; an import of a
HIGHER layer is "upward". The rules, each its own test:

  a. No top-level import points upward (and the top-level graph has no cycle).
  b. A function-level import that points upward, or that could not be hoisted to the top of its file without creating an
     import cycle, must be listed in ALLOWLIST below with a one-line reason. A function-level import that is neither (it
     points down or sideways and closes no cycle) needs no entry: it is merely hoistable.
  c. An ALLOWLIST entry that is no longer needed (the import is gone, now sits at the top of the file, points down, or
     no longer closes a cycle) fails the test, so the list only ever shrinks. Delete the entry in the same change that
     removes the need.
  d. Every module must be classified, and every classified module must exist: a new file fails until it is placed.

What counts as a cycle. Take the top-level import graph plus every function-level import that is NOT on the allowlist
(the graph as it would be if all of those were hoisted). That graph must be acyclic. A function-level import whose target
can already reach its importer in that graph closes a cycle. When two lazy imports close the same cycle, list either one:
the other then becomes an ordinary hoistable import. The allowlist is keyed by (importer, imported module); the test cannot
see inside a module, so the reason names the functions or names when that helps whoever removes the entry.

The layers (low to high), and what each one is for. A layer's number only decides which imports are allowed; the role is
what puts a module there.

  L0  stdlib leaves: no database, no network, no first-party imports beyond other L0 modules. Clocks, JSON helpers, mail
      message parsing, the legacy pipeline and pipeline_core (which tests/test_dependency_boundary.py keeps standard
      library only), and small pure parsers.
  L1  storage: the schema and migration runner, settings and profile storage, the legacy-pipeline adapter.
  L2  integrations: clients for things outside the process (Gmail REST, web fetching, SMTP, AI CLIs, the typesafe judge,
      the PDF renderer). Each is a leaf that imports nothing first-party.
  L3  domain: what the product knows. Opportunities and applications, apply policy and sensitivity, outreach
      records and configuration, the automation ledger, resumes and preparation documents, trust policy, profile.
  L4  workflows: code that runs the product across domain modules or on a schedule: sending, inbox capture, thank-you and
      schedule workflows, automation handlers, apply runs, discovery and research, and the background worker base.
  L5  entry points: the FastAPI app (api and opportunity_app.web), the launcher, the worker, and every CLI that is run as
      `python -m opportunity_app.X`.
      One exception: purge. It has a `__main__` guard (the daily run calls `python -m opportunity_app.purge`), but its
      logic is a domain operation (expire records) and main() is a thin wrapper, so it stays in L3 where refresh (L4),
      the manual refresh workflow, can import it at the top of the file. Do not move it up without moving refresh up too.

One deviation from the proposal in the refactor audit (critic.md): it put integrations ABOVE domain (L3) and workflows
at L4. Today's top-level graph forbids that. outreach.py (domain) imports web_fetch and typesafe_decisions, outreach_config
(domain, imported by the automation ledger) imports agent_providers, and preparation imports agent_providers. With integrations
above domain, every one of those domain modules would itself be an "integration" and the domain layer would be empty. Integrations
are leaves that import nothing first-party, so putting them under domain loses nothing: they still cannot reach back up.

Adding a module: put it in the lowest layer that holds everything it imports at the top of the file, unless its role says
higher. Do not raise a layer to make an upward import pass; fix the import.

This file is a source guard, not a behaviour test: it parses every module with `ast` and imports none of them.
"""

import ast
import unittest
from pathlib import Path
from typing import Iterable, NamedTuple

ROOT = Path(__file__).resolve().parents[1]

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()

PACKAGES = ("opportunity_app", "pipeline_core")
ROOT_MODULES = ("pipeline",)  # pipeline.py at the repository root: the legacy pipeline and its CLI
FIRST_PARTY = frozenset(PACKAGES) | frozenset(ROOT_MODULES)


def _mods(package: str, names: str) -> frozenset[str]:
    return frozenset(package if name == "." else f"{package}.{name}" for name in names.split())


def _app(names: str) -> frozenset[str]:
    return _mods("opportunity_app", names)


def _core(names: str) -> frozenset[str]:
    return _mods("pipeline_core", names)


LAYER_NAMES = {
    0: "stdlib leaves",
    1: "storage",
    2: "integrations",
    3: "domain",
    4: "workflows",
    5: "entry points",
}

# Every module is listed here, in exactly one layer. There is deliberately no default layer for an unlisted module.
LAYER_MEMBERS: dict[int, frozenset[str]] = {
    # L0 stdlib leaves. `opportunity_app` and `pipeline_core` are the package __init__ modules (constants and re-exports).
    0: (
        _app(". student mail core integrations core.timestamps core.user_time core.database core.json_values mail.message opportunity_metadata core.storage_paths contact_names core.daily_lock core.hooks mail.monitored_classifier outreach_replies")
        | _core(". env identity visibility regions read_model paths clock text http config sources scoring artifacts store liveness retention importers discovery fetch reports cli")
        | frozenset({"pipeline"})
    ),
    # L1 storage. company_tags is here because schema.py imports it; legacy is the one adapter onto pipeline.py; legacy_sync
    # writes the product database from the legacy one, so it sits beside schema, which it imports one way.
    1: _app("core.schema legacy_sync core.settings_store core.profile_store core.company_tags legacy"),
    # L2 integrations. Leaves: none of them imports another first-party module.
    2: _app("integrations.agent_providers integrations.web_fetch integrations.gmail_client integrations.typesafe_decisions integrations.smtp_probe integrations.pdf"),
    # L3 domain.
    3: _app(
        "actions apply_sessions auth apply_checks apply_claims apply_classify apply_greenhouse apply_policy apply_sensitive apply_schema_client "
        "automation automation_health boards captures mail.connections dossier employer market early_programs extension_apply mail.trust "
        "notifications purge ingestion student.profile student.resumes student.resume_variants student.preparation student.artifacts mail.classifiers mail.gmail_connection "
        "send_claims outreach outreach_agents outreach_callbacks outreach_config outreach_decline_reading outreach_identity outreach_label_name "
        "outreach_location outreach_greeting outreach_versions outreach_contacts outreach_linkedin outreach_batch "
        "outreach_thank_you_writing outreach_render"
    ),
    # L4 workflows. refresh is the manual refresh/purge workflow run in a background thread; api (L5) is its only importer.
    4: _app(
        "background application_inbox application_mail_rules inbox_watcher internal_automation automation_handlers auto_triage apply_runs apply_preflight "
        "outreach_gmail outreach_gmail_sends outreach_delivery outreach_inbox outreach_labels outreach_schedule "
        "outreach_thank_you outreach_reply_senders outreach_automation outreach_recontact outreach_review outreach_call_prep "
        "outreach_call_questions outreach_forms outreach_discovery outreach_research quote_check outreach_drafting "
        "outreach_interviewer outreach_email_search outreach_locate outreach_profile outreach_settings "
        "refresh desktop_notify operations backups student.agent urgent monitored_events"
    ),
    # L5 entry points. opportunity_app.web is the FastAPI app behind api: the composition root (app), the per-app context, the
    # dependencies, middleware and asset handling, the request models, and one router module per feature.
    5: (
        _app("api bootstrap launch worker daily system_status migrate ops_cli outreach_cli pipeline_mailbox setup")
        | _mods("opportunity_app.web", ". app context dependencies errors middleware assets payloads overrides")
        | _mods(
            "opportunity_app.web.models",
            ". account admin agent applications apply_agent automation captures connections dossier employer extension market "
            "opportunities outreach preparation resumes session system",
        )
        | _mods(
            "opportunity_app.web.routers",
            ". account admin agent applications apply_agent apply_sessions automation captures connections dossier employer "
            "extension market opportunities outreach_contacts outreach_delivery outreach_drafting outreach_research "
            "outreach_settings outreach_targets pages preparation resumes session system typesafe urgent",
        )
    ),
}

# (importer, imported module, reason). One entry per pair of modules; see the module docstring for what needs one.
# This list may only shrink. When you break one of these, delete its entry in the same change.
_P = "opportunity_app."
ALLOWLIST: tuple[tuple[str, str, str], ...] = (
    # --- Upward: a lower layer reaches a higher one at call time. Each is a registry or callback that is looked up late.
    (_P + "outreach_contacts", _P + "outreach_forms", "record_contact_form: contact search records the contact form the form workflow found"),
    (_P + "outreach_contacts", _P + "outreach_profile", "rendered_pages and record_site_location: contact search re-reads pages in a browser and records the site location"),
    (_P + "outreach_settings", _P + "setup", "set_env_values: the settings page writes .env through the setup CLI's helper"),
    # --- Same layer, but hoisting the import would close a top-level cycle. One entry per cycle edge that must stay lazy.
    (_P + "actions", _P + "student.resume_variants", "safe_pick_after_save: resume_variants imports actions at the top"),
    (_P + "launch", _P + "api", "create_app: api imports system_status, which would import launch if that were hoisted too"),
    (_P + "launch", _P + "web.context", "LOOPBACK_HOSTS: web.context imports system_status, which would import launch if that were hoisted too"),
    (_P + "student.profile", _P + "outreach_greeting", "greeting_style_error: outreach_greeting and outreach_location read confirmed facts through preparation, which imports profile"),
)


class Edge(NamedTuple):
    importer: str
    target: str
    lazy: bool  # inside a function body (def, async def or lambda): runs when called, not when the module loads
    line: int
    names: tuple[str, ...]


class Analysis(NamedTuple):
    unclassified: list[str]
    classified_but_missing: list[str]
    classified_twice: list[str]
    unresolved: list[str]
    top_upward: list[str]
    top_cycles: list[str]
    unlisted_lazy: list[str]
    stale_allowlist: list[str]


def discover_modules(root: Path = ROOT) -> dict[str, tuple[Path, bool]]:
    """{dotted module name: (path, is_package)} for pipeline.py and every .py under opportunity_app/ and pipeline_core/."""
    found: dict[str, tuple[Path, bool]] = {}
    for name in ROOT_MODULES:
        found[name] = (root / f"{name}.py", False)
    for package in PACKAGES:
        for path in sorted((root / package).rglob("*.py")):
            relative = path.relative_to(root).with_suffix("")
            parts = list(relative.parts)
            is_package = parts[-1] == "__init__"
            if is_package:
                parts.pop()
            found[".".join(parts)] = (path, is_package)
    return found


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def imports_of(module: str, is_package: bool, source: str, known: frozenset[str] | set[str]) -> tuple[list[Edge], list[str]]:
    """First-party import edges of one module, and the first-party imports that name no known module.

    Imports under `if TYPE_CHECKING:` never run, so they are not edges. An import inside any def, async def or lambda is lazy;
    everything else, including imports in a class body, a try block or an if block at module level, runs at load time.
    """
    tree = ast.parse(source, filename=module)
    package = module if is_package else module.rpartition(".")[0]
    edges: list[Edge] = []
    unresolved: list[str] = []

    def add(target: str, lazy: bool, node: ast.stmt, names: tuple[str, ...]) -> None:
        if target != module:
            edges.append(Edge(module, target, lazy, node.lineno, names))

    def visit(node: ast.AST, lazy: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking(child.test):
                for other in child.orelse:
                    visit_statement(other, lazy)
                continue
            visit_statement(child, lazy)

    def visit_statement(child: ast.AST, lazy: bool) -> None:
        if isinstance(child, ast.Import):
            for alias in child.names:
                if alias.name.split(".")[0] not in FIRST_PARTY:
                    continue
                if alias.name in known:
                    add(alias.name, lazy, child, ())
                else:
                    unresolved.append(f"{module}:{child.lineno} import {alias.name}")
        elif isinstance(child, ast.ImportFrom):
            if child.level:
                anchor = package.rsplit(".", child.level - 1)[0] if child.level > 1 else package
                base = f"{anchor}.{child.module}" if child.module else anchor
            else:
                base = child.module or ""
            if base.split(".")[0] in FIRST_PARTY:
                spelled = "." * child.level + (child.module or "")
                for alias in child.names:
                    submodule = f"{base}.{alias.name}"
                    if submodule in known:  # `from . import automation`: the name is itself a module
                        add(submodule, lazy, child, ())
                    elif base in known:
                        add(base, lazy, child, (alias.name,))
                    else:
                        unresolved.append(f"{module}:{child.lineno} from {spelled} import {alias.name}")
        nested = lazy or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        visit(child, nested)

    visit(tree, False)
    return edges, unresolved


def _adjacency(edges: Iterable[tuple[str, str]]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for importer, target in edges:
        graph.setdefault(importer, set()).add(target)
        graph.setdefault(target, set())
    return graph


def _path(graph: dict[str, set[str]], start: str, goal: str) -> list[str] | None:
    """A shortest path start -> goal, or None."""
    previous: dict[str, str | None] = {start: None}
    queue = [start]
    while queue:
        node = queue.pop(0)
        if node == goal:
            path = []
            while node is not None:
                path.append(node)
                node = previous[node]
            return path[::-1]
        for nxt in sorted(graph.get(node, ())):
            if nxt not in previous:
                previous[nxt] = node
                queue.append(nxt)
    return None


def _short(path: Iterable[str]) -> str:
    return " -> ".join(name.replace("opportunity_app.", "").replace("pipeline_core.", "core.") for name in path)


def analyse(
    modules: dict[str, tuple[Path, bool]],
    sources: dict[str, str],
    layer_members: dict[int, frozenset[str]],
    allowlist: Iterable[tuple[str, str, str]],
) -> Analysis:
    """All the rules at once, over any set of modules, so the rules themselves can be tested on small synthetic graphs."""
    layer_of: dict[str, int] = {}
    classified_twice: list[str] = []
    for layer, members in sorted(layer_members.items()):
        for name in sorted(members):
            if name in layer_of:
                classified_twice.append(f"{name} is in layer {layer_of[name]} and layer {layer}")
            layer_of[name] = layer
    unclassified = sorted(set(modules) - set(layer_of))
    classified_but_missing = sorted(set(layer_of) - set(modules))

    known = set(modules)
    top: dict[tuple[str, str], list[Edge]] = {}
    lazy: dict[tuple[str, str], list[Edge]] = {}
    unresolved: list[str] = []
    for name, (_path_on_disk, is_package) in sorted(modules.items()):
        edges, bad = imports_of(name, is_package, sources[name], known)
        unresolved.extend(bad)
        for edge in edges:
            (lazy if edge.lazy else top).setdefault((edge.importer, edge.target), []).append(edge)

    def layer(name: str) -> int | None:
        return layer_of.get(name)

    def upward(importer: str, target: str) -> bool:
        a, b = layer(importer), layer(target)
        return a is not None and b is not None and b > a

    def label(name: str) -> str:
        found = layer(name)
        return f"L{found}" if found is not None else "unclassified"

    top_upward = [
        f"{importer}:{edges[0].line} imports {target} at the top of the file ({label(importer)} {LAYER_NAMES.get(layer(importer), '?')}"
        f" -> {label(target)} {LAYER_NAMES.get(layer(target), '?')}): move the code, invert the dependency, or classify it differently"
        for (importer, target), edges in sorted(top.items())
        if upward(importer, target)
    ]

    top_graph = _adjacency(top)
    top_cycles = []
    for (importer, target), edges in sorted(top.items()):
        back = _path(top_graph, target, importer)
        if back is not None:
            top_cycles.append(f"{importer}:{edges[0].line} imports {target} at the top of the file, which already imports {_short(back)}")

    allowed = {(a, b) for a, b, _reason in allowlist}
    # The graph as it would be if every function-level import that is not listed moved to the top of its file. An upward one
    # can never be hoisted (the top-level rule forbids it), so it is left out whether or not it is listed.
    hoisted = _adjacency([*top, *(edge for edge in lazy if edge not in allowed and not upward(*edge))])

    unlisted_lazy = []
    for (importer, target), edges in sorted(lazy.items()):
        if (importer, target) in allowed:
            continue
        lines = ",".join(str(line) for line in sorted({edge.line for edge in edges}))
        if upward(importer, target):
            unlisted_lazy.append(
                f"{importer}:{lines} imports {target} inside a function and points upward ({label(importer)} -> {label(target)}): "
                "restructure it, or add an ALLOWLIST entry with the reason"
            )
            continue
        back = _path(hoisted, target, importer)
        if back is not None:
            unlisted_lazy.append(
                f"{importer}:{lines} imports {target} inside a function and closes a cycle ({_short([importer, *back])}): "
                "break the cycle, or add an ALLOWLIST entry with the reason"
            )

    stale = []
    seen: set[tuple[str, str]] = set()
    for importer, target, _reason in allowlist:
        pair = (importer, target)
        if pair in seen:
            stale.append(f"({importer}, {target}) is listed twice")
            continue
        seen.add(pair)
        for name in pair:
            if name not in modules:
                stale.append(f"({importer}, {target}): {name} is not a module any more")
        if any(name not in modules for name in pair):
            continue
        if pair in top and pair not in lazy:
            stale.append(f"({importer}, {target}) is now imported at the top of the file: delete the entry")
        elif pair not in lazy:
            stale.append(f"({importer}, {target}) is not imported inside a function any more: delete the entry")
        elif upward(importer, target):
            continue
        elif _path(hoisted, target, importer) is None:
            stale.append(
                f"({importer}, {target}) is no longer needed: it points down or sideways and closes no cycle, so hoist the import "
                "to the top of the file and delete the entry (if several entries guard one cycle, keep only one)"
            )
    return Analysis(unclassified, classified_but_missing, classified_twice, unresolved, top_upward, top_cycles, unlisted_lazy, stale)


def read_source(path: Path) -> str:
    """A module's text. utf-8-sig, because a file saved with a BOM (Windows PowerShell 5.1 writes one) is valid Python but ast.parse rejects it."""
    return path.read_text(encoding="utf-8-sig")


def leaf_violations(modules: dict[str, tuple[Path, bool]], sources: dict[str, str], leaves: Iterable[str]) -> list[str]:
    """First-party imports (top-level or function-level) made by modules that are documented to import nothing first-party."""
    known = frozenset(modules)
    found = []
    for name in sorted(leaves):
        if name not in modules:
            continue
        edges, unresolved = imports_of(name, modules[name][1], sources[name], known)
        found.extend(f"{name}:{edge.line} imports {edge.target}" for edge in edges)
        found.extend(unresolved)
    return found


def _real_analysis() -> Analysis:
    modules = discover_modules()
    sources = {name: read_source(path) for name, (path, _is_package) in modules.items()}
    return analyse(modules, sources, LAYER_MEMBERS, ALLOWLIST)


class LayerMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.analysis = _real_analysis()

    def assertNone(self, found, advice):
        self.assertEqual(found, [], f"{advice}\n  " + "\n  ".join(found))

    def test_every_module_is_classified_exactly_once(self):
        self.assertNone(self.analysis.unclassified, "add these modules to LAYER_MEMBERS in tests/test_layers.py (no default layer)")
        self.assertNone(self.analysis.classified_but_missing, "these classified modules no longer exist: remove them from LAYER_MEMBERS")
        self.assertNone(self.analysis.classified_twice, "a module belongs to one layer")

    def test_every_first_party_import_names_a_real_module(self):
        self.assertNone(self.analysis.unresolved, "these imports do not resolve to a module the layer test discovered")

    def test_top_level_imports_never_point_upward(self):
        self.assertNone(self.analysis.top_upward, "a module may import its own layer or a lower one at the top of the file")

    def test_top_level_imports_have_no_cycle(self):
        self.assertNone(self.analysis.top_cycles, "top-level imports must form a DAG")

    def test_upward_or_cyclic_function_level_imports_are_allowlisted(self):
        self.assertNone(self.analysis.unlisted_lazy, "a function-level import is allowed only when it guards a cycle or points upward")

    def test_allowlist_has_no_stale_entries(self):
        self.assertNone(self.analysis.stale_allowlist, "the allowlist only shrinks: delete entries that are no longer needed")

    def test_integrations_import_nothing_first_party(self):
        modules = discover_modules()
        sources = {name: read_source(path) for name, (path, _is_package) in modules.items()}
        self.assertNone(
            leaf_violations(modules, sources, LAYER_MEMBERS[2]),
            "L2 integrations are leaves; placing them below domain (see the module docstring) is only sound while they stay leaves",
        )

    def test_every_allowlist_entry_gives_a_reason(self):
        thin = [f"({a}, {b})" for a, b, reason in ALLOWLIST if len(reason.split()) < 3]
        self.assertNone(thin, "give each entry a one-line reason naming what the import is for")


class AnalysisBitesTests(unittest.TestCase):
    """The rules above are claims about code; these check the instrument on tiny synthetic packages, as the playbook asks."""

    LAYERS = {0: frozenset({"opportunity_app.low"}), 1: frozenset({"opportunity_app.mid"}), 2: frozenset({"opportunity_app.high"})}

    def run_rules(self, sources, layers=None, allowlist=()):
        modules = {f"opportunity_app.{name}": (Path(f"{name}.py"), False) for name in sources}
        return analyse(modules, {f"opportunity_app.{n}": text for n, text in sources.items()}, layers or self.LAYERS, allowlist)

    def clean(self, **overrides):
        sources = {"low": "import os\n", "mid": "from . import low\n", "high": "from .mid import x\n"}
        sources.update(overrides)
        return sources

    def test_a_clean_package_has_no_findings(self):
        self.assertEqual(self.run_rules(self.clean()), Analysis([], [], [], [], [], [], [], []))

    def test_new_upward_top_level_import_fails(self):
        found = self.run_rules(self.clean(low="from .high import y\n"))
        self.assertEqual(len(found.top_upward), 1)
        self.assertIn("opportunity_app.low:1 imports opportunity_app.high", found.top_upward[0])

    def test_upward_import_inside_a_class_body_try_block_and_if_is_still_top_level(self):
        for body in ("class A:\n    from .high import y\n", "try:\n    from .high import y\nexcept ImportError:\n    y = 0\n", "if True:\n    from .high import y\n"):
            with self.subTest(body=body):
                self.assertEqual(len(self.run_rules(self.clean(low=body)).top_upward), 1)

    def test_type_checking_imports_are_not_edges(self):
        found = self.run_rules(self.clean(low="from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .high import y\n"))
        self.assertEqual((found.top_upward, found.unlisted_lazy), ([], []))

    def test_unlisted_upward_function_level_import_fails_and_listing_it_passes(self):
        sources = self.clean(low="def f():\n    from .high import y\n")
        found = self.run_rules(sources)
        self.assertEqual(len(found.unlisted_lazy), 1)
        self.assertIn("points upward", found.unlisted_lazy[0])
        listed = self.run_rules(sources, allowlist=[("opportunity_app.low", "opportunity_app.high", "needs the high helper")])
        self.assertEqual((listed.unlisted_lazy, listed.stale_allowlist), ([], []))

    def test_every_kind_of_function_scope_counts_as_lazy(self):
        for body in (
            "def f():\n    from .high import y\n",
            "async def f():\n    from .high import y\n",
            "def g():\n    def inner():\n        from . import high\n",
            "class A:\n    def m(self):\n        from .high import y\n",
        ):
            with self.subTest(body=body):
                found = self.run_rules(self.clean(low=body))
                self.assertEqual((found.top_upward, len(found.unlisted_lazy)), ([], 1))

    def test_function_level_import_that_closes_a_same_layer_cycle_must_be_listed(self):
        layers = {0: frozenset({"opportunity_app.a", "opportunity_app.b"})}
        sources = {"a": "from .b import x\n", "b": "def f():\n    from .a import y\n"}
        found = self.run_rules(sources, layers)
        self.assertEqual(len(found.unlisted_lazy), 1)
        self.assertIn("closes a cycle", found.unlisted_lazy[0])
        listed = self.run_rules(sources, layers, [("opportunity_app.b", "opportunity_app.a", "b needs a at call time")])
        self.assertEqual((listed.unlisted_lazy, listed.stale_allowlist), ([], []))

    def test_two_lazy_imports_that_only_close_a_cycle_together_need_one_entry(self):
        layers = {0: frozenset({"opportunity_app.a", "opportunity_app.b"})}
        sources = {"a": "def f():\n    from .b import x\n", "b": "def g():\n    from .a import y\n"}
        found = self.run_rules(sources, layers)
        self.assertEqual(len(found.unlisted_lazy), 2)
        listed = self.run_rules(sources, layers, [("opportunity_app.a", "opportunity_app.b", "a needs b at call time")])
        self.assertEqual((listed.unlisted_lazy, listed.stale_allowlist), ([], []))

    def test_function_level_import_that_guards_nothing_needs_no_entry(self):
        found = self.run_rules(self.clean(high="def f():\n    from .low import y\n"))
        self.assertEqual((found.unlisted_lazy, found.stale_allowlist), ([], []))

    def test_allowlist_entry_for_an_import_that_guards_nothing_is_stale(self):
        sources = self.clean(high="def f():\n    from .low import y\n")
        found = self.run_rules(sources, allowlist=[("opportunity_app.high", "opportunity_app.low", "legacy habit")])
        self.assertEqual(len(found.stale_allowlist), 1)
        self.assertIn("no longer needed", found.stale_allowlist[0])

    def test_allowlist_entry_for_a_removed_or_hoisted_import_is_stale(self):
        entry = [("opportunity_app.mid", "opportunity_app.high", "was lazy once")]
        gone = self.run_rules(self.clean(), allowlist=entry)
        self.assertIn("not imported inside a function any more", gone.stale_allowlist[0])
        hoisted = self.run_rules(self.clean(mid="from .high import y\n"), allowlist=entry)
        self.assertEqual(len(hoisted.top_upward), 1)
        self.assertIn("now imported at the top of the file", hoisted.stale_allowlist[0])

    def test_allowlist_entry_for_a_module_that_no_longer_exists_is_stale(self):
        found = self.run_rules(self.clean(), allowlist=[("opportunity_app.mid", "opportunity_app.gone", "was there")])
        self.assertIn("is not a module any more", found.stale_allowlist[0])

    def test_duplicate_allowlist_entries_are_stale(self):
        sources = self.clean(low="def f():\n    from .high import y\n")
        entry = ("opportunity_app.low", "opportunity_app.high", "needs the high helper")
        found = self.run_rules(sources, allowlist=[entry, entry])
        self.assertEqual(len(found.stale_allowlist), 1)
        self.assertIn("listed twice", found.stale_allowlist[0])

    def test_unclassified_and_phantom_modules_fail(self):
        found = self.run_rules(self.clean(extra="x = 1\n"))
        self.assertEqual(found.unclassified, ["opportunity_app.extra"])
        layers = {**self.LAYERS, 2: self.LAYERS[2] | {"opportunity_app.ghost"}}
        self.assertEqual(self.run_rules(self.clean(), layers).classified_but_missing, ["opportunity_app.ghost"])

    def test_an_integration_that_imports_a_first_party_module_is_not_a_leaf(self):
        modules = {"opportunity_app.low": (Path("low.py"), False), "opportunity_app.mid": (Path("mid.py"), False)}
        sources = {"opportunity_app.low": "import os\n", "opportunity_app.mid": "def f():\n    from .low import x\n"}
        self.assertEqual(leaf_violations(modules, sources, ["opportunity_app.low"]), [])
        self.assertEqual(leaf_violations(modules, sources, ["opportunity_app.mid"]), ["opportunity_app.mid:2 imports opportunity_app.low"])

    def test_a_source_file_with_a_utf8_bom_is_read(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bom.py"
            path.write_bytes(b"\xef\xbb\xbfimport os\n")
            self.assertEqual(ast.parse(read_source(path)).body[0].names[0].name, "os")

    def test_module_in_two_layers_fails(self):
        layers = {**self.LAYERS, 2: self.LAYERS[2] | {"opportunity_app.low"}}
        self.assertEqual(len(self.run_rules(self.clean(), layers).classified_twice), 1)

    def test_top_level_cycle_fails_even_within_a_layer(self):
        layers = {0: frozenset({"opportunity_app.a", "opportunity_app.b"})}
        found = self.run_rules({"a": "from . import b\n", "b": "from . import a\n"}, layers)
        self.assertEqual(len(found.top_cycles), 2)

    def test_import_of_a_module_that_does_not_exist_is_reported(self):
        found = self.run_rules(self.clean(low="from .nowhere import y\nimport opportunity_app.other\nimport json\n"))
        self.assertEqual(len(found.unresolved), 2)

    def test_from_dot_import_a_non_module_name_is_an_edge_to_the_package(self):
        modules = {"opportunity_app": (Path("__init__.py"), True), "opportunity_app.low": (Path("low.py"), False)}
        edges, bad = imports_of("opportunity_app.low", False, "from . import ROOT\nfrom . import low as me\n", set(modules))
        self.assertEqual((bad, [(e.target, e.names) for e in edges]), ([], [("opportunity_app", ("ROOT",))]))

    def test_relative_imports_inside_a_nested_package_resolve_from_the_package(self):
        known = {"opportunity_app.web", "opportunity_app.web.deps", "opportunity_app.core", "opportunity_app.web.routers.x"}
        edges, bad = imports_of(
            "opportunity_app.web.routers.x", False, "from .. import deps\nfrom ... import core\nfrom ..deps import z\n", known
        )
        self.assertEqual(bad, [])
        self.assertEqual([e.target for e in edges], ["opportunity_app.web.deps", "opportunity_app.core", "opportunity_app.web.deps"])


class RealTreeShapeTests(unittest.TestCase):
    def test_discovery_finds_the_packages_and_pipeline_py(self):
        modules = discover_modules()
        for expected in ("pipeline", "pipeline_core", "pipeline_core.identity", "opportunity_app", "opportunity_app.api"):
            self.assertIn(expected, modules)
        self.assertTrue(modules["opportunity_app"][1] and modules["pipeline_core"][1])
        self.assertFalse(modules["opportunity_app.api"][1])

    def test_layers_are_numbered_without_gaps_and_named(self):
        self.assertEqual(sorted(LAYER_MEMBERS), sorted(LAYER_NAMES))
        self.assertEqual(sorted(LAYER_MEMBERS), list(range(len(LAYER_MEMBERS))))


if __name__ == "__main__":
    unittest.main()
