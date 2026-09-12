"""Tests use tiny source trees and a trusted GNU patch, never donor programs."""
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
import apply_restored_patches as arp  # noqa: E402

PATCH_BIN = shutil.which("gpatch") or shutil.which("patch")
pytestmark = pytest.mark.skipif(PATCH_BIN is None, reason="GNU patch is required")
REGEX = rb"example\.com#blocked.test" + b"\n"
ORIGINAL = b"context example.com\nold example.com\ntail\n"
RESTORED = b"context blocked.test\nold blocked.test\ntail\n"
EXPECTED = b"context blocked.test\nnew blocked.test\ntail\n"


def patch(path="listed.txt", old="old example.com", new="new example.com", *, encoding="utf-8"):
    return (f"diff --git a/{path} b/{path}\n"
            "index 1111111..2222222 100644\n"
            f"--- a/{path}\n+++ b/{path}\n"
            "@@ -1,3 +1,3 @@ example.com metadata\n"
            f" context example.com\n-{old}\n+{new}\n tail\n").encode(encoding)


def new_patch(path="sub/new.txt", content="new example.com", mode="100644"):
    return (f"diff --git a/{path} b/{path}\nnew file mode {mode}\n"
            f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+{content}\n").encode()


def put(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class Fixture:
    def __init__(self, root):
        self.repo, self.src, self.core, self.tooling = (root / name for name in
                                                     ("repo", "src", "core", "tooling"))
        for directory in (self.repo, self.src, self.core, self.tooling):
            directory.mkdir(parents=True)
        put(self.core, "domain_regex.list", REGEX)
        put(self.core, "domain_substitution.list", b"listed.txt\n")
        put(self.tooling, "domain_substitution.list", b"windows.txt\n")
        self.set_patches(patch())
        put(self.src, "listed.txt", RESTORED)

    def set_patches(self, *bodies):
        names = []
        for number, body in enumerate(bodies, 1):
            name = f"patches/{number:04d}.patch"
            put(self.repo, name, body)
            names.append(name)
        put(self.repo, "patches/series", ("# ordered series\n" + "\n".join(names) + "\n").encode())

    def run(self, platform="linux", check=False, patch_bin=PATCH_BIN):
        return arp.run_apply(self.src, self.repo, self.core, self.tooling,
                             platform, patch_bin, check=check)

    def manifest(self):
        return json.loads((self.src / arp.MARKER).read_bytes())


def test_tiny_patch_context_removal_addition_and_metadata(tmp_path, monkeypatch):
    fx = Fixture(tmp_path)
    original = (fx.repo / "patches/0001.patch").read_bytes()
    patch_mtime = (fx.repo / "patches/0001.patch").stat().st_mtime_ns
    commands = []
    subprocess_run = arp.subprocess.run

    def capture(command, **kwargs):
        commands.append(command)
        assert {"--fuzz=0", "--batch", "--forward", "--get=0"} <= set(command)
        temporary_patch = Path(command[-1])
        assert not temporary_patch.is_relative_to(fx.repo)
        assert not temporary_patch.is_relative_to(fx.src)
        assert kwargs["env"]["PATCH_GET"] == "0"
        if "chromix patch probe " in str(temporary_patch):
            assert kwargs["timeout"] == 10
            return subprocess_run(command, **kwargs)
        transformed = temporary_patch.read_bytes()
        expected = original.replace(b" context example.com", b" context blocked.test")
        expected = expected.replace(b"-old example.com", b"-old blocked.test")
        expected = expected.replace(b"+new example.com", b"+new blocked.test")
        assert transformed == expected
        return subprocess_run(command, **kwargs)

    monkeypatch.setattr(arp.subprocess, "run", capture)
    result = fx.run()
    assert result["status"] == "applied"
    assert result["changed_files"] == ["listed.txt"]
    assert result["patch_count"] == 1
    assert len(commands) == 7
    assert (fx.src / "listed.txt").read_bytes() == EXPECTED
    assert (fx.repo / "patches/0001.patch").read_bytes() == original
    assert (fx.repo / "patches/0001.patch").stat().st_mtime_ns == patch_mtime
    assert not (fx.src / arp.IN_PROGRESS).exists()
    assert {p.name for p in fx.src.glob(".chromix*")} == {arp.MARKER}


def test_nonlisted_patch_and_unrelated_source_are_not_substituted(tmp_path):
    fx = Fixture(tmp_path)
    fx.set_patches(patch("nonlisted.txt"))
    other = put(fx.src, "nonlisted.txt", ORIGINAL)
    unrelated = put(fx.src, "unrelated.txt", b"example.com\n")
    old_mtime = unrelated.stat().st_mtime_ns
    fx.run()
    assert other.read_bytes() == ORIGINAL.replace(b"old example", b"new example")
    assert unrelated.read_bytes() == b"example.com\n"
    assert unrelated.stat().st_mtime_ns == old_mtime
    assert (fx.src / "listed.txt").read_bytes() == RESTORED


def test_mtime_preserves_unchanged_payload_and_advances_changed_source(tmp_path):
    fx = Fixture(tmp_path)
    source = fx.src / "listed.txt"
    old_mtime = source.stat().st_mtime_ns + 10_000_000_000
    os.utime(source, ns=(old_mtime, old_mtime))
    unchanged = put(fx.src, "v8/same.tq", b"unchanged\n")
    put(fx.repo, arp.LITE + "/v8/same.tq", b"unchanged\n")
    different = put(fx.src, "v8/different.tq", b"old\n")
    payload = put(fx.repo, arp.LITE + "/v8/different.tq", b"new\n")
    os.utime(payload, ns=(1_000_000_000, 1_000_000_000))
    same_mtime = unchanged.stat().st_mtime_ns
    diff_mtime = different.stat().st_mtime_ns
    result = fx.run()
    assert result["changed_files"] == ["listed.txt", "v8/different.tq"]
    assert source.stat().st_mtime_ns > old_mtime
    assert unchanged.stat().st_mtime_ns == same_mtime
    assert different.stat().st_mtime_ns > diff_mtime
    assert different.read_bytes() == b"new\n"


def test_net_unchanged_patched_file_is_not_rewritten(tmp_path):
    fx = Fixture(tmp_path)
    fx.set_patches(patch(), patch(old="new example.com", new="old example.com"))
    source = fx.src / "listed.txt"
    before = source.stat().st_mtime_ns
    result = fx.run()
    assert result["status"] == "applied"
    assert result["changed_files"] == []
    assert source.read_bytes() == RESTORED
    assert source.stat().st_mtime_ns == before


def test_repeat_skip_and_read_only_check_without_patch_executable(tmp_path):
    fx = Fixture(tmp_path)
    result = fx.run()
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in fx.src.rglob("*") if p.is_file()}
    skipped = fx.run(patch_bin="not-a-patch-program")
    checked = fx.run(check=True, patch_bin="not-a-patch-program")
    assert skipped == dict(result, status="skipped", changed_files=[])
    assert checked == dict(result, status="checked", changed_files=[])
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before} == before
    manifest = fx.manifest()
    assert manifest["identity"]["series"]["patches"] == [{
        "path": "patches/0001.patch", "sha256": hashlib.sha256(patch()).hexdigest(),
    }]
    assert manifest["identity_sha256"] == result["identity_sha256"]
    assert manifest["outputs"] == {"listed.txt": hashlib.sha256(EXPECTED).hexdigest()}


