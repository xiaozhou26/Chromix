"""Explicit Windows snapshot migration stays opt-in and precedes preparation."""
import builtins
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
WORKFLOW = ROOT / ".github/workflows/build-win-x64-github.yml"
ACTION = ROOT / ".github/actions/restore-windows-snapshot/action.yml"
STAGE = ROOT / "build/windows/ci-stage.ps1"


def test_exact_migration_inputs_and_restore_are_isolated_to_selected_stage():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    events = workflow.get("on", workflow.get(True))
    for event in ("workflow_call", "workflow_dispatch"):
        for name in ("resume_source_sha", "resume_artifact_ids"):
            assert events[event]["inputs"][name]["type"] == "string"
            assert events[event]["inputs"][name]["default"] == ""
    for index in range(1, 13):
        job = workflow["jobs"][f"build-{index}"]
        steps = job["steps"]
        guard = next(s for s in steps if s.get("name") == "Check explicit snapshot migration inputs")
        restore = next(s for s in steps if s.get("name") == "Restore exact source-migration snapshot")
        stage = next(s for s in steps if s.get("id") == "stage")
        assert steps.index(guard) < steps.index(restore) < steps.index(stage)
        assert "[bool]$env:RESUME_SOURCE_SHA -ne [bool]$env:RESUME_ARTIFACT_IDS" in guard["run"]
        assert "$env:RESUME_TREE_STAGE" in guard["run"]
        assert "$env:UPSTREAM_RUN_ID" in guard["run"]
        assert restore["if"] == "${{ inputs.resume_source_sha != '' && github.job == format('build-{0}', inputs.resume_stage) }}"
        assert restore["uses"] == "./.github/actions/restore-windows-snapshot"
        assert restore["with"]["artifact-ids"] == "${{ inputs.resume_artifact_ids }}"
        assert restore["with"]["source-sha"] == "${{ inputs.resume_source_sha }}"
        if index > 1:
            legacy = next(s for s in steps if s.get("name") == "Download tree from previous run")
            assert "inputs.resume_source_sha == ''" in legacy["if"]
            previous = next(s for s in steps if s.get("name") == "Download tree from previous stage")
            assert "resume_source_sha" not in previous["if"]
        assert "continue-on-error" not in stage
        assert all("snapshot_safe == 'true'" in s["if"] for s in steps
                   if s.get("name", "").startswith("Upload tree part"))


def test_action_uses_validated_commit_and_enables_migration_only_after_download():
    steps = yaml.safe_load(ACTION.read_text())["runs"]["steps"]
    validation = next(s for s in steps if s.get("id") == "donor")
    checkout = next(s for s in steps if s.get("uses") == "actions/checkout@v4")
    isolate = next(s for s in steps if s.get("name", "").startswith("Isolate previous"))
    download = next(s for s in steps if "download_windows_snapshot.py" in s.get("run", ""))
    enable = next(s for s in steps if s.get("name") == "Enable explicit verified source migration")
    assert steps.index(validation) < steps.index(checkout) < steps.index(isolate) < steps.index(download) < steps.index(enable)
    assert checkout["with"]["ref"] == "${{ steps.donor.outputs.head_sha }}"
    assert checkout["with"]["persist-credentials"] is False
    assert "Join-Path $env:RUNNER_TEMP 'chromix-previous-repo'" in isolate["run"]
    assert "if ($LASTEXITCODE -ne 0)" in validation["run"]
    assert "if ($LASTEXITCODE -ne 0)" in download["run"]
    assert "CHROMIX_WINDOWS_MIGRATION_REPO=" in enable["run"]
    assert "CHROMIX_WINDOWS_MIGRATION_SHA=" in enable["run"]
    assert "${{" not in validation["run"] + download["run"] + enable["run"]


def migration_block():
    source = STAGE.read_text()
    start = source.index("if ($env:CHROMIX_WINDOWS_MIGRATION_REPO -or")
    end = source.index('& "$PSScriptRoot\\assert-target-arch.ps1" -WorkDir $WorkDir', start)
    return source[start:end]


def test_migration_preserves_normal_checks_and_fails_snapshot_closed():
    source = STAGE.read_text()
    block = migration_block()
    assert source.index('throw "7z restore failed"') < source.index(block)
    assert source.index(block) < source.index('$domainProgress = Join-Path')
    assert source.index(block) < source.index('& "$PSScriptRoot\\prepare-ungoogled.ps1"')
    assert block.index("Write-OutVar snapshot_safe false") < block.index("migrate_windows_snapshot.py")
    assert block.index("if ($LASTEXITCODE -ne 0)") < block.index("Write-OutVar snapshot_safe true")
    assert "third_party" not in block
    assert ".chromix-source-ready" not in block
    assert 'tools\\verify_patch_stack.py' in source
    assert 'throw "required upstream cache: restore receipt missing;' in source


@pytest.mark.parametrize("mode", ["disabled", "success", "failure", "missing_sha", "not_artifact", "arm64", "upstream", "patch_failure", "mingw_layout"])
def test_powershell_migration_guard_and_failure(mode, tmp_path):
    powershell = shutil.which("pwsh") or shutil.which("powershell") or "/opt/pwsh/pwsh"
    if not Path(powershell).is_file():
        pytest.skip("PowerShell is unavailable")
    script = tmp_path / "exercise.ps1"
    script.write_text(r'''
$ErrorActionPreference = 'Stop'
$script:events = [Collections.Generic.List[string]]::new()
function Write-OutVar($key, $value) { $script:events.Add("$key=$value") }
function Get-Command { [pscustomobject]@{Source=(Join-Path $env:TEST_ROOT $(if ($env:TEST_MODE -eq 'mingw_layout') { 'host/mingw64/bin/git.exe' } else { 'host/cmd/git.exe' }))} }
function python {
  if ($args -contains '--select-patch-bin') {
    $script:events.Add('probe')
    $global:LASTEXITCODE = $(if ($env:TEST_MODE -eq 'patch_failure') { 1 } else { 0 })
    Join-Path $env:TEST_ROOT 'host/usr/bin/patch.exe'
  } else {
    $script:events.Add('python:' + ($args -join ' '))
    $global:LASTEXITCODE = $(if ($env:TEST_MODE -eq 'failure') { 1 } else { 0 })
  }
}
$WorkDir = Join-Path $env:TEST_ROOT 'work'
$Repo = Join-Path $env:TEST_ROOT 'repo'
$FromArtifact = $env:TEST_MODE -ne 'not_artifact'
$Arch = $(if ($env:TEST_MODE -eq 'arm64') { 'arm64' } else { 'x64' })
$RequireUpstreamCache = $env:TEST_MODE -eq 'upstream'
$env:CHROMIX_WINDOWS_MIGRATION_REPO = $(if ($env:TEST_MODE -ne 'disabled') { Join-Path $env:TEST_ROOT 'previous' } else { '' })
$env:CHROMIX_WINDOWS_MIGRATION_SHA = $(if ($env:TEST_MODE -notin @('disabled', 'missing_sha')) { '97f2881b0e5f43b7e9563569d92dfe702ed1df0b' } else { '' })
$failed = $false
try {
''' + migration_block() + r'''
} catch { $failed = $true }
[pscustomobject]@{failed=$failed; events=@($script:events)} | ConvertTo-Json -Compress
''')
    patch = tmp_path / "host/usr/bin/patch.exe"
    patch.parent.mkdir(parents=True)
    patch.write_bytes(b"fixture")
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-File", str(script)],
                            env={**os.environ, "TEST_ROOT": str(tmp_path), "TEST_MODE": mode},
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["failed"] is (mode not in ("disabled", "success", "mingw_layout"))
    if mode == "disabled":
        assert state["events"] == []
    elif mode in ("success", "failure", "patch_failure", "mingw_layout"):
        assert state["events"][:2] == ["snapshot_safe=false", "probe"]
        if mode == "patch_failure":
            assert state["events"] == ["snapshot_safe=false", "probe"]
        else:
            assert state["events"][2].startswith("python:-X utf8 ")
            assert "--expected-previous-sha 97f2881" in state["events"][2]
            assert ("snapshot_safe=true" in state["events"]) is (mode in ("success", "mingw_layout"))
    else:
        assert state["events"] == []


STAGE9_SHA = "30dbab28692793fa311c82ae639186003f3a67d9"
LEGACY_SHA = "97f2881b0e5f43b7e9563569d92dfe702ed1df0b"


def test_all_windows_migration_guards_pin_both_profiles_without_broadening_restore():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert not any(name.startswith("CHROMIX_WINDOWS_MIGRATION_") for name in workflow["env"])
    action = yaml.safe_load(ACTION.read_text())
    profile = next(s for s in action["runs"]["steps"]
                   if s.get("name") == "Enable explicit verified source migration")["run"]
    assert STAGE9_SHA in profile and "windows-152-x64-stage9" in profile
    assert "windows-152-x64-legacy-0152" in profile
    guards = []
    for index in range(1, 13):
        steps = workflow["jobs"][f"build-{index}"]["steps"]
        guard = next(s for s in steps if s.get("name") == "Check explicit snapshot migration inputs")
        restore = next(s for s in steps if s.get("name") == "Restore exact source-migration snapshot")
        assert guard["env"]["RESUME_ATTEMPT"] == "${{ inputs.resume_attempt }}"
        for value in (STAGE9_SHA, LEGACY_SHA, "35315624638", "10536907942,10536997738",
                      "$env:RESUME_TREE_STAGE -cne '9'", "$env:RESUME_ATTEMPT -cne '1'",
                      "$env:CHROMIX_BUILD_PROFILE -cne 'native'"):
            assert value in guard["run"]
        branch = restore["with"]["recovery-branch"]
        assert STAGE9_SHA in branch
        assert "fix/issue3-font-resume-20260917" in branch
        assert "fix/issue3-resource-timing-20260916" in branch
        guards.append(guard["run"])
    assert len(set(guards)) == 1


@pytest.mark.parametrize("mutation", ["valid", "reverse-artifacts", "legacy", "sha", "uppercase", "run", "stage", "attempt",
                                      "profile", "extra-artifact", "duplicate-artifact", "missing-artifact", "upstream"])
