"""Linux's disabled PVT backend must match its generated CDP schema."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

import pytest

from tools import patch_selection as selection
from tools.tests.test_chromium157_locale_api import fixture, verifier

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "3da74546373f3351029064d79fe85e3d3cb4497f"
PATCHES = (
    "patches/chromium157-additions/0239-linux-disabled-private-verification-token-protocol.patch",
    "patches/chromium157-additions/0240-linux-disabled-private-verification-token-chrome-config.patch",
    "patches/chromium157-additions/0241-linux-disabled-private-verification-token-content-config.patch",
)
PDL = "third_party/blink/public/devtools_protocol/domains/Storage.pdl"
CONFIGS = ("chrome/browser/devtools/inspector_protocol_config.json",
           "content/browser/devtools/protocol_config.json")
COMMANDS = {"getPrivateVerificationTokens", "getPrivateVerificationTokensIssuerConfigs",
            "clearPrivateVerificationTokens", "deletePrivateVerificationToken",
            "setPrivateVerificationTokensTracking"}
TYPES = {"PrivateVerificationToken", "PrivateVerificationTokensIssuerConfig"}
EVENTS = {"privateVerificationTokensUpdated"}


def declarations(text):
    pattern = r"^  (?:(?:experimental|deprecated) )?(type|command|event) (\w+)[^\n]*"
    matches = list(re.finditer(pattern, text, re.M))
    result = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        lines = text[match.start():end].splitlines()
        result[match.group(1), match.group(2)] = [line for line in lines
                                                if line.strip() and not line.lstrip().startswith("#")]
    return result


def old_manifest():
    return json.loads(subprocess.check_output([
        "git", "-C", str(ROOT), "show", BASELINE + ":" + selection.MANIFEST157]))


def test_exact_append_preserves_all_old_bytes_and_platform_scope():
    old = old_manifest()
    for entry in old["base_patches"] + old["overrides"]:
        assert selection.digest((ROOT / entry["path"]).read_bytes()) == entry["sha256"]
    for platform, old_platform in old["platforms"].items():
        for entry in old_platform["additions"]:
            assert selection.digest((ROOT / entry["path"]).read_bytes()) == entry["sha256"]
        replacements = {"patches/" + Path(entry["path"]).name: entry for entry in old["overrides"]}
        previous = [{key: replacements.get(entry["path"], entry)[key] for key in ("path", "sha256")}
                    for entry in old["base_patches"]] + old_platform["additions"]
        selected, raw = selection.select(ROOT, platform)
        assert selected["patches"][:len(previous)] == previous
        assert len(raw) == {"linux": 233, "macos": 231, "windows": 235}[platform]
        assert [name for name, _ in raw[len(previous):]] == (list(PATCHES) + ["patches/chromium157-additions/0242-linux-disabled-private-verification-token-factory-dependency.patch"] if platform == "linux" else [])
    for patch in PATCHES:
        assert patch not in (ROOT / "patches/series").read_text()
        assert patch not in (ROOT / selection.MANIFEST156).read_text()


def test_real_selected_stacks_pass_unmodified_patch_linter():
    from tools import check_patches

    report = check_patches.Report()
    check_patches.check_selected_stacks(report, ROOT)
    assert report.failures == []


def test_single_source_split_preserves_original_diff_and_native_evidence():
    original = subprocess.check_output([
        "git", "-C", str(ROOT), "show", "6302b915abe3b805f756db466ff2721038978131:" + PATCHES[0]])
    parts = [(ROOT / name).read_bytes() for name in PATCHES]
    assert b"".join(parts) == original
    for raw, target in zip(parts, (PDL, *CONFIGS), strict=True):
        assert raw.count(b"diff --git ") == 1
        assert [name for name, _, _ in verifier().arp.transform_patch(raw, set(), [])[1]] == [target]
    old_review = json.loads(subprocess.check_output([
        "git", "-C", str(ROOT), "show",
        "6302b915abe3b805f756db466ff2721038978131:patches/chromium157-verification/review.json"]))
    review = json.loads((ROOT / "patches/chromium157-verification/review.json").read_bytes())
    for key in ("source_files", "native", "generation_commands"):
        assert review["storage_protocol_evidence"][key] == old_review["storage_protocol_evidence"][key]


@pytest.mark.parametrize("substituted", [False, True])
def test_only_previously_disabled_pvt_schema_is_removed(tmp_path, substituted):
    data = fixture()
    verify = verifier()
    rules = verify.arp._rules(data["domain_regex"].encode()) if substituted else []
    listed = set(data["domain_lists"]["linux"].splitlines()) if substituted else set()
    initial = {}
    for name in (PDL, *CONFIGS):
        text = data["files"][name]
        if name in listed:
            text = verify.arp._substitute(text, rules)
        initial[name] = text.encode()
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(initial[name])
    prepared = []
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CEILING_DIRECTORIES=str(tmp_path.parent))
    for patch, target in zip(PATCHES, (PDL, *CONFIGS), strict=True):
        raw, targets = verify.arp.transform_patch((ROOT / patch).read_bytes(), listed, rules)
        assert [name for name, _, _ in targets] == [target]
        verify.apply(tmp_path, raw, env, check=True)
        verify.apply(tmp_path, raw, env)
        prepared.append(raw)
    before = declarations(initial[PDL].decode())
    removed = {("command", name) for name in COMMANDS} | {("type", name) for name in TYPES} | {
        ("event", name) for name in EVENTS}
    assert removed <= before.keys()
    assert declarations((tmp_path / PDL).read_text()) == {
        key: value for key, value in before.items() if key not in removed}
    for name in CONFIGS:
        expected = json.loads(initial[name])
        storage = next(entry for entry in expected["protocol"]["options"] if entry["domain"] == "Storage")
        for key, values in storage.items():
            if isinstance(values, list):
                storage[key] = [value for value in values if value not in COMMANDS | EVENTS]
        assert json.loads((tmp_path / name).read_bytes()) == expected
    final = {name: (tmp_path / name).read_bytes() for name in initial}
    for reverse in (True, False):
        for raw in reversed(prepared) if reverse else prepared:
            verify.apply(tmp_path, raw, env, check=True, reverse=reverse)
            verify.apply(tmp_path, raw, env, reverse=reverse)
        assert {name: (tmp_path / name).read_bytes() for name in initial} == (initial if reverse else final)


def test_platform_disable_is_not_a_core_or_other_platform_policy():
    data = fixture()
    targets = {PDL, *CONFIGS, "chrome/browser/devtools/protocol/storage_handler.cc",
               "chrome/browser/devtools/protocol/storage_handler.h"}
    for layer in ("core", "linux_core", "macos", "windows"):
        assert not any(entry["target"] in targets for entry in data["predecessors"][layer])
        coverage = data["source"]["overlay_coverage"][layer]
        assert not any(entry["target"] in targets for entry in coverage["omitted"])
    platform = ROOT / "build/linux/157.0.8103.78/patches/chromium-156-remove-private-verification-tokens.patch"
    raw = platform.read_text()
    for command in COMMANDS:
        assert "-" in raw and "StorageHandler::" + command[0].upper() + command[1:] in raw
    assert '"//chrome/browser/private_verification_tokens"' in raw
    assert "Storage.pdl" not in raw
    assert "inspector_protocol_config.json" not in raw
    assert "protocol_config.json" not in raw


def test_full_replay_and_fixed_source_identities_cover_protocol():
    report = json.loads((ROOT / "patches/chromium157-verification/verified.json").read_bytes())
    evidence = json.loads((ROOT / "patches/chromium157-verification/review.json").read_bytes())["storage_protocol_evidence"]
    assert report["status"] == "verified"
    assert report["manifest_sha256"] == selection.REVIEW157_SHA256
    assert len(report["cases"]) == 6
    for case in report["cases"]:
        assert case["status"] == case["roundtrip"]["status"] == "verified"
        assert case["patch_count"] == selection.PATCH_COUNTS157[case["platform"]]
        if case["platform"] == "linux":
            assert case["roundtrip"]["patch_count"] == 242
            for entry, patch, name in zip(case["patches"][229:232], PATCHES, (PDL, *CONFIGS), strict=True):
                assert entry["path"] == patch
                hashes = evidence["source_files"][name]
                assert entry["preimages"] == {name: hashes["pre_sha256"]}
                assert entry["postimages"] == {name: hashes["post_sha256"]}


@pytest.mark.parametrize("platform", ["macos", "windows"])
def test_protocol_patch_cannot_be_selected_for_enabled_platform(platform):
    manifest = json.loads((ROOT / selection.MANIFEST157).read_bytes())
    manifest["platforms"][platform]["additions"].append(copy.deepcopy(manifest["platforms"]["linux"]["additions"][-2]))
    with pytest.raises(selection.SelectionError, match="incomplete Chromium 157 additions"):
        selection.validate_manifest157(manifest)


def test_old_ua_checkpoint_routes_reject_new232_stack():
    from tools import linux157_ua

    selected, raw = selection.select(ROOT, "linux")
    for profile in (linux157_ua.load(ROOT), linux157_ua.load(ROOT, "arm64")):
        with pytest.raises(ValueError, match="require unchanged old228 plus exact one addition"):
            linux157_ua.validate_additions(selected, raw, profile)


def test_native_complete_objects_with_latest_donor_gn_commands(tmp_path):
    supplied = os.environ.get("CHROMIX_STORAGE157_SOURCE")
    if not supplied:
        pytest.skip("set CHROMIX_STORAGE157_SOURCE to the patched latest ARM stage5 donor")
    src = Path(supplied)
    out = src / "out/Chromix"
    review = json.loads((ROOT / "patches/chromium157-verification/review.json").read_bytes())
    evidence = review["storage_protocol_evidence"]
    for name, hashes in evidence["source_files"].items():
        assert selection.digest((src / name).read_bytes()) == hashes["post_sha256"]
    for name in ("chrome_devtools_session", "storage_handler"):
        target = "obj/chrome/browser/devtools/devtools/" + name + ".o"
        command = subprocess.check_output(["ninja", "-t", "commands", target], cwd=out,
                                          text=True, errors="replace").splitlines()[-1]
        assert selection.digest((command + "\n").encode()) == evidence["native"]["objects"][name]["command_sha256"]
        args = shlex.split(command)
        obj = tmp_path / (name + ".o")
        args[args.index("-o") + 1] = str(obj)
        args[args.index("-MF") + 1] = str(obj) + ".d"
        result = subprocess.run(args, cwd=out, capture_output=True, text=True, timeout=600)
        assert result.returncode == 0, result.stdout + result.stderr
        assert obj.read_bytes().startswith(b"BC\xc0\xde")
    for base in ("gen/chrome/browser/devtools/protocol", "gen/content/browser/devtools/protocol"):
        for suffix in ("h", "cc"):
            text = (out / base / ("storage." + suffix)).read_text()
            assert "PrivateVerificationToken" not in text
            assert "privateVerificationToken" not in text
    protocol = json.loads((out / "gen/third_party/blink/public/devtools_protocol/protocol.json").read_bytes())
    storage = next(domain for domain in protocol["domains"] if domain["domain"] == "Storage")
    names = {entry["name"] for entry in storage["commands"]}
    assert not names & COMMANDS
    assert {"getTrustTokens", "clearTrustTokens", "setStorageBucketTracking", "getCookies",
            "getUsageAndQuota", "runBounceTrackingMitigations"} <= names


def test_native_generated_schema_and_objects():
    supplied = os.environ.get("CHROMIX_STORAGE157_EVIDENCE")
    if not supplied:
        pytest.skip("set CHROMIX_STORAGE157_EVIDENCE to the actual latest-donor native probe")
    path = Path(supplied)
    report = json.loads((path / "native.json").read_bytes())
    assert report["donor_run"] == 44660206036
    assert report["donor_artifact"] == 14608249261
    assert report["before_returncode"] != 0
    assert report["generated_commands_removed"] == sorted(COMMANDS)
    assert report["unrelated_protocol_unchanged"]
    assert report["unrelated_out_unchanged"]
    assert set(report["objects"]) == {"chrome_devtools_session", "storage_handler"}
    for name, entry in report["objects"].items():
        obj = path / (name + ".o")
        assert entry["returncode"] == 0
        assert obj.stat().st_size == entry["bytes"] > 0
        assert selection.digest(obj.read_bytes()) == entry["sha256"]