@pytest.mark.parametrize("change", ["series", "patch", "lite", "regex", "list", "platform"])
def test_changed_inputs_require_clean_workdir(tmp_path, change):
    fx = Fixture(tmp_path)
    put(fx.repo, arp.LITE + "/payload.txt", b"payload\n")
    fx.run()
    if change == "series":
        with (fx.repo / "patches/series").open("ab") as stream:
            stream.write(b"# changed series identity\n")
    elif change == "patch":
        put(fx.repo, "patches/0001.patch", patch(new="different example.com"))
    elif change == "lite":
        put(fx.repo, arp.LITE + "/payload.txt", b"different\n")
    elif change == "regex":
        put(fx.core, "domain_regex.list", REGEX + b"unused#replacement\n")
    elif change == "list":
        put(fx.core, "domain_substitution.list", b"listed.txt\nextra.txt\n")
    for check in (False, True):
        with pytest.raises(arp.ApplyError, match="clean workdir"):
            fx.run(platform="macos" if change == "platform" else "linux", check=check)
    assert (fx.src / "listed.txt").read_bytes() == EXPECTED
    assert not (fx.src / arp.IN_PROGRESS).exists()


@pytest.mark.parametrize("state", ["modified", "missing", "malformed", "incomplete", "in-progress"])
def test_completed_but_partial_or_corrupt_state_rejected(tmp_path, state):
    fx = Fixture(tmp_path)
    fx.run()
    if state == "modified":
        put(fx.src, "listed.txt", b"manually changed\n")
    elif state == "missing":
        (fx.src / "listed.txt").unlink()
    elif state == "malformed":
        put(fx.src, arp.MARKER, b"not JSON")
    elif state == "incomplete":
        saved = fx.manifest()
        saved["outputs"] = {}
        put(fx.src, arp.MARKER, json.dumps(saved).encode())
    else:
        put(fx.src, arp.IN_PROGRESS, b"{}")
    with pytest.raises(arp.ApplyError, match="clean workdir"):
        fx.run(check=True)


