# Third-party notices

This project has no runtime dependencies. It does, however, contain logic ported
from other open-source projects. Those portions remain under their original
licences, reproduced below.

---

## career-ops

- **Source:** <https://github.com/santifer/career-ops>
- **Copyright:** © 2026 Santiago Fernández de Valderrama
- **Licence:** MIT

Ported into `pipeline.py` (transliterated from JavaScript to Python, with the
upstream explanatory comments retained because they document the real-world
failures each guard exists to prevent):

| Upstream file | Ported as | What it does |
| --- | --- | --- |
| `liveness-core.mjs` | `normalize_for_match`, the `_HARD_EXPIRED_PATTERNS` / `_BOT_CHALLENGE_PATTERNS` / `_APPLY_PATTERNS` sets, and `classify_liveness` | Decides whether a job posting page is still open |
| `fingerprint-core.mjs` | `normalize_jd_text`, `fingerprint_text`, `fingerprint_similarity` | 64-bit SimHash fingerprinting of description text for cross-source duplicate detection |

Behavioural changes made during the port are noted in comments at each site. The
one structural difference worth recording here: upstream's "has been filled"
pattern uses a variable-width negative lookbehind, which Python's `re` module
does not support. It is expressed here as two fixed-width lookbehinds
(`(?<!application\s)(?<!form\s)`), which is equivalent for the cases the
upstream pattern was written to handle.

### MIT License

```
MIT License

Copyright (c) 2026 Santiago Fernández de Valderrama

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
