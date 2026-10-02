"""Source-text helpers for tests that read shipped code as text.

A negative source assertion ("this phrase appears nowhere in the shipped script") is only meaningful while it reads every
file the code could live in. Reading one hard-coded file turns it into a silent no-op the day code moves to a new file. These
helpers make the scan cover a whole directory, so a split or a move cannot hollow the guard out.

Nothing here is named test*, so pytest never collects it.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "opportunity_app" / "static"
EXTENSION_DIR = ROOT / "apps" / "extension"
PACKAGE_DIR = ROOT / "opportunity_app"


def read_all(directory, *patterns, exclude=()):
    """Return {posix path relative to `directory`: text} for every file matching any glob pattern, recursively.

    `exclude` holds relative posix paths (or a path prefix ending in "/") to leave out. Raises if nothing matched, so a scan
    that finds no files fails loudly and never passes vacuously.
    """
    directory = Path(directory)
    found = {}
    for pattern in patterns:
        for path in sorted(directory.rglob(pattern)):
            if not path.is_file():
                continue
            relative = path.relative_to(directory).as_posix()
            if any(relative == item or (item.endswith("/") and relative.startswith(item)) for item in exclude):
                continue
            found[relative] = path.read_text(encoding="utf-8")
    if not found:
        raise AssertionError(f"no files matching {patterns} under {directory}: the guard would scan nothing")
    return found


def static_scripts():
    """Every shipped browser script: opportunity_app/static/**/*.js."""
    return read_all(STATIC_DIR, "*.js")


def static_pages():
    """Every shipped browser page and script: opportunity_app/static/**/*.js and *.html."""
    return read_all(STATIC_DIR, "*.js", "*.html")


def static_script_text():
    """All shipped scripts joined into one string, for assertions that only need to know the text is somewhere."""
    return "\n".join(static_scripts().values())


def python_modules(*patterns, exclude=()):
    """{relative posix path: text} for Python files under opportunity_app/ matching the glob patterns."""
    return read_all(PACKAGE_DIR, *patterns, exclude=exclude)


def _skip_string(text, i, quote):
    """Return the index just past the string or template literal that opens at text[i]."""
    n = len(text)
    i += 1
    while i < n:
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == quote:
            return i + 1
        if quote == "`" and char == "$" and text[i + 1:i + 2] == "{":
            i = _skip_block(text, i + 1)
            continue
        i += 1
    raise ValueError("unterminated string")


def _skip_block(text, i):
    """Return the index just past the balanced {...} that opens at text[i], ignoring braces in strings, comments and regexes."""
    assert text[i] == "{"
    n = len(text)
    depth = 0
    last = ""  # the last significant character, to tell a regex literal from a division sign
    while i < n:
        char = text[i]
        if char in "'\"`":
            i = _skip_string(text, i, char)
            last = "x"
            continue
        if char == "/" and text[i + 1:i + 2] == "/":
            i = text.find("\n", i)
            if i < 0:
                break
            continue
        if char == "/" and text[i + 1:i + 2] == "*":
            i = text.index("*/", i + 2) + 2
            continue
        if char == "/" and last in "(,=:[!&|?{};":
            i += 1
            in_class = False
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == "[":
                    in_class = True
                elif text[i] == "]":
                    in_class = False
                elif text[i] == "/" and not in_class:
                    break
                i += 1
            i += 1
            last = "x"
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        if not char.isspace():
            last = char
        i += 1
    raise ValueError("unbalanced braces")


def is_apply_module(relative, exclude_store=False):
    """Whether the posix path `relative` (from opportunity_app/) is an Apply-for-me module, at any depth.

    That is a file named `apply*.py` or any file inside a directory named `apply*`, wherever it sits: opportunity_app/apply_x.py,
    opportunity_app/apply/x.py, opportunity_app/routers/apply_x.py or opportunity_app/routers/apply/x.py. `exclude_store` leaves
    out the sensitive-answer store's own module: apply/sensitive.py, an apply_sensitive/ package, or apply*/sensitive.py.
    """
    parts = relative.split("/")
    if not any(part.startswith("apply") for part in parts):
        return False
    if exclude_store:
        stems = [part[:-3] if part.endswith(".py") else part for part in parts]
        if "apply_sensitive" in stems:
            return False
        if any(before.startswith("apply") and after == "sensitive" for before, after in zip(stems, stems[1:])):
            return False
    return True


def apply_modules(exclude_store=False):
    """{relative posix path: text} for every Apply-for-me module under opportunity_app/ (see is_apply_module).

    The browser extension's server half, applications/extension.py, is not part of it on purpose: its label-pattern regexes are the very
    thing the agent's policy must not copy.
    """
    modules = {relative: text for relative, text in python_modules("*.py").items() if is_apply_module(relative, exclude_store)}
    if not modules:
        raise AssertionError("no apply modules found under opportunity_app/")
    return modules


def js_function(text, name):
    """Return the full source of `function <name>(...) {...}`, found by name and cut by brace matching.

    Position independent: it does not care where the function sits in the file or how long it is. Raises AssertionError when the
    function is not defined in `text`, so a moved function fails loudly and never reads an empty string.
    """
    marker = f"function {name}("
    start = text.find(marker)
    if start < 0:
        raise AssertionError(f"{marker!r} not found; if it moved, point the test at the file that holds it")
    # The parameter list may hold default values with braces (`context = {}`), so find the body brace after the closing paren.
    depth = 0
    i = start + len(marker) - 1
    while True:
        char = text[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                break
        elif char in "'\"`":
            i = _skip_string(text, i, char)
            continue
        i += 1
    brace = text.index("{", i)
    return text[start:_skip_block(text, brace)]
