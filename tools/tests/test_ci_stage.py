import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
CI_STAGE = REPO / "build" / "windows" / "ci-stage.ps1"
WORKFLOW = REPO / ".github" / "workflows" / "build-win-x64-github.yml"
RESTORED_SOURCE_UPDATE = REPO / "build" / "windows" / "update-restored-source.ps1"


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
    def test_uses_cmd_wrapper_status_instead_of_process_exit_code(self):
        source = invoke_tracked_source()
        self.assertIn(
            '$wrapperName = "ci-tracked-$PID-$([Guid]::NewGuid().ToString(\'N\'))"',
            source,
        )
        self.assertIn('$wrapper = Join-Path $env:TEMP "$wrapperName.cmd"', source)
        self.assertIn('$status = Join-Path $env:TEMP "$wrapperName.exit"', source)
        self.assertRegex(
            source,
            r'"`"\$cmdFile`" \$cmdArgs",\s+'
            r"'set \"ci_tracked_exit=%ERRORLEVEL%\"',\s+"
            r'">`"\$cmdStatus`" echo %ci_tracked_exit%",\s+'
            r'"exit /b %ci_tracked_exit%"',
        )
        self.assertNotIn("$process.ExitCode", source)

    def test_cleans_old_tracking_files_and_quotes_wrapper_path(self):
        source = invoke_tracked_source()
        self.assertIn(
            "Remove-Item $log, $err, $wrapper, $status -ErrorAction SilentlyContinue",
            source,
        )
        self.assertIn('$File.Replace("%", "%%")', source)
        self.assertIn('$ArgList.Replace("%", "%%")', source)
        self.assertIn('$status.Replace("%", "%%")', source)
        self.assertRegex(
            source,
            r'Start-Process -FilePath \$env:COMSPEC\s+`\n'
            r'\s+-ArgumentList "/d /s /c `"`"\$wrapper`"`""',
        )

    def test_waits_before_strictly_parsing_status_file(self):
        source = invoke_tracked_source()
        self.assertEqual(source.count("$process.WaitForExit()"), 2)
        self.assertRegex(
            source,
            r"\n    \}\n    \$process\.WaitForExit\(\)\n\n"
            r"    \$code = 1\n"
            r"    if \(-not \(Test-Path -LiteralPath \$status -PathType Leaf\)\)",
        )
        self.assertIn("$statusValue -notmatch '^-?\\d+$'", source)
        self.assertIn("[int]::TryParse($statusValue, [ref]$parsedCode)", source)

    def test_missing_or_invalid_status_fails_conservatively(self):
        source = invoke_tracked_source()
        self.assertIn("$code = 1", source)
        self.assertIn(
            'Write-Host "==> tracked process exit status file is missing: '
            '$status; treating as failure"',
            source,
        )
        self.assertIn(
            'Write-Host "==> tracked process exit status is invalid: '
            "'$displayStatus'; treating as failure\"",
            source,
        )
        self.assertIn('Write-Host "==> tracked process exit code: $code"', source)

    def test_timeout_still_returns_124(self):
        source = invoke_tracked_source()
        self.assertRegex(
            source,
            r"(?s)taskkill\.exe /PID \$process\.Id /T /F.*?"
            r"\$process\.WaitForExit\(\).*?return 124",
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


class ResumeWorkflowRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKFLOW.read_text(encoding="utf-8")

    def test_resume_skips_predecessors_and_starts_requested_stage(self):
        self.assertIn("resume_run_id:", self.source)
        self.assertIn("if: ${{ inputs.resume_run_id == '' }}", self.source)
        for stage in range(2, 13):
            self.assertIn(f"inputs.resume_stage == '{stage}'", self.source)
            self.assertIn(
                f"inputs.resume_run_id == '' || inputs.resume_stage != '{stage}'",
                self.source,
            )
            self.assertIn(
                f"pattern: tree-s{stage - 1}-attempt-",
                self.source,
            )
        self.assertEqual(self.source.count("Download tree from previous run"), 11)

    def test_every_stage_uploads_a_tree_on_success_or_failure(self):
        self.assertEqual(self.source.count("- name: Ensure build tree snapshot"), 12)
        self.assertEqual(
            self.source.count(
                "if: ${{ always() && steps.stage.outputs.upload_parts != 'true' }}"
            ),
            12,
        )
        self.assertEqual(self.source.count("if: ${{ always() }}"), 48)
        self.assertEqual(self.source.count("- name: Upload tree part 1"), 12)
        self.assertEqual(self.source.count("- name: Upload tree part 4"), 12)
        self.assertNotIn("if: steps.stage.outputs.upload_parts == 'true'", self.source)
        self.assertEqual(
            self.source.count(
                ". build\\windows\\ci-parts.ps1 -Root C:\\c "
                "-PartsDir C:\\parts -Mode Synced"
            ),
            12,
        )

    def test_resume_uses_official_cross_run_artifact_download(self):
        self.assertIn("actions: read", self.source)
        self.assertEqual(self.source.count("github-token: ${{ github.token }}"), 11)
        self.assertEqual(self.source.count("run-id: ${{ inputs.resume_run_id }}"), 11)
        self.assertEqual(self.source.count("merge-multiple: true"), 22)
        self.assertIn("resume_tree_stage:", self.source)
        self.assertIn(
            "pattern: tree-s${{ inputs.resume_tree_stage != '' && inputs.resume_tree_stage || '11' }}-attempt-*-part*",
            self.source,
        )
        self.assertNotIn("download-stage-artifacts.ps1", self.source)


class RestoredSourceUpdateRegressionTest(unittest.TestCase):
    def test_resume_updates_stale_media_recorder_source(self):
        stage_source = CI_STAGE.read_text(encoding="utf-8")
        update_source = RESTORED_SOURCE_UPDATE.read_text(encoding="utf-8")
        restore = stage_source.index('& $sevenZip x "C:\\restore\\tree.7z.001"')
        update = stage_source.index('update-restored-source.ps1', restore)
        prepare = stage_source.index('.chromix-source-ready', update)
        self.assertLess(restore, update)
        self.assertLess(update, prepare)
        self.assertIn("type.LowerASCII().Utf8()", update_source)
        self.assertIn("type.ToAsciiLower().Utf8()", update_source)
        self.assertIn("json_file_value_deserializer.h", update_source)
        self.assertIn("json_file_value_serializer.h", update_source)
        self.assertIn("base::Value::Dict", update_source)
        self.assertIn("base::DictValue", update_source)
        self.assertIn("uxr-webgl-renderer", update_source)
        self.assertIn("uxr-webgl-vendor", update_source)
        self.assertIn("WebGLPersonaRenderer", update_source)
        self.assertIn("WebGLPersonaVendor", update_source)
        self.assertIn("ClampWebGLPersonaLimit", update_source)
        self.assertIn("ClampWebGL2PersonaLimit", update_source)
        self.assertIn("WebGL 2.0 (OpenGL ES 3.0 Chromium)", update_source)
        self.assertIn("UxrFarbleReadPixels", update_source)
        self.assertIn("UxrJitterMetric", update_source)
        self.assertIn("UxrFontFamilyAllowed", update_source)
        self.assertIn("kWindowsFamilies", update_source)
        self.assertIn('Get("uxr-platform")', update_source)
        self.assertIn("base::SplitString", update_source)
        self.assertIn("FontCache::GetFontPlatformData", update_source)
        self.assertIn("TextMetrics::Update", update_source)
        current = update_source.rindex('$content.Contains($NewText)')
        stale = update_source.index('$content.Contains($OldText)')
        self.assertGreater(current, stale)
        self.assertIn("Normalize-RestoredSource", update_source)
        normalize_definition = update_source.index("function Normalize-RestoredSource {")
        first_normalize_call = update_source.index("Normalize-RestoredSource `")
        self.assertLess(normalize_definition, first_normalize_call)
        self.assertIn("normalized restored source", update_source)
        self.assertIn("already current or not applicable", update_source)
        self.assertNotIn(
            "resume source migration has unknown state (expected legacy text or current marker)",
            update_source,
        )
        self.assertIn("UxrFontFamilyIsGeneric", update_source)
        self.assertIn("NVIDIA GeForce RTX 3060 Direct3D11", update_source)
        self.assertIn("String(WebGLPersonaRenderer().c_str())", update_source)
        self.assertIn("String(WebGLPersonaVendor().c_str())", update_source)
        self.assertIn("UxrJitterQuads", update_source)
        self.assertIn('third_party\\blink\\renderer\\core\\dom\\element.cc', update_source)
        self.assertIn('third_party\\blink\\renderer\\modules\\webgpu\\gpu_adapter_info.cc', update_source)
        self.assertIn('third_party\\blink\\renderer\\modules\\webgpu\\gpu_adapter.cc', update_source)
        self.assertIn('chromix-renderer-objects-invalidated.txt', update_source)
        self.assertIn("$namespaceMarker", update_source)
        self.assertIn("$content.Insert($namespaceIndex + $namespaceMarker.Length, $helper)", update_source)
        self.assertIn('[switch]$PreferCurrentMarker', update_source)
        self.assertIn("GLint ClampWebGL2PersonaLimit", update_source)
        self.assertIn("GLint ClampPersonaLimit", update_source)
        self.assertIn("$legacyFunctions", update_source)
        self.assertIn("$hasCurrentClamp", update_source)
        self.assertIn("[regex]::Escape($legacyFunction.Name)", update_source)
        self.assertIn("$content.IndexOf('{', $start)", update_source)
        self.assertIn("GLint WebGL2PersonaVaryingVectors", update_source)
        self.assertIn("-PreferCurrentMarker", update_source)

    def test_resume_accepts_historical_webgl_persona_markers(self):
        update_source = RESTORED_SOURCE_UPDATE.read_text(encoding="utf-8")
        self.assertIn('[string[]]$CurrentMarker = @()', update_source)
        current = update_source.rindex('$content.Contains($NewText)')
        marker = update_source.rindex('foreach ($marker in $CurrentMarker)')
        stale = update_source.index('$content.Contains($OldText)')
        self.assertGreater(current, stale)
        self.assertGreater(marker, stale)
        self.assertIn("'String(renderer.c_str())'", update_source)
        self.assertIn("'String(WebGLPersonaRenderer().c_str())'", update_source)
        self.assertIn("'const std::string renderer = config.Get(\"uxr-webgl-renderer\");'", update_source)
        self.assertIn("'String(vendor.c_str())'", update_source)
        self.assertIn("'String(WebGLPersonaVendor().c_str())'", update_source)
        self.assertIn("'const std::string vendor = config.Get(\"uxr-webgl-vendor\");'", update_source)
        self.assertIn(
            "-CurrentMarker 'String(\"WebGL GLSL ES 3.00 "
            "(OpenGL ES GLSL ES 3.0 Chromium)\")'",
            update_source,
        )

    def test_resume_passes_output_directory_and_invalidates_webgl_objects(self):
        stage = CI_STAGE.read_text(encoding="utf-8")
        update = RESTORED_SOURCE_UPDATE.read_text(encoding="utf-8")
        self.assertIn('update-restored-source.ps1" -Src $Src -OutDir $OutDir', stage)
        self.assertIn('chromix-renderer-objects-invalidated.txt', update)
        self.assertIn('removed $($staleObjects.Count) restored renderer object files', update)
        self.assertIn('SetLastWriteTimeUtc', update)
        self.assertIn(
            'third_party\\blink\\renderer\\modules\\webgl\\webgl_rendering_context_base.cc',
            update,
        )
        self.assertIn(
            'third_party\\blink\\renderer\\modules\\webgl\\webgl2_rendering_context_base.cc',
            update,
        )


if __name__ == "__main__":
    unittest.main()
