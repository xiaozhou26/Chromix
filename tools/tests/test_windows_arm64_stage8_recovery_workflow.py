"""Static contract tests for the exact stage-12 checkpoint recovery workflow."""
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/build-win-arm64-stage8-recovery.yml"

def test_recovery_starts_at_stage13_and_keeps_native_acceptance():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert workflow["name"] == "build-win-arm64-stage8-recovery"
    assert set(workflow["jobs"]) == {"build-13", "complete", "verify-arm64"}
    job = workflow["jobs"]["build-13"]
    assert "needs" not in job
    assert workflow["jobs"]["complete"]["needs"] == "build-13"
    assert workflow["jobs"]["verify-arm64"]["needs"] == "complete"
    assert workflow["env"]["CHROMIX_TARGET_ARCH"] == "arm64"
    text = "\n".join(step.get("run", "") for step in job["steps"])
    assert "verify_windows_arm64_stage8_recovery.py --metadata-report" in text
    assert "download_windows_snapshot.py --manifest" in text
    assert "--source-proof" in text
    assert "ref: 91adf3cc3e2df651ad5af43f0fd72aba0f0f0e9a" in WORKFLOW.read_text(encoding="utf-8")
    assert "-StageIndex 13 -MaxStages 13 -FromArtifact" in text
    assert "--metadata-report" in text
    assert "--manifest" in text
    assert "Download exact stage12 snapshot volumes" in WORKFLOW.read_text(encoding="utf-8")
    assert any(step.get("name") == "Preflight ARM64 host prerequisites" for step in job["steps"])

def test_recovery_job_preserves_native_arm64_gate():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    native = workflow["jobs"]["verify-arm64"]
    assert native["runs-on"] == "windows-11-arm"
    assert any("--arch arm64 --native" in step.get("run", "") for step in native["steps"])
    assert any("--source-report source-receipt/source-verification.json" in step.get("run", "") for step in native["steps"])
