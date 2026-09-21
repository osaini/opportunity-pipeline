"""Guard the legacy pipeline's dependency boundary described in AGENTS.md rule 4.

pipeline.py and pipeline_core/ must run with no third-party packages. Two imports
outside the standard library are allowed, both deliberate: the stdlib-only
opportunity_app.opportunity_metadata module (imported lazily for deadline
parsing), and Playwright, optional for PDF export and only inside an
``except ImportError`` guard.
"""

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIRST_PARTY = {"pipeline", "pipeline_core"}
# Modules outside the stdlib that the legacy pipeline may import, each of which
# must itself be stdlib-only (checked below).
STDLIB_ONLY_PRODUCT_MODULES = {"opportunity_app.opportunity_metadata"}
OPTIONAL_THIRD_PARTY = {"playwright"}


def _imports(path: Path) -> list[tuple[str, bool]]:
    """(module, guarded_by_ImportError) for every absolute import in a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(
            isinstance(handler.type, ast.Name) and handler.type.id in {"ImportError", "ModuleNotFoundError"}
            for handler in node.handlers
        ):
            for child in node.body:
                guarded.update(id(inner) for inner in ast.walk(child))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, id(node) in guarded) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.module, id(node) in guarded))
    return found


def _is_stdlib(module: str) -> bool:
    return module.split(".")[0] in sys.stdlib_module_names or module == "__future__"


class LegacyPipelineDependencyBoundaryTests(unittest.TestCase):
    def test_pipeline_and_core_import_only_the_allowed_modules(self):
        files = [ROOT / "pipeline.py", *sorted((ROOT / "pipeline_core").glob("*.py"))]
        for path in files:
            for module, guarded in _imports(path):
                with self.subTest(file=path.name, module=module):
                    top = module.split(".")[0]
                    if _is_stdlib(module) or top in FIRST_PARTY or module in STDLIB_ONLY_PRODUCT_MODULES:
                        continue
                    self.assertIn(top, OPTIONAL_THIRD_PARTY, f"{path.name} imports {module}")
                    self.assertTrue(guarded, f"{path.name} imports optional {module} without an ImportError guard")

    def test_product_modules_the_pipeline_imports_are_stdlib_only(self):
        for dotted in STDLIB_ONLY_PRODUCT_MODULES:
            path = ROOT.joinpath(*dotted.split(".")).with_suffix(".py")
            package_init = path.parent / "__init__.py"
            for source in (path, package_init):
                for module, _guarded in _imports(source):
                    with self.subTest(file=str(source.relative_to(ROOT)), module=module):
                        self.assertTrue(_is_stdlib(module), f"{source.name} imports non-stdlib {module}")


if __name__ == "__main__":
    unittest.main()