def test_historical152_workflow_guard_executes_exact_profile_checks(tmp_path, mutation):
    powershell = shutil.which("pwsh") or shutil.which("powershell") or "/opt/pwsh/pwsh"
    if not Path(powershell).is_file():
        pytest.skip("PowerShell is unavailable")
    (tmp_path / "CHROMIUM_VERSION").write_text("152.0.7977.82\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build/ungoogled-revisions.psd1").write_text('@{\n  ChromiumVersion = "152.0.7977.82"\n}\n')
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["build-9"]["steps"]
    guard = next(s for s in steps if s.get("name") == "Check explicit snapshot migration inputs")["run"]
    env = dict(os.environ, RESUME_SOURCE_SHA=STAGE9_SHA, RESUME_RUN_ID="35315624638", RESUME_TREE_STAGE="9",
               RESUME_ATTEMPT="1", RESUME_ARTIFACT_IDS="10536907942,10536997738", CHROMIX_BUILD_PROFILE="native",
               USE_UPSTREAM_CACHE="false", UPSTREAM_RUN_ID="", GITHUB_WORKSPACE=str(tmp_path))
    overrides = {
        "valid": {}, "reverse-artifacts": {"RESUME_ARTIFACT_IDS": "10536997738, 10536907942"},
        "legacy": {"RESUME_SOURCE_SHA": LEGACY_SHA, "RESUME_RUN_ID": "35054494898", "RESUME_TREE_STAGE": "3",
                   "RESUME_ARTIFACT_IDS": "10462681393,10462257055"},
        "sha": {"RESUME_SOURCE_SHA": "a" * 40}, "uppercase": {"RESUME_SOURCE_SHA": STAGE9_SHA.upper()},
        "run": {"RESUME_RUN_ID": "35315624639"}, "stage": {"RESUME_TREE_STAGE": "8"},
        "attempt": {"RESUME_ATTEMPT": "2"}, "profile": {"CHROMIX_BUILD_PROFILE": "fast"},
        "extra-artifact": {"RESUME_ARTIFACT_IDS": "10536907942,10536997738,1"},
        "duplicate-artifact": {"RESUME_ARTIFACT_IDS": "10536907942,10536907942"},
        "missing-artifact": {"RESUME_ARTIFACT_IDS": "10536907942"}, "upstream": {"USE_UPSTREAM_CACHE": "true"},
    }
    env.update(overrides[mutation])
    script = tmp_path / "guard.ps1"
    script.write_text("$ErrorActionPreference = 'Stop'\ntry {\n" + guard + "\n} catch { exit 1 }\nexit 0\n")
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-File", str(script)],
                            env=env, cwd=tmp_path, text=True, capture_output=True, timeout=30)
    assert result.returncode == (0 if mutation in ("valid", "reverse-artifacts", "legacy") else 1), result.stderr


@pytest.mark.parametrize('version,pin,enabled,expected', [
    ('154.0.8037.57', '154.0.8037.57', True, 1),
    ('154.0.8037.57', '154.0.8037.57', False, 0),
    ('153.0.8010.36', '153.0.8010.36', True, 1),
    ('152.0.7977.82', '153.0.8010.36', True, 1),
    ('153.0.8010.36', '152.0.7977.82', True, 1),
    ('153.0.8010.36', '153.0.8010.36', False, 0),
])
def test_historical_migration_guard_rejects_new_target_before_restore(tmp_path, version, pin, enabled, expected):
    powershell = shutil.which('pwsh') or shutil.which('powershell') or '/opt/pwsh/pwsh'
    if not Path(powershell).is_file():
        pytest.skip('PowerShell is unavailable')
    (tmp_path / 'CHROMIUM_VERSION').write_text(version + '\n')
    (tmp_path / 'build').mkdir()
    (tmp_path / 'build/ungoogled-revisions.psd1').write_text('@{ ChromiumVersion = "' + pin + '" }\n')
    steps = yaml.safe_load(WORKFLOW.read_text())['jobs']['build-9']['steps']
    guard = next(s for s in steps if s.get('name') == 'Check explicit snapshot migration inputs')['run']
    env = dict(os.environ, RESUME_SOURCE_SHA=STAGE9_SHA if enabled else '',
               RESUME_ARTIFACT_IDS='10536907942,10536997738' if enabled else '',
               RESUME_RUN_ID='35315624638', RESUME_TREE_STAGE='9', RESUME_ATTEMPT='1',
               CHROMIX_BUILD_PROFILE='native', USE_UPSTREAM_CACHE='false', UPSTREAM_RUN_ID='',
               GITHUB_WORKSPACE=str(tmp_path))
    script = tmp_path / 'guard.ps1'
    script.write_text("$ErrorActionPreference = 'Stop'\ntry {\n" + guard + '\n} catch { exit 1 }\nexit 0\n')
    result = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-File', str(script)],
                            env=env, cwd=tmp_path, text=True, capture_output=True, timeout=30)
    assert result.returncode == expected, result.stderr


WINDOWS153_SHA = "e5b29c58b44e381924a2dd4bd60f54d01abb9d9c"
STAGE8_SHA = "2a55082adb89cb8bac7aa7ab8bb61162b53f4c35"
WINDOWS155_SHA = "4470c3c7435a9f2de203396587c6f57705a8c9d5"
VERIFY_MODE = "${{ (inputs.resume_source_sha == '" + WINDOWS153_SHA + "' || inputs.resume_source_sha == '" + STAGE8_SHA + "' || inputs.resume_source_sha == '" + WINDOWS155_SHA + "') && 'verify-source' || 'migration' }}"


def run_powershell(tmp_path, script, env):
    powershell = shutil.which("pwsh") or shutil.which("powershell") or "/opt/pwsh/pwsh"
    if not Path(powershell).is_file():
        pytest.skip("PowerShell is unavailable")
    path = tmp_path / "exercise.ps1"
    path.write_text("$ErrorActionPreference = 'Stop'\n" + script)
    return subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-File", str(path)],
                          env={**os.environ, **env}, cwd=tmp_path, text=True, capture_output=True, timeout=30)


def test_verify_source_mode_is_separate_and_only_enabled_after_exact_download():
    action = yaml.safe_load(ACTION.read_text())
    assert action["inputs"]["mode"]["default"] == "migration"
    steps = action["runs"]["steps"]
    guard = steps[0]
    enable = next(s for s in steps if s.get("name") == "Enable explicit unchanged-source verification")
    migration = next(s for s in steps if s.get("name") == "Enable explicit verified source migration")
    download = next(s for s in steps if "download_windows_snapshot.py" in s.get("run", ""))
    assert steps.index(guard) < steps.index(download) < steps.index(enable)
    assert "-cnotin @('migration', 'verify-source')" in guard["run"]
    assert enable["if"] == "${{ inputs.mode == 'verify-source' }}"
    assert migration["if"] == "${{ inputs.mode == 'migration' }}"
    assert "CHROMIX_WINDOWS_MIGRATION_" not in enable["run"]
    assert "CHROMIX_WINDOWS_VERIFY_SOURCE_REPO=" in enable["run"]
    assert "CHROMIX_WINDOWS_VERIFY_SOURCE_SHA=" in enable["run"]
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert not any("MIGRATION" in name or "VERIFY_SOURCE" in name for name in workflow["env"])
    for index in range(1, 13):
        steps = workflow["jobs"][f"build-{index}"]["steps"]
        restore = next(s for s in steps if s.get("uses") == "./.github/actions/restore-windows-snapshot")
        assert restore["with"]["mode"] == VERIFY_MODE
        assert WINDOWS153_SHA in restore["with"]["recovery-branch"]
        assert STAGE8_SHA in restore["with"]["recovery-branch"]
        assert "&& 'fix/win153-midl-stat-20260920'" in restore["with"]["recovery-branch"]
        assert "&& 'main'" in restore["with"]["recovery-branch"]
        assert "github.job == format('build-{0}', inputs.resume_stage)" in restore["if"]
        checkout = next(s for s in steps if s.get("uses") == "actions/checkout@v4")
        assert "ref" not in checkout.get("with", {})
        assert steps.index(checkout) < steps.index(restore)


def native_midl_read_preflight():
    steps = yaml.safe_load(ACTION.read_text())["runs"]["steps"]
    return next(s for s in steps if s.get("name") == "Preflight native Windows MIDL reads")


def test_native_midl_read_preflight_is_strict_and_precedes_all_restore_io():
    action = yaml.safe_load(ACTION.read_text())
    steps = action["runs"]["steps"]
    preflight = native_midl_read_preflight()
    assert steps[0]["name"] == "Check explicit restore mode"
    assert steps[1] == preflight
    assert preflight["if"] == "${{ inputs.mode == 'verify-source' && (inputs.source-sha == '" + WINDOWS153_SHA + "' || inputs.source-sha == '" + STAGE8_SHA + "') }}"
    assert preflight["shell"] == "python"
    assert preflight["env"] == {"PYTHONDONTWRITEBYTECODE": "1"}
    assert "continue-on-error" not in preflight
    assert "from tools.repair_windows_midl import _read" in preflight["run"]
    assert '${{' not in preflight["run"]
    assert not any("setup-python" in s.get("uses", "") for s in steps)
    for name in ("Validate exact Windows snapshot origin", "Checkout exact previous source identity",
                 "Download digest-pinned Windows snapshot", "Enable explicit unchanged-source verification"):
        assert steps.index(preflight) < next(i for i, s in enumerate(steps) if s.get("name") == name)
    workflow = yaml.safe_load(WORKFLOW.read_text())
    for index in range(1, 13):
        job = workflow["jobs"][f"build-{index}"]
        assert job["runs-on"] == "windows-2022"
        steps = job["steps"]
        guard = next(s for s in steps if s.get("name") == "Check explicit snapshot migration inputs")
        restore = next(s for s in steps if s.get("name") == "Restore exact source-migration snapshot")
        assert steps.index(guard) + 1 == steps.index(restore)
        assert "continue-on-error" not in guard and "continue-on-error" not in restore
        assert restore["if"] == "${{ inputs.resume_source_sha != '' && github.job == format('build-{0}', inputs.resume_stage) }}"
        assert restore["with"]["mode"] == VERIFY_MODE
        assert restore["with"]["source-sha"] == "${{ inputs.resume_source_sha }}"
        assert not any("setup-python" in s.get("uses", "") for s in steps)


def execute_midl_read_preflight(tmp_path, monkeypatch, *, platform="win32"):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(ROOT))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    native_import = builtins.__import__
    interpreter = SimpleNamespace(platform=platform, version=sys.version, executable=sys.executable, path=list(sys.path))

    def fixture_import(name, *args, **kwargs):
        return interpreter if name == "sys" else native_import(name, *args, **kwargs)

    namespace = {"__builtins__": {**vars(builtins), "__import__": fixture_import}}
    exec(compile(native_midl_read_preflight()["run"], str(ACTION), "exec"), namespace)


