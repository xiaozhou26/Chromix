"""The ARM64 migration resumes a verified tree once, with no cold-build path."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/build-win-arm64-stage12-migrate.yml"
STAGE = ROOT / "build/windows/ci-stage.ps1"


def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_dispatch_is_fixed_read_only_and_starts_with_verified_donor():
    flow = workflow()
    assert flow["on"] == {"workflow_dispatch": {}}
    assert flow["permissions"] == {"contents": "read", "actions": "read"}
    assert flow["env"]["CHROMIX_TARGET_ARCH"] == "arm64"
    assert flow["env"]["CHROMIX_USE_UPSTREAM_CACHE"] == "1"
    assert flow["env"]["CHROMIX_PREFER_UPSTREAM_CACHE"] == "0"
    assert set(flow["jobs"]) == {"build-13", "build-14", "build-15", "build-16", "complete", "verify-arm64"}
    job = flow["jobs"]["build-13"]
    assert "needs" not in job
    steps = job["steps"]
    named = {step.get("name"): step for step in steps}
    target, donor = [step for step in steps if step.get("uses") == "actions/checkout@v4"]
    assert target["with"] == {"path": "target", "fetch-depth": 0, "persist-credentials": False}
    assert job["defaults"]["run"]["working-directory"] == "target"
    assert steps[0]["working-directory"] == "${{ github.workspace }}"
    assert donor["with"]["path"] == "donor"
    assert donor["with"]["ref"] == "15a6425cfbd7a7ecd4a69ad206d0650778bf8253"
    assert donor["with"]["repository"] == "xiaozhou26/Chromix"
    assert donor["with"]["persist-credentials"] is False
    assert donor["with"]["submodules"] is False
    order = [
        "Validate exact stage12 metadata", "Download exact stage12 snapshot volumes",
        "Validate ARM64 snapshot markers and receipts", "Preflight ARM64 host prerequisites",
        "Restore verified checkpoint without cold fallback",
        "Migrate exact donor checkpoint to checked-out target source", "Run stage 13",
    ]
    assert [steps.index(named[name]) for name in order] == sorted(steps.index(named[name]) for name in order)
    metadata = named[order[0]]["run"]
    assert "validate_windows154_arm64_checkpoint.py" in metadata
    assert '--repository "${{ github.repository }}"' in metadata
    assert "--metadata-report" in metadata
    download = named[order[1]]["run"]
    assert "download_windows_snapshot.py --manifest" in download
    assert "--destination 'C:\\restore'" in download
    restore = named[order[4]]["run"]
    assert "Test-Path -LiteralPath 'C:\\c'" in restore
    assert "& $sevenZip t " in restore
    assert "& $sevenZip x " in restore and "'-oC:\\c'" in restore
    migrate = named[order[5]]["run"]
    assert "migrate_windows154_arm64_snapshot.py --work-dir 'C:\\c\\chromix'" in migrate
    assert '--target-repo "$env:GITHUB_WORKSPACE\\target"' in migrate
    assert '--donor-repo "$env:GITHUB_WORKSPACE\\donor"' in migrate
    for flag in ("--donor-repo", "--target-repo", "--donor-sha", "--output"):
        assert flag in migrate
    assert "if ($LASTEXITCODE -ne 0) { throw" in migrate
    stage = named[order[6]]["run"]
    assert "-StageIndex 13 -MaxStages 16 -FromSynced -UseUpstreamCache -Arch arm64" in stage
    assert "-FromArtifact" not in stage
    assert "325 - $elapsed" in stage and "-StageBudgetMinutes $budget" in stage
    assert "CHROMIX_MIGRATION_JOB_STARTED=" in steps[0]["run"]
    assert all("continue-on-error" not in step for step in steps)
    text = WORKFLOW.read_text()
    assert "verify_windows_arm64_stage8_recovery.py" not in text
    assert "-StageIndex 1 " not in text
    assert "--source-proof" not in text


def test_same_run_handoffs_preserve_attempt_and_no_cold_fallback():
    flow = workflow()
    for index in range(13, 17):
        job = flow["jobs"][f"build-{index}"]
        steps = job["steps"]
        named = {step.get("name"): step for step in steps}
        assert job["runs-on"] == "windows-2022"
        assert job["timeout-minutes"] == 355
        assert job["outputs"]["snapshot_attempt"] == "${{ steps.snapshot_origin.outputs.attempt }}"
        assert "-UseUpstreamCache -Arch arm64" in named[f"Run stage {index}"]["run"]
        assert "Preflight ARM64 host prerequisites" in named
        assert "-Install" in named["Preflight ARM64 host prerequisites"]["run"]
        snapshot = named["Ensure build tree snapshot"]
        assert "always()" in snapshot["if"] and "snapshot_safe == 'true'" in snapshot["if"]
        assert "finished != 'true'" in snapshot["if"]
        for part in range(1, 5):
            upload = named[f"Upload tree part {part}"]
            assert "always()" in upload["if"] and "snapshot_safe == 'true'" in upload["if"]
            assert upload["with"]["name"] == f"win-arm64-tree-s{index}-attempt-${{{{ github.run_attempt }}}}-part{part}"
        assert named["Upload final bundle"]["if"] == "steps.stage.outputs.finished == 'true'"
        assert named["Upload final source receipt"]["with"]["if-no-files-found"] == "error"
        if index > 13:
            previous = index - 1
            assert job["needs"] == f"build-{previous}"
            assert f"needs.build-{previous}.result == 'success'" in job["if"]
            download = named["Download this run's previous checkpoint"]["with"]
            assert download["pattern"] == f"win-arm64-tree-s{previous}-attempt-${{{{ needs.build-{previous}.outputs.snapshot_attempt }}}}-part*"
            assert "run-id" not in download and "github-token" not in download
            assert "-FromArtifact" in named[f"Run stage {index}"]["run"]
            assert not any("migrate_windows154" in step.get("run", "") for step in steps)


def test_native_acceptance_is_required_after_any_finished_stage():
    flow = workflow()
    assert flow["jobs"]["complete"]["needs"] == [f"build-{index}" for index in range(13, 17)]
    native = flow["jobs"]["verify-arm64"]
    assert native["needs"] == "complete"
    assert native["runs-on"] == "windows-11-arm"
    script = "\n".join(step.get("run", "") for step in native["steps"])
    assert "--arch arm64 --native" in script
    assert "--source-report source-receipt/source-verification.json" in script
    assert "fingerprint_acceptance.py" in script


def test_prerestored_entry_is_fail_closed_and_cannot_reextract():
    text = STAGE.read_text()
    start = text.index("if ($FromSynced) {")
    guard = text[start:text.index("if ($FromArtifact) {", start)]
    for condition in ("$FromArtifact -or $StageIndex -ne 13", "$Arch -cne 'arm64'",
                      "-not $RequireUpstreamCache", "$ValidateOnly", "$BuildProfile -cne 'native'"):
        assert condition in guard
    for required in ("src\\.chromix-upstream-restored.json", "src\\.chromix-restored-patches.json",
                     "src\\.chromix-source-ready", "src\\out\\Default\\build.ninja",
                     ".chromix-windows154-arm64-migration.json", "out\\Chromix"):
        assert required in guard
    for binding in ("$arm64MigrationReceipt.target_sha -cne $env:GITHUB_SHA",
                    "$arm64MigrationReceipt.previous_sha -cne '15a6425cfbd7a7ecd4a69ad206d0650778bf8253'",
                    "$arm64MigrationReceipt.patch_count -ne 224", "$arm64MigrationReceipt.donor_run_id -ne 37455471388",
                    "$arm64MigrationReceipt.donor_job_id -ne 113438156537"):
        assert binding in guard
    assert 'if (-not $FromArtifact -and -not $FromSynced -and $StageIndex -gt 1)' in guard
    assert 'snapshot_safe $(if ($FromSynced -or' in text
    extraction = text[text.index("if ($FromArtifact) {", start):text.index("if ($env:CHROMIX_WINDOWS_VERIFY_SOURCE_REPO", start)]
    assert '$FromSynced' not in extraction
    assert '7z restore failed' in extraction
    require_receipt = text.index('required upstream cache: restore receipt missing; refusing cold preparation or compilation')
    preparation = text.index('& "$PSScriptRoot\\prepare-ungoogled.ps1"')
    safe = text.index('if ($FromSynced) { Write-OutVar snapshot_safe true }')
    assert start < require_receipt < preparation < safe
    assert '-RequireMarker:(($FromArtifact -or $FromSynced) -and $Arch -eq "arm64")' in text
