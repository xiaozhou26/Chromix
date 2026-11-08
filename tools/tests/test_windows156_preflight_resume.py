"""Fixed donor recovery must fail closed before any source preparation or compilation."""
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import gen_windows_arm64_workflow as generator
import windows156_preflight_resume as recovery


class Client:
    def __init__(self, arch):
        donor = recovery.DONORS[arch]
        self.run_id = donor["run_id"]
        self.run = dict(id=donor["run_id"], head_sha=recovery.DONOR_SHA, head_branch="main", run_attempt=1,
                        status="completed", conclusion="failure", event="workflow_dispatch",
                        workflow_id=donor["workflow_id"], path=f".github/workflows/build-win-{arch}-156-fresh.yml",
                        repository=dict(id=1342691290, full_name=recovery.REPOSITORY),
                        head_repository=dict(id=1342691290, full_name=recovery.REPOSITORY))
        steps = {"Run stage 1": "failure", "Restore latest valid preserved snapshot": "skipped",
                 "Upload final bundle": "skipped", "Ensure build tree snapshot": "success",
                 "Upload fingerprint diagnostics": "success",
                 **{f"Upload tree part {index}": "success" for index in range(1, 5)}}
        self.jobs = [dict(id=donor["job_id"], name=f"Windows156 {arch} stage 1", head_sha=recovery.DONOR_SHA,
                          run_id=donor["run_id"], run_attempt=1, status="completed", conclusion="failure",
                          steps=[dict(name=name, conclusion=value) for name, value in steps.items()])]
        self.artifacts = deepcopy(recovery.manifest(arch)["artifacts"])
        for item in self.artifacts:
            item["workflow_run"] = dict(id=donor["run_id"], head_sha=recovery.DONOR_SHA)

    def get(self, path):
        assert path == f'/actions/runs/{self.run_id}'
        return self.run

    def items(self, path, name):
        return getattr(self, name)


@pytest.mark.parametrize("arch", recovery.DONORS)
def test_exact_latest_failed_stage1_metadata(arch):
    assert recovery.metadata(Client(arch), arch) == recovery.manifest(arch)


@pytest.mark.parametrize("field,value", [
    ("id", 1), ("head_sha", "f" * 40), ("head_branch", "other"), ("run_attempt", 2),
    ("status", "in_progress"), ("conclusion", "success"), ("event", "push"),
    ("workflow_id", 1), ("path", ".github/workflows/build-win-x64-github.yml"),
    ("head_repository", dict(id=1342691290, full_name="other/repository")),
])
def test_metadata_rejects_cross_run_sha_workflow_or_incomplete(field, value):
    client = Client("x64")
    client.run[field] = value
    with pytest.raises(ValueError, match="run identity"):
        recovery.metadata(client, "x64")


@pytest.mark.parametrize("mutation", ["job-id", "job-sha", "job-attempt", "step", "duplicate-job",
                                        "newer-stage", "missing-part", "digest", "bytes", "expired", "artifact-sha"])
def test_metadata_rejects_incomplete_or_newer_evidence(mutation):
    client = Client("arm64")
    if mutation.startswith("job-"):
        key = {"job-id": "id", "job-sha": "head_sha", "job-attempt": "run_attempt"}[mutation]
        client.jobs[0][key] = 2
    elif mutation == "step":
        client.jobs[0]["steps"][-1]["conclusion"] = "failure"
    elif mutation == "duplicate-job":
        client.jobs.append(deepcopy(client.jobs[0]))
    elif mutation == "newer-stage":
        client.artifacts.append(dict(client.artifacts[0], id=99, name="win-arm64-156-tree-s2-attempt-1-part1"))
    elif mutation == "missing-part":
        client.artifacts.pop()
    elif mutation == "artifact-sha":
        client.artifacts[0]["workflow_run"]["head_sha"] = "b" * 40
    else:
        key = {"digest": "digest", "bytes": "size_in_bytes", "expired": "expired"}[mutation]
        client.artifacts[0][key] = True if mutation == "expired" else 123
    with pytest.raises(ValueError):
        recovery.metadata(client, "arm64")


