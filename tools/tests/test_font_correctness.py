import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
TEXT_METRICS = REPO / "patches" / "0033-third_party-blink-renderer-core-html-canvas-text_metrics-cc.patch"
FONT_CACHE = REPO / "patches" / "0047-third_party-blink-renderer-platform-fonts-font_cache-cc.patch"
PACKAGE_WIN = REPO / "build" / "windows" / "package-win.ps1"
PACKAGE_LINUX = REPO / "build" / "linux" / "package-linux.sh"
FONTS = REPO / "assets" / "fonts"
PY_FONTS = REPO / "sdk" / "python" / "chromix" / "_fonts.py"
NODE_FONTS = REPO / "sdk" / "node" / "_fonts.js"
NODE_PACKAGE = REPO / "sdk" / "node" / "package.json"


class FontCorrectnessRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.metrics = TEXT_METRICS.read_text(encoding="utf-8")
        cls.fonts = FONT_CACHE.read_text(encoding="utf-8")
        cls.package = PACKAGE_WIN.read_text(encoding="utf-8")
        cls.linux_package = PACKAGE_LINUX.read_text(encoding="utf-8")
        cls.py_fonts = PY_FONTS.read_text(encoding="utf-8")
        cls.node_fonts = NODE_FONTS.read_text(encoding="utf-8")
        cls.node_package = NODE_PACKAGE.read_text(encoding="utf-8")

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

    def test_linux_windows_persona_keeps_real_families_visible(self):
        self.assertIn("kBundledWindowsFamilies", self.fonts)
        for family in ("Arial Narrow", "MS Gothic", "Segoe UI Light", "Wingdings 3", "ＭＳ ゴシック"):
            self.assertIn(f'"{family}"', self.fonts)

    def test_generics_keep_native_resolution(self):
        for family in ("serif", "sans-serif", "monospace", "system-ui", "emoji"):
            self.assertIn(f'"{family}"', self.fonts)

    def test_text_metrics_are_not_independently_jittered(self):
        self.assertNotIn("UxrJitterMetric", self.metrics)
        self.assertNotIn("uxr-canvas-seed", self.metrics)
        self.assertNotIn("base/uxr_config.h", self.metrics)
        self.assertIn("actual shaped and rendered font", self.metrics)

    def test_windows_font_bundle_has_provenance_and_expected_formats(self):
        self.assertTrue((FONTS / "NOTICE").is_file())
        self.assertTrue((FONTS / "SOURCE.md").is_file())
        self.assertTrue((FONTS / "fonts.conf.template").is_file())
        font_files = [p for p in FONTS.iterdir() if p.is_file()]
        self.assertGreaterEqual(len(font_files), 150)
        self.assertTrue(any(p.suffix.lower() == ".ttc" for p in font_files))
        self.assertTrue(any(p.suffix.lower() == ".fon" for p in font_files))
        for family in ("Arial", "Calibri", "Cambria", "Consolas", "SegoeUI", "Tahoma", "TimesNewRoman", "Verdana"):
            self.assertTrue(any(FONTS.glob(f"{family}-*.ttf")), family)
        self.assertNotIn("FORTRESS-LICENSE", " ".join(p.name for p in font_files))
        self.assertNotIn("ATTRIBUTION.md", " ".join(p.name for p in font_files))

    def test_linux_package_bundles_supported_font_formats_and_launcher(self):
        for text in ("fonts.conf.template", "FONTCONFIG_FILE", "NOTICE", "SOURCE.md", "*.ttc"):
            self.assertIn(text, self.linux_package)
        self.assertIn("-iname '*.ttf'", self.linux_package)
        self.assertIn("-iname '*.ttc'", self.linux_package)
        self.assertNotIn("FORTRESS-LICENSE", self.linux_package)
        self.assertIn("exec \"$HERE/chrome\"", self.linux_package)

    def test_windows_package_does_not_install_or_register_clone_fonts(self):
        for forbidden in ("AddFontResource", "Fonts\\", "fonts.conf", "FONTCONFIG_FILE"):
            self.assertNotIn(forbidden, self.package)

    def test_sdk_font_wiring_is_linux_only_and_caller_env_wins(self):
        for source in (self.py_fonts, self.node_fonts):
            self.assertIn('sys.platform != "linux"' if source == self.py_fonts else 'process.platform !== "linux"', source)
            self.assertIn("FONTCONFIG_FILE", source)
            self.assertIn("user_env" if source == self.py_fonts else "userEnv", source)
        self.assertIn('"_fonts.js"', self.node_package)


if __name__ == "__main__":
    unittest.main()
