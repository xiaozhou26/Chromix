"""Actual stage6 provenance and transactional controller migration regressions."""
import copy
import gzip
import json
import os
import shutil
import subprocess
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import migrate_macos155_arm64_controller_snapshot as controller
import validate_macos155_arm64_controller_checkpoint as donor
import validate_posix_snapshot as snapshots
from test_migrate_restored_snapshot import Fixture, PATCH_BIN, patch, put, series, snapshot
from tools.tests.macos155_historical_fixture import BASELINE, donor_checkout
from tools.tests.test_macos155_managed_profile_link import mac_sources, source_data
from tools.tests.test_macos152_patch_selection import run_patch

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads(gzip.decompress(
    (ROOT / "tools/tests/fixtures/macos155_arm64_controller_receipts.json.gz").read_bytes()))


@pytest.fixture(scope="module", autouse=True)
def historical_controller_target(tmp_path_factory):
    # The controller gate admits exactly 225 patches, not the later PNG target.
    root = donor_checkout(tmp_path_factory.mktemp("controller-target") / "repo", "4062bb584a4dc14d132aba1dd81df3094f35ebcd")
    subprocess.run(["git", "-C", str(root), "sparse-checkout", "add", "/.github/"], check=True)
    with pytest.MonkeyPatch.context() as context:
        context.setitem(globals(), "ROOT", root)
        context.setattr(controller, "__file__", str(root / "tools" / Path(controller.__file__).name))
        yield root


class Client:
    def __init__(self):
        self.data = copy.deepcopy(FIXTURE)

    def get(self, path):
        return {f"/actions/runs/{donor.RUN}": self.data["run"],
                f"/actions/jobs/{donor.JOB}": self.data["job"]}[path]

    def items(self, path, key):
        assert path == f"/actions/runs/{donor.RUN}/artifacts" and key == "artifacts"
        return self.data["artifacts"]


def test_actual_latest_failed_stage6_not_original_stage8():
    report = donor.validate_metadata(Client(), donor.REPOSITORY, donor.IDS)
    assert (report["run_id"], report["attempt"], report["stage"], report["job_id"]) == (
        39280602881, 1, 6, 118598389953)
    assert report["artifacts"] == donor.ARTIFACTS
    receipt = json.loads(FIXTURE["files"][donor.LATEST_RECEIPT])
    assert receipt["state"] == donor.STATE
    assert receipt["continuation_run"] == str(donor.RUN)
    assert receipt["run_id"] != donor.RUN
    for name, raw in FIXTURE["files"].items():
        assert donor.sha(raw.encode()) == donor.PINNED_FILES[name]


@pytest.mark.parametrize("subject,field,value", [
    ("run", "id", 39104897472), ("run", "head_sha", donor.ancestor.SHA),
    ("run", "run_attempt", 2), ("run", "conclusion", "success"),
    ("run", "head_branch", "main"), ("run", "event", "push"),
    ("run", "path", ".github/workflows/build-macos-arm64.yml"),
    ("run", "repository", {"full_name": "foreign/repo"}),
    ("run", "head_repository", {"full_name": "foreign/repo"}),
    ("job", "id", donor.ancestor.JOB), ("job", "run_id", donor.ancestor.RUN),
    ("job", "head_sha", donor.ancestor.SHA), ("job", "run_attempt", 3),
    ("job", "labels", ["macos-15-intel"]), ("job", "conclusion", "success"),
    ("job", "status", "in_progress"), ("job", "name", "macos-arm64 stage 5 (resume compile)"),
])
def test_metadata_identity_tampering(subject, field, value):
    client = Client()
    client.data[subject][field] = value
    with pytest.raises(ValueError):
        donor.validate_metadata(client, donor.REPOSITORY, donor.IDS)


@pytest.mark.parametrize("name", ["Run stage 6", "Verify handoff snapshot",
    "Preserve latest macOS compiled checkpoint", "Upload failed macOS runtime bundle",
    *[f"Upload tree part {i}" for i in range(1, 5)]])
