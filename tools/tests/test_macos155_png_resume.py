"""Pinned native donor metadata, real PNG preimages and atomic continuation gates."""
import copy
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import migrate_macos155_png_snapshot as png
import validate_macos155_png_checkpoint as donor
import validate_posix_snapshot as snapshots
from tools.tests.macos155_historical_fixture import donor_checkout
from test_migrate_restored_snapshot import Fixture, patch, put, series, snapshot
from tools.tests.test_linux155_opaque_png_export import evidence, apply
from tools.tests.test_linux155_managed_profile_link import source_data

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads(gzip.decompress((ROOT / "tools/tests/fixtures/macos155_png_receipts.json.gz").read_bytes()))


class Client:
    def __init__(self, arch):
        self.d = donor.checkpoint(arch)
        self.data = copy.deepcopy(FIXTURE[arch])

    def get(self, path):
        return {f'/actions/runs/{self.d["run_id"]}': self.data["run"],
                f'/actions/jobs/{self.d["job_id"]}': self.data["job"]}[path]

    def items(self, path, key):
        assert path == f'/actions/runs/{self.d["run_id"]}/artifacts' and key == "artifacts"
        return self.data["artifacts"]


@pytest.mark.parametrize("arch", ["arm64", "x64"])
def test_actual_compiled_metadata_and_all_nested_receipts(arch):
    d = donor.checkpoint(arch)
    report = donor.validate_metadata(Client(arch), d["repository"], [a["id"] for a in d["artifacts"]], arch)
    assert (report["stage"], report["attempt"], report["job_id"]) == (1, 1, d["job_id"])
    for name, raw in FIXTURE[arch]["files"].items():
        assert donor.sha(raw.encode()) == d["pinned_files"][name]
    latest = json.loads(FIXTURE[arch]["files"][d["latest_receipt"]])
    assert latest["continuation_run"] == str(d["run_id"]) and latest["run_id"] != d["run_id"]
    assert latest["state"] == d["state"]
    assert 'chrome_pgo_phase = 0' in FIXTURE[arch]["files"]["src/out/Default/args.gn"]


@pytest.mark.parametrize("arch", ["arm64", "x64"])
@pytest.mark.parametrize("subject,field,value", [
    ("run", "id", 1), ("run", "head_sha", "0" * 40), ("run", "run_attempt", 2),
    ("run", "conclusion", "success"), ("run", "head_branch", "main"), ("run", "event", "push"),
    ("run", "path", ".github/workflows/build-macos-arm64.yml"),
    ("run", "head_repository", {"full_name": "foreign/repo"}),
    ("job", "id", 1), ("job", "run_id", 1), ("job", "head_sha", "0" * 40),
    ("job", "run_attempt", 2), ("job", "labels", ["ubuntu-latest"]),
    ("job", "conclusion", "success"), ("job", "name", "macos-arm64 stage 8"),
])
def test_metadata_tamper(arch, subject, field, value):
    client = Client(arch)
    client.data[subject][field] = value
    with pytest.raises(ValueError):
        donor.validate_metadata(client, client.d["repository"], [a["id"] for a in client.d["artifacts"]], arch)


@pytest.mark.parametrize("arch", ["arm64", "x64"])
@pytest.mark.parametrize("part", [0, 1])
@pytest.mark.parametrize("field,value", [("id", 1), ("size_in_bytes", 1), ("digest", "sha256:" + "0" * 64),
    ("expired", True), ("name", "wrong-part"), ("workflow_run", {"id": 1})])
def test_artifact_tamper(arch, part, field, value):
    client = Client(arch)
    client.data["artifacts"][part][field] = value
    with pytest.raises(ValueError):
        donor.validate_metadata(client, client.d["repository"], [a["id"] for a in client.d["artifacts"]], arch)


