"""Exact Windows156 fresh fixtures; production pins are supplied by the pins agent."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import gen_windows_arm64_workflow as generator
import repair_windows_midl as repair
import windows156_fresh as fresh


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "build").mkdir(parents=True)
    pins = dict(ChromiumVersion=fresh.VERSION, UngoogledVersion=fresh.VERSION + "-1",
                UngoogledCommit=fresh.CORE, UngoogledWindowsVersion=fresh.VERSION + "-1.1",
                UngoogledWindowsCommit=fresh.WINDOWS)
    (repo / "build/ungoogled-revisions.psd1").write_text(
        "@{\n" + "".join(f'  {key} = "{value}"\n' for key, value in pins.items()) + "}\n")
    (repo / "CHROMIUM_VERSION").write_text(fresh.VERSION)
    return repo


@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_fresh_merge_keeps_pgo2_and_x64_host_and_native_bindings(repo, tmp_path, arch):
    work = tmp_path / "work"
    fresh.initialize(repo, work, arch, resume=False)
    src = work / "src"
    src.mkdir()
    (src / ".chromix-source-ready").write_text(fresh.VERSION)
    overlay = fresh.overlay(repo, work, arch)
    flags = tmp_path / "upstream.gn"
    flags.write_text('chrome_pgo_phase=2\nhost_cpu="x64"\n')
    args = src / "out/Chromix/args.gn"
    command = [sys.executable, str(ROOT / "tools/merge_gn_args.py"), str(args), str(flags),
               str(ROOT / "build/args.windows.gn")]
    if arch == "arm64":
        command.append(str(ROOT / "build/args.windows.arm64.gn"))
    subprocess.run([*command, str(overlay)], check=True)
    text = args.read_text()
    assert 'chrome_pgo_phase = 2' in text
    assert f'target_cpu = "{arch}"' in text
    assert 'host_cpu="x64"' in text
    assert 'v8_metagen_libclang_so = ' in text
    assert '/llvm-build/Release+Asserts/bindings/python"' in text
    states = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (args, overlay)}
    fresh.initialize(repo, work, arch, resume=True)
    fresh.overlay(repo, work, arch)
    subprocess.run([*command, str(overlay)], check=True)
    assert states == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in states}
    args.write_text(text.replace('chrome_pgo_phase = 2', 'chrome_pgo_phase = 0'))
    with pytest.raises(ValueError, match="preserved args"):
        fresh.initialize(repo, work, arch, resume=True)


def test_missing_or_wrong_receipt_never_cold_falls_back(repo, tmp_path):
    work = tmp_path / "work"
    with pytest.raises(ValueError, match="receipt missing"):
        fresh.initialize(repo, work, "x64", resume=True)
    assert not work.exists()
    fresh.initialize(repo, work, "x64", resume=False)
    (work / "src/out/Default").mkdir(parents=True)
    with pytest.raises(ValueError, match="upstream cache"):
        fresh.initialize(repo, work, "x64", resume=True)
    with pytest.raises(ValueError, match="identity mismatch"):
        fresh.initialize(repo, work, "arm64", resume=True)
    (work / fresh.RECEIPT).unlink()
    with pytest.raises(ValueError, match="receipt missing"):
        fresh.initialize(repo, work, "x64", resume=False)


@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_target_pgo_download_is_atomic_and_preserved(tmp_path, arch):
    target = "win64" if arch == "x64" else "win-arm64"
    build = tmp_path / "chrome/build"
    build.mkdir(parents=True)
    (build / f"{target}.pgo.txt").write_text(fresh.PROFILES[arch])
    payload = b"profile test fixture, not real profdata"
    response = io.BytesIO(payload)
    response.status = 200
    response.headers = {"Content-Length": str(len(payload))}
    url = "https://storage.googleapis.com/chromium-optimization-profiles/pgo_profiles/" + fresh.PROFILES[arch]
    response.geturl = lambda: url
    with mock.patch.object(fresh.urllib.request, "urlopen", return_value=response) as fetch:
        path = fresh.profile(tmp_path, arch)
        fetch.assert_called_once_with(url, timeout=120)
    before = path.stat().st_mtime_ns
    with mock.patch.object(fresh.urllib.request, "urlopen", side_effect=AssertionError("must reuse")):
        assert fresh.profile(tmp_path, arch) == path
    assert path.stat().st_mtime_ns == before
    path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="download proof"):
        fresh.profile(tmp_path, arch)


def test_failed_pgo_download_cannot_publish_partial(tmp_path):
    build = tmp_path / "chrome/build"
    build.mkdir(parents=True)
    (build / "win64.pgo.txt").write_text(fresh.PROFILES["x64"])
    response = io.BytesIO(b"short")
    response.status = 200
    response.headers = {"Content-Length": "100"}
    response.geturl = lambda: "https://storage.googleapis.com/chromium-optimization-profiles/pgo_profiles/" + fresh.PROFILES["x64"]
    with mock.patch.object(fresh.urllib.request, "urlopen", return_value=response), pytest.raises(ValueError, match="incomplete"):
        fresh.profile(tmp_path, "x64")
    assert not list((build / "pgo_profiles").iterdir())


@pytest.mark.parametrize("substituted", [False, True])
def test_audited_fresh_midl_repair_is_needed_and_idempotent(tmp_path, substituted):
    source = (ROOT / "tools/tests/fixtures/windows153_midl.py").read_bytes()
    if not substituted:
        source = source.replace(b"chromium-review.9oo91esource.qjz9zk", b"chromium-review.googlesource.com")
        assert hashlib.sha256(source).hexdigest() == "501ad1740d960ab1f2afeb8278162b05163e3772edb6ea167554f67ef82ca99c"
    else:
        (tmp_path / ".chromix-domain-substituted").write_text(fresh.CORE)
    path = tmp_path / repair.SCRIPT
    path.parent.mkdir(parents=True)
    path.write_bytes(source)
    fresh.midl(tmp_path)
    assert repair.COMPARE_AFTER in path.read_bytes()
    assert repair.AFTER in path.read_bytes()
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    fresh.midl(tmp_path)
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)
    out = tmp_path / "out/Chromix"
    out.mkdir(parents=True)
    (out / "build.ninja").write_text("preserved")
    path.write_bytes(source)
    with pytest.raises(ValueError, match="already has output"):
        fresh.midl(tmp_path)
    assert path.read_bytes() == source
    assert (out / "build.ninja").read_text() == "preserved"


class Client:
    def __init__(self, arch="x64"):
        self.sha = "a" * 40
        self.run = dict(id=42, head_sha=self.sha, status="completed", event="workflow_dispatch",
                        path=f".github/workflows/build-win-{arch}-156-fresh.yml", run_attempt=2,
                        repository={"full_name": "owner/repo"}, head_repository={"full_name": "owner/repo"})
        self.artifacts = []
        self.jobs = []
        for stage in (1, 2):
            steps = [dict(name=name, conclusion="success") for name in
                     ["Ensure build tree snapshot", *[f"Upload tree part {n}" for n in range(1, 5)]]]
            self.jobs.append(dict(name=f"Windows156 {arch} stage {stage}", status="completed", head_sha=self.sha,
                                  run_id=42, run_attempt=2, steps=steps))
            for part in (1, 2):
                self.artifacts.append(dict(id=stage * 10 + part, name=f"win-{arch}-156-tree-s{stage}-attempt-2-part{part}",
                                           expired=False, digest="sha256:" + "b" * 64, size_in_bytes=100,
                                           workflow_run=dict(id=42, head_sha=self.sha)))

    def get(self, path):
        return self.run

    def items(self, path, kind):
        return self.artifacts if kind == "artifacts" else self.jobs


@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_latest_valid_snapshot_ignores_failed_save_and_never_crosses_sha(arch):
    client = Client(arch)
    assert fresh.latest_snapshot(client, "owner/repo", 42, arch, client.sha)["stage"] == 2
    client.jobs[1]["steps"][0]["conclusion"] = "failure"
    assert fresh.latest_snapshot(client, "owner/repo", 42, arch, client.sha)["stage"] == 1
    client.artifacts[0]["expired"] = True
    with pytest.raises(ValueError, match="no valid"):
        fresh.latest_snapshot(client, "owner/repo", 42, arch, client.sha)
    with pytest.raises(ValueError, match="identity mismatch"):
        fresh.latest_snapshot(client, "owner/repo", 42, arch, "f" * 40)


@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_fresh_workflow_is_explicit_preserves_output_and_keeps_native_acceptance(arch):
    workflow = generator.fresh_workflow(arch)
    assert (ROOT / f".github/workflows/build-win-{arch}-156-fresh.yml").read_text() == generator.render_fresh(arch)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["on"]["workflow_dispatch"]["inputs"]["use_upstream_cache"]["default"] is True
    assert workflow["env"]["CHROMIX_BUILD_PROFILE"] == "native"
    assert workflow["env"]["CHROMIX_PREFER_UPSTREAM_CACHE"] == "0"
    for index in range(1, 13):
        job = workflow["jobs"][f"build-{index}"]
        assert job["runs-on"] == "windows-2022"
        stage = next(step for step in job["steps"] if step.get("id") == "stage")
        assert "-FreshNative" in stage["run"] and "-FromSynced" in stage["run"]
        assert "-FromArtifact" not in stage["run"]
        assert "continue-on-error" not in stage
        guard = next(step for step in job["steps"] if step.get("name") == "Require explicit Windows156 fresh authorization")
        assert "-cne 'false'" in guard["run"]
        snapshot = next(step for step in job["steps"] if step.get("name") == "Ensure build tree snapshot")
        assert "$env:UPLOAD_PARTS -ne 'true' -or $env:STAGE_OUTCOME -ne 'success'" in snapshot["run"]
        assert "No complete snapshot volume set" in snapshot["run"]
        assert "steps.stage.outputs.snapshot_safe == 'true'" in snapshot["if"]
        diagnostics = next(step for step in job["steps"] if step.get("name") == "Upload fingerprint diagnostics")
        assert f"fingerprint-win-{arch}-" in diagnostics["with"]["name"]
    if arch == "arm64":
        native = workflow["jobs"]["verify-arm64"]
        assert native["runs-on"] == "windows-11-arm"
        assert native["needs"] == "complete"
        assert "--native" in str(native)
        assert "fingerprint_acceptance.py" in str(native)
        assert "continue-on-error" not in str(native)
    else:
        assert "verify-arm64" not in workflow["jobs"]


def test_entrypoints_metagen_after_merge_before_and_after_gn_even_cache_off():
    for name, out in (("ci-stage.ps1", "$OutDir"), ("build.ps1", "$Out")):
        text = (ROOT / "build/windows" / name).read_text()
        assert text.index("python @mergeArgs") < text.index("$metagenBindings --configure")
        assert text.index("$metagenBindings --configure") < text.index(f"& $gn gen {out}")
        assert text.index(f"& $gn gen {out}") < text.index("$metagenBindings --src")
        assert "--chromium-version $Revisions.ChromiumVersion --download" in text
        assert "windows156-native.gn" in text
        assert "--phase probe" in text
    prepare = (ROOT / "build/windows/prepare-ungoogled.ps1").read_text()
    assert prepare.index('"retrieve"') < prepare.index('"unpack"')
    assert "Repair-FreshMidl" in prepare
    assert "typescript\\windows-amd64\\src" in prepare
    assert "microsoft_multimedia_redirection\\src" in prepare
    assert "refusing to delete the build tree" in prepare
    assert '"third_party/typescript/windows-amd64/src/lib/tsc.exe"' in (ROOT / "tools/windows156_fresh.py").read_text()


def test_chunked_gcs_profile_download_is_supported(tmp_path):
    build = tmp_path / 'chrome/build'
    build.mkdir(parents=True)
    (build / 'win64.pgo.txt').write_text(fresh.PROFILES['x64'])
    response = io.BytesIO(b'expanded chunked profile')
    response.status = 200
    response.headers = {'Transfer-Encoding': 'chunked', 'x-goog-stored-content-encoding': 'gzip'}
    response.geturl = lambda: 'https://storage.googleapis.com/chromium-optimization-profiles/pgo_profiles/' + fresh.PROFILES['x64']
    with mock.patch.object(fresh.urllib.request, 'urlopen', return_value=response):
        assert fresh.profile(tmp_path, 'x64').read_bytes() == b'expanded chunked profile'


def test_native_generators_and_profile_reader_are_mandatory(tmp_path):
    profile = tmp_path / 'pinned.profdata'
    with mock.patch.object(fresh, 'dependencies') as deps, mock.patch.object(fresh, 'profile', return_value=profile), \
            mock.patch.object(fresh.subprocess, 'run') as run:
        run.side_effect = [mock.Mock(stdout=value) for value in ('v24.12.0', '0.28.2', 'Version 7.0.2', '')]
        fresh.probe(tmp_path, 'arm64')
        deps.assert_called_once_with(tmp_path)
        assert run.call_args_list[2].args[0][0].endswith('/windows-amd64/src/lib/tsc.exe')
        assert run.call_args_list[3].args[0][1:] == ['show', str(profile)]
        assert all(call.kwargs['check'] is True for call in run.call_args_list)
    with mock.patch.object(fresh, 'dependencies'), mock.patch.object(fresh.subprocess, 'run', return_value=mock.Mock(stdout='Version 6')):
        with pytest.raises(ValueError, match='host generator version'):
            fresh.probe(tmp_path, 'x64')


def test_resume_rejects_readyless_or_cross_version_output_without_deletion(repo, tmp_path):
    work = tmp_path / 'work'
    fresh.initialize(repo, work, 'x64', resume=False)
    out = work / 'src/out/Chromix'
    out.mkdir(parents=True)
    obj = out / 'keep.obj'
    obj.write_bytes(b'compiled output')
    with pytest.raises(ValueError, match='lacks a ready source'):
        fresh.initialize(repo, work, 'x64', resume=True)
    (work / 'src/.chromix-source-ready').write_text(fresh.VERSION)
    (work / 'src/.chromix-source-unpacked').write_text('155.0.8059.39')
    with pytest.raises(ValueError, match='version mismatch'):
        fresh.initialize(repo, work, 'x64', resume=True)
    assert obj.read_bytes() == b'compiled output'


def test_official_download_fixture_keeps_source_and_host_tool_contract():
    import configparser
    audit = json.loads((ROOT / 'tools/tests/fixtures/windows156_fresh.json').read_text())
    assert (audit['chromium_version'], audit['core'], audit['windows']) == (fresh.VERSION, fresh.CORE, fresh.WINDOWS)
    for item in audit['files'].values():
        assert hashlib.sha256(item['text'].encode()).hexdigest() == item['sha256']
    core = configparser.ConfigParser(interpolation=None)
    core.read_string(audit['files']['core_downloads']['text'])
    assert core['chromium']['url'].endswith('chromium-%(_chromium_version)s-lite.tar.xz')
    assert 'hash_url' in core['chromium']
    downloads = configparser.ConfigParser()
    downloads.read_string(audit['files']['windows_downloads']['text'])
    for name, version, output in (
        ('llvm', '23.1.0', 'third_party/llvm-build/Release+Asserts'),
        ('nodejs', '24.12.0', 'third_party/node/win'),
        ('esbuild', '0.28.2', 'third_party/devtools-frontend/src/third_party/esbuild'),
        ('typescript', '7.0.2', 'third_party/typescript/windows-amd64/src'),
    ):
        assert downloads[name]['version'] == version
        assert downloads[name]['output_path'] == output
        assert len(downloads[name]['sha512']) == 128


def test_official_156_midl_patch_reproduces_reviewed_transform(tmp_path):
    audit = json.loads((ROOT / 'tools/tests/fixtures/windows156_fresh.json').read_text())['files']
    path = tmp_path / repair.SCRIPT
    path.parent.mkdir(parents=True)
    path.write_text(audit['midl_raw']['text'])
    patch = audit['midl_patch']['text'].split('--- a/build/win/')[0]
    result = subprocess.run(['git', 'apply', '--no-index', '--unsafe-paths', '-'], input=patch,
                            cwd=tmp_path, text=True, capture_output=True,
                            env={**os.environ, 'GIT_CEILING_DIRECTORIES': str(tmp_path.parent)})
    assert result.returncode == 0, result.stderr
    assert hashlib.sha256(path.read_bytes()).hexdigest() == '501ad1740d960ab1f2afeb8278162b05163e3772edb6ea167554f67ef82ca99c'
    fresh.midl(tmp_path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == 'f113aabeee53f6ba0fce6d364d45d294b094b656afc03e24029bf7503fb99b3a'
