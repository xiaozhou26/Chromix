"""Strict PGO preservation without changing normal GN overlay precedence."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools import merge_gn_args


REPO = Path(__file__).resolve().parents[2]


class MergeGnArgsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="merge GN PGO ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.donor = self.root / "donor.gn"
        self.overlay = self.root / "overlay.gn"
        self.output = self.root / "args.gn"
        self.donor.write_text('# donor\nchrome_pgo_phase=0\n# Windows override\n'
                              'chrome_pgo_phase = 2 # optimized\nhost_cpu="x64"\nsymbol_level=2\n')
        self.overlay.write_text('chrome_pgo_phase = 0\nsymbol_level = 0\n')

    def merge(self, *, preserve=True, profile=None, donor=None):
        donor = donor or self.donor
        command = [sys.executable, str(REPO / "tools/merge_gn_args.py"), str(self.output)]
        if preserve:
            command += ["--preserve-pgo-from", str(donor)]
        if profile:
            command += ["--build-profile", profile]
        command += [str(donor), str(self.overlay)]
        return subprocess.run(command, capture_output=True, text=True, timeout=10)

    def test_preserves_last_donor_pgo_and_comments_without_other_donor_overrides(self):
        result = self.merge()
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.output.read_text()
        self.assertIn('# Windows override\nchrome_pgo_phase = 2 # optimized\n', output)
        self.assertEqual(output.count('chrome_pgo_phase'), 1)
        self.assertIn('symbol_level = 0\n', output)
        self.assertIn('host_cpu="x64"\n', output)

    def test_all_valid_pgo_phases_are_preserved_without_guessing(self):
        for value in (0, 1, 2):
            with self.subTest(value=value):
                self.donor.write_text(f'chrome_pgo_phase = {value}\n')
                result = self.merge()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'chrome_pgo_phase = {value}\n', self.output.read_text())

    def test_normal_merge_still_uses_last_file(self):
        result = self.merge(preserve=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('chrome_pgo_phase = 0\n', self.output.read_text())
        self.assertNotIn('# Windows override', self.output.read_text())

    def test_build_profiles_do_not_override_preserved_pgo(self):
        for profile, expected in (('fast', 'false'), ('release', 'true')):
            with self.subTest(profile=profile):
                result = self.merge(profile=profile)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('chrome_pgo_phase = 2 # optimized\n', self.output.read_text())
                self.assertIn(f'thin_lto_enable_optimizations = {expected}\n', self.output.read_text())

    def test_in_place_resume_preserves_bytes_and_mtime(self):
        first = self.merge()
        self.assertEqual(first.returncode, 0, first.stderr)
        expected = self.output.read_bytes()
        timestamp = 1_700_000_000_000_000_000
        os.utime(self.output, ns=(timestamp, timestamp))
        resumed = self.merge(donor=self.output)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(self.output.read_bytes(), expected)
        self.assertEqual(self.output.stat().st_mtime_ns, timestamp)
        self.assertIn('unchanged', resumed.stdout)

    def test_missing_and_malformed_pgo_fail_without_mutating_output(self):
        self.output.write_text('sentinel output\n')
        for text in ('', '# chrome_pgo_phase=2\n', 'chrome_pgo_phase=\n',
                     'chrome_pgo_phase=3\n', 'chrome_pgo_phase=-1\n', 'chrome_pgo_phase=true\n',
                     'chrome_pgo_phase="2"\n', 'chrome_pgo_phase=[2]\n', 'chrome_pgo_phase=2.0\n',
                     'chrome_pgo_phase=02\n', 'chrome_pgo_phase=2 + 0\n',
                     'chrome_pgo_phase=getenv("PGO")\n', 'chrome_pgo_phase=2; is_debug=true\n',
                     'chrome_pgo_phase=bad\nchrome_pgo_phase=2\n'):
            with self.subTest(text=text):
                self.donor.write_text(text)
                result = self.merge()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('chrome_pgo_phase', result.stderr)
                self.assertEqual(self.output.read_text(), 'sentinel output\n')
        self.donor.unlink()
        missing = self.merge()
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(self.output.read_text(), 'sentinel output\n')

    def test_parser_default_remains_last_assignment_wins(self):
        self.donor.write_text('chrome_pgo_phase=expression\nchrome_pgo_phase=2\n')
        _, values = merge_gn_args.parse(self.donor)
        self.assertEqual(values['chrome_pgo_phase'], 'chrome_pgo_phase=2')
        with self.assertRaisesRegex(ValueError, 'literal'):
            merge_gn_args.parse(self.donor, require_pgo=True)


if __name__ == '__main__':
    unittest.main()