@pytest.mark.parametrize("failed_case", [None, "write-fsync", "readonly", "100ns-mtime", "midl-temp"])
@pytest.mark.parametrize("failure", ["read-error", "content-mismatch"])
def test_native_midl_read_preflight_cases_diagnostics_and_cleanup(tmp_path, monkeypatch, capsys, failed_case, failure):
    from tools import repair_windows_midl as midl

    cases = ["write-fsync", "readonly", "100ns-mtime", "midl-temp"]
    read, fsync, chmod, fstat = midl._read, os.fsync, Path.chmod, os.fstat
    calls, syncs, modes = [], [], []

    def record_fsync(fd):
        syncs.append(fd)
        fsync(fd)

    def record_chmod(path, mode):
        modes.append((path, mode))
        chmod(path, mode)

    def exercise_read(path):
        case = cases[len(calls)]
        calls.append(path)
        assert path.parent.parent == tmp_path
        assert path.name == (".midl-temp.tmp" if case == "midl-temp" else "probe.py")
        assert len(syncs) == (2 if case == "midl-temp" else 1)
        if case == "readonly":
            assert not path.stat().st_mode & stat.S_IWRITE
        if case == "100ns-mtime":
            assert path.stat().st_mode & stat.S_IWRITE
            assert path.stat().st_mtime_ns == 1_700_000_000_123_456_700
        if case == failed_case:
            if failure == "read-error":
                raise ValueError("injected MIDL read failure")
            return b"different", path.stat()
        # Keep the diagnostic-only difference out of the real helper read.
        with monkeypatch.context() as context:
            context.setattr(os, "fstat", fstat)
            return read(path)

    def diagnostic_fstat(fd):
        info = fstat(fd)
        values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        values["st_ctime_ns"] += 100
        return SimpleNamespace(**values)

    monkeypatch.setattr(midl, "_read", exercise_read)
    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(Path, "chmod", record_chmod)
    monkeypatch.setattr(os, "fstat", diagnostic_fstat)
    if failed_case is None:
        execute_midl_read_preflight(tmp_path, monkeypatch)
        expected = len(cases)
    else:
        error = "injected MIDL read failure" if failure == "read-error" else "content mismatch"
        with pytest.raises((ValueError, RuntimeError), match=error):
            execute_midl_read_preflight(tmp_path, monkeypatch)
        expected = cases.index(failed_case) + 1
    output = capsys.readouterr().out
    assert "sys.version=" + sys.version in output
    assert "executable=" + sys.executable in output
    assert len(calls) == expected
    for index, case in enumerate(cases):
        assert (f"{case}: OK" in output) is (index < expected and case != failed_case)
        assert (f"{case}: lstat=" in output) is (index < expected)
    assert output.count("differences={'st_ctime_ns': (") == expected
    for path in set(calls):
        assert (path, stat.S_IREAD | stat.S_IWRITE) in modes
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure,error", [
    ("platform", "requires native Windows Python"),
    ("fsync", "unsupported fsync"),
    ("utime", "unsupported utime"),
    ("precision", "did not preserve 100ns mtime"),
    ("readonly", "did not preserve readonly mode"),
])
def test_native_midl_read_preflight_rejects_unsupported_cases(tmp_path, monkeypatch, capsys, failure, error):
    from tools import repair_windows_midl as midl

    def unsupported(*args, **kwargs):
        raise OSError("unsupported " + failure)

    utime, chmod = os.utime, Path.chmod
    if failure in ("fsync", "utime"):
        monkeypatch.setattr(os, failure, unsupported)
    elif failure == "precision":
        monkeypatch.setattr(os, "utime", lambda path, *, ns: utime(path, ns=tuple(t // 1_000 * 1_000 for t in ns)))
    elif failure == "readonly":
        monkeypatch.setattr(Path, "chmod", lambda path, mode: None if mode == stat.S_IREAD else chmod(path, mode))
    monkeypatch.setattr(midl, "_read", lambda path: (path.read_bytes(), path.stat()))
    with pytest.raises((OSError, RuntimeError), match=error):
        execute_midl_read_preflight(tmp_path, monkeypatch, platform="linux" if failure == "platform" else "win32")
    output = capsys.readouterr().out
    assert "sys.version=" in output
    assert "midl-temp: OK" not in output
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("mutation", ["valid", "reverse-artifacts", "sha", "uppercase", "run", "resume-stage", "tree-stage",
    "attempt", "fast", "release", "upstream-false", "upstream-run", "upstream-empty", "missing-artifact", "extra-artifact",
    "duplicate-artifact", "sha-only", "ids-only", "wrong-version", "wrong-pin", "cross-profile", "target154", "fresh154"])
@pytest.mark.parametrize("stage", [6, 8])
def test_windows153_workflow_guard_checks_exact_native_upstream_resume(tmp_path, mutation, stage):
    for name in ("CHROMIUM_VERSION", "CHROMIUM_WINDOWS_VERSION", "build/ungoogled-revisions.psd1",
                 "build/windows/read-platform-pins.ps1", "tools/platform_pins.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, path)
    from tools.tests.test_fetch_upstream_cache import pinned_windows153_source

    pinned_windows153_source(tmp_path)
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["build-6"]["steps"]
    guard = next(s for s in steps if s.get("name") == "Check explicit snapshot migration inputs")
    assert guard["env"]["RESUME_STAGE"] == "${{ inputs.resume_stage }}"
    env = dict(RESUME_SOURCE_SHA=WINDOWS153_SHA, RESUME_RUN_ID="35387701778", RESUME_STAGE="6", RESUME_TREE_STAGE="6",
               RESUME_ATTEMPT="1", RESUME_ARTIFACT_IDS="10593998213,10593823308", CHROMIX_BUILD_PROFILE="native",
               USE_UPSTREAM_CACHE="true", UPSTREAM_RUN_ID="35059013905", GITHUB_WORKSPACE=str(tmp_path))
    overrides = {
        "valid": {}, "reverse-artifacts": {"RESUME_ARTIFACT_IDS": "10593823308, 10593998213"},
        "sha": {"RESUME_SOURCE_SHA": "a" * 40}, "uppercase": {"RESUME_SOURCE_SHA": WINDOWS153_SHA.upper()},
        "run": {"RESUME_RUN_ID": "35387701779"}, "resume-stage": {"RESUME_STAGE": "7"},
        "tree-stage": {"RESUME_TREE_STAGE": "5"}, "attempt": {"RESUME_ATTEMPT": "2"},
        "fast": {"CHROMIX_BUILD_PROFILE": "fast"}, "release": {"CHROMIX_BUILD_PROFILE": "release"},
        "upstream-false": {"USE_UPSTREAM_CACHE": "false"}, "upstream-run": {"UPSTREAM_RUN_ID": "35059013906"},
        "upstream-empty": {"UPSTREAM_RUN_ID": ""}, "missing-artifact": {"RESUME_ARTIFACT_IDS": "10593823308"},
        "extra-artifact": {"RESUME_ARTIFACT_IDS": "10593823308,10593998213,1"},
        "duplicate-artifact": {"RESUME_ARTIFACT_IDS": "10593823308,10593823308"},
        "sha-only": {"RESUME_ARTIFACT_IDS": ""}, "ids-only": {"RESUME_SOURCE_SHA": ""},
        "wrong-version": {}, "wrong-pin": {}, "target154": {},
        "fresh154": {"RESUME_SOURCE_SHA": "", "RESUME_ARTIFACT_IDS": "", "RESUME_RUN_ID": "",
                     "UPSTREAM_RUN_ID": "36093095228"},
        "cross-profile": {"RESUME_SOURCE_SHA": STAGE8_SHA if stage == 6 else WINDOWS153_SHA},
    }
    if stage == 8:
        env.update(RESUME_SOURCE_SHA=STAGE8_SHA, RESUME_RUN_ID="35485726877", RESUME_STAGE="8",
                   RESUME_TREE_STAGE="8", RESUME_ARTIFACT_IDS="10608631230,10608606403")
        overrides["reverse-artifacts"] = {"RESUME_ARTIFACT_IDS": "10608606403, 10608631230"}
        overrides["uppercase"] = {"RESUME_SOURCE_SHA": STAGE8_SHA.upper()}
    env.update(overrides[mutation])
    if mutation in ("target154", "fresh154"):
        for name in ("CHROMIUM_WINDOWS_VERSION", "build/ungoogled-revisions.psd1"):
            path = tmp_path / name
            path.write_text(path.read_text().replace("153.0.8010.47", "154.0.8037.57"))
    if mutation == "wrong-version":
        (tmp_path / "CHROMIUM_WINDOWS_VERSION").write_text("153.0.8010.36\n")
    if mutation == "wrong-pin":
        path = tmp_path / "build/ungoogled-revisions.psd1"
        path.write_text(path.read_text().replace("153.0.8010.47", "153.0.8010.36"))
    result = run_powershell(tmp_path, "try {\n" + guard["run"] + "\n} catch { Write-Host $_; exit 1 }\nexit 0\n", env)
    assert result.returncode == (0 if mutation in ("valid", "reverse-artifacts", "fresh154") else 1), result.stdout + result.stderr


@pytest.mark.parametrize("mutation", ["valid", "migration", "153-migration", "unknown-migration", "unknown-mode", "uppercase-mode", "sha", "run", "stage", "attempt",
                                       "artifact", "duplicate", "mixed-mode"])
def test_action_mode_guard_is_fail_closed(tmp_path, mutation):
    guard = yaml.safe_load(ACTION.read_text())["runs"]["steps"][0]["run"]
    env = dict(SNAPSHOT_MODE="verify-source", SNAPSHOT_SHA=WINDOWS153_SHA, SNAPSHOT_RUN="35387701778",
               SNAPSHOT_STAGE="6", SNAPSHOT_ATTEMPT="1", SNAPSHOT_ARTIFACT_IDS="10593998213,10593823308",
               CHROMIX_WINDOWS_MIGRATION_REPO="", CHROMIX_WINDOWS_MIGRATION_SHA="", CHROMIX_WINDOWS_MIGRATION_PROFILE="")
    overrides = {"valid": {}, "migration": {"SNAPSHOT_MODE": "migration", "SNAPSHOT_SHA": LEGACY_SHA},
                 "153-migration": {"SNAPSHOT_MODE": "migration"},
                 "unknown-migration": {"SNAPSHOT_MODE": "migration", "SNAPSHOT_SHA": "a" * 40},
                 "unknown-mode": {"SNAPSHOT_MODE": "auto"}, "uppercase-mode": {"SNAPSHOT_MODE": "VERIFY-SOURCE"},
                 "sha": {"SNAPSHOT_SHA": LEGACY_SHA}, "run": {"SNAPSHOT_RUN": "1"}, "stage": {"SNAPSHOT_STAGE": "7"},
                 "attempt": {"SNAPSHOT_ATTEMPT": "2"}, "artifact": {"SNAPSHOT_ARTIFACT_IDS": "10593823308"},
                 "duplicate": {"SNAPSHOT_ARTIFACT_IDS": "10593823308,10593823308"},
                 "mixed-mode": {"CHROMIX_WINDOWS_MIGRATION_PROFILE": "windows-152-x64-stage9"}}
    env.update(overrides[mutation])
    result = run_powershell(tmp_path, "try {\n" + guard + "\n} catch { exit 1 }\nexit 0\n", env)
    assert result.returncode == (0 if mutation in ("valid", "migration") else 1), result.stderr


@pytest.mark.parametrize("mutation", ["valid", "nonterminal", "wrong-workflow", "wrong-sha", "wrong-attempt", "wrong-stage",
                                       "extra-id", "missing-id", "expired", "wrong-origin", "branch", "missing-digest"])
def test_exact_windows153_donor_uses_existing_metadata_validator(mutation):
    from tools import validate_windows_snapshot as validator
    from tools.tests.test_validate_windows_snapshot import Client, REPO

    identifiers = [10593998213, 10593823308]
    client = Client(run_id=35387701778, stage=6, attempt=1, sha=WINDOWS153_SHA)
    for artifact, identifier in zip(client.artifacts, identifiers):
        artifact["id"] = identifier
    options = dict(repository=REPO, run_id=35387701778, stage=6, attempt=1,
                   expected_sha=WINDOWS153_SHA, expected_artifact_ids=identifiers, recovery_branch="main")
    if mutation == "nonterminal":
        client.run["status"] = "in_progress"
    elif mutation == "wrong-workflow":
        client.run["path"] = ".github/workflows/build-win-arm64-github.yml"
    elif mutation == "wrong-sha":
        options["expected_sha"] = LEGACY_SHA
    elif mutation == "wrong-attempt":
        options["attempt"] = 2
    elif mutation == "wrong-stage":
        options["stage"] = 5
    elif mutation == "extra-id":
        options["expected_artifact_ids"] = identifiers + [1]
    elif mutation == "missing-id":
        options["expected_artifact_ids"] = identifiers[:1]
    elif mutation == "expired":
        client.artifacts[0]["expired"] = True
    elif mutation == "wrong-origin":
        client.artifacts[0]["workflow_run"]["id"] = 1
    elif mutation == "branch":
        client.run.update(head_branch="other", event="workflow_dispatch")
    elif mutation == "missing-digest":
        client.artifacts[0].pop("digest")
    if mutation != "valid":
        with pytest.raises(ValueError):
            validator.validate(client, **options)
    else:
        report = validator.validate(client, **options)
        assert report["arch"] == "x64"
        assert report["pattern"] == "tree-s6-attempt-1-part*"
        assert [item["id"] for item in report["artifacts"]] == identifiers
        assert client.calls == ["/actions/runs/35387701778", "/actions/runs/35387701778/attempts/1/jobs",
                                "/actions/runs/35387701778/artifacts"]


def stage_preflight_block():
    source = STAGE.read_text()
    start = source.index("Write-OutVar finished false\nWrite-OutVar upload_parts false")
    end = source.index('\nif ($Arch -eq "arm64") {', start)
    return source[start:end]


@pytest.mark.parametrize("mode", ["strict", "strict-stage8", "strict-repo-only", "strict-sha-only", "strict-not-artifact",
                                  "normal-resume", "cold", "legacy152", "stage9-152", "arm64"])
@pytest.mark.parametrize("failure", ["preflight", "disk", "vs", "sdk", "none"])
def test_initial_snapshot_gate_precedes_preflight_and_sdk_failures(tmp_path, mode, failure):
    script = r'''
$script:events = [Collections.Generic.List[string]]::new()
function Write-OutVar($key, $value) { $script:events.Add("$key=$value") }
function Invoke-PreflightFixture($name) {
  $script:events.Add("preflight:$name")
  if ($env:TEST_FAILURE -eq $name) { throw "injected $name failure before extraction" }
}
function Assert-CiScripts { Invoke-PreflightFixture 'preflight' }
function Free-Disk { Invoke-PreflightFixture 'disk' }
function Initialize-VisualStudio { Invoke-PreflightFixture 'vs' }
function Install-WindowsSdk { Invoke-PreflightFixture 'sdk' }
$FromArtifact = $env:TEST_MODE -notin @('cold', 'strict-not-artifact')
$Arch = $(if ($env:TEST_MODE -eq 'arm64') { 'arm64' } else { 'x64' })
$failed = $false
try {
''' + stage_preflight_block() + r'''
} catch { $failed = $true }
[pscustomobject]@{failed=$failed; events=@($script:events)} | ConvertTo-Json -Compress
'''
    strict = mode.startswith("strict")
    env = dict(TEST_MODE=mode, TEST_FAILURE=failure,
               CHROMIX_WINDOWS_VERIFY_SOURCE_REPO="previous" if strict and mode != "strict-sha-only" else "",
               CHROMIX_WINDOWS_VERIFY_SOURCE_SHA=(STAGE8_SHA if mode == "strict-stage8" else WINDOWS153_SHA) if strict and mode != "strict-repo-only" else "",
               CHROMIX_WINDOWS_MIGRATION_REPO="previous" if mode in ("legacy152", "stage9-152") else "",
               CHROMIX_WINDOWS_MIGRATION_SHA={"legacy152": LEGACY_SHA, "stage9-152": STAGE9_SHA}.get(mode, ""),
               CHROMIX_WINDOWS_MIGRATION_PROFILE={"legacy152": "windows-152-x64-legacy-0152",
                                                  "stage9-152": "windows-152-x64-stage9"}.get(mode, ""))
    result = run_powershell(tmp_path, script, env)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["failed"] is (failure != "none")
    calls = ["preflight", "disk", "vs", "sdk"]
    if failure != "none":
        calls = calls[:calls.index(failure) + 1]
    gate = "snapshot_safe=false"
    assert state["events"] == ["finished=false", "upload_parts=false", gate,
                               *("preflight:" + name for name in calls)]


def verify_source_block():
    source = STAGE.read_text()
    start = source.index("\nif ($env:CHROMIX_WINDOWS_VERIFY_SOURCE_REPO -or") + 1
    end = source.index("if ($env:CHROMIX_WINDOWS_MIGRATION_REPO -or", start)
    return source[start:end]


def test_unchanged_source_verification_precedes_prepare_without_bypassing_any_existing_checks():
    source = STAGE.read_text()
    block = verify_source_block()
    assert source.index('throw "7z restore failed"') < source.index(block)
    assert source.index(block) < source.index('tools\\restore_upstream_cache.py')
    assert source.index(block) < source.index('& "$PSScriptRoot\\prepare-ungoogled.ps1"')
    assert "migrate_windows_snapshot.py" not in block
    assert block.index("snapshot_safe false") < block.index("verify_windows_snapshot_source.py")
    assert block.index("if ($LASTEXITCODE -ne 0)") < block.index("snapshot_safe true")
    assert "Remove-Item Env:CHROMIX_WINDOWS_VERIFY_SOURCE_REPO, Env:CHROMIX_WINDOWS_VERIFY_SOURCE_SHA" in block
    assert 'tools\\prepare_restored_build.py' in source
    assert 'tools\\verify_patch_stack.py' in source
    assert 'throw "required upstream cache: restore receipt missing;' in source
    assert 'if ($RestoredUpstream) {\n    python (Join-Path $Repo "tools\\prepare_restored_build.py") --phase finish' in source
    copy = block.index('[IO.File]::Copy(')
    assert block.index('throw "Windows unchanged-source verification failed;') < copy
    assert copy < block.index('Remove-Item Env:CHROMIX_WINDOWS_VERIFY_SOURCE_REPO') < block.index('snapshot_safe true')
    assert '(Join-Path $env:RUNNER_TEMP $name), (Join-Path $verifyDiagnostics $name), $false)' in block
    action_steps = yaml.safe_load(ACTION.read_text())["runs"]["steps"]
    for name, helper in (("windows-snapshot.json", "validate_windows_snapshot.py"),
                         ("windows-snapshot-download.json", "download_windows_snapshot.py")):
        assert '"' + name + '"' in block
        action_run = next(s["run"] for s in action_steps if helper in s.get("run", ""))
        assert '--report "$env:RUNNER_TEMP\\' + name + '"' in action_run


def test_every_stage_uploads_preparation_and_preserved_snapshot_diagnostics():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    preparation = r"C:\c\chromix\upstream-cache-preparation.json"
    for index in range(1, 13):
        steps = workflow["jobs"][f"build-{index}"]["steps"]
        evidence_name = "Upload upstream cache diagnostics" if index == 1 else "Upload restored reuse evidence"
        evidence = next(s for s in steps if s.get("name") == evidence_name)
        assert evidence["if"] == "${{ always() }}"
        assert evidence["uses"] == "actions/upload-artifact@v4"
        assert evidence["with"]["if-no-files-found"] == "ignore"
        paths = evidence["with"]["path"].splitlines()
        assert paths.count(preparation) == 1
        assert r"C:\c\chromix\upstream-reuse\baseline.json" in paths
        assert r"C:\c\chromix\upstream-reuse\result.json" in paths
        fingerprint = next(s for s in steps if s.get("name") == "Upload fingerprint diagnostics")
        assert fingerprint["if"] == "${{ always() }}"
        assert fingerprint["with"]["if-no-files-found"] == "ignore"
        assert fingerprint["with"]["path"] == "C:\\c\\chromix\\fingerprint-diagnostics\\"
        stage = next(s for s in steps if s.get("id") == "stage")
        assert steps.index(stage) < steps.index(evidence)
        assert steps.index(stage) < steps.index(fingerprint)


@pytest.mark.parametrize("mode", ["disabled", "success", "failure", "missing-sha", "missing-repo", "cold", "stage7", "arm64",
                                  "not-artifact", "fast", "migration-repo", "migration-sha", "migration-profile",
                                  "missing-origin", "missing-download", "existing-origin", "existing-download"])
def test_powershell_unchanged_source_guard_and_one_shot_environment(tmp_path, mode):
    script = r'''
$script:events = [Collections.Generic.List[string]]::new()
function Write-OutVar($key, $value) { $script:events.Add("$key=$value") }
function python { $script:events.Add('python:' + ($args -join ' ')); $global:LASTEXITCODE = $(if ($env:TEST_MODE -eq 'failure') { 1 } else { 0 }) }
$WorkDir = Join-Path $env:TEST_ROOT 'work'
$Repo = Join-Path $env:TEST_ROOT 'repo'
$FromArtifact = $env:TEST_MODE -ne 'not-artifact'
$StageIndex = $(if ($env:TEST_MODE -eq 'stage7') { 7 } else { 6 })
$Arch = $(if ($env:TEST_MODE -eq 'arm64') { 'arm64' } else { 'x64' })
$BuildProfile = $(if ($env:TEST_MODE -eq 'fast') { 'fast' } else { 'native' })
$RequireUpstreamCache = $env:TEST_MODE -ne 'cold'
$env:CHROMIX_WINDOWS_VERIFY_SOURCE_REPO = $(if ($env:TEST_MODE -notin @('disabled', 'missing-repo')) { 'previous' } else { '' })
$env:CHROMIX_WINDOWS_VERIFY_SOURCE_SHA = $(if ($env:TEST_MODE -notin @('disabled', 'missing-sha')) { 'e5b29c58b44e381924a2dd4bd60f54d01abb9d9c' } else { '' })
$env:CHROMIX_WINDOWS_MIGRATION_REPO = $(if ($env:TEST_MODE -eq 'migration-repo') { 'previous' } else { '' })
$env:CHROMIX_WINDOWS_MIGRATION_SHA = $(if ($env:TEST_MODE -eq 'migration-sha') { 'sha' } else { '' })
$env:CHROMIX_WINDOWS_MIGRATION_PROFILE = $(if ($env:TEST_MODE -eq 'migration-profile') { 'legacy' } else { '' })
$failed = $false
try {
''' + verify_source_block() + r'''
} catch { $failed = $true }
[pscustomobject]@{failed=$failed; events=@($script:events); cleared=(-not $env:CHROMIX_WINDOWS_VERIFY_SOURCE_REPO -and -not $env:CHROMIX_WINDOWS_VERIFY_SOURCE_SHA)} | ConvertTo-Json -Compress
'''
    reports = {"windows-snapshot.json": b'{"run_id":35387701778,"stage":6,"attempt":1}\r\n',
               "windows-snapshot-download.json": b'{"status":"verified","artifacts":[10593998213,10593823308]}\n'}
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    diagnostics = tmp_path / "work/fingerprint-diagnostics"
    diagnostics.mkdir(parents=True)
    existing_proof = diagnostics / "windows-unchanged-source-existing.json"
    existing_proof.write_bytes(b'{"status":"prior-proof"}\n')
    sources = {}
    destinations = {}
    for name, payload in reports.items():
        kind = "origin" if name == "windows-snapshot.json" else "download"
        if mode != "missing-" + kind:
            path = runner_temp / name
            path.write_bytes(payload)
            sources[name] = (path.read_bytes(), path.stat().st_mtime_ns)
        if mode == "existing-" + kind:
            path = diagnostics / name
            path.write_bytes(b'{"preserve":"existing diagnostic"}\n')
            destinations[name] = (path.read_bytes(), path.stat().st_mtime_ns)
    result = run_powershell(tmp_path, script, {"TEST_MODE": mode, "TEST_ROOT": str(tmp_path),
                                             "RUNNER_TEMP": str(runner_temp)})
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["failed"] is (mode not in ("success", "disabled"))
    for name, before in sources.items():
        path = runner_temp / name
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    for name, before in destinations.items():
        path = diagnostics / name
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert existing_proof.read_bytes() == b'{"status":"prior-proof"}\n'
    for name, payload in reports.items():
        path = diagnostics / name
        copied = mode == "success" or (name == "windows-snapshot.json" and mode in ("missing-download", "existing-download"))
        if copied:
            assert path.read_bytes() == payload
        elif name not in destinations:
            assert not path.exists()
    if mode == "disabled":
        assert state["events"] == []
    else:
        assert state["events"][0] == "snapshot_safe=false"
        assert ("snapshot_safe=true" in state["events"]) is (mode == "success")
        assert state["cleared"] is (mode == "success")
        if mode in ("success", "failure", "missing-origin", "missing-download", "existing-origin", "existing-download"):
            assert "verify_windows_snapshot_source.py" in state["events"][1]
            assert "--arch x64 --build-profile native" in state["events"][1]
        else:
            assert len(state["events"]) == 1


@pytest.mark.parametrize("mutation", ["valid", "reverse", "sha", "old-sha", "run", "stage", "attempt", "missing",
    "extra", "duplicate", "branch", "repository", "profile", "cachefalse", "migration", "mixed-mode"])
def test_stage8_action_guard_pins_exact_tuple(tmp_path, mutation):
    guard = yaml.safe_load(ACTION.read_text())["runs"]["steps"][0]["run"]
    env = dict(SNAPSHOT_MODE="verify-source", SNAPSHOT_SHA=STAGE8_SHA, SNAPSHOT_RUN="35485726877",
               SNAPSHOT_STAGE="8", SNAPSHOT_ATTEMPT="1", SNAPSHOT_ARTIFACT_IDS="10608631230,10608606403",
               SNAPSHOT_BRANCH="fix/win153-midl-stat-20260920", SNAPSHOT_REPOSITORY="xiaozhou26/Chromix",
               CHROMIX_BUILD_PROFILE="native", CHROMIX_USE_UPSTREAM_CACHE="1",
               CHROMIX_WINDOWS_MIGRATION_REPO="", CHROMIX_WINDOWS_MIGRATION_SHA="", CHROMIX_WINDOWS_MIGRATION_PROFILE="")
    env.update({"valid": {}, "reverse": {"SNAPSHOT_ARTIFACT_IDS": "10608606403, 10608631230"},
                "sha": {"SNAPSHOT_SHA": "a" * 40}, "old-sha": {"SNAPSHOT_SHA": WINDOWS153_SHA},
                "run": {"SNAPSHOT_RUN": "35387701778"}, "stage": {"SNAPSHOT_STAGE": "6"},
                "attempt": {"SNAPSHOT_ATTEMPT": "2"}, "missing": {"SNAPSHOT_ARTIFACT_IDS": "10608631230"},
                "extra": {"SNAPSHOT_ARTIFACT_IDS": "10608631230,10608606403,1"},
                "duplicate": {"SNAPSHOT_ARTIFACT_IDS": "10608631230,10608631230"},
                "branch": {"SNAPSHOT_BRANCH": "main"}, "repository": {"SNAPSHOT_REPOSITORY": "other/Chromix"},
                "profile": {"CHROMIX_BUILD_PROFILE": "fast"}, "cachefalse": {"CHROMIX_USE_UPSTREAM_CACHE": "0"},
                "migration": {"SNAPSHOT_MODE": "migration"},
                "mixed-mode": {"CHROMIX_WINDOWS_MIGRATION_PROFILE": "windows-152-x64-stage9"}}[mutation])
    result = run_powershell(tmp_path, "try {\n" + guard + "\n} catch { exit 1 }\nexit 0\n", env)
    assert result.returncode == (0 if mutation in ("valid", "reverse") else 1), result.stderr


def stage8_metadata():
    from tools import validate_windows_snapshot as validator
    from tools.tests.test_validate_windows_snapshot import Client
    from tools import verify_windows_snapshot_source as verifier

    client = Client(run_id=35485726877, stage=8, attempt=1, sha=STAGE8_SHA)
    client.run.update(head_branch="fix/win153-midl-stat-20260920", event="workflow_dispatch", workflow_id=339967931)
    for key in ("repository", "head_repository"):
        client.run[key] = {"id": 1342691290, "full_name": "xiaozhou26/Chromix"}
    client.jobs[0]["id"] = 106081168976
    client.jobs[0]["steps"].insert(0, {"name": "Run stage 8", "conclusion": "failure"})
    for artifact, expected in zip(client.artifacts, verifier.STAGE8_ARTIFACTS):
        artifact.update(expected)
    options = dict(repository="xiaozhou26/Chromix", run_id=35485726877, stage=8, attempt=1,
                   expected_sha=STAGE8_SHA, expected_artifact_ids=[10608631230, 10608606403],
                   recovery_branch="fix/win153-midl-stat-20260920")
    return client, options, validator


@pytest.mark.parametrize("mutation", ["valid", "job", "branch", "main", "event", "workflow-id", "repository-id",
    "conclusion", "job-conclusion", "run-step", "digest", "size", "nonterminal", "part", "expired", "upload", "sha"])
def test_stage8_metadata_binding_is_exact_before_checkout_and_download(tmp_path, monkeypatch, mutation):
    client, options, validator = stage8_metadata()
    steps = yaml.safe_load(ACTION.read_text())["runs"]["steps"]
    binding = next(s for s in steps if s.get("name") == "Bind exact stage8 checkpoint metadata")
    assert steps.index(binding) < next(i for i, s in enumerate(steps) if s.get("uses") == "actions/checkout@v4")
    assert steps.index(binding) < next(i for i, s in enumerate(steps) if s.get("name") == "Download digest-pinned Windows snapshot")
    if mutation == "job":
        client.jobs[0]["id"] += 1
    elif mutation in ("branch", "main", "event", "workflow-id", "conclusion", "nonterminal", "sha"):
        key, value = {"branch": ("head_branch", "other"), "main": ("head_branch", "main"),
                      "event": ("event", "push"), "workflow-id": ("workflow_id", 1),
                      "conclusion": ("conclusion", "success"), "nonterminal": ("status", "in_progress"),
                      "sha": ("head_sha", "a" * 40)}[mutation]
        client.run[key] = value
    elif mutation == "repository-id":
        client.run["repository"]["id"] += 1
    elif mutation == "job-conclusion":
        client.jobs[0]["conclusion"] = "success"
    elif mutation == "run-step":
        client.jobs[0]["steps"][0]["conclusion"] = "success"
    elif mutation == "upload":
        client.jobs[0]["steps"][-1]["conclusion"] = "failure"
    else:
        if mutation != "valid":
            key, value = {"digest": ("digest", "sha256:" + "b" * 64), "size": ("size_in_bytes", 1),
                          "part": ("name", "tree-s8-attempt-1-part3"), "expired": ("expired", True)}[mutation]
            client.artifacts[0][key] = value
    monkeypatch.setenv("GITHUB_WORKSPACE", str(ROOT))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GH_TOKEN", "offline-fixture")
    import validate_windows_snapshot as action_validator
    monkeypatch.setattr(action_validator, "Client", lambda *args: client)

    def exercise():
        manifest = validator.validate(client, **options)
        (tmp_path / "windows-snapshot.json").write_text(json.dumps(manifest))
        exec(compile(binding["run"], str(ACTION), "exec"), {})

    if mutation == "valid":
        exercise()
    else:
        with pytest.raises((ValueError, RuntimeError)):
            exercise()


@pytest.mark.parametrize("mode", ["success", "failure", "stage6", "old-sha", "wrong-sha", "cachefalse", "fast", "arm64",
    "not-artifact", "validate-only", "migration", "missing-run", "wrong-job", "bad-sha", "existing-hop",
    "missing-origin", "missing-download", "old-origin", "old-download", "old-source", "binding-failure"])
def test_stage8_current_hop_preserves_old_reports_and_fails_closed(tmp_path, mode):
    from tools import verify_windows_snapshot_source as verifier

    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    diagnostics = tmp_path / "work/fingerprint-diagnostics"
    diagnostics.mkdir(parents=True)
    old = {"windows-snapshot.json": b'{"run_id":35387701778}\r\n',
           "windows-snapshot-download.json": b'{"run_id":35387701778}\n',
           "windows-unchanged-source-existing.json": b'{"previous_sha":"historical"}\n',
           "runtime-failed/acceptance.json": b'{"ci_gate_passed":false}\n'}
    for name, payload in old.items():
        path = diagnostics / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    before = {name: (payload, (diagnostics / name).stat().st_mtime_ns) for name, payload in old.items()}
    client, options, validator = stage8_metadata()
    origin = validator.validate(client, **options)
    download = {**origin, "status": "success", "phase": "complete", "publication": "published"}
    for kind, data in (("origin", origin), ("download", download)):
        if mode != "missing-" + kind:
            name = "windows-snapshot.json" if kind == "origin" else "windows-snapshot-download.json"
            (runner_temp / name).write_text(json.dumps({"run_id": 35387701778} if mode == "old-" + kind else data))
    relative = "recovery-hops/d35485726877-a1-s8-j106081168976/c90001-a2-build-8-" + "a" * 40
    hop = diagnostics / relative
    if mode == "existing-hop":
        hop.mkdir(parents=True)
        (hop / "recovery-hop.json").write_text('{"status":"verified","historical":true}')
    script = r'''
$script:events = [Collections.Generic.List[string]]::new()
function Write-OutVar($key, $value) { $script:events.Add("$key=$value") }
function python {
  $script:events.Add('python:' + ($args -join ' '))
  if ($args -contains '--publish-hop') {
    if ($env:TEST_MODE -eq 'binding-failure') { $global:LASTEXITCODE = 1; return }
    & $env:TEST_PYTHON @args
    $global:LASTEXITCODE = $LASTEXITCODE
  } else {
    $report = $args[[array]::IndexOf($args, '--report') + 1]
    $proof = @{
      status='verified'; operation='windows-unchanged-source'; profile='windows-153-x64-stage8'; changed_files=@()
      previous_sha='2a55082adb89cb8bac7aa7ab8bb61162b53f4c35'; target_sha=$env:GITHUB_SHA
      run=@{GITHUB_RUN_ID=$env:GITHUB_RUN_ID; GITHUB_RUN_ATTEMPT=$env:GITHUB_RUN_ATTEMPT; GITHUB_JOB=$env:GITHUB_JOB; GITHUB_SHA=$env:GITHUB_SHA}
    }
    if ($env:TEST_MODE -eq 'old-source') { $proof.previous_sha = 'e5b29c58b44e381924a2dd4bd60f54d01abb9d9c' }
    $proof | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $report
    $global:LASTEXITCODE = $(if ($env:TEST_MODE -eq 'failure') { 1 } else { 0 })
  }
}
$WorkDir = Join-Path $env:TEST_ROOT 'work'
$Repo = $env:TEST_REPO
$FromArtifact = $env:TEST_MODE -ne 'not-artifact'
$StageIndex = $(if ($env:TEST_MODE -eq 'stage6') { 6 } else { 8 })
$Arch = $(if ($env:TEST_MODE -eq 'arm64') { 'arm64' } else { 'x64' })
$BuildProfile = $(if ($env:TEST_MODE -eq 'fast') { 'fast' } else { 'native' })
$ValidateOnly = $env:TEST_MODE -eq 'validate-only'
$RequireUpstreamCache = $env:TEST_MODE -ne 'cachefalse'
$failed = $false
try {
''' + verify_source_block() + r'''
} catch { $failed = $true }
[pscustomobject]@{failed=$failed; events=@($script:events); cleared=(-not $env:CHROMIX_WINDOWS_VERIFY_SOURCE_REPO -and -not $env:CHROMIX_WINDOWS_VERIFY_SOURCE_SHA)} | ConvertTo-Json -Compress
'''
    env = dict(TEST_ROOT=str(tmp_path), TEST_REPO=str(ROOT), TEST_MODE=mode, TEST_PYTHON=sys.executable,
               RUNNER_TEMP=str(runner_temp), GITHUB_RUN_ID="90001", GITHUB_RUN_ATTEMPT="2", GITHUB_JOB="build-8",
               GITHUB_SHA="a" * 40, CHROMIX_WINDOWS_VERIFY_SOURCE_REPO="previous", CHROMIX_WINDOWS_VERIFY_SOURCE_SHA=STAGE8_SHA,
               CHROMIX_WINDOWS_MIGRATION_REPO="", CHROMIX_WINDOWS_MIGRATION_SHA="", CHROMIX_WINDOWS_MIGRATION_PROFILE="")
    env.update({"old-sha": {"CHROMIX_WINDOWS_VERIFY_SOURCE_SHA": WINDOWS153_SHA},
                "wrong-sha": {"CHROMIX_WINDOWS_VERIFY_SOURCE_SHA": "b" * 40},
                "missing-run": {"GITHUB_RUN_ID": ""}, "wrong-job": {"GITHUB_JOB": "build-6"},
                "bad-sha": {"GITHUB_SHA": "../old"}, "migration": {"CHROMIX_WINDOWS_MIGRATION_PROFILE": "legacy"}}.get(mode, {}))
    result = run_powershell(tmp_path, script, env)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout.strip().splitlines()[-1])
    assert state["failed"] is (mode != "success"), result.stdout + result.stderr
    assert state["events"][0] == "snapshot_safe=false"
    assert ("snapshot_safe=true" in state["events"]) is (mode == "success")
    assert state["cleared"] is (mode == "success")
    for name, expected in before.items():
        path = diagnostics / name
        assert (path.read_bytes(), path.stat().st_mtime_ns) == expected
    if mode == "success":
        assert "recovery_report_dir=" + relative in state["events"]
        proof = json.loads((hop / "recovery-hop.json").read_bytes())
        assert proof["donor"] == verifier.STAGE8_DONOR
        assert proof["consumer"] == {key: env[key] for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB", "GITHUB_SHA")}
        assert proof["runtime_acceptance"] == "runtime/acceptance.json"
        assert proof["source_verification"] == "source-verification.json"
        for name in ("windows-snapshot.json", "windows-snapshot-download.json"):
            assert (hop / name).read_bytes() == (runner_temp / name).read_bytes()
    elif mode == "existing-hop":
        assert (hop / "recovery-hop.json").read_text() == '{"status":"verified","historical":true}'
    else:
        assert not (hop / "recovery-hop.json").exists()


@pytest.mark.parametrize("mode", ["current", "old-passed", "current-conflict", "runtime-failed", "ordinary"])
def test_runtime_acceptance_uses_only_current_hop_and_preserves_historical_evidence(tmp_path, mode):
    source = STAGE.read_text()
    start = source.index("function Invoke-FingerprintAcceptance(")
    function = source[start:source.index("\nfunction Verify-FinalBundle", start)]
    diagnostics = tmp_path / "work/fingerprint-diagnostics"
    historical = diagnostics / "runtime-old/acceptance.json"
    historical.parent.mkdir(parents=True)
    historical.write_text(json.dumps({"ci_gate_passed": mode == "old-passed"}))
    before = (historical.read_bytes(), historical.stat().st_mtime_ns)
    hop = diagnostics / ("recovery-hops/d35485726877-a1-s8-j106081168976/c90001-a2-build-8-" + "a" * 40)
    hop.mkdir(parents=True)
    if mode == "current-conflict":
        (hop / "runtime").mkdir()
        (hop / "runtime/acceptance.json").write_text('{"ci_gate_passed":true}')
    browser = tmp_path / "chrome.exe"
    browser.write_bytes(b"fixture browser")
    script = function + r'''
$script:events = [Collections.Generic.List[string]]::new()
function Get-Command { [pscustomobject]@{Source='fixture-python'} }
function Invoke-Tracked {
  param($File, $Cwd, $TimeoutSec, $ArgList, [switch]$FullFailureOutput)
  $script:events.Add($ArgList)
  if ($ArgList.Contains('fingerprint_acceptance.py') -and $env:TEST_MODE -eq 'runtime-failed') { return 1 }
  return 0
}
$WorkDir = Join-Path $env:TEST_ROOT 'work'
$Repo = $env:TEST_REPO
$Src = Join-Path $WorkDir 'src'
$RecoveryDiagnostics = $(if ($env:TEST_MODE -eq 'ordinary') { $null } else { $env:TEST_HOP })
$FingerprintSourceReport = Join-Path $env:TEST_HOP 'source-verification.json'
$Revisions = @{ChromiumVersion='153.0.8010.47'}
$failed = $false
try { Invoke-FingerprintAcceptance -Browser (Join-Path $env:TEST_ROOT 'chrome.exe') } catch { $failed = $true }
[pscustomobject]@{failed=$failed; events=@($script:events)} | ConvertTo-Json -Compress
'''
    result = run_powershell(tmp_path, script, {"TEST_ROOT": str(tmp_path), "TEST_HOP": str(hop), "TEST_REPO": str(ROOT), "TEST_MODE": mode})
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["failed"] is (mode in ("current-conflict", "runtime-failed"))
    assert (historical.read_bytes(), historical.stat().st_mtime_ns) == before
    if mode == "current-conflict":
        assert len(state["events"]) == 1
        assert (hop / "runtime/acceptance.json").read_text() == '{"ci_gate_passed":true}'
    else:
        assert len(state["events"]) == 2
        command = state["events"][1]
        assert '--source-report "' + str(hop / "source-verification.json") + '"' in command
        if mode == "ordinary":
            assert str(hop / "runtime") not in command
            assert "fingerprint-diagnostics/runtime-" in command.replace("\\", "/")
        else:
            assert '--output-dir "' + str(hop / "runtime") + '"' in command


def test_current_post_prepare_source_proof_is_not_selected_from_historical_reports():
    source = STAGE.read_text()
    start = source.index('  $FingerprintSourceReport = if ($RecoveryDiagnostics)')
    end = source.index('  & $gn gen', start)
    block = source[start:end]
    assert "Join-Path $RecoveryDiagnostics 'source-verification.json'" in block
    assert "Current source verification report already exists" in block
    assert "--output $FingerprintSourceReport" in block
    assert "Get-ChildItem" not in block
    assert source.index('tools\\prepare_restored_build.py') < start
    assert start < source.index('$rc = Invoke-Tracked -File $Ninja')


@pytest.mark.parametrize("mutation", ["valid", "reverse", "sha", "old-sha", "run", "stage", "attempt", "missing",
    "extra", "duplicate", "branch", "repository", "profile", "cachefalse", "migration", "mixed-mode"])
def test_windows155_action_guard_pins_exact_tuple(tmp_path, mutation):
    guard = yaml.safe_load(ACTION.read_text())["runs"]["steps"][0]["run"]
    env = dict(SNAPSHOT_MODE="verify-source", SNAPSHOT_SHA=WINDOWS155_SHA, SNAPSHOT_RUN="38136799729",
               SNAPSHOT_STAGE="2", SNAPSHOT_ATTEMPT="1", SNAPSHOT_ARTIFACT_IDS="11712073707,11712278108",
               SNAPSHOT_BRANCH="build/chromium155-donors-20261011", SNAPSHOT_REPOSITORY="xiaozhou26/Chromix",
               CHROMIX_BUILD_PROFILE="native", CHROMIX_USE_UPSTREAM_CACHE="1",
               CHROMIX_WINDOWS_MIGRATION_REPO="", CHROMIX_WINDOWS_MIGRATION_SHA="", CHROMIX_WINDOWS_MIGRATION_PROFILE="")
    env.update({"valid": {}, "reverse": {"SNAPSHOT_ARTIFACT_IDS": "11712278108, 11712073707"},
                "sha": {"SNAPSHOT_SHA": "a" * 40}, "old-sha": {"SNAPSHOT_SHA": WINDOWS153_SHA},
                "run": {"SNAPSHOT_RUN": "35387701778"}, "stage": {"SNAPSHOT_STAGE": "6"},
                "attempt": {"SNAPSHOT_ATTEMPT": "2"}, "missing": {"SNAPSHOT_ARTIFACT_IDS": "11712073707"},
                "extra": {"SNAPSHOT_ARTIFACT_IDS": "11712073707,11712278108,1"},
                "duplicate": {"SNAPSHOT_ARTIFACT_IDS": "11712073707,11712073707"},
                "branch": {"SNAPSHOT_BRANCH": "main"}, "repository": {"SNAPSHOT_REPOSITORY": "other/Chromix"},
                "profile": {"CHROMIX_BUILD_PROFILE": "fast"}, "cachefalse": {"CHROMIX_USE_UPSTREAM_CACHE": "0"},
                "migration": {"SNAPSHOT_MODE": "migration"},
                "mixed-mode": {"CHROMIX_WINDOWS_MIGRATION_PROFILE": "windows-152-x64-stage9"}}[mutation])
    result = run_powershell(tmp_path, "try {\n" + guard + "\n} catch { exit 1 }\nexit 0\n", env)
    assert result.returncode == (0 if mutation in ("valid", "reverse") else 1), result.stderr


def windows155_metadata():
    from tools import validate_windows_snapshot as validator
    from tools.tests.test_validate_windows_snapshot import Client
    from tools import verify_windows_snapshot_source as verifier

    client = Client(run_id=38136799729, stage=2, attempt=1, sha=WINDOWS155_SHA)
    client.run.update(head_branch="build/chromium155-donors-20261011", event="workflow_dispatch", workflow_id=339967931)
    for key in ("repository", "head_repository"):
        client.run[key] = {"id": 1342691290, "full_name": "xiaozhou26/Chromix"}
    client.jobs[0]["id"] = 114518496535
    client.jobs[0]["steps"].insert(0, {"name": "Run stage 2", "conclusion": "failure"})
    for artifact, expected in zip(client.artifacts, verifier.WINDOWS155_ARTIFACTS):
        artifact.update(expected)
    options = dict(repository="xiaozhou26/Chromix", run_id=38136799729, stage=2, attempt=1,
                   expected_sha=WINDOWS155_SHA, expected_artifact_ids=[11712073707, 11712278108],
                   recovery_branch="build/chromium155-donors-20261011")
    return client, options, validator


@pytest.mark.parametrize("mutation", ["valid", "job", "branch", "main", "event", "workflow-id", "repository-id",
    "conclusion", "job-conclusion", "run-step", "digest", "size", "nonterminal", "part", "expired", "upload", "sha"])
def test_windows155_metadata_binding_is_exact_before_checkout_and_download(tmp_path, monkeypatch, mutation):
    client, options, validator = windows155_metadata()
    steps = yaml.safe_load(ACTION.read_text())["runs"]["steps"]
    binding = next(s for s in steps if s.get("name") == "Bind exact Windows155 stage2 checkpoint metadata")
    assert steps.index(binding) < next(i for i, s in enumerate(steps) if s.get("uses") == "actions/checkout@v4")
    assert steps.index(binding) < next(i for i, s in enumerate(steps) if s.get("name") == "Download digest-pinned Windows snapshot")
    if mutation == "job":
        client.jobs[0]["id"] += 1
    elif mutation in ("branch", "main", "event", "workflow-id", "conclusion", "nonterminal", "sha"):
        key, value = {"branch": ("head_branch", "other"), "main": ("head_branch", "main"),
                      "event": ("event", "push"), "workflow-id": ("workflow_id", 1),
                      "conclusion": ("conclusion", "success"), "nonterminal": ("status", "in_progress"),
                      "sha": ("head_sha", "a" * 40)}[mutation]
        client.run[key] = value
    elif mutation == "repository-id":
        client.run["repository"]["id"] += 1
    elif mutation == "job-conclusion":
        client.jobs[0]["conclusion"] = "success"
    elif mutation == "run-step":
        client.jobs[0]["steps"][0]["conclusion"] = "success"
    elif mutation == "upload":
        client.jobs[0]["steps"][-1]["conclusion"] = "failure"
    else:
        if mutation != "valid":
            key, value = {"digest": ("digest", "sha256:" + "b" * 64), "size": ("size_in_bytes", 1),
                          "part": ("name", "tree-s2-attempt-1-part3"), "expired": ("expired", True)}[mutation]
            client.artifacts[0][key] = value
    monkeypatch.setenv("GITHUB_WORKSPACE", str(ROOT))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GH_TOKEN", "offline-fixture")
    import validate_windows_snapshot as action_validator
    monkeypatch.setattr(action_validator, "Client", lambda *args: client)

    def exercise():
        manifest = validator.validate(client, **options)
        (tmp_path / "windows-snapshot.json").write_text(json.dumps(manifest))
        exec(compile(binding["run"], str(ACTION), "exec"), {})

    if mutation == "valid":
        exercise()
    else:
        with pytest.raises((ValueError, RuntimeError)):
            exercise()


@pytest.mark.parametrize("mode", ["success", "failure", "stage6", "old-sha", "wrong-sha", "cachefalse", "fast", "arm64",
    "not-artifact", "validate-only", "migration", "missing-run", "wrong-job", "bad-sha", "existing-hop",
    "missing-origin", "missing-download", "old-origin", "old-download", "old-source", "binding-failure"])
def test_windows155_current_hop_preserves_old_reports_and_fails_closed(tmp_path, mode):
    from tools import verify_windows_snapshot_source as verifier

    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    diagnostics = tmp_path / "work/fingerprint-diagnostics"
    diagnostics.mkdir(parents=True)
    old = {"windows-snapshot.json": b'{"run_id":35387701778}\r\n',
           "windows-snapshot-download.json": b'{"run_id":35387701778}\n',
           "windows-unchanged-source-existing.json": b'{"previous_sha":"historical"}\n',
           "runtime-failed/acceptance.json": b'{"ci_gate_passed":false}\n'}
    for name, payload in old.items():
        path = diagnostics / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    before = {name: (payload, (diagnostics / name).stat().st_mtime_ns) for name, payload in old.items()}
    client, options, validator = windows155_metadata()
    origin = validator.validate(client, **options)
    download = {**origin, "status": "success", "phase": "complete", "publication": "published"}
    for kind, data in (("origin", origin), ("download", download)):
        if mode != "missing-" + kind:
            name = "windows-snapshot.json" if kind == "origin" else "windows-snapshot-download.json"
            (runner_temp / name).write_text(json.dumps({"run_id": 35387701778} if mode == "old-" + kind else data))
    relative = "recovery-hops/d38136799729-a1-s2-j114518496535/c90001-a2-build-2-" + "a" * 40
    hop = diagnostics / relative
    if mode == "existing-hop":
        hop.mkdir(parents=True)
        (hop / "recovery-hop.json").write_text('{"status":"verified","historical":true}')
    script = r'''
$script:events = [Collections.Generic.List[string]]::new()
function Write-OutVar($key, $value) { $script:events.Add("$key=$value") }
function python {
  $script:events.Add('python:' + ($args -join ' '))
  if ($args -contains '--publish-hop') {
    if ($env:TEST_MODE -eq 'binding-failure') { $global:LASTEXITCODE = 1; return }
    & $env:TEST_PYTHON @args
    $global:LASTEXITCODE = $LASTEXITCODE
  } else {
    $report = $args[[array]::IndexOf($args, '--report') + 1]
    $proof = @{
      status='verified'; operation='windows-unchanged-source'; profile='windows-155-x64-stage2'; changed_files=@()
      previous_sha='4470c3c7435a9f2de203396587c6f57705a8c9d5'; target_sha=$env:GITHUB_SHA
      run=@{GITHUB_RUN_ID=$env:GITHUB_RUN_ID; GITHUB_RUN_ATTEMPT=$env:GITHUB_RUN_ATTEMPT; GITHUB_JOB=$env:GITHUB_JOB; GITHUB_SHA=$env:GITHUB_SHA}
    }
    if ($env:TEST_MODE -eq 'old-source') { $proof.previous_sha = 'e5b29c58b44e381924a2dd4bd60f54d01abb9d9c' }
    $proof | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $report
    $global:LASTEXITCODE = $(if ($env:TEST_MODE -eq 'failure') { 1 } else { 0 })
  }
}
$WorkDir = Join-Path $env:TEST_ROOT 'work'
$Repo = $env:TEST_REPO
$FromArtifact = $env:TEST_MODE -ne 'not-artifact'
$StageIndex = $(if ($env:TEST_MODE -eq 'stage6') { 6 } else { 2 })
$Arch = $(if ($env:TEST_MODE -eq 'arm64') { 'arm64' } else { 'x64' })
$BuildProfile = $(if ($env:TEST_MODE -eq 'fast') { 'fast' } else { 'native' })
$ValidateOnly = $env:TEST_MODE -eq 'validate-only'
$RequireUpstreamCache = $env:TEST_MODE -ne 'cachefalse'
$failed = $false
try {
''' + verify_source_block() + r'''
} catch { $failed = $true }
[pscustomobject]@{failed=$failed; events=@($script:events); cleared=(-not $env:CHROMIX_WINDOWS_VERIFY_SOURCE_REPO -and -not $env:CHROMIX_WINDOWS_VERIFY_SOURCE_SHA)} | ConvertTo-Json -Compress
'''
    env = dict(TEST_ROOT=str(tmp_path), TEST_REPO=str(ROOT), TEST_MODE=mode, TEST_PYTHON=sys.executable,
               RUNNER_TEMP=str(runner_temp), GITHUB_RUN_ID="90001", GITHUB_RUN_ATTEMPT="2", GITHUB_JOB="build-2",
               GITHUB_SHA="a" * 40, CHROMIX_WINDOWS_VERIFY_SOURCE_REPO="previous", CHROMIX_WINDOWS_VERIFY_SOURCE_SHA=WINDOWS155_SHA,
               CHROMIX_WINDOWS_MIGRATION_REPO="", CHROMIX_WINDOWS_MIGRATION_SHA="", CHROMIX_WINDOWS_MIGRATION_PROFILE="")
    env.update({"old-sha": {"CHROMIX_WINDOWS_VERIFY_SOURCE_SHA": WINDOWS153_SHA},
                "wrong-sha": {"CHROMIX_WINDOWS_VERIFY_SOURCE_SHA": "b" * 40},
                "missing-run": {"GITHUB_RUN_ID": ""}, "wrong-job": {"GITHUB_JOB": "build-6"},
                "bad-sha": {"GITHUB_SHA": "../old"}, "migration": {"CHROMIX_WINDOWS_MIGRATION_PROFILE": "legacy"}}.get(mode, {}))
    result = run_powershell(tmp_path, script, env)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout.strip().splitlines()[-1])
    assert state["failed"] is (mode != "success"), result.stdout + result.stderr
    assert state["events"][0] == "snapshot_safe=false"
    assert ("snapshot_safe=true" in state["events"]) is (mode == "success")
    assert state["cleared"] is (mode == "success")
    for name, expected in before.items():
        path = diagnostics / name
        assert (path.read_bytes(), path.stat().st_mtime_ns) == expected
    if mode == "success":
        assert "recovery_report_dir=" + relative in state["events"]
        proof = json.loads((hop / "recovery-hop.json").read_bytes())
        assert proof["donor"] == verifier.WINDOWS155_DONOR
        assert proof["consumer"] == {key: env[key] for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB", "GITHUB_SHA")}
        assert proof["runtime_acceptance"] == "runtime/acceptance.json"
        assert proof["source_verification"] == "source-verification.json"
        for name in ("windows-snapshot.json", "windows-snapshot-download.json"):
            assert (hop / name).read_bytes() == (runner_temp / name).read_bytes()
    elif mode == "existing-hop":
        assert (hop / "recovery-hop.json").read_text() == '{"status":"verified","historical":true}'
    else:
        assert not (hop / "recovery-hop.json").exists()



def test_windows155_workflow_routes_only_selected_build2_and_keeps_following_checks():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    for index in range(1, 13):
        steps = workflow["jobs"][f"build-{index}"]["steps"]
        guard = next(step for step in steps if step.get("name") == "Check explicit snapshot migration inputs")
        restore = next(step for step in steps if step.get("uses") == "./.github/actions/restore-windows-snapshot")
        for value in (WINDOWS155_SHA, "155.0.8059.39", "38136799729", "37831345909",
                      "11712073707,11712278108", "$env:RESUME_STAGE -cne '2'", "$env:RESUME_TREE_STAGE -cne '2'"):
            assert value in guard["run"]
        assert restore["with"]["mode"] == VERIFY_MODE
        assert "build/chromium155-donors-20261011" in restore["with"]["recovery-branch"]
        assert "github.job == format('build-{0}', inputs.resume_stage)" in restore["if"]
        if index > 1:
            normal = next(step for step in steps if step.get("name") == "Download tree from previous stage")
            assert normal["if"] == "${{ inputs.resume_run_id == '' || inputs.resume_stage != '" + str(index) + "' }}"
    source = STAGE.read_text()
    assert source.index("verify_windows_snapshot_source.py") < source.index("tools\\install_windows_metagen_bindings.py")
    assert 'Remove-Item Env:CHROMIX_WINDOWS_VERIFY_SOURCE_REPO, Env:CHROMIX_WINDOWS_VERIFY_SOURCE_SHA' in source
    assert 'throw "required upstream cache: restore receipt missing;' in source
    assert 'tools\\verify_patch_stack.py' in source


@pytest.mark.parametrize("mutation", ["valid", "run", "attempt", "consumer-stage", "tree-stage", "version",
    "artifact", "upstream", "cold", "fast"])
def test_windows155_workflow_input_guard_executes(tmp_path, mutation):
    build = tmp_path / "build/windows"
    build.mkdir(parents=True)
    (build / "read-platform-pins.ps1").write_text("param($Repo)\n@{ ChromiumVersion = $env:TEST_VERSION }\n")
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["build-2"]["steps"]
    guard = next(step for step in steps if step.get("name") == "Check explicit snapshot migration inputs")["run"]
    env = dict(GITHUB_WORKSPACE=str(tmp_path), TEST_VERSION="155.0.8059.39", RESUME_SOURCE_SHA=WINDOWS155_SHA,
               RESUME_RUN_ID="38136799729", RESUME_STAGE="2", RESUME_TREE_STAGE="2", RESUME_ATTEMPT="1",
               RESUME_ARTIFACT_IDS="11712073707,11712278108", CHROMIX_BUILD_PROFILE="native",
               USE_UPSTREAM_CACHE="true", UPSTREAM_RUN_ID="37831345909")
    env.update({"valid": {}, "run": {"RESUME_RUN_ID": "1"}, "attempt": {"RESUME_ATTEMPT": "2"},
                "consumer-stage": {"RESUME_STAGE": "3"}, "tree-stage": {"RESUME_TREE_STAGE": "1"},
                "version": {"TEST_VERSION": "153.0.8010.47"}, "artifact": {"RESUME_ARTIFACT_IDS": "11712073707"},
                "upstream": {"UPSTREAM_RUN_ID": "35059013905"}, "cold": {"USE_UPSTREAM_CACHE": "false"},
                "fast": {"CHROMIX_BUILD_PROFILE": "fast"}}[mutation])
    result = run_powershell(tmp_path, "try {\n" + guard + "\n} catch { exit 1 }\nexit 0\n", env)
    assert result.returncode == (0 if mutation == "valid" else 1), result.stderr


def test_snapshot_gate_waits_for_restore_and_archive_publication():
    source = STAGE.read_text()
    start = source.index('Write-OutVar finished false\nWrite-OutVar upload_parts false')
    restore = source.index('throw "7z restore failed"', start)
    assert 'snapshot_safe true' not in source[start:restore]
    enable = source.index('if ($FromArtifact) { Write-OutVar snapshot_safe true }', restore)
    assert source.index('required upstream cache: restore receipt missing;', restore) < enable
    save = source[source.index('function Save-Handoff {'):source.index('function Assert-CiScripts {')]
    assert save.index('Write-OutVar snapshot_safe false') < save.index('ci-parts.ps1')
    assert save.index('ci-parts.ps1') < save.index('Write-OutVar upload_parts true')
    assert save.index('ci-parts.ps1') < save.index('Write-OutVar snapshot_safe true')


@pytest.mark.parametrize('mode', ['unrestored', 'no-marker', 'pack-failure', 'move-failure', 'no-volumes', 'success'])
def test_snapshot_packer_removes_stale_and_partial_uploads(tmp_path, mode):
    work = tmp_path / 'root/chromix'
    work.mkdir(parents=True)
    if mode not in ('unrestored', 'no-marker'):
        (work / 'src').mkdir()
        with (work / 'src/input').open('wb') as stream:
            stream.truncate(1024 ** 3)
    parts = tmp_path / 'parts'
    (parts / 'p1').mkdir(parents=True)
    (parts / 'p1/old-stage6.7z.001').write_bytes(b'donor')
    script = r'''
function Get-Command { [pscustomobject]@{Source='Invoke-Fake7zip'} }
function Get-PSDrive { [pscustomobject]@{Free=100GB} }
function Invoke-Fake7zip {
  if ($env:TEST_MODE -ne 'no-volumes') {
    [IO.File]::WriteAllText((Join-Path $env:TEST_ROOT 'parts/stage/tree.7z.001'), 'new-volume')
    [IO.File]::WriteAllText((Join-Path $env:TEST_ROOT 'parts/stage/tree.7z.002'), 'new-volume-2')
  }
  $global:LASTEXITCODE = $(if ($env:TEST_MODE -eq 'pack-failure') { 2 } else { 0 })
}
function Move-Item {
  param($Path, $Destination)
  if ($env:TEST_MODE -eq 'move-failure' -and $Path.EndsWith('.002')) { throw 'move failed' }
  Microsoft.PowerShell.Management\Move-Item $Path $Destination
}
$failed = $false
try {
  & $env:TEST_HELPER -Root (Join-Path $env:TEST_ROOT 'root') -PartsDir (Join-Path $env:TEST_ROOT 'parts') -Arch $(if ($env:TEST_MODE -eq 'no-marker') { 'arm64' } else { 'x64' })
} catch { $failed = $true }
Write-Output "FAILED=$failed"
'''
    result = run_powershell(tmp_path, script, dict(TEST_ROOT=str(tmp_path), TEST_MODE=mode,
                            TEST_HELPER=str(ROOT / 'build/windows/ci-parts.ps1')))
    assert result.returncode == 0, result.stderr
    assert ('FAILED=False' if mode == 'success' else 'FAILED=True') in result.stdout
    if mode == 'success':
        assert sorted(p.name for p in parts.rglob('*') if p.is_file()) == ['tree.7z.001', 'tree.7z.002']
    else:
        assert not parts.exists()