def test_check_before_apply_is_read_only(tmp_path):
    fx = Fixture(tmp_path)
    with pytest.raises(arp.ApplyError, match="not completed"):
        fx.run(check=True)
    assert list(fx.src.glob(".chromix*")) == []


@pytest.mark.parametrize("partial", [False, True])
def test_conflict_is_staged_and_leaves_blocking_progress(tmp_path, partial):
    fx = Fixture(tmp_path)
    fx.set_patches(patch(), patch("second.txt"))
    second = put(fx.src, "second.txt", ORIGINAL.replace(b"old", b"new" if partial else b"conflict"))
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (fx.src / "listed.txt", second)}
    with pytest.raises(arp.ApplyError, match="patch failed.*0002"):
        fx.run()
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before} == before
    assert (fx.src / arp.IN_PROGRESS).exists()
    assert not (fx.src / arp.MARKER).exists()
    assert not list(fx.src.rglob("*.rej"))
    assert not list(fx.src.rglob("*.orig"))
    with pytest.raises(arp.ApplyError, match="in-progress"):
        fx.run()


def test_fuzz_is_not_permitted(tmp_path):
    fx = Fixture(tmp_path)
    put(fx.src, "listed.txt", RESTORED.replace(b"context blocked.test", b"wrong context"))
    with pytest.raises(arp.ApplyError, match="patch failed"):
        fx.run()


def test_unknown_non_creation_path_is_rejected(tmp_path):
    fx = Fixture(tmp_path)
    fx.set_patches(patch("unknown.txt"))
    with pytest.raises(arp.ApplyError, match="unknown/missing patch path"):
        fx.run()
    assert not (fx.src / "unknown.txt").exists()
    assert (fx.src / arp.IN_PROGRESS).exists()


def test_new_files_and_duplicate_without_completion_marker(tmp_path):
    fx = Fixture(tmp_path)
    fx.set_patches(new_patch("sub/listed.txt"), new_patch("sub/nonlisted.txt"))
    put(fx.core, "domain_substitution.list", b"sub/listed.txt\n")
    fx.run()
    assert (fx.src / "sub/listed.txt").read_bytes() == b"new blocked.test\n"
    assert (fx.src / "sub/nonlisted.txt").read_bytes() == b"new example.com\n"
    (fx.src / arp.MARKER).unlink()
    with pytest.raises(arp.ApplyError, match="already exists.*duplicate/partial"):
        fx.run()


