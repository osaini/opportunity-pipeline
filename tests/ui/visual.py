"""A small screenshot-baseline comparator.

Playwright's ``toHaveScreenshot`` ships only with the JavaScript test runner, so
the Python bindings need this. It is deliberately tiny: Pillow is already a
project dependency, and the comparison it performs — same dimensions, and fewer
than N per-mille of pixels differing beyond a per-channel tolerance — is all a
layout guard needs.

Baselines are platform-specific. Fonts and text rasterisation differ enough
between Windows and the Ubuntu CI runner that a shared baseline would fail
permanently, so baselines are stored per platform tag and the CI job treats a
missing baseline as a skip rather than a failure.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops

BASELINE_ROOT = Path(__file__).resolve().parent / "baselines"

# A pixel counts as changed when any channel moves more than this. Anti-aliasing
# and subpixel text rendering routinely move a channel by a few units.
CHANNEL_TOLERANCE = 12
# Fraction of the image allowed to differ before the comparison fails.
MAX_CHANGED_FRACTION = 0.002


def platform_tag() -> str:
    return f"{platform.system().lower()}-{platform.machine().lower()}"


@dataclass(frozen=True)
class Comparison:
    changed_pixels: int
    total_pixels: int
    diff_path: Path | None

    @property
    def fraction(self) -> float:
        return self.changed_pixels / self.total_pixels if self.total_pixels else 0.0

    @property
    def within_tolerance(self) -> bool:
        return self.fraction <= MAX_CHANGED_FRACTION


def baseline_path(name: str) -> Path:
    return BASELINE_ROOT / platform_tag() / f"{name}.png"


def write_baseline(name: str, image_bytes: bytes) -> Path:
    target = baseline_path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(image_bytes)
    return target


def compare(name: str, image_bytes: bytes, artifact_dir: Path) -> Comparison:
    """Compare a fresh screenshot against its stored baseline."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    actual_path = artifact_dir / f"{name}.actual.png"
    actual_path.write_bytes(image_bytes)

    with Image.open(baseline_path(name)) as baseline_image, Image.open(actual_path) as actual_image:
        baseline = baseline_image.convert("RGB")
        actual = actual_image.convert("RGB")

        if baseline.size != actual.size:
            raise AssertionError(
                f"{name}: the layout changed size — baseline {baseline.size}, actual {actual.size}"
            )

        difference = ImageChops.difference(baseline, actual)
        # Collapse the three channels to the largest per-pixel deviation, then
        # threshold. point() on a mono image is far cheaper than iterating pixels.
        largest = difference.convert("L").point(lambda value: 255 if value > CHANNEL_TOLERANCE else 0)
        changed = sum(largest.histogram()[255:])
        total = baseline.size[0] * baseline.size[1]

        diff_path = None
        if changed:
            diff_path = artifact_dir / f"{name}.diff.png"
            ImageChops.difference(baseline, actual).save(diff_path)

    return Comparison(changed_pixels=changed, total_pixels=total, diff_path=diff_path)