def test_no_stale_upload_exception(name):
    client = Client()
    next(s for s in client.data["job"]["steps"] if s["name"] == name)["status"] = "in_progress"
    with pytest.raises(ValueError):
        donor.validate_metadata(client, donor.REPOSITORY, donor.IDS)


@pytest.mark.parametrize("field,value", [("id", 1), ("size_in_bytes", 1),
    ("digest", "sha256:" + "a" * 64), ("expired", True),
    ("name", donor.PREFIX + "3"), ("workflow_run", {"id": donor.ancestor.RUN})])
@pytest.mark.parametrize("part", [0, 1])
def test_archive_metadata_tampering(part, field, value):
    client = Client()
    client.data["artifacts"][part][field] = value
    with pytest.raises(ValueError):
        donor.validate_metadata(client, donor.REPOSITORY, donor.IDS)


@pytest.mark.parametrize("ids", [[], [12266957738], [12267091707], [12267091707, 12266957738],
                                [donor.ancestor.ARTIFACT], [12266957738, 12266957738]])
def test_exact_ordered_parts(ids):
    with pytest.raises(ValueError):
        donor.validate_metadata(Client(), donor.REPOSITORY, ids)


def test_extra_and_missing_parts():
    for parts in ([], Client().data["artifacts"][:1], Client().data["artifacts"] * 2):
        client = Client()
        client.data["artifacts"] = parts
        with pytest.raises(ValueError):
            donor.validate_metadata(client, donor.REPOSITORY, donor.IDS)


def download_reports(root):
    origin = donor.validate_metadata(Client(), donor.REPOSITORY, donor.IDS)
    put(root, "snapshot-origin.json", json.dumps(origin))
    report = dict(status="success", publication="published", phase="complete", repository=donor.REPOSITORY,
                  run_id=donor.RUN, head_sha=donor.SHA, artifacts=[], volumes=[])
    for number, a in enumerate(donor.ARTIFACTS, 1):
        report["artifacts"].append(dict(a, attempts=[dict(status="success", size_in_bytes=a["size_in_bytes"],
                                                        sha256=a["digest"][7:])]))
        report["volumes"].append(dict(name=f"tree.tar.zst.{number:03d}", artifact_id=a["id"],
                                      size_in_bytes=100, sha256="b" * 64))
    put(root, "snapshot-download.json", json.dumps(report))
    return report


def test_download_requires_archive_content_verification(tmp_path):
    report = download_reports(tmp_path)
    assert len(donor.downloaded(tmp_path)) == 2
    for changes in (dict(status="verified"), dict(publication="unconfirmed"), dict(artifacts=[]), dict(volumes=[])):
        put(tmp_path, "snapshot-download.json", json.dumps(dict(report, **changes)))
        with pytest.raises(ValueError):
            donor.downloaded(tmp_path)
    for index in (0, 1):
        broken = copy.deepcopy(report)
        broken["artifacts"][index]["attempts"][-1]["sha256"] = "0" * 64
        put(tmp_path, "snapshot-download.json", json.dumps(broken))
        with pytest.raises(ValueError):
            donor.downloaded(tmp_path)


@pytest.mark.parametrize("name", list(donor.PINNED_FILES))
def test_actual_donor_receipts_cannot_be_rewritten(tmp_path, name):
    for filename, raw in FIXTURE["files"].items():
        put(tmp_path, filename, raw)
    put(tmp_path, "src/.chromix-restored-build-prepared.json",
        FIXTURE["files"]["upstream-cache-preparation.json"])
    put(tmp_path, name, (tmp_path / name).read_bytes() + b"\n")
    with pytest.raises(ValueError, match="latest stage6 receipt/GN mismatch"):
        donor.donor_state(tmp_path)


def test_original_stage8_or_empty_ninja_state_cannot_replace_latest(tmp_path):
    for filename, raw in FIXTURE["files"].items():
        put(tmp_path, filename, raw)
    put(tmp_path, "src/.chromix-restored-build-prepared.json",
        FIXTURE["files"]["upstream-cache-preparation.json"])
    for name in donor.STATE:
        put(tmp_path / "src", name, b"older stage8 incremental state")
    with pytest.raises(ValueError, match="latest stage6 Ninja state required"):
        donor.donor_state(tmp_path)


