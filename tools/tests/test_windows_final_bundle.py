"""Exercise final Windows bundle validation with tiny ZIPs and mocked browser calls."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

REPO = Path(__file__).resolve().parents[2]
STAGE = REPO / "build/windows/ci-stage.ps1"


class WindowsFinalBundleTest(unittest.TestCase):
    def setUp(self):
        self.pwsh = shutil.which("pwsh") or "/opt/pwsh/pwsh"
        if not Path(self.pwsh).is_file():
            self.skipTest("PowerShell is unavailable")
        temp = tempfile.TemporaryDirectory(prefix="windows bundle ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.dist = self.root / "dist"
        self.dist.mkdir()
        self.asset = self.dist / "chromix-win-x64.zip"
        self.make_archive()
        source = STAGE.read_text()
        start = source.index("function Verify-FinalBundle {")
        end = source.index('\nWrite-Host "==> Chromix CI stage', start)
        self.script = self.root / "verify.ps1"
        self.script.write_text(r'''
$ErrorActionPreference = "Stop"
$Root = $env:TEST_ROOT
$Revisions = @{ ChromiumVersion = "152.0.7977.82" }
function Get-Item {
  param($LiteralPath)
  if ($LiteralPath -in @((Join-Path $Root "smoke/chromix/chrome.exe"),
                         (Join-Path $Root "smoke/chromix/chrome.dll"))) {
    $name = Split-Path $LiteralPath -Leaf
    Write-Host "MOCK_VERSION:$name"
    $major = if ($env:TEST_BAD_VERSION -eq $name) { 1 } else { 152 }
    if ($env:TEST_NO_VERSION -eq $name) { return [pscustomobject]@{ VersionInfo = $null } }
    return [pscustomobject]@{ VersionInfo = [pscustomobject]@{
      ProductMajorPart = $major; ProductMinorPart = 0; ProductBuildPart = 7977; ProductPrivatePart = 82
    } }
  }
  Microsoft.PowerShell.Management\Get-Item @PSBoundParameters
}
function Invoke-BoundedBrowser {
  param($Launcher, $Arguments, $WorkingDirectory, $TimeoutSec)
  if ($Launcher -ne (Join-Path $Root "smoke/chromix/chromix.cmd")) { throw "wrong extracted launcher" }
  if ($WorkingDirectory -ne (Join-Path $Root "smoke/chromix")) { throw "wrong extracted working directory" }
  if (-not (Test-Path (Join-Path $WorkingDirectory "chrome.exe"))) { throw "missing extracted executable" }
  if ($Arguments -contains "--version") { throw "Windows does not implement the POSIX version handler" }
  if ($TimeoutSec -ne 60 -or $Arguments -notcontains "data:text/html,<p>chromix-smoke-ok</p>" -or
      $Arguments -notcontains "--dump-dom") { throw "invalid DOM check" }
  Write-Host "MOCK_DOM"
  if ($env:TEST_BAD_DOM -eq "1") { return "<p>wrong page</p>" }
  return "<p>chromix-smoke-ok</p>"
}
function Invoke-FingerprintAcceptance {
  param($Browser)
  if ($Browser -ne (Join-Path $Root "smoke/chromix/chrome.exe")) { throw "wrong audit executable" }
  Write-Host "MOCK_FINGERPRINT"
  if ($env:TEST_BAD_FINGERPRINT -eq "1") { throw "fingerprint acceptance failed" }
}
''' + source[start:end] + "\nVerify-FinalBundle\n")

    def make_archive(self, missing=None, versioned_dll=None):
        with zipfile.ZipFile(self.asset, "w") as archive:
            for name in ("chromix.cmd", "chrome.exe", "chrome.dll"):
                if name != missing:
                    archive.writestr("chromix/" + name, "tiny fixture, never executed")
            if versioned_dll is not None:
                archive.writestr("chromix/152.0.7977.82/chrome.dll", versioned_dll)
        self.digest = hashlib.sha256(self.asset.read_bytes()).hexdigest()
        self.manifest = self.dist / "SHA256SUMS"
        self.manifest.write_text(f"{self.digest}  chromix-win-x64.zip\n")

    def verify(self, **env):
        return subprocess.run([self.pwsh, "-NoProfile", "-NonInteractive", "-File", str(self.script)],
                              env={**os.environ, "TEST_ROOT": str(self.root), **env},
                              capture_output=True, text=True, timeout=20)

    def test_single_entry_is_a_full_hash_and_reaches_version_and_browser_checks(self):
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("checksum verified: " + self.digest, result.stdout)
        self.assertIn("MOCK_VERSION:chrome.exe", result.stdout)
        self.assertIn("MOCK_VERSION:chrome.dll", result.stdout)
        self.assertEqual(result.stdout.count("MOCK_DOM"), 1)
        self.assertLess(result.stdout.index("MOCK_VERSION:chrome.dll"), result.stdout.index("MOCK_DOM"))
        self.assertIn("headless smoke checks passed", result.stdout)
        self.assertLess(result.stdout.index('MOCK_DOM'), result.stdout.index('MOCK_FINGERPRINT'))

    def test_fingerprint_failure_is_not_hidden_by_startup_smoke(self):
        result = self.verify(TEST_BAD_FINGERPRINT='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('MOCK_DOM', result.stdout)
        self.assertIn('fingerprint acceptance failed', result.stderr)

    def test_missing_duplicate_and_mismatched_entries_stop_before_extraction(self):
        for value, error in (("", "no unique"),
                             (self.manifest.read_text() * 2, "no unique"),
                             ("0" * 64 + "  chromix-win-x64.zip\n", "checksum mismatch")):
            with self.subTest(value=value):
                self.manifest.write_text(value)
                result = self.verify()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)
                self.assertNotIn("MOCK_VERSION", result.stdout)
                self.assertFalse((self.root / "smoke").exists())

    def test_uppercase_digest_and_unrelated_manifest_entry_are_supported(self):
        self.manifest.write_text("0" * 64 + "  other.zip\n" + self.digest.upper() + "  chromix-win-x64.zip\n")
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_launcher_or_binary_fails_before_version_and_browser_checks(self):
        for name in ("chromix.cmd", "chrome.exe", "chrome.dll"):
            with self.subTest(name=name):
                self.make_archive(missing=name)
                result = self.verify()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("extracted Windows bundle is missing", result.stderr)
                self.assertNotIn("MOCK_VERSION", result.stdout)
                self.assertNotIn("MOCK_DOM", result.stdout)

    def test_versioned_dll_must_match_the_newly_linked_portable_dll(self):
        self.make_archive(versioned_dll="tiny fixture, never executed")
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("MOCK_DOM", result.stdout)
        self.make_archive(versioned_dll="stale upstream browser")
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("versioned Windows DLL differs", result.stderr)
        self.assertNotIn("MOCK_DOM", result.stdout)

    def test_version_and_render_failures_are_not_reported_success(self):
        for env, error in (({"TEST_BAD_VERSION": "chrome.exe"}, "does not match"),
                           ({"TEST_BAD_VERSION": "chrome.dll"}, "does not match"),
                           ({"TEST_NO_VERSION": "chrome.exe"}, "does not match"),
                           ({"TEST_NO_VERSION": "chrome.dll"}, "does not match"),
                           ({"TEST_BAD_DOM": "1"}, "did not render")):
            with self.subTest(env=env):
                result = self.verify(**env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)
                self.assertNotIn("headless smoke checks passed", result.stdout)
                if "TEST_BAD_DOM" not in env:
                    self.assertNotIn("MOCK_DOM", result.stdout)