@pytest.mark.parametrize("arch", ["arm64", "x64"])
def test_missing_reordered_duplicate_extra_parts_and_failed_preservation(arch):
    client = Client(arch)
    ids = [a["id"] for a in client.d["artifacts"]]
    for wrong in ([], ids[::-1], ids * 2, ids[:1]):
        with pytest.raises(ValueError):
            donor.validate_metadata(client, client.d["repository"], wrong, arch)
    for parts in ([], client.data["artifacts"][:1], client.data["artifacts"] * 2):
        broken = Client(arch)
        broken.data["artifacts"] = parts
        with pytest.raises(ValueError):
            donor.validate_metadata(broken, client.d["repository"], ids, arch)
    for step in ("Preserve latest macOS compiled checkpoint", "Verify handoff snapshot", "Upload tree part 2"):
        broken = Client(arch)
        next(s for s in broken.data["job"]["steps"] if s["name"] == step)["conclusion"] = "skipped"
        with pytest.raises(ValueError):
            donor.validate_metadata(broken, client.d["repository"], ids, arch)


@pytest.fixture(scope="module", params=["arm64", "x64"])
def previous(request, tmp_path_factory):
    d = donor.checkpoint(request.param)
    return d, donor_checkout(tmp_path_factory.mktemp("png-donor") / "repo", d["head_sha"])


def test_exact_real_donors_have_identical_225_prefix(previous):
    d, repo = previous
    png.validate_inputs(repo, ROOT, "macos", d["arch"])
    old, patches = png.arp.patch_selection.select(repo, "macos")
    current, target = png.arp.patch_selection.select(ROOT, "macos")
    assert len(patches) == 225 and len(target) == 226
    assert target == patches + [(png.PATCH, (ROOT / png.PATCH).read_bytes())]
    assert current["selection"]["additions"]["schema_version"] == 2
    assert (ROOT / png.PATCH).read_bytes() == (ROOT / "patches/chromium155-linux/0227-opaque-png-export-consistency.patch").read_bytes()
    with pytest.raises(ValueError):
        png.validate_inputs(repo, ROOT, "macos", "arm64" if d["arch"] == "x64" else "x64")
    with pytest.raises(ValueError):
        png.validate_inputs(repo, ROOT, "linux", d["arch"])


def test_real_source_receipts_and_target_identity(previous, tmp_path):
    d, repo = previous
    raw = FIXTURE[d["arch"]]["files"]["fingerprint-diagnostics/source-final.json"]
    result = json.loads(raw)
    png.validate_source(result, d, old=True)
    selected, _ = png.arp.patch_selection.select(ROOT, "macos")
    result["identity"]["selection"] = selected["selection"]
    result["identity"]["series"].update(sha256=selected["series_sha256"], patches=selected["patches"])
    result["outputs"][png.SOURCE] = png.AFTER
    result["patch_count"] = 226
    png.validate_source(result, d)
    for field in ("identity", "outputs"):
        broken = copy.deepcopy(result)
        broken[field]["unreviewed"] = "unauthorized"
        with pytest.raises(ValueError):
            png.validate_source(broken, d)
    data = evidence("sources")
    put(tmp_path, png.SOURCE, data["before"]["text"])
    assert donor.sha((tmp_path / png.SOURCE).read_bytes()) == png.BEFORE
    apply(tmp_path, ROOT / png.PATCH)
    assert donor.sha((tmp_path / png.SOURCE).read_bytes()) == png.AFTER
    apply(tmp_path, ROOT / png.PATCH, reverse=True)
    assert donor.sha((tmp_path / png.SOURCE).read_bytes()) == png.BEFORE


def test_real_restored_patch_identity_relocates_only_tooling_paths(previous, source_data, tmp_path):
    d, repo = previous
    core = tmp_path / "work/tooling/ungoogled-chromium"
    for name in ("domain_regex.list", "domain_substitution.list"):
        put(core, name, source_data["tooling"]["core/" + name]["text"])
    put(core, "chromium_version.txt", "155.0.8059.39\n")
    tooling = core.parent / "ungoogled-chromium-macos"
    tooling.mkdir()
    old, patches, lite = png.arp._load(repo, core, tooling, "macos")
    target, new_patches, new_lite = png.arp._load(ROOT, core, tooling, "macos")
    saved = json.loads(FIXTURE[d["arch"]]["files"]["src/.chromix-restored-patches.json"])
    relocated = copy.deepcopy(saved["identity"])
    for name in ("regex", "list"):
        relocated[name]["path"] = old[name]["path"]
        target[name]["path"] = saved["identity"][name]["path"]
    assert old == relocated
    assert png.identity_hash(target) == d["target_source_identity"]
    assert len(patches) == 225 and len(new_patches) == 226 and lite == new_lite
    assert new_patches[:-1] == patches
    assert new_patches[-1][2] == [(png.SOURCE, "modify", None)]
    outputs = {name: value for name, value in saved["outputs"].items() if name not in lite}
    assert png.identity_hash(outputs) == d["source_outputs"]
    outputs[png.SOURCE] = png.AFTER
    assert png.identity_hash(outputs) == d["target_source_outputs"]