def test_unarmed_patch_contract_cannot_admit_or_migrate(tmp_path, monkeypatch):
    monkeypatch.setattr(controller, "REVIEWED_SELECTOR_SHA256", None)
    with pytest.raises(ValueError, match="unarmed"):
        controller.reviewed(ROOT)
    with pytest.raises(ValueError, match="unarmed"):
        controller.migrate(tmp_path, ROOT, ROOT, logs=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_cli_routes_only_exact_latest_donor(tmp_path, monkeypatch):
    args = ["validate_posix_snapshot.py", "--repository", donor.REPOSITORY,
            "--run-id", str(donor.RUN), "--stage", "6", "--attempt", "1",
            "--artifact-ids", ",".join(map(str, donor.IDS)), "--arch", "arm64",
            "--report", str(tmp_path / "origin.json")]
    monkeypatch.setattr(snapshots, "Client", lambda *a: Client())
    monkeypatch.setenv("GITHUB_WORKFLOW", donor.WORKFLOW)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr("sys.argv", args)
    assert snapshots.main() == 0
    for flag, value in [("--run-id", str(donor.ancestor.RUN)), ("--stage", "8"),
                        ("--attempt", "3"), ("--arch", "x64")]:
        wrong = list(args)
        wrong[wrong.index(flag) + 1] = value
        monkeypatch.setattr("sys.argv", wrong)
        with pytest.raises(ValueError):
            snapshots.main()
    monkeypatch.setattr("sys.argv", args)
    monkeypatch.setenv("GITHUB_WORKFLOW", donor.ancestor.WORKFLOW)
    with pytest.raises(ValueError):
        snapshots.main()


def fixture_policy(tmp_path, monkeypatch):
    fx = Fixture(tmp_path, arch="arm64")
    put(fx.src, controller.SOURCE, "base GN source\n")
    fx.prepare()
    series(fx.repo, patch(), patch(controller.SOURCE, "base GN source", "controller GN source"))
    monkeypatch.setattr(controller.Policy, "validate_inputs", staticmethod(controller.migration._same_inputs))
    monkeypatch.setattr(controller, "REVIEWED_SOURCE_BEFORE_SHA256", donor.sha(b"base GN source\n"))
    monkeypatch.setattr(controller, "REVIEWED_SOURCE_AFTER_SHA256", donor.sha(b"controller GN source\n"))
    monkeypatch.setattr(donor, "STATE", {n: donor.sha((fx.src / n).read_bytes()) for n in donor.STATE})
    monkeypatch.setitem(donor.ancestor.PINNED_FILES, "src/out/Default/args.gn",
                        donor.sha((fx.src / "out/Default/args.gn").read_bytes()))
    return fx, controller.Policy(fx.work)


def test_real_private_reverse_forward_retains_objects_and_only_publishes_gn(tmp_path, monkeypatch):
    fx, policy = fixture_policy(tmp_path, monkeypatch)
    before = snapshot(fx.src)
    result = fx.run(policy=policy)
    assert result["changed_files"] == [controller.SOURCE]
    after = snapshot(fx.src)
    for name in before.keys() - {controller.SOURCE, controller.migration.READY, controller.arp.MARKER}:
        assert after[name] == before[name]
    assert (fx.src / controller.SOURCE).stat().st_mtime_ns > before[controller.SOURCE][1]
    policy.validate_preserved(fx.src)
    fx.check()


@pytest.mark.parametrize("kind", ["postimage", "extra-source", "patch-conflict", "out-change"])
def test_failed_private_migration_never_publishes_source_or_receipts(tmp_path, monkeypatch, kind):
    fx, policy = fixture_policy(tmp_path, monkeypatch)
    before = snapshot(fx.src)
    if kind == "postimage":
        monkeypatch.setattr(controller, "REVIEWED_SOURCE_AFTER_SHA256", "0" * 64)
    elif kind == "extra-source":
        series(fx.repo, patch(new="different example.com"), patch(controller.SOURCE, "base GN source", "controller GN source"))
    elif kind == "patch-conflict":
        series(fx.repo, patch(), patch(controller.SOURCE, "wrong preimage", "controller GN source"))
    else:
        put(fx.src, "out/Default/obj/cached.o", "changed object")
    with pytest.raises((ValueError, controller.arp.ApplyError)):
        fx.run(policy=policy)
    after = snapshot(fx.src)
    for name in (controller.SOURCE, controller.migration.READY, controller.arp.MARKER, "listed.txt"):
        assert before[name] == after[name]
    assert (fx.src / controller.migration.TRANSACTION).exists()
    with pytest.raises(controller.arp.ApplyError, match="in-progress/partial"):
        fx.run(policy=policy)


def test_controller_stage_routes_inherited_receipt_and_preserves_failed_checkpoint(monkeypatch):
    from tools.tests.test_posix_stages import PosixStageHandoffExecutionTest

    harness = PosixStageHandoffExecutionTest()
    harness.setUp()
    try:
        harness.put(harness.bindir / "python3", '#!/bin/sh\nprintf "guard %s\\n" "$*" >> "$CALL_LOG"\nexit 0\n')
        put(harness.work, donor.ancestor.RECEIPT, FIXTURE["files"][donor.ancestor.RECEIPT])
        monkeypatch.setenv("GITHUB_WORKFLOW", donor.WORKFLOW)
        result = harness.run_stage("macos", "arm64", "fast", 1, phase="timeout", build_rc=42)
        assert result.returncode != 0
        assert "upload_snapshot=true" in harness.output.read_text()
        assert "finished=true" not in harness.output.read_text()
        calls = harness.calls.read_text()
        assert calls.count("migrate_macos155_arm64_controller_snapshot.py") == 2
        assert "validate_macos155_arm64_stage8_checkpoint.py" not in calls
        assert calls.index("--phase before") < calls.index("build macos")
        assert calls.index("--phase after") < calls.index("\nsnapshot\n")
    finally:
        harness.doCleanups()


def test_workflow_preserves_native_gates_and_never_uses_ancestor_admission():
    workflow = (ROOT / ".github/workflows/build-mac155-arm64-controller-resume.yml").read_text()
    assert "uses: ./.github/workflows/build-posix-github.yml" in workflow
    for text in ["runner: macos-15", "arch: arm64", "max-stages: 8", "build_profile: fast",
                 "resume_run_id: '39280602881'", "resume_tree_stage: '6'", "resume_attempt: '1'",
                 "resume_artifact_ids: '12266957738,12267091707'", "use_upstream_cache: true"]:
        assert text in workflow
    stage = (ROOT / "build/posix/ci-stage.sh").read_text()
    assert "elif [ \"${GITHUB_WORKFLOW:-}\" = build-mac155-arm64-stage8-resume ]" in stage
    assert stage.index("controller-migration.json") < stage.index("mac155-arm64-stage8-resume.json")
    assert "if [ \"$RC\" -ne 0 ] && [ \"$RC\" -ne 124 ]; then" in stage
    assert 'emit upload_snapshot true' in stage
    assert "tools/macos_browser_smoke.py" in stage


@pytest.fixture(scope="module")
def exact_donor(tmp_path_factory):
    return donor_checkout(tmp_path_factory.mktemp("controller-donor") / "repo", donor.SHA)


def test_real_donor_224_to_current_225_runs_the_production_gate(exact_donor):
    controller.reviewed(ROOT)
    controller.validate_inputs(exact_donor, ROOT, "macos", "arm64")
    controller.Policy.validate_inputs(exact_donor, ROOT, "macos", "arm64")
    old, old_patches = controller.arp.patch_selection.select(exact_donor, "macos")
    current, patches = controller.arp.patch_selection.select(ROOT, "macos")
    assert len(old_patches) == 224 and len(patches) == 225
    assert patches == old_patches + [(controller.PATCH, (ROOT / controller.PATCH).read_bytes())]
    assert donor.ancestor.identity_hash(old) == controller.OLD_SELECTION
    assert current["selection"]["additions"]["manifest"]["sha256"] == controller.REVIEWED_MANIFEST_SHA256
    assert controller.migration.source_ready_key(exact_donor, "macos", "arm64") == donor.ancestor.KEY
    assert controller.migration.source_ready_key(ROOT, "macos", "arm64") == controller.TARGET_KEY
    assert controller.TARGET_KEY.endswith("e1bf946113b645cd4463da31483f601437093c9b79140ae4e0dae880bb90c5d9")


@pytest.mark.parametrize("platform,arch", [("linux", "arm64"), ("macos", "x64")])
def test_real_gate_rejects_wrong_platform(exact_donor, platform, arch):
    with pytest.raises(ValueError, match="native macOS ARM64"):
        controller.validate_inputs(exact_donor, ROOT, platform, arch)


def test_same_tree_local_baseline_is_not_the_exact_published_donor(tmp_path):
    historical = donor_checkout(tmp_path / "local-baseline", BASELINE)
    assert controller.migration._donor_git(historical, ROOT, "rev-parse", "HEAD^{tree}").decode().strip() == donor.TREE
    with pytest.raises(ValueError, match="exact latest stage6 producer checkout"):
        controller.validate_inputs(historical, ROOT, "macos", "arm64")


@pytest.mark.parametrize("name", ["tools/patch_selection.py", controller.MANIFEST, controller.PATCH,
                                 "build/args.macos.gn", "build/ungoogled-revisions.psd1",
                                 "patches/series", "patches/0224-webgl-seeded-readback-noise.patch"])
def test_real_gate_rejects_tampered_target_inputs(exact_donor, tmp_path, name):
    repo = tmp_path / "repo"
    for directory in ("tools", "build", "patches"):
        shutil.copytree(ROOT / directory, repo / directory,
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "tests"))
    for path in ROOT.glob("CHROMIUM*"):
        shutil.copy2(path, repo / path.name)
    controller.validate_inputs(exact_donor, repo, "macos", "arm64")
    path = repo / name
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises((ValueError, controller.arp.ApplyError)):
        controller.validate_inputs(exact_donor, repo, "macos", "arm64")


