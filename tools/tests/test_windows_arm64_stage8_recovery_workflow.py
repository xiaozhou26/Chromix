"""Static contract tests for the dedicated Windows ARM64 stage-8 recovery workflow."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/build-win-arm64-stage8-recovery.yml"


def test_recovery_starts_at_stage8_and_keeps_native_acceptance():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert workflow["name"] == "build-win-arm64-stage8-recovery"
    assert set(workflow["jobs"]) == {"build-8", "build-9", "build-10", "build-11", "build-12", "complete", "verify-arm64"}
    assert "needs" not in workflow["jobs"]["build-8"]
    assert workflow["jobs"]["complete"]["needs"] == [f"build-{index}" for index in range(8, 13)]
    assert workflow["jobs"]["verify-arm64"]["needs"] == "complete"
    assert workflow["env"]["CHROMIX_TARGET_ARCH"] == "arm64"

    first_steps = workflow["jobs"]["build-8"]["steps"]
    text = "\n".join(step.get("run", "") for step in first_steps)
    assert "verify_windows_arm64_stage8_recovery.py --metadata-report" in text
    assert "download_windows_snapshot.py --manifest" in text
    assert "--source-proof" in text
    assert "verify_windows_snapshot_source.py" not in text
    assert any(step.get("name") == "Preflight ARM64 host prerequisites" for step in first_steps)


def test_recovery_stages_preserve_resume_handoff_and_native_arm64_gate():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for index in range(8, 13):
        job = workflow["jobs"][f"build-{index}"]
        if index > 8:
            assert job["needs"] == f"build-{index - 1}"
        stage = next(step for step in job["steps"] if step.get("id") == "stage")
        assert f"-StageIndex {index} -MaxStages 12 -FromArtifact" in stage["run"]
    native = workflow["jobs"]["verify-arm64"]
    assert native["runs-on"] == "windows-11-arm"
    assert any("--arch arm64 --native" in step.get("run", "") for step in native["steps"])
    assert any("--source-report source-receipt/source-verification.json" in step.get("run", "") for step in native["steps"])
