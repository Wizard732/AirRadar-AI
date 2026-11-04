"""Доступність нової карти (miniapp/map2): контраст текстових токенів, reduced motion, safe-area, цілі дотику у CSS."""
import re
import unittest
from pathlib import Path

CSS = (Path(__file__).parent / "miniapp" / "map2" / "map2.css").read_text(encoding="utf-8")


def _tokens(block: str) -> dict:
    return {k: v.lower() for k, v in re.findall(r"--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})", block)}


def _lum(h: str) -> float:
    c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _ratio(a: str, b: str) -> float:
    x, y = _lum(a), _lum(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


DARK = _tokens(re.search(r":root\{(.*?)\}", CSS, re.S).group(1))
LIGHT = {**DARK, **_tokens(re.search(r'prefers-color-scheme:light\)\{\s*:root\[data-theme="auto"\]\{(.*?)\}', CSS, re.S).group(1))}
TEXT_TOKENS = ("fg", "muted", "faint", "alert", "hint", "ok", "nodata", "focus")


class Map2A11yTests(unittest.TestCase):
    def test_text_tokens_meet_wcag_aa_on_surfaces(self):
        for name, t in (("dark", DARK), ("light", LIGHT)):
            for surf in ("surface", "surface-2"):
                for tok in TEXT_TOKENS:
                    self.assertGreaterEqual(_ratio(t[tok], t[surf]), 4.5, f"{name}: {tok} на {surf} = {_ratio(t[tok], t[surf]):.2f}")

    def test_reduced_motion_and_focus_visible(self):
        self.assertIn("prefers-reduced-motion:reduce", CSS)
        self.assertIn(":focus-visible", CSS)

    def test_safe_area_variables_used(self):
        for v in ("--sat", "--sab", "--sal", "--sar"):
            self.assertIn(v, CSS)
        self.assertNotIn("env(safe-area-inset-top,0px));display:flex", CSS)   # верхня смуга бере --sat, щоб Telegram міг її перекрити

    def test_touch_targets_on_mobile(self):
        self.assertRegex(CSS, r"\.chip\{min-height:44px")
        self.assertRegex(CSS, r"\.card \.x\{width:44px;height:44px")


if __name__ == "__main__":
    unittest.main()
