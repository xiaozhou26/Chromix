import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
TEXT_METRICS = REPO / "patches" / "0033-third_party-blink-renderer-core-html-canvas-text_metrics-cc.patch"
FONT_CACHE = REPO / "patches" / "0047-third_party-blink-renderer-platform-fonts-font_cache-cc.patch"
PACKAGE_WIN = REPO / "build" / "windows" / "package-win.ps1"


class FontCorrectnessRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.metrics = TEXT_METRICS.read_text(encoding="utf-8")
        cls.fonts = FONT_CACHE.read_text(encoding="utf-8")
        cls.package = PACKAGE_WIN.read_text(encoding="utf-8")

    def test_allowlist_preserves_multiword_family_names(self):
        self.assertIn("base::SplitString", self.fonts)
        self.assertIn("base::TRIM_WHITESPACE", self.fonts)
        self.assertIn("base::EqualsCaseInsensitiveASCII", self.fonts)
        self.assertNotIn("ph_c == ' '", self.fonts)
        self.assertNotIn("ph_norm", self.fonts)

    def test_filter_is_limited_to_native_family_lookup(self):
        self.assertIn("CreationType() == kCreateFontByFamily", self.fonts)
        self.assertIn("AlternateFontName::kLocalUniqueFace", self.fonts)
        self.assertIn("AlternateFontName::kLastResort", self.fonts)
        self.assertIn("UxrFontFamilyAllowed(creation_params.Family())", self.fonts)
        self.assertNotIn("UxrFontHidden(family)", self.fonts)

    def test_generics_keep_native_resolution(self):
        for family in ("serif", "sans-serif", "monospace", "system-ui", "emoji"):
            self.assertIn(f'"{family}"', self.fonts)

    def test_text_metrics_are_not_independently_jittered(self):
        self.assertNotIn("UxrJitterMetric", self.metrics)
        self.assertNotIn("uxr-canvas-seed", self.metrics)
        self.assertNotIn("base/uxr_config.h", self.metrics)
        self.assertIn("actual shaped and rendered font", self.metrics)

    def test_windows_package_does_not_install_or_register_clone_fonts(self):
        for forbidden in ("AddFontResource", "Fonts\\", "fonts.conf", "FONTCONFIG_FILE"):
            self.assertNotIn(forbidden, self.package)


if __name__ == "__main__":
    unittest.main()
