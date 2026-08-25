from pathlib import Path
import re
import unittest


REPO = Path(__file__).resolve().parents[2]
CI_STAGE = REPO / "build" / "windows" / "ci-stage.ps1"


def invoke_tracked_source() -> str:
    script = CI_STAGE.read_text(encoding="utf-8")
    start = script.index("function Invoke-Tracked {")
    end = script.index("function Get-FreeGB", start)
    return script[start:end]


def validate_only_source() -> str:
    script = CI_STAGE.read_text(encoding="utf-8")
    start = script.index("if ($ValidateOnly) {")
    end = script.index("$ninjaBudget =", start)
    return script[start:end]


class InvokeTrackedRegressionTest(unittest.TestCase):
    def test_waits_before_reading_natural_exit_code(self):
        source = invoke_tracked_source()
        self.assertEqual(source.count("$process.WaitForExit()"), 2)
        self.assertRegex(
            source,
            r"\n  \}\n  \$process\.WaitForExit\(\)\n  \$code = \$process\.ExitCode\n",
        )

    def test_failure_output_keeps_long_stdout_tail_and_full_stderr(self):
        source = invoke_tracked_source()
        stdout_tails = re.findall(r"Get-Content \$log -Tail (\d+)", source)
        self.assertTrue(stdout_tails)
        self.assertGreaterEqual(max(map(int, stdout_tails)), 100)
        self.assertIn("Get-Content $err | ForEach-Object", source)
        self.assertNotRegex(source, r"Get-Content \$err -Tail \d+")

    def test_can_emit_complete_stdout_on_failure(self):
        source = invoke_tracked_source()
        self.assertIn("[switch]$FullFailureOutput", source)
        self.assertRegex(
            source,
            r"if \(\$FullFailureOutput\) \{\s+Write-Host "
            r'"==> tracked process stdout \(complete\)"\s+'
            r"Get-Content \$log \| ForEach-Object",
        )

    def test_reports_whether_exit_code_is_null(self):
        source = invoke_tracked_source()
        self.assertIn(
            'Write-Host "==> tracked process exit code is null after WaitForExit; '
            'treating as failure"',
            source,
        )
        self.assertIn('Write-Host "==> tracked process exit code: $code"', source)


class ValidateOnlyRegressionTest(unittest.TestCase):
    def test_serial_verbose_ninja_preserves_failures_and_full_output(self):
        source = validate_only_source()
        self.assertIn(
            '-ArgList "-C `"$OutDir`" -j 1 -v '
            'gen/v8/torque-generated/bit-field-asserts.cc"',
            source,
        )
        self.assertIn("-FullFailureOutput", source)
        self.assertRegex(
            source,
            r'if \(\$validationRc -ne 0\) \{ throw "V8 Torque validation failed '
            r'\(exit \$validationRc\)" \}',
        )
        self.assertNotIn("Test-Path", source)


if __name__ == "__main__":
    unittest.main()