def download(arch):
    origin = recovery.manifest(arch)
    return dict(status="success", phase="complete", publication="published", repository=recovery.REPOSITORY,
                head_sha=recovery.DONOR_SHA, run_id=origin["run_id"], artifacts=deepcopy(origin["artifacts"]),
                volumes=[dict(name=f"tree.7z.{index:03d}", artifact_id=part["id"], size_in_bytes=100,
                              sha256="a" * 64) for index, part in enumerate(origin["artifacts"], 1)],
                total_size_in_bytes=200)


@pytest.mark.parametrize("arch", recovery.DONORS)
def test_complete_archive_receipt_is_required(arch):
    origin = recovery.manifest(arch)
    receipt = download(arch)
    recovery.checked_download(origin, receipt, arch)
    for key in receipt:
        changed = deepcopy(receipt)
        changed.pop(key)
        with pytest.raises(ValueError):
            recovery.checked_download(origin, changed, arch)
    receipt["artifacts"][1]["digest"] = "sha256:" + "b" * 64
    with pytest.raises(ValueError, match="artifact"):
        recovery.checked_download(origin, receipt, arch)


@pytest.mark.parametrize("content", [None, b"# ninja log v5\n", b"# ninja log v5\r\n"])
def test_precompile_phase_does_not_invent_compile_logs(tmp_path, content):
    out = tmp_path / "out"
    out.mkdir()
    if content is not None:
        (out / ".ninja_log").write_bytes(content)
    (out / "build.ninja").write_text("generated GN")
    (out / "gn.exe").write_bytes(b"bootstrap executable")
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in out.iterdir()}
    recovery.precompile_logs(out)
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in out.iterdir()}


@pytest.mark.parametrize("name,data", [
    (".ninja_log", b""), (".ninja_log", b"corrupt"), (".ninja_log", b"# ninja log v5\n0\t1\t1\tfoo.obj\tabc\n"),
    (".ninja_deps", b""), (".ninja_deps", b"# ninjadeps\n\x04\0\0\0bad"),
    ("keep.obj", b"compiled object"), ("chrome.exe", b"compiled browser"),
])
def test_bad_or_compiled_logs_never_take_missing_log_exception(tmp_path, name, data):
    (tmp_path / name).write_bytes(data)
    before = (tmp_path / name).read_bytes()
    with pytest.raises(ValueError, match="Ninja|compiled"):
        recovery.precompile_logs(tmp_path)
    assert (tmp_path / name).read_bytes() == before


def test_complete_original_source_receipt_not_a_subset(tmp_path):
    payload = gzip.decompress((ROOT / "tools/tests/fixtures/windows156_preflight_source.json.gz").read_bytes())
    assert hashlib.sha256(payload).hexdigest() == recovery.SOURCE_RECEIPT_SHA256
    proof = json.loads(payload)
    assert proof["patch_count"] == 232 and len(proof["outputs"]) == 171
    for arch, donor in recovery.DONORS.items():
        target = tmp_path / "fingerprint-diagnostics" / donor["source_receipt"]
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(payload)
        recovery.source_receipt(tmp_path, arch, proof)
        changed = deepcopy(proof)
        changed["outputs"].pop(next(iter(changed["outputs"])))
        with pytest.raises(ValueError, match="full source proof"):
            recovery.source_receipt(tmp_path, arch, changed)
        target.write_text(json.dumps(changed))
        with pytest.raises(ValueError, match="original source receipt"):
            recovery.source_receipt(tmp_path, arch, changed)


