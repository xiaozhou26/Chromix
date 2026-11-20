"""Exact157 Version declaration, direct include, replay and native UA compilation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess

import pytest

from tools import patch_selection as selection
from tools.tests.test_chromium157_locale_api import fixture, verifier

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "35e2db1b0687a4525017f5ec8fd3820823aef8ff"
SOURCE = "components/embedder_support/user_agent_utils.cc"
PATCH = "patches/chromium157-additions/0238-user-agent-version-header.patch"
INCLUDE = '#include "base/version.h"\n'


def test_real_version_definition_and_forward_declaration():
    data = fixture()
    for name in ("base/version.h", "base/version_info/version_info.h"):
        raw = data["files"][name].encode()
        origin = data["source"]["files"][name]
        assert origin["revision"] == "4b3d0dabb8250b6f152d2d85d79e7706e309d087"
        assert selection.digest(raw) == origin["sha256"]
        assert hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() == origin["git_blob"]
    assert "class BASE_EXPORT Version {" in data["files"]["base/version.h"]
    assert "explicit Version(std::string_view version_str);" in data["files"]["base/version.h"]
    assert "class Version;" in data["files"]["base/version_info/version_info.h"]
    assert INCLUDE not in data["files"][SOURCE]


@pytest.mark.parametrize("platform", ["linux", "macos", "windows"])
@pytest.mark.parametrize("substituted", [False, True])
def test_exact_ua_replay_changes_only_direct_include(tmp_path, platform, substituted):
    data = fixture()
    verify = verifier()
    listed = set(data["domain_lists"][platform].splitlines()) if substituted else set()
    rules = verify.arp._rules(data["domain_regex"].encode()) if substituted else []
    path = tmp_path / SOURCE
    path.parent.mkdir(parents=True)
    path.write_text(data["files"][SOURCE])
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CEILING_DIRECTORIES=str(tmp_path.parent))
    layers = ("linux_core", "linux") if platform == "linux" else ("core", platform)
    for layer in layers:
        for entry in data["predecessors"][layer]:
            if entry["target"] == SOURCE:
                verify.apply(tmp_path, entry["text"].encode(), env)
    if SOURCE in listed:
        path.write_text(verify.arp._substitute(path.read_text(), rules))
    initial = path.read_bytes()
    _, patches = selection.select(ROOT, platform)
    prepared = []
    for name, raw in patches:
        transformed, targets = verify.arp.transform_patch(raw, listed, rules)
        if not any(target == SOURCE for target, _, _ in targets):
            continue
        assert len(targets) == 1
        if name == PATCH:
            before = path.read_bytes()
        verify.apply(tmp_path, transformed, env, check=True)
        verify.apply(tmp_path, transformed, env)
        prepared.append(transformed)
    final = path.read_bytes()
    assert final.count(INCLUDE.encode()) == 1
    assert final.replace(INCLUDE.encode(), b"") == before
    for reverse in (True, False):
        for raw in reversed(prepared) if reverse else prepared:
            verify.apply(tmp_path, raw, env, check=True, reverse=reverse)
            verify.apply(tmp_path, raw, env, reverse=reverse)
        assert path.read_bytes() == (initial if reverse else final)


def test_old228_patch_bytes_and_only_added_version_translation_unit():
    old = json.loads(subprocess.check_output([
        "git", "-C", str(ROOT), "show", BASELINE + ":" + selection.MANIFEST157]))
    for entry in old["base_patches"] + old["overrides"]:
        assert selection.digest((ROOT / entry["path"]).read_bytes()) == entry["sha256"]
    verify = verifier()
    for platform, entry in old["platforms"].items():
        for patch in entry["additions"]:
            assert selection.digest((ROOT / patch["path"]).read_bytes()) == patch["sha256"]
        identity, patches = selection.select(ROOT, platform)
        replacements = {"patches/" + Path(patch["path"]).name: patch for patch in old["overrides"]}
        previous = [{key: replacements.get(patch["path"], patch)[key] for key in ("path", "sha256")}
                    for patch in old["base_patches"]] + entry["additions"]
        assert identity["patches"][:len(previous)] == previous
        assert patches[len(previous)][0] == PATCH
        assert len(patches) == {"linux": 230, "macos": 231, "windows": 235}[platform]
        introduced = set()
        for _, raw in patches:
            if any(line.startswith(b"+") and not line.startswith(b"+++") and b"base::Version" in line
                   for line in raw.splitlines()):
                introduced.update(target for target, _, _ in verify.arp.transform_patch(raw, set(), [])[1])
        assert introduced == {SOURCE}
    assert "0238-user-agent" not in (ROOT / selection.MANIFEST156).read_text()
    assert "0238-user-agent" not in (ROOT / "patches/series").read_text()


def test_native_complete_ua_object_with_exact_donor_command(tmp_path):
    supplied = os.environ.get("CHROMIX_UA157_SOURCE")
    if not supplied:
        pytest.skip("set CHROMIX_UA157_SOURCE to the patched exact157 donor tree")
    src = Path(supplied).resolve()
    out = src / "out/Chromix"
    data = fixture()
    assert (src / "base/version.h").read_text() == data["files"]["base/version.h"]
    report = json.loads((ROOT / "patches/chromium157-verification/verified.json").read_bytes())
    case = next(case for case in report["cases"] if case["platform"] == "linux" and case["domain_substituted"])
    assert selection.digest((src / SOURCE).read_bytes()) == case["roundtrip"]["final"][SOURCE]
    target = "obj/components/embedder_support/user_agent/user_agent_utils.o"
    command = subprocess.check_output(["ninja", "-t", "commands", target], cwd=out,
                                      text=True, errors="replace").splitlines()[-1]
    review = json.loads((ROOT / "patches/chromium157-verification/review.json").read_bytes())
    evidence = review["ua_header_evidence"]
    assert selection.digest((command + "\n").encode()) == evidence["compile_command_sha256"]
    args = shlex.split(command)
    obj = tmp_path / "user_agent_utils.o"
    args[args.index("-o") + 1] = str(obj)
    args[args.index("-MF") + 1] = str(obj) + ".d"
    result = subprocess.run(args, cwd=out, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
    assert obj.stat().st_size > 0