def test_real_gate_rejects_dirty_donor(tmp_path):
    previous = donor_checkout(tmp_path / "dirty-donor", donor.SHA)
    put(previous, "build/unreviewed-input", "unexpected")
    with pytest.raises(ValueError, match="producer checkout must be clean"):
        controller.validate_inputs(previous, ROOT, "macos", "arm64")


def install_donor_receipts(work):
    for name, raw in FIXTURE["files"].items():
        put(work, name, raw)
    put(work, "src/.chromix-restored-build-prepared.json", FIXTURE["files"]["upstream-cache-preparation.json"])


def test_latest_receipts_and_upstream_identity_match_both_real_recipes(exact_donor, tmp_path):
    install_donor_receipts(tmp_path)
    receipt = donor.donor_receipts(tmp_path)
    assert (receipt["continuation_run"], receipt["continuation_stage"], receipt["logical_stage"]) == (
        str(donor.RUN), 6, 14)
    put(tmp_path, "src/chrome/VERSION", "MAJOR=155\nMINOR=0\nBUILD=8059\nPATCH=39\n")
    for name in controller.migration.restore.REQUIRED:
        if not (tmp_path / "src" / name).exists():
            put(tmp_path / "src", name, "receipt validation fixture\n")
    before = controller.migration.restore.verify_restored(tmp_path, "macos", "arm64", repo=exact_donor)
    after = controller.migration.restore.verify_restored(tmp_path, "macos", "arm64", repo=ROOT)
    assert before == after
    assert before["identity"]["chromium_version"] == "155.0.8059.39"
    assert donor.sha((tmp_path / "upstream-cache-preparation.json").read_bytes()) != donor.ancestor.PREPARATION_HASH