@pytest.mark.parametrize("arch", recovery.DONORS)
def test_gn_contract_preserves_all_args_and_pgo(tmp_path, arch):
    work, repo, core, tooling = (tmp_path / name for name in ("work", "repo", "core", "tooling"))
    for path in (core, tooling, repo / "build", work / "src/out/Chromix"):
        path.mkdir(parents=True)
    (core / "flags.gn").write_text('chrome_pgo_phase = 2\nhost_cpu = "x64"\n')
    (tooling / "flags.windows.gn").write_text('target_os = "win"\n')
    (repo / "build/args.windows.gn").write_text('target_cpu = "x64"\nis_debug = false\n')
    (repo / "build/args.windows.arm64.gn").write_text('target_cpu = "arm64"\n')
    with mock.patch.object(recovery.fresh, "initialize"), mock.patch.object(recovery.fresh, "checked_pins"):
        overlay = recovery.fresh.overlay(repo, work, arch)
    args = work / "src/out/Chromix/args.gn"
    inputs = [core / "flags.gn", tooling / "flags.windows.gn", repo / "build/args.windows.gn"]
    if arch == "arm64":
        inputs.append(repo / "build/args.windows.arm64.gn")
    subprocess.run([sys.executable, str(ROOT / "tools/merge_gn_args.py"), str(args), *map(str, inputs), str(overlay)], check=True)
    recovery.gn_contract(repo, work, arch, core, tooling)
    original = args.read_text()
    for changed in (original.replace("chrome_pgo_phase = 2", "chrome_pgo_phase = 0"),
                    original.replace("is_debug = false", "is_debug = true"), original + "use_remoteexec = true\n"):
        args.write_text(changed)
        with pytest.raises(ValueError, match="GN arguments"):
            recovery.gn_contract(repo, work, arch, core, tooling)
        assert args.read_text() == changed


@pytest.mark.parametrize("arch", recovery.DONORS)
def test_workflow_is_fixed_no_cold_and_preserves_native_gate(arch):
    workflow = generator.preflight_workflow(arch)
    assert (ROOT / f".github/workflows/build-win-{arch}-156-preflight-resume.yml").read_text() == generator.render_preflight(arch)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert set(workflow["on"]["workflow_dispatch"]["inputs"]) == {"compile_jobs"}
    for index in range(1, 13):
        steps = workflow["jobs"][f"build-{index}"]["steps"]
        stage = next(step for step in steps if step.get("id") == "stage")
        assert stage["run"].endswith("-FreshNative -FromSynced")
        assert "continue-on-error" not in stage
        logs = next(step for step in steps if step.get("name") == "Upload fingerprint diagnostics")
        assert f"win-{arch}-" in logs["with"]["name"]
        if index == 1:
            restore = next(step for step in steps if step.get("name") == "Restore exact reviewed precompile donor")
            assert "if" not in restore and steps.index(restore) < steps.index(stage)
            assert restore["run"].index("--phase metadata") < restore["run"].index("download_windows_snapshot.py")
            assert restore["run"].index("7z.exe t") < restore["run"].index("--phase verify")
            assert "--phase initialize" not in restore["run"]
    if arch == "arm64":
        assert workflow["jobs"]["verify-arm64"] == generator.fresh_workflow(arch)["jobs"]["verify-arm64"]
        assert workflow["jobs"]["verify-arm64"]["runs-on"] == "windows-11-arm"