@pytest.mark.parametrize("platform", ["linux", "macos", "windows"])
def test_platform_list_selection_and_lite_substitution(tmp_path, platform):
    fx = Fixture(tmp_path)
    fx.set_patches(patch(), patch("windows.txt"))
    core_list = b"listed.txt\nv8/core.tq\n"
    windows_list = b"windows.txt\nv8/windows.tq\n"
    put(fx.core, "domain_substitution.list", core_list)
    put(fx.tooling, "domain_substitution.list", windows_list)
    put(fx.src, "listed.txt", ORIGINAL if platform == "windows" else RESTORED)
    put(fx.src, "windows.txt", RESTORED if platform == "windows" else ORIGINAL)
    for name in ("core", "windows"):
        put(fx.repo, f"{arp.LITE}/v8/{name}.tq", b"example.com\n")
    fx.run(platform=platform)
    assert (fx.src / "listed.txt").read_bytes() == (EXPECTED if platform != "windows" else
                                                         ORIGINAL.replace(b"old", b"new"))
    assert (fx.src / "windows.txt").read_bytes() == (EXPECTED if platform == "windows" else
                                                          ORIGINAL.replace(b"old", b"new"))
    assert (fx.src / "v8/core.tq").read_bytes() == (b"example.com\n" if platform == "windows" else
                                                        b"blocked.test\n")
    assert (fx.src / "v8/windows.tq").read_bytes() == (b"blocked.test\n" if platform == "windows" else
                                                           b"example.com\n")
    selected = fx.tooling if platform == "windows" else fx.core
    assert fx.manifest()["identity"]["list"]["path"] == str(selected / "domain_substitution.list")


@pytest.mark.parametrize("encoding", ["utf-8", "latin-1"])
def test_utf8_and_latin1_patch_and_lite_roundtrip(tmp_path, encoding):
    fx = Fixture(tmp_path)
    fx.set_patches(patch(old="café old example.com", new="café new example.com", encoding=encoding))
    put(fx.src, "listed.txt", "context blocked.test\ncafé old blocked.test\ntail\n".encode(encoding))
    put(fx.core, "domain_substitution.list", b"listed.txt\nlatin.txt\n")
    put(fx.repo, arp.LITE + "/latin.txt", "café example.com\n".encode(encoding))
    fx.run()
    assert (fx.src / "listed.txt").read_bytes() == "context blocked.test\ncafé new blocked.test\ntail\n".encode(encoding)
    assert (fx.src / "latin.txt").read_bytes() == "café blocked.test\n".encode(encoding)


def test_no_newline_marker_and_crlf_content_are_preserved(tmp_path):
    fx = Fixture(tmp_path)
    body = (b"diff --git a/listed.txt b/listed.txt\n--- a/listed.txt\n+++ b/listed.txt\n"
            b"@@ -1 +1 @@\n-old example.com\n\\ No newline at end of file\n"
            b"+new example.com\n\\ No newline at end of file\n")
    fx.set_patches(body)
    put(fx.src, "listed.txt", b"old blocked.test")
    fx.run()
    assert (fx.src / "listed.txt").read_bytes() == b"new blocked.test"
    fx = Fixture(tmp_path / "crlf")
    body = patch().replace(b"context example.com\n", b"context example.com\r\n")
    body = body.replace(b"old example.com\n", b"old example.com\r\n")
    body = body.replace(b"new example.com\n", b"new example.com\r\n").replace(b" tail\n", b" tail\r\n")
    fx.set_patches(body)
    put(fx.src, "listed.txt", RESTORED.replace(b"\n", b"\r\n"))
    fx.run()
    assert (fx.src / "listed.txt").read_bytes() == EXPECTED.replace(b"\n", b"\r\n")


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/../../escape", "a//b", "a/./b",
                                  "C:/escape", "a\\..\\escape", ".git/config",
                                  ".chromix-source-ready", "NUL.txt", "a./b", "a/b "])
def test_unsafe_paths_rejected(tmp_path, name):
    fx = Fixture(tmp_path)
    fx.set_patches(patch(name))
    with pytest.raises(arp.ApplyError, match="unsafe|unsupported"):
        fx.run()
    assert not (fx.src / arp.MARKER).exists()


@pytest.mark.parametrize("input_kind", ["series", "list", "patch_symlink", "lite_symlink",
                                        "source_symlink", "parent_symlink", "marker_symlink"])
