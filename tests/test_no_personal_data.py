"""No tracked file names the student this copy was first built for.

The repository is handed to other students, so identity, employers, and school
accounts belong only in gitignored files (config/profile.json,
config/sources.local.json, private/). The words are checked by hash so that
this test does not itself carry them.
"""

import hashlib
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_WORD_HASHES = {
    "8c0ea1395a4df0d5e57d70b88c5c6a35694754d4b951c0dd4e61cf4a0b06faf7",
    "5255930dc4cc36fced1710cab43a366835102fcd03aa38c79cae10acabbafb0e",
    "06d0ebc261250c3804e3677983751dcd1b8cf046ed11e7cc10a0ca517a9ffe78",
    "f7d34ce42c83fcc9a44b96b2c6254be59768520ca66ae237b916811fb4589e82",
    "d8427dd00cdc7ef42dd7fd0a6e2a8590bbcc3f24be9656923a95dd87d0eb3df6",
    "d10922fe8ed49b2cba56e713615587e8afb213621b540d4da45253b6db5f8386",
    "372ffaf8340f15a6abb32093e47d35b09ca90812cf8a6941a6071be5ac034d62",
    "5e9b2a28d3789417c540eb979830cb62c64329b9eaa0ec2c2872a62c0cc2bb41",
}
WORD = re.compile(r"[a-z0-9]+")
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".docx", ".woff", ".woff2", ".webm", ".zip"}


def tracked_files() -> list[Path]:
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        ).stdout.decode("utf-8")
    except (OSError, subprocess.CalledProcessError):
        return []
    return [ROOT / name for name in listing.split("\0") if name]


class NoPersonalDataTests(unittest.TestCase):
    def test_tracked_files_do_not_name_the_original_owner(self):
        files = tracked_files()
        if not files:
            self.skipTest("not a git checkout")
        hits = []
        for path in files:
            if path.suffix.lower() in BINARY_SUFFIXES or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if any(
                    hashlib.sha256(word.encode()).hexdigest() in FORBIDDEN_WORD_HASHES
                    for word in WORD.findall(line.lower())
                ):
                    hits.append(f"{path.relative_to(ROOT).as_posix()}:{number}")
        self.assertEqual(hits, [], "personal identifiers in tracked files; move them to a gitignored file")


if __name__ == "__main__":
    unittest.main()