def test_unknown_helper_hash_is_fail_closed():
    assert recovery.OLD_HELPER_SHA256 not in recovery.REVIEWED_HELPER_SHA256
    assert all(len(value) == 64 for value in recovery.REVIEWED_HELPER_SHA256)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def test_main_checkout_rejects_nonhelper_changes_and_unreviewed_helper(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "core.autocrlf", "false")
    helper = repo / recovery.HELPER
    helper.parent.mkdir()
    helper.write_text("old helper\n")
    (repo / "source-patch").write_text("unchanged source\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "donor")
    base = git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(recovery, "DONOR_SHA", base)
    monkeypatch.setattr(recovery, "MAIN_BASE", base)
    monkeypatch.setattr(recovery, "OLD_HELPER_SHA256", hashlib.sha256(helper.read_bytes()).hexdigest())
    helper.write_text("reviewed helper fix\n")
    git(repo, "commit", "-qam", "helper fix")
    target = git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(recovery, "REVIEWED_HELPER_SHA256", frozenset())
    with pytest.raises(ValueError, match="hash-reviewed"):
        recovery.checkout_identity(repo, target)
    monkeypatch.setattr(recovery, "REVIEWED_HELPER_SHA256", {hashlib.sha256(helper.read_bytes()).hexdigest()})
    recovery.checkout_identity(repo, target)
    helper.write_text("uncommitted code\n")
    with pytest.raises(RuntimeError, match="tracked content"):
        recovery.checkout_identity(repo, target)
    git(repo, "checkout", "--", recovery.HELPER)
    (repo / "source-patch").write_text("source migration forbidden\n")
    git(repo, "commit", "-qam", "source change")
    with pytest.raises(ValueError, match="outside reviewed"):
        recovery.checkout_identity(repo, git(repo, "rev-parse", "HEAD"))


def test_consumer_is_main_only_and_not_donor(monkeypatch):
    values = dict(GITHUB_SHA="a" * 40, GITHUB_REPOSITORY=recovery.REPOSITORY, GITHUB_REF="refs/heads/main",
                  GITHUB_JOB="build-1", GITHUB_RUN_ID="999", GITHUB_RUN_ATTEMPT="1",
                  GITHUB_WORKFLOW_REF=f"{recovery.REPOSITORY}/.github/workflows/build-win-x64-156-preflight-resume.yml@refs/heads/main")
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert recovery.consumer("x64") == "a" * 40
    for key, value in dict(GITHUB_REF="refs/heads/fix", GITHUB_RUN_ID=str(recovery.DONORS["x64"]["run_id"]),
                           GITHUB_SHA=recovery.DONOR_SHA, GITHUB_JOB="build-2", GITHUB_WORKFLOW_REF="other").items():
        monkeypatch.setenv(key, value)
        with pytest.raises(ValueError, match="main-only"):
            recovery.consumer("x64")
        monkeypatch.setenv(key, values[key])


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    work, repo, previous = (tmp_path / name for name in ("work", "repo", "previous"))
    src = work / "src"
    src.mkdir(parents=True)
    repo.mkdir()
    previous.mkdir()
    arch, key = "x64", "a" * 64
    markers = {
        ".chromix-source-unpacked": recovery.fresh.VERSION,
        ".chromix-ungoogled-core": recovery.fresh.CORE,
        ".chromix-ungoogled-windows": recovery.fresh.WINDOWS,
        ".chromix-binaries-pruned": recovery.fresh.CORE,
        ".chromix-domain-substituted": recovery.fresh.CORE,
        ".chromix-patches": key, ".chromix-patch-selection": key,
        ".chromix-source-ready": "|".join((recovery.fresh.VERSION, recovery.fresh.CORE, recovery.fresh.WINDOWS, key)),
    }
    for name, value in markers.items():
        (src / name).write_text(value)
    for name in ("out/Chromix/args.gn", "out/Chromix/build.ninja", "out/Chromix/gn.exe",
                 "build/toolchain/win/midl.py", "chrome/build/pgo_profiles/" + recovery.fresh.PROFILES[arch],
                 "chrome/build/pgo_profiles/" + recovery.fresh.PROFILES[arch] + ".chromix-sha256"):
        path = src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"preserved")
    for name in ("ungoogled-chromium", "ungoogled-chromium-windows"):
        (work / "tooling" / name).mkdir(parents=True)
    payload = gzip.decompress((ROOT / "tools/tests/fixtures/windows156_preflight_source.json.gz").read_bytes())
    receipt = work / "fingerprint-diagnostics" / recovery.DONORS[arch]["source_receipt"]
    receipt.parent.mkdir()
    receipt.write_bytes(payload)
    monkeypatch.setattr(recovery.fresh, "initialize", mock.Mock())
    monkeypatch.setattr(recovery.fresh, "checked_pins", mock.Mock(return_value={"version": recovery.fresh.VERSION}))
    monkeypatch.setattr(recovery.patch_selection, "preparation_key", mock.Mock(return_value=key))
    monkeypatch.setattr(recovery.files, "_host_git", mock.Mock(return_value="git"))
    monkeypatch.setattr(recovery.files, "_tree", mock.Mock(side_effect=lambda git, root, pin: (pin, {})))
    monkeypatch.setattr(recovery.files, "_tracked", mock.Mock(return_value={}))
    monkeypatch.setattr(recovery.patches, "_load", mock.Mock(return_value=({"identity": "same"}, [], {})))
    monkeypatch.setattr(recovery, "gn_contract", mock.Mock())
    monkeypatch.setattr(recovery.verify_patch_stack, "verify", mock.Mock(return_value=json.loads(payload)))
    monkeypatch.setattr(recovery.fresh, "probe", mock.Mock())
    import repair_windows_midl
    import install_windows_metagen_bindings as bindings
    monkeypatch.setattr(repair_windows_midl, "REPAIRED_SHA256", hashlib.sha256(b"preserved").hexdigest())
    monkeypatch.setattr(bindings, "source_contract", mock.Mock())
    monkeypatch.setattr(bindings, "validate_sources", mock.Mock())
    return repo, previous, work, arch