@pytest.mark.parametrize("arch", ["arm64", "x64"])
def test_download_requires_exact_digest_verified_two_parts(tmp_path, arch):
    d = donor.checkpoint(arch)
    origin = donor.validate_metadata(Client(arch), d["repository"], [a["id"] for a in d["artifacts"]], arch)
    put(tmp_path, "snapshot-origin.json", json.dumps(origin))
    report = dict(status="success", publication="published", phase="complete", repository=d["repository"],
                  run_id=d["run_id"], head_sha=d["head_sha"], artifacts=[], volumes=[])
    for n, a in enumerate(d["artifacts"], 1):
        report["artifacts"].append(dict(a, attempts=[dict(status="success", size_in_bytes=a["size_in_bytes"],
                                                        sha256=a["digest"][7:])]))
        report["volumes"].append(dict(name=f"tree.tar.zst.{n:03d}", artifact_id=a["id"], size_in_bytes=100, sha256="a" * 64))
    put(tmp_path, "snapshot-download.json", json.dumps(report))
    assert len(donor.downloaded(tmp_path, d)) == 2
    broken_reports = [dict(report, publication="unconfirmed"), dict(report, volumes=[]), dict(report, artifacts=[])]
    for n in (0, 1):
        bad = copy.deepcopy(report)
        bad["artifacts"][n]["attempts"][-1]["sha256"] = "0" * 64
        broken_reports.append(bad)
    for bad in broken_reports:
        put(tmp_path, "snapshot-download.json", json.dumps(bad))
        with pytest.raises(ValueError):
            donor.downloaded(tmp_path, d)


@pytest.mark.parametrize("arch", ["arm64", "x64"])
def test_all_original_receipts_and_latest_ninja_state_fail_closed(tmp_path, arch):
    d = donor.checkpoint(arch)
    for name, raw in FIXTURE[arch]["files"].items():
        put(tmp_path, name, raw)
    for name in d["pinned_files"]:
        path = tmp_path / name
        raw = path.read_bytes()
        path.write_bytes(raw + b"\n")
        with pytest.raises(ValueError, match="receipt/GN mismatch"):
            donor.donor_receipts(tmp_path, d)
        path.write_bytes(raw)
    for name in d["state"]:
        put(tmp_path / "src", name, "older nested stage state")
    with pytest.raises(ValueError, match="latest compiled Ninja state"):
        donor.donor_receipts(tmp_path, d)


@pytest.mark.parametrize("arch", ["arm64", "x64"])
def test_architecture_receipts_cannot_mix(tmp_path, arch):
    d = donor.checkpoint(arch)
    donor.reject_mixed_receipts(tmp_path, d)
    other = donor.checkpoint("x64" if arch == "arm64" else "arm64")
    put(tmp_path, other["latest_receipt"], FIXTURE[other["arch"]]["files"][other["latest_receipt"]])
    with pytest.raises(ValueError, match="mixed ARM64/Intel"):
        donor.reject_mixed_receipts(tmp_path, d)


@pytest.mark.parametrize("name", ["tools/patch_selection.py", png.MANIFEST, png.PATCH, "build/args.macos.gn",
                                 "build/ungoogled-revisions.psd1", "patches/series",
                                 "patches/0224-webgl-seeded-readback-noise.patch"])