def test_real_224_receipt_reconstruction_and_225_identity(exact_donor, source_data, tmp_path):
    core = tmp_path / "work/tooling/ungoogled-chromium"
    for name in ("domain_regex.list", "domain_substitution.list"):
        put(core, name, source_data["tooling"]["core/" + name]["text"])
    put(core, "chromium_version.txt", "155.0.8059.39\n")
    tooling = core.parent / "ungoogled-chromium-macos"
    tooling.mkdir()
    old, patches, lite = controller.arp._load(exact_donor, core, tooling, "macos")
    current, new_patches, new_lite = controller.arp._load(ROOT, core, tooling, "macos")
    saved = json.loads(FIXTURE["files"]["src/.chromix-restored-patches.json"])
    relocated = copy.deepcopy(saved["identity"])
    for name in ("regex", "list"):
        relocated[name]["path"] = old[name]["path"]
    assert old == relocated
    assert donor.ancestor.identity_hash(saved["identity"]) == donor.ancestor.SOURCE_IDENTITY
    patch_outputs = {name: value for name, value in saved["outputs"].items() if name not in lite}
    assert donor.ancestor.identity_hash(patch_outputs) == donor.ancestor.SOURCE_OUTPUTS
    assert len(patches) == 224 and len(new_patches) == 225 and lite == new_lite
    assert new_patches[:-1] == patches
    assert new_patches[-1][2] == [(controller.SOURCE, "modify", None)]
    assert controller.SOURCE not in saved["outputs"]
    assert current["selection"]["additions"]["patches"] == [
        {"path": controller.PATCH, "sha256": controller.REVIEWED_PATCH_SHA256}]