def test_source_gate_requires_ready_gn_and_full_source_but_not_installed_metagen(prepared):
    repo, previous, work, arch = prepared
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in work.rglob("*") if p.is_file()}
    report = recovery.verify_source(*prepared)
    assert report["status"] == "verified" and report["phase"] == "post-gn-precompile"
    assert report["changed_source_files"] == []
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in work.rglob("*") if p.is_file()}
    assert not (work / "src/out/Chromix/.ninja_log").exists()
    assert not (work / "src/third_party/llvm-build/Release+Asserts/bindings/python/clang").exists()
    recovery.fresh.probe.assert_called_once_with(work / "src", arch)


@pytest.mark.parametrize("change", ["ready", "patches", "progress", "key", "old-patches", "old-pins", "pgo", "source", "tool-head", "tool-bytes"])
def test_source_gate_rejects_changed_or_incomplete_tree_before_compile(prepared, change):
    repo, previous, work, arch = prepared
    src = work / "src"
    if change in ("ready", "patches"):
        (src / (".chromix-source-ready" if change == "ready" else ".chromix-patches")).write_text("wrong")
    elif change == "progress":
        (src / ".chromix-layer-in-progress").write_text("unfinished")
    elif change == "key":
        recovery.patch_selection.preparation_key.side_effect = ["a" * 64, "b" * 64]
    elif change == "old-patches":
        recovery.patches._load.side_effect = [({"identity": "old"}, [], {}), ({"identity": "new"}, [], {})]
    elif change == "old-pins":
        recovery.fresh.checked_pins.side_effect = [{"version": "new"}, {"version": "old"}]
    elif change == "pgo":
        (src / "chrome/build/pgo_profiles" / recovery.fresh.PROFILES[arch]).unlink()
    elif change == "source":
        recovery.verify_patch_stack.verify.return_value["outputs"].clear()
    elif change == "tool-head":
        recovery.files._tree.side_effect = RuntimeError("tooling head mismatch")
    elif change == "tool-bytes":
        recovery.files._tracked.side_effect = RuntimeError("tooling bytes mismatch")
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in work.rglob("*") if p.is_file()}
    with pytest.raises((ValueError, RuntimeError)):
        recovery.verify_source(*prepared)
    recovery.fresh.probe.assert_not_called()
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in work.rglob("*") if p.is_file()}


def test_donor_data_checkout_retains_authenticated_windows_representation(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "core.autocrlf", "false")
    (repo / "same.tq").write_bytes(b"source\n")
    (repo / "helper.py").write_bytes(b"old helper\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "donor")
    monkeypatch.setattr(recovery, "DONOR_SHA", git(repo, "rev-parse", "HEAD"))
    (repo / "helper.py").write_bytes(b"new helper\n")
    git(repo, "commit", "-qam", "helper")
    (repo / "same.tq").write_bytes(b"source\r\n")
    previous = tmp_path / "previous"
    previous.mkdir()
    recovery.old_checkout("git", repo, previous)
    assert (previous / "same.tq").read_bytes() == b"source\r\n"
    assert (previous / "helper.py").read_bytes() == b"old helper\n"
    assert not (previous / ".git").exists()