def test_exact_png_gate_rejects_target_tamper(previous, tmp_path, name):
    import shutil
    d, previous_repo = previous
    repo = tmp_path / "repo"
    for directory in ("tools", "build", "patches"):
        shutil.copytree(ROOT / directory, repo / directory,
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "tests"))
    for path in ROOT.glob("CHROMIUM*"):
        shutil.copy2(path, repo / path.name)
    png.validate_inputs(previous_repo, repo, "macos", d["arch"])
    path = repo / name
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises((ValueError, png.arp.ApplyError)):
        png.validate_inputs(previous_repo, repo, "macos", d["arch"])


def test_png_cli_reaches_verified_download_without_legacy_admission(previous, tmp_path):
    d, repo = previous
    work = tmp_path / "work"
    work.mkdir()
    logs = tmp_path / "chromix-logs"
    logs.mkdir()
    result = subprocess.run([sys.executable, str(ROOT / "tools/migrate_restored_snapshot.py"),
        "--workdir", str(work), "--previous-repo", str(repo), "--repo", str(ROOT),
        "--platform", "macos", "--arch", d["arch"]], capture_output=True, text=True,
        env={**os.environ, "GITHUB_WORKFLOW": d["consumer_workflow"], "RUNNER_TEMP": str(tmp_path)})
    assert result.returncode == 1 and "snapshot-origin.json" in result.stderr
    assert "reviewed controller input changed" not in result.stderr
    assert list(work.iterdir()) == []


def test_exact_png_gate_rejects_dirty_donor(previous):
    d, repo = previous
    path = repo / "build/unreviewed-png-input"
    path.write_text("changed producer checkout")
    try:
        with pytest.raises(ValueError, match="producer checkout must be clean"):
            png.validate_inputs(repo, ROOT, "macos", d["arch"])
    finally:
        path.unlink()


def fixture_policy(tmp_path, monkeypatch, arch):
    fx = Fixture(tmp_path, arch=arch)
    put(fx.src, png.SOURCE, "old PNG conversion\n")
    put(fx.src, png.controller.SOURCE, "protected GN controller\n")
    fx.prepare()
    series(fx.repo, patch(), patch(png.SOURCE, "old PNG conversion", "new PNG conversion"))
    monkeypatch.setattr(png.Policy, "validate_inputs", staticmethod(png.migration._same_inputs))
    monkeypatch.setattr(png, "BEFORE", donor.sha(b"old PNG conversion\n"))
    monkeypatch.setattr(png, "AFTER", donor.sha(b"new PNG conversion\n"))
    monkeypatch.setattr(png.controller, "REVIEWED_SOURCE_AFTER_SHA256", donor.sha(b"protected GN controller\n"))
    d = donor.checkpoint(arch)
    for name, raw in FIXTURE[arch]["files"].items():
        if not (fx.work / name).exists():
            put(fx.work, name, raw)
    d["state"] = {n: donor.sha((fx.src / n).read_bytes()) for n in d["state"]}
    return fx, png.Policy(fx.work, d)


@pytest.mark.parametrize("arch", ["arm64", "x64"])
def test_private_full_reverse_forward_retains_cache_gn_and_receipts(tmp_path, monkeypatch, arch):
    fx, policy = fixture_policy(tmp_path, monkeypatch, arch)
    before = snapshot(fx.src)
    result = fx.run(policy=policy)
    assert result["changed_files"] == [png.SOURCE]
    after = snapshot(fx.src)
    for name in before.keys() - {png.SOURCE, png.migration.READY, png.arp.MARKER}:
        assert before[name] == after[name]
    assert (fx.src / png.SOURCE).stat().st_mtime_ns > before[png.SOURCE][1]
    policy.validate_preserved(fx.src)
    fx.check()


