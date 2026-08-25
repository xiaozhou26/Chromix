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
        stdout_tail = re.search(
            r"if \(Test-Path \$log\) \{ Get-Content \$log -Tail (\d+)", source
        )
        self.assertIsNotNone(stdout_tail)
        self.assertGreaterEqual(int(stdout_tail.group(1)), 100)
        self.assertIn("Get-Content $err | ForEach-Object", source)
        self.assertNotRegex(source, r"Get-Content \$err -Tail \d+")


if __name__ == "__main__":
    unittest.main()