def test_input_and_source_path_boundaries(tmp_path, input_kind):
    fx = Fixture(tmp_path)
    outside = put(tmp_path, "outside.txt", b"do not touch\n")
    if input_kind == "series":
        put(fx.repo, "patches/series", b"../outside.txt\n")
    elif input_kind == "list":
        put(fx.core, "domain_substitution.list", b"../outside.txt\n")
    elif input_kind == "patch_symlink":
        (fx.repo / "patches/0001.patch").unlink()
        (fx.repo / "patches/0001.patch").symlink_to(outside)
    elif input_kind == "lite_symlink":
        root = fx.repo / arp.LITE
        root.mkdir(parents=True)
        (root / "payload").symlink_to(outside)
    elif input_kind == "source_symlink":
        (fx.src / "listed.txt").unlink()
        (fx.src / "listed.txt").symlink_to(outside)
    elif input_kind == "parent_symlink":
        (fx.src / "sub").symlink_to(tmp_path, target_is_directory=True)
        fx.set_patches(new_patch())
    else:
        (fx.src / arp.MARKER).symlink_to(outside)
    with pytest.raises(arp.ApplyError, match="unsafe|symlink"):
        fx.run()
    assert outside.read_bytes() == b"do not touch\n"


@pytest.mark.parametrize("body", [
    b"--- a/listed.txt\n+++ b/listed.txt\n@@ -1 +1 @@\n-old\n+new\n",
    b"diff --git a/listed.txt b/other.txt\nrename from listed.txt\nrename to other.txt\n",
    b"diff --git a/listed.txt b/listed.txt\nGIT binary patch\nliteral 3\nabc\n",
    b"diff --git a/listed.txt b/listed.txt\nold mode 100644\nnew mode 100755\n",
    new_patch(mode="120000"),
    patch().replace(b"+++ b/listed.txt", b"+++ b/unknown.txt"),
    patch().replace(b"-1,3", b"-1,8"),
    patch() + b"unparsed trailing garbage\n",
])
def test_unsupported_and_malformed_diffs_rejected_before_writes(tmp_path, body):
    fx = Fixture(tmp_path)
    fx.set_patches(body)
    with pytest.raises(arp.ApplyError, match="unsupported|mismatch"):
        fx.run()
    assert list(fx.src.glob(".chromix*")) == []
    assert (fx.src / "listed.txt").read_bytes() == RESTORED


@pytest.mark.parametrize("regex", [b"missing delimiter\n", b"[#bad\n", rb"x#\g<2>" + b"\n",
                                   rb"example\.com#one\ntwo" + b"\n"])
def test_invalid_or_line_changing_regex_rejected(tmp_path, regex):
    fx = Fixture(tmp_path)
    put(fx.core, "domain_regex.list", regex)
    with pytest.raises(arp.ApplyError, match="domain_regex|line boundaries"):
        fx.run()
    assert (fx.src / "listed.txt").read_bytes() == RESTORED


def test_regex_order_backreferences_and_per_file_encoding():
    rules = arp._rules(rb"(example)\.com#\g<1>.net" + b"\nexample.net#blocked.test\n")
    latin = patch("latin.txt", old="café old example.com", new="café new example.com", encoding="latin-1")
    utf = patch("utf.txt", old="中文 old example.com", new="中文 new example.com")
    transformed, entries = arp.transform_patch(latin + utf, {"latin.txt", "utf.txt"}, rules)
    assert "café new blocked.test".encode("latin-1") in transformed
    assert "中文 new blocked.test".encode() in transformed
    assert [entry[0] for entry in entries] == ["latin.txt", "utf.txt"]


def test_lite_supplies_missing_patch_input_before_series(tmp_path):
    fx = Fixture(tmp_path)
    (fx.src / "listed.txt").unlink()
    put(fx.repo, arp.LITE + "/listed.txt", ORIGINAL)
    fx.run()
    assert (fx.src / "listed.txt").read_bytes() == EXPECTED


