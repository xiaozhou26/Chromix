import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
PACKAGE_WIN = REPO / "build" / "windows" / "package-win.ps1"


class PackageWinRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = PACKAGE_WIN.read_text(encoding="utf-8")

    def test_scans_chrome_dll_before_packaging(self):
        source = self.source
        self.assertIn('$dll = Join-Path $Bundle "chrome.dll"', source)
        self.assertIn('[IO.File]::ReadAllBytes($dll)', source)
        self.assertIn('[Text.Encoding]::ASCII.GetString($bytes)', source)
        self.assertIn('[Text.Encoding]::Unicode.GetString($bytes)', source)
        self.assertIn(
            'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)',
            source,
        )
        self.assertIn('throw "chrome.dll contains forbidden WebGL identity marker', source)

    def test_requires_current_persona_markers(self):
        source = self.source
        self.assertIn('Google Inc. (Intel)', source)
        self.assertIn('ANGLE (Intel, Intel(R) UHD Graphics 770', source)
        self.assertIn('uxr-webgl-vendor', source)
        self.assertIn('uxr-webgl-renderer', source)
        self.assertIn('chrome.dll WebGL persona marker scan passed', source)


if __name__ == "__main__":
    unittest.main()