@pytest.mark.parametrize("arch", ["arm64", "x64"])
@pytest.mark.parametrize("kind", ["postimage", "extra-source", "patch-conflict", "out-change", "receipt-change", "gn-change"])
def test_failed_private_migration_never_publishes(tmp_path, monkeypatch, arch, kind):
    fx, policy = fixture_policy(tmp_path, monkeypatch, arch)
    before = snapshot(fx.src)
    if kind == "postimage":
        monkeypatch.setattr(png, "AFTER", "0" * 64)
    elif kind == "extra-source":
        series(fx.repo, patch(new="different example.com"), patch(png.SOURCE, "old PNG conversion", "new PNG conversion"))
    elif kind == "patch-conflict":
        series(fx.repo, patch(), patch(png.SOURCE, "wrong PNG conversion", "new PNG conversion"))
    elif kind == "out-change":
        put(fx.src, "out/Default/obj/cached.o", "changed object")
    elif kind == "gn-change":
        put(fx.src, png.controller.SOURCE, "changed GN")
    else:
        put(fx.work, policy.d["latest_receipt"], "changed history")
    with pytest.raises((ValueError, png.arp.ApplyError)):
        fx.run(policy=policy)
    after = snapshot(fx.src)
    for name in (png.SOURCE, png.migration.READY, png.arp.MARKER, "listed.txt"):
        assert before[name] == after[name]
    assert (fx.src / png.migration.TRANSACTION).exists()
    with pytest.raises(png.arp.ApplyError, match="in-progress/partial"):
        fx.run(policy=policy)


@pytest.mark.parametrize("arch", ["arm64", "x64"])
def test_cli_exact_metadata_route(tmp_path, monkeypatch, arch):
    d = donor.checkpoint(arch)
    args = ["validate_posix_snapshot.py", "--repository", d["repository"], "--run-id", str(d["run_id"]),
            "--stage", "1", "--attempt", "1", "--artifact-ids", ",".join(str(a["id"]) for a in d["artifacts"]),
            "--arch", arch, "--report", str(tmp_path / "origin.json")]
    monkeypatch.setattr(snapshots, "Client", lambda *a: Client(arch))
    monkeypatch.setenv("GITHUB_WORKFLOW", d["consumer_workflow"])
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr("sys.argv", args)
    assert snapshots.main() == 0
    for flag, value in [("--run-id", "1"), ("--stage", "8"), ("--attempt", "2"),
                        ("--arch", "x64" if arch == "arm64" else "arm64")]:
        wrong = list(args)
        wrong[wrong.index(flag) + 1] = value
        monkeypatch.setattr("sys.argv", wrong)
        with pytest.raises(ValueError):
            snapshots.main()


@pytest.mark.parametrize("arch", ["arm64", "x64"])
def test_no_cold_fallback(tmp_path, arch):
    d = donor.checkpoint(arch)
    result = subprocess.run(["bash", str(ROOT / "build/posix/ci-stage.sh"), "--platform", "macos", "--arch", arch,
                             "--workdir", str(tmp_path)], capture_output=True, text=True,
        env={**os.environ, "GITHUB_WORKFLOW": d["consumer_workflow"], "CHROMIX_BUILD_PROFILE": "fast",
             "CHROMIX_USE_UPSTREAM_CACHE": "1"})
    assert result.returncode != 0 and "cold fallback forbidden" in result.stderr
    assert not (tmp_path / "src").exists()


@pytest.mark.parametrize("arch", ["arm64", "x64"])
@pytest.mark.parametrize("stage,code", [(1, 42), (1, 124), (8, 124)])
def test_png_stage_owns_nested_controller_receipts_and_preserves_tree(monkeypatch, arch, stage, code):
    from tools.tests.test_posix_stages import PosixStageHandoffExecutionTest
    harness = PosixStageHandoffExecutionTest()
    harness.setUp()
    try:
        harness.put(harness.bindir / "python3", '#!/bin/sh\nprintf "guard %s\\n" "$*" >> "$CALL_LOG"\nexit 0\n')
        d = donor.checkpoint(arch)
        for name, raw in FIXTURE[arch]["files"].items():
            if "controller-migration.json" in name:
                put(harness.work, name, raw)
        monkeypatch.setenv("GITHUB_WORKFLOW", d["consumer_workflow"])
        result = harness.run_stage("macos", arch, "fast", stage, phase="timeout", build_rc=code)
        assert (result.returncode != 0) == (code != 124 or stage == 8)
        assert "upload_snapshot=true" in harness.output.read_text()
        assert "finished=true" not in harness.output.read_text()
        calls = harness.calls.read_text()
        assert calls.count("migrate_macos155_png_snapshot.py") == 2
        assert "controller_snapshot.py" not in calls
        assert calls.index("--phase after") < calls.index("\nsnapshot\n")
    finally:
        harness.doCleanups()