def test_donor_code_is_never_executed(tmp_path):
    fx = Fixture(tmp_path)
    for root in (fx.core, fx.tooling):
        put(root, "utils/domain_substitution.py", b"raise RuntimeError('must not run')\n")
    fx.run()
    other = Fixture(tmp_path / "other")
    program = put(other.core, "patch", b"#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    with pytest.raises(arp.ApplyError, match="donor code"):
        other.run(patch_bin=program)
    assert list(other.src.glob(".chromix*")) == []


def test_temp_under_repo_is_rejected(tmp_path, monkeypatch):
    fx = Fixture(tmp_path)
    monkeypatch.setattr(arp.tempfile, "gettempdir", lambda: str(fx.repo))
    with pytest.raises(arp.ApplyError, match="temporary directory must be outside"):
        fx.run()
    assert list(fx.src.glob(".chromix*")) == []


def test_publish_failure_keeps_progress_and_refuses_resume(tmp_path, monkeypatch):
    fx = Fixture(tmp_path)
    write = arp._atomic_write

    def fail_marker(path, *args, **kwargs):
        if path.name == arp.MARKER:
            raise OSError("injected disk failure")
        write(path, *args, **kwargs)

    monkeypatch.setattr(arp, "_atomic_write", fail_marker)
    with pytest.raises(OSError, match="disk failure"):
        fx.run()
    assert (fx.src / "listed.txt").read_bytes() == EXPECTED
    assert (fx.src / arp.IN_PROGRESS).exists()
    assert not (fx.src / arp.MARKER).exists()
    with pytest.raises(arp.ApplyError, match="in-progress"):
        fx.run()


def test_cli_apply_check_and_failure_status(tmp_path):
    fx = Fixture(tmp_path)
    command = [sys.executable, str(Path(arp.__file__)), "--src", str(fx.src), "--repo", str(fx.repo),
               "--core", str(fx.core), "--platform-tooling", str(fx.tooling),
               "--platform", "linux", "--patch-bin", PATCH_BIN]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "applied"
    result = subprocess.run(command + ["--check"], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "checked"
    put(fx.src, "listed.txt", b"changed\n")
    result = subprocess.run(command + ["--check"], capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "clean workdir" in result.stderr


def test_real_series_parses_without_donor_execution():
    repo = Path(__file__).resolve().parents[2]
    names = [line for line in (repo / "patches/series").read_text().splitlines() if line and not line.startswith("#")]
    for name in names:
        raw = (repo / name).read_bytes()
        transformed, entries = arp.transform_patch(raw, set(), [])
        assert transformed == raw
        assert entries
    assert len(names) == 128
    assert [Path(name).name[:4] for name in names] == [f"{i:04d}" for i in range(1, 127)]


def patch_stub(root, name, *, compatible):
    if os.name == "nt":
        pytest.skip("POSIX executable fixture; native Windows uses installed patch.exe")
    log = root / (name.replace("/", "_") + ".jsonl")
    program = put(root, name, (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(log)!r}, 'a') as output:\n"
        "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('GNU patch fixture')\n"
        "    raise SystemExit(0)\n"
        "assert '--get=0' in sys.argv and '--fuzz=0' in sys.argv\n"
        "assert os.environ['PATCH_GET'] == '0'\n"
        + (f"os.execv({PATCH_BIN!r}, [{PATCH_BIN!r}, *sys.argv[1:]])\n" if compatible else
           "print(\"patch.exe: option `--get' doesn't allow an argument\")\nraise SystemExit(2)\n")
    ).encode())
    program.chmod(0o755)
    return program, log


def test_strawberry_version_success_is_not_capability_and_fallback_is_probed(tmp_path):
    fx = Fixture(tmp_path / "work space")
    bad, bad_log = patch_stub(tmp_path, "Strawberry/c/bin/patch.exe", compatible=False)
    good, good_log = patch_stub(tmp_path, "Compatible Git/usr/bin/patch.exe", compatible=True)
    assert subprocess.run([str(bad), "--version"], capture_output=True).returncode == 0
    roots = (fx.src, fx.core, fx.tooling)
    selected = arp.select_patch_program([str(bad), str(good)], roots)
    assert selected == str(good)
    assert "--get=0" in bad_log.read_text()
    assert len(good_log.read_text().splitlines()) == 6
    assert not list(fx.src.glob(".chromix*"))
    assert fx.run(patch_bin=selected)["status"] == "applied"
    count = len(good_log.read_text().splitlines())
    assert fx.run(patch_bin=selected)["status"] == "skipped"
    assert fx.run(patch_bin=selected, check=True)["status"] == "checked"
    assert len(good_log.read_text().splitlines()) == count


def test_no_compatible_tool_fails_before_source_or_progress_writes(tmp_path):
    fx = Fixture(tmp_path / "work")
    bad, _ = patch_stub(tmp_path, "Strawberry/c/bin/patch.exe", compatible=False)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in fx.src.rglob("*") if p.is_file()}
    with pytest.raises(arp.ApplyError, match="no compatible host patch.*"):
        arp.select_patch_program([str(bad)], (fx.src, fx.core, fx.tooling))
    with pytest.raises(arp.ApplyError, match="doesn't allow an argument"):
        fx.run(patch_bin=bad)
    assert not list(fx.src.glob(".chromix*"))
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before} == before


