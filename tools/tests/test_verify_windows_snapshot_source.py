"""Real Git and patch-stack checks for the read-only Windows153 resume gate."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import verify_windows_snapshot_source as verify

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "third_party/blink/fixture.cc"
LITE = "v8/test/torque/test-torque.tq"
PATCH = "patches/0001-fixture.patch"


def put(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


def run_cli(*args):
    return subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "tools/verify_windows_snapshot_source.py"), *args],
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True)


def git(root, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-C", str(root), *args],
                          env=env, check=True, capture_output=True).stdout


def commit(root):
    if not (root / ".git").exists():
        git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    return git(root, "rev-parse", "HEAD").decode().strip()


def snapshot(root):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns, stat.S_IMODE(p.stat().st_mode))
            for p in root.rglob("*") if p.is_file()}


class Fixture:
    def __init__(self, root, monkeypatch):
        self.previous, self.repo, self.work = (root / name for name in ("previous", "current", "work"))
        self.src = self.work / "src"
        self.core = self.work / "tooling/ungoogled-chromium"
        self.tooling = self.work / "tooling/ungoogled-chromium-windows"
        put(self.core, "domain_regex.list", rb"example\.com#blocked.test" + b"\n")
        put(self.core, "flags.gn", "is_debug = false\n")
        put(self.core, "utils/domain_substitution.py", "raise RuntimeError('donor code executed')\n")
        core_sha = commit(self.core)
        put(self.tooling, "domain_substitution.list", SOURCE + "\n")
        put(self.tooling, "flags.windows.gn", 'target_cpu = "x64"\n')
        put(self.tooling, "download.py", "raise RuntimeError('donor code executed')\n")
        platform_sha = commit(self.tooling)
        for name in ("CHROMIUM_VERSION", "CHROMIUM_WINDOWS_VERSION", "CHROMIUM_MACOS_VERSION", "CHROMIUM_LINUX_VERSION",
                     "build/ungoogled-revisions.psd1", "build/upstream-cache.json", "build/args.windows.gn"):
            path = ROOT / name
            if path.exists():
                put(self.previous, name, path.read_text())
        from tools.tests.test_fetch_upstream_cache import pinned_windows153_source

        manifest_path = self.previous / "build/upstream-cache.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["sources"]["windows"] = pinned_windows153_source(self.previous)
        put(self.previous, "build/upstream-cache.json", json.dumps(manifest))
        for name in ("build/ungoogled-revisions.psd1", "build/upstream-cache.json"):
            path = self.previous / name
            data = path.read_text().replace("31e6f2dd3bb2f113800d25ae359f024684addb51", core_sha)
            data = data.replace("657b9731b68aae35d4ee02428684ab8bdceb9181", platform_sha)
            put(self.previous, name, data)
        put(self.previous, "build/windows/prepare-ungoogled.ps1", "throw 'donor code executed'\n")
        put(self.previous, "tools/prepare_restored_build.py", "raise RuntimeError('donor code executed')\n")
        put(self.previous, "assets/fixture.dat", b"asset\0")
        put(self.previous, verify.arp.LITE + "/" + LITE, "lite payload\n")
        put(self.previous, "patches/series", PATCH + "\n")
        put(self.previous, PATCH, f"diff --git a/{SOURCE} b/{SOURCE}\n--- a/{SOURCE}\n+++ b/{SOURCE}\n"
            "@@ -1,3 +1,3 @@\n context\n-base example.com\n+patched example.com\n tail\n")
        self.sha = commit(self.previous)
        shutil.copytree(self.previous, self.repo)
        monkeypatch.setattr(verify, "DONOR_SHA", self.sha)
        monkeypatch.setattr(verify, "TRUSTED_REPO", self.repo)
        put(self.src, SOURCE, "context\npatched blocked.test\ntail\n")
        put(self.src, LITE, "lite payload\n")
        put(self.src, verify.MIDL_SOURCE, "# pinned wrapper, not executed by the verifier\n")
        put(self.src, "chrome/VERSION", "MAJOR=153\nMINOR=0\nBUILD=8010\nPATCH=47\n")
        put(self.src, "BUILD.gn", "# fixture\n")
        for name in ("build.ninja", ".ninja_log", ".ninja_deps", "obj/cached.obj"):
            put(self.src, "out/Default/" + name, "cached state\n")
        original = 'target_cpu = "x64"\ntarget_os = "win"\nthin_lto_enable_optimizations = true\n'
        put(self.src, "out/Default/args.gn", original)
        repo_identity, _, manifest = verify.upstream.identities(self.repo, "windows", "x64")
        self.receipt = {"schema_version": 1, "owner": verify.upstream.OWNER, "status": "restored",
                        "platform": "windows", "arch": "x64", "identity": repo_identity, "manifest": manifest,
                        "extraction_scope": verify.upstream.fetcher.SOURCE_SCOPE,
                        "external_symlink_paths": [], "original_args": verify.upstream.source_args(self.src, repo_identity)}
        put(self.src, verify.upstream.MARKER, json.dumps(self.receipt))
        args = original + (self.core / "flags.gn").read_text() + (self.repo / "build/args.windows.gn").read_text()
        values = verify.upstream.parse_gn_assignments(args)
        put(self.src, "out/Default/args.gn", "\n".join(f"{k} = {v}" for k, v in values.items()) + "\n")
        pins = verify.load_pins(self.repo, "windows")
        key = verify._ready_key(self.repo, pins)
        for name, value in {
            ".chromix-source-unpacked": verify.VERSION, ".chromix-ungoogled-core": core_sha,
            ".chromix-ungoogled-windows": platform_sha, ".chromix-binaries-pruned": core_sha,
            ".chromix-domain-substituted": core_sha, ".chromix-toolchain-ready": core_sha + "|" + platform_sha,
            ".chromix-patches": key.rsplit("|", 1)[1], ".chromix-source-ready": key,
        }.items():
            put(self.src, name, value + "\n")
        identity, _, _ = verify.arp._load(self.repo, self.core, self.tooling, "windows")
        put(self.src, verify.arp.MARKER, verify.arp._json({
            "schema_version": 1, "identity": identity, "identity_sha256": verify.arp._sha(verify.arp._json(identity)),
            "outputs": {name: hashlib.sha256((self.src / name).read_bytes()).hexdigest() for name in (SOURCE, LITE)},
        }))

    def run(self, **kwargs):
        return verify.verify(self.work, self.previous, self.repo, kwargs.pop("sha", self.sha), **kwargs)


@pytest.fixture(params=[6, 8])
def fx(tmp_path, monkeypatch, request):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.stage = request.param
    if fixture.stage == 8:
        monkeypatch.setattr(verify, "STAGE8_SHA", fixture.sha)
        put(fixture.repo, "tools/verify_windows_snapshot_source.py", "# target helper\n")
        target = commit(fixture.repo)
        for key, value in {"GITHUB_RUN_ID": "90001", "GITHUB_RUN_ATTEMPT": "1",
                           "GITHUB_JOB": "build-8", "GITHUB_SHA": target}.items():
            monkeypatch.setenv(key, value)
    return fixture


def test_verification_is_read_only_and_repeatable_with_real_git_stack(fx):
    before = snapshot(fx.work)
    result = fx.run()
    assert result["status"] == "verified"
    assert result["changed_files"] == []
    assert result["verification"]["method"] == "reverse-forward-in-scratch"
    assert result["verification"]["patch_count"] == 1
    assert result == fx.run()
    assert snapshot(fx.work) == before


@pytest.mark.parametrize("name", sorted(verify.ALLOWED_CHANGES | verify.STAGE8_ALLOWED_CHANGES))
@pytest.mark.parametrize("committed", [False, True])
def test_exact_allowlisted_repair_and_orchestration_changes_are_allowed(fx, name, committed, monkeypatch):
    put(fx.repo, name, "# changed target helper; never run by verifier\n")
    if committed:
        if fx.stage == 8:
            git(fx.repo, "add", ".")
            git(fx.repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "--amend", "--no-edit", "-q")
            monkeypatch.setenv("GITHUB_SHA", git(fx.repo, "rev-parse", "HEAD").decode().strip())
        else:
            commit(fx.repo)
    allowed = verify.STAGE8_ALLOWED_CHANGES if fx.stage == 8 else verify.ALLOWED_CHANGES
    if name not in allowed:
        with pytest.raises((ValueError, verify.arp.ApplyError)):
            fx.run()
    else:
        result = fx.run()
        assert result["status"] == "verified"
        if fx.stage == 8:
            assert result["profile"] == verify.STAGE8_PROFILE
            assert result["run"]["GITHUB_SHA"] == os.environ["GITHUB_SHA"]
            assert name in result["repository_changed_files"]


@pytest.mark.parametrize("name", ["build/upstream-cache.json", "build/ungoogled-revisions.psd1", "CHROMIUM_WINDOWS_VERSION",
                                   "build/args.windows.gn", "build/windows/prepare-ungoogled.ps1", "assets/fixture.dat",
                                   PATCH, "patches/series", verify.arp.LITE + "/" + LITE, "tools/unknown.py",
                                   "tools/tests/test_transport_lifecycle_diagnostics_extra.py",
                                   "tools/tests/test_transport_lifecycle_diagnostics.py.bak",
                                   "tools/tests/diagnostics/test_transport_lifecycle_diagnostics.py",
                                   ".github/workflows/build-win-arm64-github.yml", "README.md"])
@pytest.mark.parametrize("committed", [False, True])
def test_other_repository_changes_fail_closed(fx, name, committed, monkeypatch):
    put(fx.repo, name, "unexpected change\n")
    if committed:
        if fx.stage == 8:
            git(fx.repo, "add", ".")
            git(fx.repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "--amend", "--no-edit", "-q")
            monkeypatch.setenv("GITHUB_SHA", git(fx.repo, "rev-parse", "HEAD").decode().strip())
        else:
            commit(fx.repo)
    before = snapshot(fx.work)
    with pytest.raises((ValueError, verify.arp.ApplyError)):
        fx.run()
    assert snapshot(fx.work) == before


@pytest.mark.parametrize("mutation", ["sha", "fast", "release", "arm64", "previous-dirty", "previous-extra", "previous-head",
    "tooling", "tooling-head", "marker", "toolchain-marker", "progress", "transaction", "migration", "arch-marker",
    "cold", "receipt", "manifest", "upstream-run", "upstream-artifact", "missing-receipt", "patch-receipt",
    "missing-patch-receipt", "source", "lite", "args-fast", "args-extra", "args-duplicate", "args-arm64", "version", "midl-missing"])
def test_invalid_snapshot_or_identity_is_rejected_without_mutation(fx, mutation):
    kwargs = {}
    if mutation == "sha":
        kwargs["sha"] = "f" * 40
    elif mutation in ("fast", "release"):
        kwargs["build_profile"] = mutation
    elif mutation == "arm64":
        kwargs["arch"] = "arm64"
    elif mutation.startswith("previous-"):
        put(fx.previous, "CHROMIUM_WINDOWS_VERSION" if mutation == "previous-dirty" else "extra.py", "changed\n")
        if mutation == "previous-head":
            commit(fx.previous)
    elif mutation.startswith("tooling"):
        put(fx.core, "flags.gn", "is_debug = true\n")
        if mutation == "tooling-head":
            commit(fx.core)
    elif mutation in ("marker", "toolchain-marker", "progress", "migration"):
        name = {"marker": ".chromix-source-ready", "toolchain-marker": ".chromix-toolchain-ready",
                "progress": ".chromix-layer-in-progress", "migration": verify.identity.RECEIPT}[mutation]
        put(fx.src, name, "invalid\n")
    elif mutation == "transaction":
        (fx.work / ".chromix-upstream-restore-interrupted").mkdir()
    elif mutation == "arch-marker":
        put(fx.work, ".chromix-target-arch", "arm64\n")
    elif mutation == "cold":
        (fx.src / "out/Chromix").mkdir()
    elif mutation in ("receipt", "manifest", "upstream-run", "upstream-artifact"):
        if mutation == "receipt":
            fx.receipt["arch"] = "arm64"
        else:
            key = {"manifest": "sha256", "upstream-run": "run_id", "upstream-artifact": "artifact_id"}[mutation]
            fx.receipt["manifest"][key] = "incorrect"
        put(fx.src, verify.upstream.MARKER, json.dumps(fx.receipt))
    elif mutation in ("missing-receipt", "missing-patch-receipt", "midl-missing"):
        name = {"missing-receipt": verify.upstream.MARKER, "missing-patch-receipt": verify.arp.MARKER,
                "midl-missing": verify.MIDL_SOURCE}[mutation]
        (fx.src / name).unlink()
    elif mutation == "patch-receipt":
        put(fx.src, verify.arp.MARKER, "{}")
    elif mutation in ("source", "lite", "version"):
        put(fx.src, {"source": SOURCE, "lite": LITE, "version": "chrome/VERSION"}[mutation], "changed\n")
    elif mutation.startswith("args-"):
        path = fx.src / "out/Default/args.gn"
        text = path.read_text()
        text = {"args-fast": text.replace("thin_lto_enable_optimizations = true", "thin_lto_enable_optimizations = false"),
                "args-extra": text + "unexpected = true\n", "args-duplicate": text + 'target_cpu = "x64"\n',
                "args-arm64": text.replace('"x64"', '"arm64"')}[mutation]
        path.write_text(text)
    before = snapshot(fx.work)
    with pytest.raises((ValueError, verify.arp.ApplyError, OSError)):
        fx.run(**kwargs)
    assert snapshot(fx.work) == before


def test_matching_output_receipt_cannot_replace_structural_stack_check(fx):
    put(fx.src, SOURCE, "context\nstale blocked.test\ntail\n")
    path = fx.src / verify.arp.MARKER
    receipt = json.loads(path.read_bytes())
    receipt["outputs"][SOURCE] = hashlib.sha256((fx.src / SOURCE).read_bytes()).hexdigest()
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="stale/incompatible source"):
        fx.run()


@pytest.mark.parametrize("target", ["repo", "previous", "source"])
def test_linked_inputs_are_rejected(fx, target):
    root, name = (fx.src, SOURCE) if target == "source" else (getattr(fx, target), PATCH)
    path = root / name
    destination = root / "linked-content"
    path.rename(destination)
    path.symlink_to(destination)
    with pytest.raises((ValueError, verify.arp.ApplyError)):
        fx.run()


def test_source_or_repository_race_is_not_certified(fx, monkeypatch):
    real_verify = verify.stack.verify

    def mutate_after_stack(*args, **kwargs):
        result = real_verify(*args, **kwargs)
        put(fx.src, verify.MIDL_SOURCE, "changed concurrently\n")
        return result

    monkeypatch.setattr(verify.stack, "verify", mutate_after_stack)
    with pytest.raises(verify.arp.ApplyError, match="changed concurrently"):
        fx.run()


def test_cli_writes_read_only_success_and_failure_reports(fx):
    report = fx.work / "diagnostics/verified.json"
    args = ["--workdir", str(fx.work), "--previous-repo", str(fx.previous), "--repo", str(fx.repo),
            "--expected-previous-sha", fx.sha, "--report", str(report)]
    before = snapshot(fx.src)
    assert verify.main(args) == 0
    assert json.loads(report.read_bytes())["status"] == "verified"
    assert snapshot(fx.src) == before
    with pytest.raises(SystemExit):
        verify.main(args)
    args[-1] = str(fx.work / "diagnostics/failed.json")
    put(fx.src, ".chromix-source-ready", "wrong\n")
    assert verify.main(args) == 1
    assert json.loads(Path(args[-1]).read_bytes())["status"] == "failed"


@pytest.mark.parametrize("profile", [verify.WINDOWS155_PROFILE, verify.STAGE8_PROFILE])
@pytest.mark.parametrize("option,value", [
    ("--workdir", "work"), ("--previous-repo", "previous"), ("--repo", str(ROOT)),
    ("--expected-previous-sha", verify.WINDOWS155_SHA), ("--arch", "x64"),
    ("--build-profile", "native"), ("--report", "report.json"),
])
def test_publish_cli_rejects_every_verification_argument(tmp_path, profile, option, value):
    before = snapshot(tmp_path)
    result = run_cli("--publish-hop", str(tmp_path), "--profile", profile, option, value)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "not allowed with argument" in result.stderr or "cannot be combined" in result.stderr
    assert not result.stdout
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("args", [
    [], ["--publish-hop", "."], ["--profile", verify.WINDOWS155_PROFILE],
    ["--publish-hop", ".", "--profile", "155"],
    ["--publish-hop", ".", "--profile", "windows-153-x64-stage6"],
    ["--workdir", ".", "--profile", verify.STAGE8_PROFILE],
    ["--workdir", "."], ["--workdir", ".", "--previous-repo", "previous"],
    ["--workdir", ".", "--previous-repo", "previous", "--expected-previous-sha", verify.DONOR_SHA],
])
def test_cli_rejects_unknown_profiles_and_incomplete_modes(args):
    result = run_cli(*args)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "error:" in result.stderr
    assert not result.stdout


def test_powershell_publishes_with_real_script_and_exact_profiles():
    source = (ROOT / "build/windows/ci-stage.ps1").read_text()
    start = source.index("\nif ($env:CHROMIX_WINDOWS_VERIFY_SOURCE_REPO -or")
    block = source[start:source.index("\nif ($env:CHROMIX_WINDOWS_MIGRATION_REPO -or", start)]
    assert "$recoveryProfile = if ($windows155Verify) { 'windows-155-x64-stage2' } else { 'windows-153-x64-stage8' }" in block
    invocation = ('& python -X utf8 (Join-Path $Repo "tools\\verify_windows_snapshot_source.py") `\n'
                  '      --publish-hop $verifyDiagnostics --profile $recoveryProfile')
    assert invocation in block
    assert " -c " not in block
    assert "$publishHop" not in block
    assert "publish_stage8_hop" not in block
    assert "publish_windows155_hop" not in block
    assert block.index('[IO.File]::Copy(') < block.index(invocation)
    failure = "if ($LASTEXITCODE -ne 0) { throw 'Current checkpoint recovery evidence binding failed' }"
    assert block.index(invocation) < block.index(failure) < block.index("Write-OutVar recovery_report_dir")
    assert block.index(failure) < block.index("snapshot_safe true")


def test_windows_crlf_checkout_representation_does_not_change_identity(fx, monkeypatch):
    monkeypatch.setattr(verify.identity, "WINDOWS", True)
    for root in (fx.previous, fx.repo):
        path = root / "build/windows/prepare-ungoogled.ps1"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    for path in fx.src.glob(".chromix-*"):
        if path.name.endswith(".json"):
            continue
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes().replace(b"\n", b"\r\n"))
    assert fx.run()["status"] == "verified"


def test_current_base_has_224_patches_and_no_midl_overlap():
    assert len(verify.identity._series(ROOT)) == 224
    for name in verify.identity._series(ROOT):
        _, entries = verify.arp.transform_patch((ROOT / name).read_bytes(), set(), [])
        assert verify.MIDL_SOURCE not in {entry[0] for entry in entries}
    assert verify.VERSION == "153.0.8010.47"
    assert verify.load_pins(ROOT, "windows")["ChromiumVersion"] == "155.0.8059.79"


@pytest.mark.parametrize("mutation", ["shallow", "target-sha", "missing-run", "wrong-job", "grandchild", "same-sha"])
def test_stage8_consumer_and_direct_parent_are_exact(fx, monkeypatch, mutation):
    if fx.stage != 8:
        return
    if mutation == "shallow":
        (fx.repo / ".git/shallow").write_text(os.environ["GITHUB_SHA"] + "\n")
        assert fx.run()["status"] == "verified"
        return
    if mutation == "target-sha":
        monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    elif mutation == "missing-run":
        monkeypatch.delenv("GITHUB_RUN_ID")
    elif mutation == "wrong-job":
        monkeypatch.setenv("GITHUB_JOB", "build-6")
    elif mutation == "grandchild":
        put(fx.repo, "tools/tests/test_windows_snapshot_workflow.py", "# another commit\n")
        monkeypatch.setenv("GITHUB_SHA", commit(fx.repo))
    elif mutation == "same-sha":
        git(fx.repo, "reset", "--hard", fx.sha)
        monkeypatch.setenv("GITHUB_SHA", fx.sha)
    with pytest.raises(ValueError):
        fx.run()


def test_stage8_permissions_do_not_expand_historical_preparation_profile():
    diagnostics = {"tools/fingerprint_transport_audit.py", "tools/fingerprint_transport_lifecycle_audit.py",
                   "tools/tests/test_transport_lifecycle.py", "tools/tests/test_transport_lifecycle_diagnostics.py"}
    recovery = {".github/actions/restore-windows-snapshot/action.yml", ".github/workflows/build-win-x64-github.yml",
                "build/windows/ci-stage.ps1", "tools/verify_windows_snapshot_source.py",
                "tools/tests/test_verify_windows_snapshot_source.py", "tools/tests/test_windows_snapshot_workflow.py",
                "tools/tests/test_windows_upstream_cache.py"}
    assert verify.STAGE8_ALLOWED_CHANGES == recovery | diagnostics
    assert len(verify.STAGE8_ALLOWED_CHANGES) == 11
    assert verify.STAGE8_ALLOWED_CHANGES - verify.ALLOWED_CHANGES == diagnostics
    assert not diagnostics & verify.ALLOWED_CHANGES
    assert "tools/repair_windows_midl.py" not in verify.STAGE8_ALLOWED_CHANGES
    assert "tools/prepare_restored_build.py" not in verify.STAGE8_ALLOWED_CHANGES
    assert all(not any(char in name for char in "*?[") for name in verify.STAGE8_ALLOWED_CHANGES)


@pytest.mark.parametrize("entrypoint", ["api", "cli"])
@pytest.mark.parametrize("mutation", ["valid", "wrong-directory", "source-run", "source-target", "source-profile", "source-status",
    "download-status", "download-phase", "download-publication", "download-artifact", "origin-job", "existing-report", "report-link",
    "missing-origin", "missing-download", "missing-source", "wrong-job"])
def test_stage8_hop_publication_binds_current_identity_paths_and_hashes(tmp_path, monkeypatch, mutation, entrypoint):
    consumer = {"GITHUB_RUN_ID": "90001", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_JOB": "build-8", "GITHUB_SHA": "a" * 40}
    for key, value in consumer.items():
        monkeypatch.setenv(key, value)
    hop = tmp_path / "fingerprint-diagnostics/recovery-hops/d35485726877-a1-s8-j106081168976" / ("c90001-a2-build-8-" + "a" * 40)
    if mutation == "wrong-directory":
        hop = hop.with_name("historical")
    hop.mkdir(parents=True)
    origin = {key: value for key, value in verify.STAGE8_DONOR.items() if key != "branch"}
    origin.update(platform="windows", arch="x64", workflow="build-win-x64-github",
                  pattern="tree-s8-attempt-1-part*", artifacts=verify.STAGE8_ARTIFACTS)
    download = {**origin, "status": "success", "phase": "complete", "publication": "published"}
    source = {"status": "verified", "operation": "windows-unchanged-source", "profile": verify.STAGE8_PROFILE,
              "previous_sha": verify.STAGE8_SHA, "target_sha": consumer["GITHUB_SHA"], "run": consumer, "changed_files": []}
    if mutation == "source-run":
        source["run"] = {**consumer, "GITHUB_RUN_ID": "90000"}
    elif mutation == "source-target":
        source["target_sha"] = "b" * 40
    elif mutation == "source-profile":
        source["profile"] = "windows-153-x64-stage6"
    elif mutation == "source-status":
        source["status"] = "failed"
    elif mutation.startswith("download-"):
        key, value = {"download-status": ("status", "verified"), "download-phase": ("phase", "publish"),
                      "download-publication": ("publication", "unconfirmed"), "download-artifact": ("artifacts", [])}[mutation]
        download[key] = value
    elif mutation == "origin-job":
        origin["job_id"] += 1
    for name, data in (("windows-snapshot.json", origin), ("windows-snapshot-download.json", download),
                       ("windows-unchanged-source.json", source)):
        put(hop, name, json.dumps(data))
    if mutation == "existing-report":
        put(hop, "recovery-hop.json", '{"status":"historical"}')
    elif mutation == "report-link":
        destination = put(tmp_path, "historical.json", "preserve\n")
        (hop / "recovery-hop.json").symlink_to(destination)
    if mutation.startswith("missing-"):
        name = {"missing-origin": "windows-snapshot.json", "missing-download": "windows-snapshot-download.json",
                "missing-source": "windows-unchanged-source.json"}[mutation]
        (hop / name).unlink()
    elif mutation == "wrong-job":
        monkeypatch.setenv("GITHUB_JOB", "build-2")
    before = snapshot(hop)
    if entrypoint == "cli":
        result = run_cli("--publish-hop", str(hop), "--profile", verify.STAGE8_PROFILE)
        assert result.returncode == (0 if mutation == "valid" else 1), result.stdout + result.stderr
        assert json.loads(result.stdout)["status"] == ("verified" if mutation == "valid" else "failed")
    if mutation == "valid":
        if entrypoint == "api":
            verify.publish_stage8_hop(hop)
        report = json.loads((hop / "recovery-hop.json").read_bytes())
        assert report["profile"] == verify.STAGE8_PROFILE
        assert report["consumer"] == consumer
        assert report["donor"] == verify.STAGE8_DONOR
        for record in report["reports"].values():
            assert record["sha256"] == hashlib.sha256((hop / record["path"]).read_bytes()).hexdigest()
        for name, value in before.items():
            assert snapshot(hop)[name] == value
    else:
        if entrypoint == "api":
            with pytest.raises((ValueError, OSError, verify.arp.ApplyError)):
                verify.publish_stage8_hop(hop)
        assert snapshot(hop) == before


@pytest.fixture
def fx155(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    pins = verify.load_pins(fixture.previous, "windows")
    core_sha, platform_sha = pins["UngoogledCommit"], pins["UngoogledWindowsCommit"]
    put(fixture.core, "chromium_version.txt", verify.WINDOWS155_VERSION + "\n")
    core_sha = commit(fixture.core)
    data = (fixture.previous / "build/ungoogled-revisions.psd1").read_text()
    data = data.replace(pins["UngoogledCommit"], core_sha).replace("153.0.8010.47", verify.WINDOWS155_VERSION)
    put(fixture.previous, "build/ungoogled-revisions.psd1", data)
    put(fixture.previous, "CHROMIUM_WINDOWS_VERSION", verify.WINDOWS155_VERSION + "\n")
    manifest = json.loads((fixture.previous / "build/upstream-cache.json").read_bytes())
    source = manifest["sources"]["windows"]
    source.update(chromium_version=verify.WINDOWS155_VERSION, ungoogled_commit=core_sha,
                  head_branch=verify.WINDOWS155_VERSION + "-1.1", run_id=verify.WINDOWS155_UPSTREAM_RUN_ID)
    source["artifacts"]["x64"]["id"] = verify.WINDOWS155_UPSTREAM_ARTIFACT_ID
    put(fixture.previous, "build/upstream-cache.json", json.dumps(manifest))
    names = [f"patches/{index:04d}-fixture.patch" for index in range(224)]
    put(fixture.previous, "patches/series", "\n".join(names) + "\n")
    (fixture.previous / PATCH).unlink()
    for index, name in enumerate(names):
        put(fixture.previous, name, f"diff --git a/{SOURCE} b/{SOURCE}\n--- a/{SOURCE}\n+++ b/{SOURCE}\n"
            f"@@ -1,3 +1,3 @@\n context\n-value{index} example.com\n+value{index + 1} example.com\n tail\n")
    override = "patches/chromium155/" + Path(names[0]).name
    original = (fixture.previous / names[0]).read_bytes()
    replacement = original.replace(b"--- a/", b"index 1234567..abcdef0 100644\n--- a/", 1)
    put(fixture.previous, override, replacement)
    monkeypatch.setattr(verify.patch_selection, "CORE155", core_sha)
    monkeypatch.setitem(verify.patch_selection.PLATFORM155_COMMITS, "windows", platform_sha)
    monkeypatch.setattr(verify.patch_selection, "OVERRIDES155", {
        Path(names[0]).name: (hashlib.sha256(original).hexdigest(), hashlib.sha256(replacement).hexdigest())})
    fixture.sha = commit(fixture.previous)
    monkeypatch.setattr(verify, "WINDOWS155_SHA", fixture.sha)
    shutil.rmtree(fixture.repo)
    shutil.copytree(fixture.previous, fixture.repo)
    put(fixture.repo, "tools/verify_windows_snapshot_source.py", "# trusted fixture helper\n")
    target = commit(fixture.repo)
    for name, value in {"GITHUB_RUN_ID": "90001", "GITHUB_RUN_ATTEMPT": "2",
                        "GITHUB_JOB": "build-2", "GITHUB_SHA": target}.items():
        monkeypatch.setenv(name, value)
    put(fixture.src, SOURCE, "context\nvalue224 blocked.test\ntail\n")
    put(fixture.src, "chrome/VERSION", "MAJOR=155\nMINOR=0\nBUILD=8059\nPATCH=39\n")
    original = ('target_cpu = "x64"\ntarget_os = "win"\nchrome_pgo_phase = 2\n'
                'v8_metagen_libclang_so = "C:/donor/libclang.dll"\n'
                'v8_metagen_libclang_bindings_dir = "C:/donor/site-packages"\n')
    put(fixture.src, "out/Default/args.gn", original)
    repo_identity, _, manifest = verify.upstream.identities(fixture.repo, "windows", "x64")
    fixture.receipt.update(identity=repo_identity, manifest=manifest,
                           original_args=verify.upstream.source_args(fixture.src, repo_identity))
    monkeypatch.setattr(verify, "WINDOWS155_ORIGINAL_ARGS_SHA256", fixture.receipt["original_args"]["sha256"])
    put(fixture.src, verify.upstream.MARKER, json.dumps(fixture.receipt))
    args = original + (fixture.core / "flags.gn").read_text() + (fixture.repo / "build/args.windows.gn").read_text()
    values = verify.upstream.parse_gn_assignments(args)
    values["chrome_pgo_phase"] = "2"
    put(fixture.src, "out/Default/args.gn", "\n".join(f"{k} = {v}" for k, v in values.items()) + "\n")
    pins = verify.load_pins(fixture.repo, "windows")
    key = verify._ready_key(fixture.repo, pins)
    for name, value in {
        ".chromix-source-unpacked": verify.WINDOWS155_VERSION, ".chromix-ungoogled-core": core_sha,
        ".chromix-binaries-pruned": core_sha, ".chromix-domain-substituted": core_sha,
        ".chromix-toolchain-ready": core_sha + "|" + platform_sha,
        ".chromix-patches": key.rsplit("|", 1)[1], ".chromix-patch-selection": key.rsplit("|", 1)[1],
        ".chromix-source-ready": key,
    }.items():
        put(fixture.src, name, value + "\n")
    selected, _, _ = verify.arp._load(fixture.repo, fixture.core, fixture.tooling, "windows")
    put(fixture.src, verify.arp.MARKER, verify.arp._json({
        "schema_version": 1, "identity": selected, "identity_sha256": verify.arp._sha(verify.arp._json(selected)),
        "outputs": {name: hashlib.sha256((fixture.src / name).read_bytes()).hexdigest() for name in (SOURCE, LITE)},
    }))
    return fixture


def amend_target(fixture, monkeypatch):
    git(fixture.repo, "add", ".")
    git(fixture.repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--amend", "--no-edit", "-q")
    monkeypatch.setenv("GITHUB_SHA", git(fixture.repo, "rev-parse", "HEAD").decode().strip())


def test_windows155_selected_stack_pgo2_and_read_only_current_consumer(fx155):
    before = snapshot(fx155.work)
    report = fx155.run()
    assert report["profile"] == verify.WINDOWS155_PROFILE
    assert report["version"] == "155.0.8059.39"
    assert report["run"]["GITHUB_JOB"] == "build-2"
    assert report["verification"]["patch_count"] == 224
    assert report["verification"]["identity"]["selection"]["version"] == "155.0.8059.39"
    assert report["changed_files"] == []
    assert report == fx155.run()
    assert snapshot(fx155.work) == before


@pytest.mark.parametrize("name", sorted(verify.WINDOWS155_ALLOWED_CHANGES))
@pytest.mark.parametrize("committed", [True, False])
def test_windows155_runtime_allowlist_requires_committed_inputs(fx155, monkeypatch, name, committed):
    put(fx155.repo, name, "# runtime-only change\n")
    if committed:
        amend_target(fx155, monkeypatch)
        assert name in fx155.run()["repository_changed_files"]
    else:
        with pytest.raises((ValueError, verify.arp.ApplyError)):
            fx155.run()


@pytest.mark.parametrize("name", ["tools/linux_restored_generators.py", "tools/patch_selection.py",
    ".github/workflows/fingerprint-contracts.yml", "build/windows/prepare-ungoogled.ps1", "build/args.windows.gn",
    "CHROMIUM_WINDOWS_VERSION", "build/upstream-cache.json", "patches/chromium155/0000-fixture.patch",
    "tools/tests/test_windows_metagen_bindings.py.bak", "tools/tests/test_windows_upstream_cache.py"])
def test_windows155_never_allows_cross_branch_or_preparation_changes(fx155, monkeypatch, name):
    put(fx155.repo, name, "unexpected committed change\n")
    amend_target(fx155, monkeypatch)
    with pytest.raises((ValueError, verify.arp.ApplyError)):
        fx155.run()


@pytest.mark.parametrize("mutation", ["pgo0", "receipt-pgo0", "runtime-dll", "runtime-bindings", "selection-marker",
    "marker", "source", "lite", "receipt", "patch-receipt", "job", "run", "attempt", "target", "donor-run",
    "grandchild", "previous-dirty", "progress", "cold"])
def test_windows155_mismatches_fail_without_source_mutation(fx155, monkeypatch, mutation):
    if mutation in ("pgo0", "runtime-dll", "runtime-bindings"):
        path = fx155.src / "out/Default/args.gn"
        old, new = {"pgo0": ("chrome_pgo_phase = 2", "chrome_pgo_phase = 0"),
                    "runtime-dll": ("C:/donor/libclang.dll", "C:/runtime/libclang.dll"),
                    "runtime-bindings": ("C:/donor/site-packages", "C:/runtime/site-packages")}[mutation]
        path.write_text(path.read_text().replace(old, new))
    elif mutation == "receipt-pgo0":
        receipt = fx155.receipt
        raw = receipt["original_args"]["text"].replace("chrome_pgo_phase = 2", "chrome_pgo_phase = 0")
        receipt["original_args"].update(text=raw, bytes=len(raw.encode()), sha256=hashlib.sha256(raw.encode()).hexdigest(),
                                        assignments=verify.upstream.parse_gn_assignments(raw))
        put(fx155.src, verify.upstream.MARKER, json.dumps(receipt))
    elif mutation in ("job", "run", "attempt", "target", "donor-run"):
        key, value = {"job": ("GITHUB_JOB", "build-3"), "run": ("GITHUB_RUN_ID", ""),
                      "attempt": ("GITHUB_RUN_ATTEMPT", "0"), "target": ("GITHUB_SHA", "a" * 40),
                      "donor-run": ("GITHUB_RUN_ID", "38136799729")}[mutation]
        monkeypatch.setenv(key, value)
    elif mutation == "grandchild":
        put(fx155.repo, "tools/install_windows_metagen_bindings.py", "# second commit\n")
        monkeypatch.setenv("GITHUB_SHA", commit(fx155.repo))
    elif mutation == "previous-dirty":
        put(fx155.previous, "build/windows/prepare-ungoogled.ps1", "changed\n")
    elif mutation == "cold":
        (fx155.src / "out/Chromix").mkdir()
    else:
        name = {"selection-marker": ".chromix-patch-selection", "marker": ".chromix-source-ready", "source": SOURCE,
                "lite": LITE, "receipt": verify.upstream.MARKER, "patch-receipt": verify.arp.MARKER,
                "progress": ".chromix-layer-in-progress"}[mutation]
        put(fx155.src, name, "{}\n")
    before = snapshot(fx155.work)
    with pytest.raises((ValueError, OSError, verify.arp.ApplyError)):
        fx155.run()
    assert snapshot(fx155.work) == before


def test_windows155_allowlist_is_exact_and_does_not_expand_other_profiles():
    assert verify.WINDOWS155_ALLOWED_CHANGES == frozenset({
        ".github/actions/restore-windows-snapshot/action.yml", ".github/workflows/build-win-x64-github.yml",
        "build/windows/ci-stage.ps1", "tools/verify_windows_snapshot_source.py",
        "tools/install_windows_metagen_bindings.py", "tools/tests/test_windows_metagen_bindings.py",
        "tools/tests/test_verify_windows_snapshot_source.py", "tools/tests/test_windows_snapshot_workflow.py"})
    assert "tools/install_windows_metagen_bindings.py" not in verify.ALLOWED_CHANGES | verify.STAGE8_ALLOWED_CHANGES


@pytest.mark.parametrize("ref", ["cc28d2ca381d1bcfdfc7af61b0c53e969e632cf6", verify.WINDOWS155_SHA])
def test_windows155_full_donor_checkout_inputs_reject_later_linux_branch(tmp_path, monkeypatch, ref):
    import io
    import tarfile

    monkeypatch.setattr(verify.identity, "WINDOWS", True)
    previous, target = tmp_path / "previous", tmp_path / "target"
    previous.mkdir()
    try:
        git(ROOT, "cat-file", "-e", ref + "^{commit}")
    except subprocess.CalledProcessError:
        pytest.skip("exact donor object is absent from the shallow checkout")
    payload = git(ROOT, "archive", ref)
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        archive.extractall(previous, filter="data")
    commit(previous)
    shutil.copytree(previous, target)
    for name in verify.WINDOWS155_ALLOWED_CHANGES:
        path = ROOT / name
        if path.exists():
            put(target, name, path.read_bytes())
    commit(target)
    program = verify.identity._host_git((previous, target))
    _, old_tree = verify.identity._tree(program, previous)
    _, new_tree = verify.identity._tree(program, target)
    verify._inputs(previous, target, old_tree, new_tree, verify.WINDOWS155_ALLOWED_CHANGES)
    assert verify._ready_key(previous, verify.load_pins(previous, "windows")) == (
        "155.0.8059.39|ffeb45a2bb88bc56a1dfed31410aeb10e9efcd5a|"
        "989b4382364baaddb8b3180e1eb1030ca96a7273|225dd1606daeaecb234598db5e5dc4c20b4665823d4cc3cf0105cba6436716bd")
    name = "tools/linux_restored_generators.py"
    put(target, name, (ROOT / name).read_bytes() + b"\n# disallowed Linux branch change\n")
    commit(target)
    _, changed_tree = verify.identity._tree(program, target)
    with pytest.raises(ValueError, match="outside the allowlist"):
        verify._inputs(previous, target, old_tree, changed_tree, verify.WINDOWS155_ALLOWED_CHANGES)


@pytest.mark.parametrize("entrypoint", ["api", "cli"])
@pytest.mark.parametrize("mutation", ["valid", "wrong-directory", "source-run", "source-target", "source-profile", "source-status",
    "download-status", "download-phase", "download-publication", "download-artifact", "origin-job", "existing-report", "report-link",
    "missing-origin", "missing-download", "missing-source", "wrong-job"])
def test_windows155_hop_publication_binds_current_identity_paths_and_hashes(tmp_path, monkeypatch, mutation, entrypoint):
    consumer = {"GITHUB_RUN_ID": "90001", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_JOB": "build-2", "GITHUB_SHA": "a" * 40}
    for key, value in consumer.items():
        monkeypatch.setenv(key, value)
    hop = tmp_path / "fingerprint-diagnostics/recovery-hops/d38136799729-a1-s2-j114518496535" / ("c90001-a2-build-2-" + "a" * 40)
    if mutation == "wrong-directory":
        hop = hop.with_name("historical")
    hop.mkdir(parents=True)
    origin = {key: value for key, value in verify.WINDOWS155_DONOR.items() if key != "branch"}
    origin.update(platform="windows", arch="x64", workflow="build-win-x64-github",
                  pattern="tree-s2-attempt-1-part*", artifacts=verify.WINDOWS155_ARTIFACTS)
    download = {**origin, "status": "success", "phase": "complete", "publication": "published"}
    source = {"status": "verified", "operation": "windows-unchanged-source", "profile": verify.WINDOWS155_PROFILE,
              "previous_sha": verify.WINDOWS155_SHA, "target_sha": consumer["GITHUB_SHA"], "run": consumer, "changed_files": []}
    if mutation == "source-run":
        source["run"] = {**consumer, "GITHUB_RUN_ID": "90000"}
    elif mutation == "source-target":
        source["target_sha"] = "b" * 40
    elif mutation == "source-profile":
        source["profile"] = "windows-153-x64-stage6"
    elif mutation == "source-status":
        source["status"] = "failed"
    elif mutation.startswith("download-"):
        key, value = {"download-status": ("status", "verified"), "download-phase": ("phase", "publish"),
                      "download-publication": ("publication", "unconfirmed"), "download-artifact": ("artifacts", [])}[mutation]
        download[key] = value
    elif mutation == "origin-job":
        origin["job_id"] += 1
    for name, data in (("windows-snapshot.json", origin), ("windows-snapshot-download.json", download),
                       ("windows-unchanged-source.json", source)):
        put(hop, name, json.dumps(data))
    if mutation == "existing-report":
        put(hop, "recovery-hop.json", '{"status":"historical"}')
    elif mutation == "report-link":
        destination = put(tmp_path, "historical.json", "preserve\n")
        (hop / "recovery-hop.json").symlink_to(destination)
    if mutation.startswith("missing-"):
        name = {"missing-origin": "windows-snapshot.json", "missing-download": "windows-snapshot-download.json",
                "missing-source": "windows-unchanged-source.json"}[mutation]
        (hop / name).unlink()
    elif mutation == "wrong-job":
        monkeypatch.setenv("GITHUB_JOB", "build-8")
    before = snapshot(hop)
    if entrypoint == "cli":
        result = run_cli("--publish-hop", str(hop), "--profile", verify.WINDOWS155_PROFILE)
        assert result.returncode == (0 if mutation == "valid" else 1), result.stdout + result.stderr
        assert json.loads(result.stdout)["status"] == ("verified" if mutation == "valid" else "failed")
    if mutation == "valid":
        if entrypoint == "api":
            verify.publish_windows155_hop(hop)
        report = json.loads((hop / "recovery-hop.json").read_bytes())
        assert report["profile"] == verify.WINDOWS155_PROFILE
        assert report["consumer"] == consumer
        assert report["donor"] == verify.WINDOWS155_DONOR
        for record in report["reports"].values():
            assert record["sha256"] == hashlib.sha256((hop / record["path"]).read_bytes()).hexdigest()
        for name, value in before.items():
            assert snapshot(hop)[name] == value
    else:
        if entrypoint == "api":
            with pytest.raises((ValueError, OSError, verify.arp.ApplyError)):
                verify.publish_windows155_hop(hop)
        assert snapshot(hop) == before


def test_windows155_shallow_consumer_still_checks_commit_parent(fx155):
    (fx155.repo / ".git/shallow").write_text(os.environ["GITHUB_SHA"] + "\n")
    assert fx155.run()["target_sha"] == os.environ["GITHUB_SHA"]


def test_windows155_runtime_input_race_is_rejected(fx155, monkeypatch):
    check = verify.stack.verify

    def change_runtime_after_proof(*args, **kwargs):
        proof = check(*args, **kwargs)
        put(fx155.repo, "tools/verify_windows_snapshot_source.py", "# concurrent runtime edit\n")
        return proof

    monkeypatch.setattr(verify.stack, "verify", change_runtime_after_proof)
    with pytest.raises(ValueError, match="inputs changed concurrently"):
        fx155.run()