@pytest.mark.parametrize("workflow,arch", [("build-mac155-intel-png-resume", "arm64"),
    ("build-mac155-arm64-png-resume", "x64"), ("build-mac155-intel-controller-resume", "x64"),
    ("build-mac155-arm64-controller-resume", "arm64"), ("unregistered", "arm64")])
def test_png_receipt_never_enters_wrong_architecture_or_legacy_gate(tmp_path, workflow, arch):
    put(tmp_path, png.RECEIPT, "{}")
    result = subprocess.run(["bash", str(ROOT / "build/posix/ci-stage.sh"), "--platform", "macos", "--arch", arch,
                             "--workdir", str(tmp_path)], capture_output=True, text=True,
        env={**os.environ, "GITHUB_WORKFLOW": workflow, "CHROMIX_BUILD_PROFILE": "fast", "CHROMIX_USE_UPSTREAM_CACHE": "1"})
    assert result.returncode != 0 and "cold fallback forbidden" in result.stderr
    assert not (tmp_path / "src").exists()


def test_real_native_arm_png_pixels_equal_but_original_wire_gate_fails():
    import base64
    from tools.tests.test_linux155_opaque_png_export import png_chunks
    import importlib.util
    spec = importlib.util.spec_from_file_location("mac_png_chain", ROOT / "sdk/python/chromix/_canvas_chain.py")
    chain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chain)

    raw = (ROOT / "tools/tests/fixtures/macos155_png_native_before.json.gz").read_bytes()
    assert donor.sha(raw) == "4fdb22663f2a906ef9a68c114318d53177a8bffe9f3cb29a3524f261f887f5f5"
    data = json.loads(gzip.decompress(raw))
    assert data["run_id"] == donor.checkpoint("arm64")["run_id"]
    assert data["probe_sha256"] == donor.sha((ROOT / "sdk/python/chromix/canvas_chain_probe.js").read_bytes())
    assert data["status"] == "failed" and len(data["errors"]) == 24
    assert len(set(data["signatures"])) == 1
    for sample in data["samples"]:
        item = sample["export"]
        url = item["dataURL"].removeprefix("data:image/png;base64,")
        blob, sync = (base64.b64decode(x, validate=True) for x in (item["bytes"], url))
        blob_chunks, sync_chunks = png_chunks(blob), png_chunks(sync)
        assert blob != sync and blob_chunks[0][1][9] == 2 and sync_chunks[0][1][9] == 6
        assert [x for x in blob_chunks if x[0] == b'iCCP'] == [x for x in sync_chunks if x[0] == b'iCCP']
        assert chain.decode(item["bytes"], "image/png")["rgba"] == chain.decode(url, "image/png")["rgba"] == sample["reference"]
        assert chain.export_evidence_errors(item, "html", raw_payloads=True) == [
            "DataURL repeat/Blob mismatch", "DataURL/Blob payload mismatch"]


def test_workflows_reuse_all_native_gates_and_compiled_failure_preservation():
    for arch, d in donor.checkpoints().items():
        text = (ROOT / f'.github/workflows/{d["consumer_workflow"]}.yml').read_text()
        for required in ("uses: ./.github/workflows/build-posix-github.yml", "max-stages: 8", "build_profile: fast",
                         "use_upstream_cache: true", "resume_tree_stage: '1'", "resume_attempt: '1'",
                         f'resume_run_id: \'{d["run_id"]}\'', f'arch: {arch}', "cancel-in-progress: false"):
            assert required in text
        assert "push:" not in text
    stage = (ROOT / "build/posix/ci-stage.sh").read_text()
    for gate in ("macos_browser_smoke.py", "fingerprint_acceptance.py", "--source-report", "--expected-sha256",
                 "shasum -a 256 -c SHA256SUMS", "emit runtime_verified true"):
        assert gate in stage
    assert stage.index("fingerprint_acceptance.py") < stage.index("emit runtime_verified true")
    shared = (ROOT / ".github/workflows/build-posix-github.yml").read_text()
    assert "Preserve latest macOS compiled checkpoint" in shared and "Verify handoff snapshot" in shared
    assert "steps.stage.outputs.compiled_ready == 'true'" in shared