@pytest.mark.parametrize("candidate", ["", " ", "\t", "missing patch.exe"])
def test_empty_whitespace_and_missing_patch_paths_fail_before_progress(tmp_path, candidate):
    fx = Fixture(tmp_path)
    with pytest.raises(arp.ApplyError, match="patch binary"):
        fx.run(patch_bin=candidate)
    command = [sys.executable, arp.__file__, "--src", str(fx.src), "--repo", str(fx.repo),
               "--core", str(fx.core), "--platform-tooling", str(fx.tooling),
               "--platform", "windows", "--patch-bin", candidate]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 1, result.stderr
    assert "patch binary" in result.stderr
    assert not list(fx.src.glob(".chromix*"))
    assert (fx.src / "listed.txt").read_bytes() == RESTORED


def test_empty_executable_is_rejected_and_compatible_fallback_is_used(tmp_path):
    empty = put(tmp_path, "empty patch.exe", b"")
    empty.chmod(0o755)
    assert arp.select_patch_program([str(empty), PATCH_BIN], ()) == str(Path(PATCH_BIN).resolve())


@pytest.mark.parametrize("failure", ["no-op", "ignores-fuzz", "timeout"])
def test_probe_rejects_false_success_ignored_safety_and_timeout(tmp_path, monkeypatch, failure):
    fx = Fixture(tmp_path)
    real_run = arp.subprocess.run

    def incompatible(command, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if failure == "no-op":
            return subprocess.CompletedProcess(command, 0, b"GNU patch fixture\n")
        return real_run([arg for arg in command if arg != "--fuzz=0"], **kwargs)

    monkeypatch.setattr(arp.subprocess, "run", incompatible)
    with pytest.raises(arp.ApplyError, match="capability probe failed"):
        fx.run()
    assert not list(fx.src.glob(".chromix*"))
    assert (fx.src / "listed.txt").read_bytes() == RESTORED


def test_selector_never_executes_donor_fallback(tmp_path):
    fx = Fixture(tmp_path)
    donor, log = patch_stub(fx.core, "patch.exe", compatible=True)
    selected = arp.select_patch_program([str(donor), PATCH_BIN], (fx.src, fx.core, fx.tooling))
    assert selected == str(Path(PATCH_BIN).resolve())
    assert not log.exists()


def resolve_host_patch(tmp_path, git_paths, patch_paths, *, program_files=""):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell is required")
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "build/windows/prepare-ungoogled.ps1").read_text()
    resolver = source[source.index("function Resolve-HostPatch"):source.index("function Assert-RestoredToolchain")]
    script = put(tmp_path, "resolve.ps1", (r'''
$ErrorActionPreference = "Stop"
$Repo = $env:PROBE_REPO
$Src = Join-Path $env:PROBE_ROOT "src"
$Ungoogled = Join-Path $env:PROBE_ROOT "core"
$Windows = Join-Path $env:PROBE_ROOT "windows"
$Python = $env:PROBE_PYTHON
function Assert-Budget([string]$Step) {}
function Get-Command {
  param([string]$Name, [switch]$All, [string]$ErrorAction)
  $paths = if ($Name -eq "git.exe") { $env:PROBE_GITS } else { $env:PROBE_PATCHES }
  foreach ($path in ($paths | ConvertFrom-Json)) { [pscustomobject]@{ Source = $path } }
}
''' + resolver + "\nResolve-HostPatch\n").encode())
    environment = dict(os.environ, PROBE_REPO=str(repo), PROBE_ROOT=str(tmp_path),
                       PROBE_PYTHON=sys.executable, PROBE_GITS=json.dumps(git_paths),
                       PROBE_PATCHES=json.dumps(patch_paths), ProgramW6432="",
                       ProgramFiles=program_files)
    environment["ProgramFiles(x86)"] = ""
    return subprocess.run([pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
                          env=environment, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("git_directory", ["cmd", "bin", "mingw64/bin"])
def test_windows_resolver_prefers_git_over_strawberry_in_path(tmp_path, git_directory):
    bad, bad_log = patch_stub(tmp_path, "Strawberry/c/bin/patch.exe", compatible=False)
    good, good_log = patch_stub(tmp_path, "Program Files/Git/usr/bin/patch.exe", compatible=True)
    git = tmp_path / "Program Files/Git" / git_directory / "git.exe"
    result = resolve_host_patch(tmp_path, ["", " ", str(git)], [str(bad)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == str(good)
    assert len(good_log.read_text().splitlines()) == 6
    assert not bad_log.exists()


def test_windows_resolver_probes_rejected_path_tool_then_compatible_alternative(tmp_path):
    bad, bad_log = patch_stub(tmp_path, "Strawberry/c/bin/patch.exe", compatible=False)
    good, good_log = patch_stub(tmp_path, "Alternate Tools/patch.exe", compatible=True)
    result = resolve_host_patch(tmp_path, [], ["", " ", str(bad), str(good)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == str(good)
    assert len(bad_log.read_text().splitlines()) == 1
    assert len(good_log.read_text().splitlines()) == 6


def test_windows_resolver_finds_standard_git_with_empty_command_sources(tmp_path):
    good, log = patch_stub(tmp_path, "Program Files/Git/usr/bin/patch.exe", compatible=True)
    result = resolve_host_patch(tmp_path, ["", " "], ["", " "], program_files=str(tmp_path / "Program Files"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == str(good)
    assert len(log.read_text().splitlines()) == 6


@pytest.mark.parametrize("has_bad_tool", [False, True])
def test_windows_resolver_without_compatible_alternative_fails_early(tmp_path, has_bad_tool):
    paths = ["", " "]
    if has_bad_tool:
        bad, _ = patch_stub(tmp_path, "Strawberry/c/bin/patch.exe", compatible=False)
        paths.append(str(bad))
    result = resolve_host_patch(tmp_path, [], paths)
    assert result.returncode != 0
    message = "no compatible host patch" if has_bad_tool else "no host patch.exe candidates"
    assert message in result.stderr
    assert not (tmp_path / "src").exists()


def test_cold_and_restored_use_same_probed_host_selector_and_keep_safety_options():
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "build/windows/prepare-ungoogled.ps1").read_text()
    cold = source.index("if (-not $RestoredUpstream) { $PatchExe = Resolve-HostPatch }")
    assert cold < source.index('$resumeChromixPatch = ""') < source.index("Ensure-Checkout \"")
    assert '$RestoredPatchExe = Resolve-HostPatch' in source
    assert '"--patch-bin", $PatchExe' in source
    assert '"--patch-bin", $RestoredPatchExe' in source
    assert '$env:PATCH_GET = "0"' in source
    for option in ("--fuzz=0", "--binary", "--get=0", "--no-backup-if-mismatch", "--reject-file=-"):
        assert f'"{option}"' in source
    commands = [line for line in source.splitlines() if "& $PatchExe " in line]
    assert commands
    assert all("@PatchSafetyOptions" in line for line in commands)