def test_real_gn_preimage_postimage_and_patch_roundtrip(exact_donor, mac_sources, tmp_path):
    controller.Policy.validate_inputs(exact_donor, ROOT, "macos", "arm64")
    target = mac_sources / controller.SOURCE
    before = target.read_bytes()
    assert donor.sha(before) == controller.REVIEWED_SOURCE_BEFORE_SHA256
    result = run_patch(mac_sources, ROOT / controller.PATCH)
    assert result.returncode == 0, result.stdout + result.stderr
    assert donor.sha(target.read_bytes()) == controller.REVIEWED_SOURCE_AFTER_SHA256
    assert target.read_bytes().replace(b'      "signin/managed_profile_creation_controller.cc",\n', b"") == before
    result = run_patch(mac_sources, ROOT / controller.PATCH, reverse=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert target.read_bytes() == before


def test_cli_real_gate_reaches_download_evidence_without_ancestor_recipe_check(exact_donor, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    logs = tmp_path / "chromix-logs"
    logs.mkdir()
    result = subprocess.run([
        sys.executable, str(ROOT / "tools/migrate_restored_snapshot.py"), "--workdir", str(work),
        "--previous-repo", str(exact_donor), "--repo", str(ROOT), "--platform", "macos", "--arch", "arm64"],
        capture_output=True, text=True,
        env={**os.environ, "GITHUB_WORKFLOW": donor.WORKFLOW, "RUNNER_TEMP": str(tmp_path)})
    assert result.returncode == 1
    assert "snapshot-origin.json" in result.stderr
    assert "preparation key changed" not in result.stderr
    assert list(work.iterdir()) == []
    download_reports(logs)
    install_donor_receipts(work)
    for name in donor.STATE:
        put(work / "src", name, "not the latest compiled checkpoint\n")
    result = subprocess.run(result.args, capture_output=True, text=True,
                            env={**os.environ, "GITHUB_WORKFLOW": donor.WORKFLOW, "RUNNER_TEMP": str(tmp_path)})
    assert result.returncode == 1
    assert "latest stage6 Ninja state required" in result.stderr
    assert not (work / controller.RECEIPT).exists()
    assert not (work / "src" / controller.migration.TRANSACTION).exists()
