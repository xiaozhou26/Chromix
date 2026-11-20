"""Linux's excluded PVT target must not remain a remover factory dependency."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from tools import patch_selection as selection
from tools.tests.test_chromium157_locale_api import fixture, verifier

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "e411ccf25cae004b9e6f5f7815e003b3efc3a9b8"
SOURCE = "chrome/browser/browsing_data/chrome_browsing_data_remover_delegate_factory.cc"
PATCH = "patches/chromium157-additions/0242-linux-disabled-private-verification-token-factory-dependency.patch"
REMOVED = (
    '#include "chrome/browser/private_verification_tokens/private_verification_tokens_service_factory.h"\n',
    '  DependsOn(PrivateVerificationTokensServiceFactory::GetInstance());\n',
)


def test_old232_prefix_and_other_platform_bytes_unchanged():
    old = json.loads(subprocess.check_output(["git", "-C", str(ROOT), "show", BASELINE + ":" + selection.MANIFEST157]))
    for entry in old["base_patches"] + old["overrides"]:
        assert selection.digest((ROOT / entry["path"]).read_bytes()) == entry["sha256"]
    for platform, value in old["platforms"].items():
        replacements = {"patches/" + Path(entry["path"]).name: entry for entry in old["overrides"]}
        previous = [{key: replacements.get(entry["path"], entry)[key] for key in ("path", "sha256")}
                    for entry in old["base_patches"]] + value["additions"]
        for entry in previous:
            assert selection.digest((ROOT / entry["path"]).read_bytes()) == entry["sha256"]
        chosen, raw = selection.select(ROOT, platform)
        assert chosen["patches"][:len(previous)] == previous
        assert [name for name, _ in raw[len(previous):]] == ([PATCH] if platform == "linux" else [])
        assert len(raw) == {"linux": 233, "macos": 231, "windows": 235}[platform]


@pytest.mark.parametrize("substituted", [False, True])
def test_exact_two_line_removal_and_reverse_forward(tmp_path, substituted):
    data = fixture()
    verify = verifier()
    before = data["files"][SOURCE]
    for line in REMOVED:
        assert before.count(line) == 1
    rules = verify.arp._rules(data["domain_regex"].encode()) if substituted else []
    listed = set(data["domain_lists"]["linux"].splitlines()) if substituted else set()
    if SOURCE in listed:
        before = verify.arp._substitute(before, rules)
    path = tmp_path / SOURCE
    path.parent.mkdir(parents=True)
    path.write_text(before)
    raw, entries = verify.arp.transform_patch((ROOT / PATCH).read_bytes(), listed, rules)
    assert entries == [(SOURCE, "modify", None)]
    assert not verify.added(raw)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CEILING_DIRECTORIES=str(tmp_path.parent))
    verify.apply(tmp_path, raw, env, check=True)
    verify.apply(tmp_path, raw, env)
    after = before
    for line in REMOVED:
        after = after.replace(line, "")
    assert path.read_text() == after
    assert "DependsOn(ProtocolHandlerRegistryFactory::GetInstance());" in after
    assert "DependsOn(ReadingListModelFactory::GetInstance());" in after
    for reverse in (True, False):
        verify.apply(tmp_path, raw, env, check=True, reverse=reverse)
        verify.apply(tmp_path, raw, env, reverse=reverse)
        assert path.read_text() == (before if reverse else after)


def test_linux_disabled_backend_and_protocol_are_not_reenabled():
    data = fixture()
    for layer in ("core", "linux_core", "macos", "windows"):
        assert not any(entry["target"] == SOURCE for entry in data["predecessors"][layer])
        assert not any(entry["target"] == SOURCE for entry in data["source"]["overlay_coverage"][layer]["omitted"])
    report = json.loads((ROOT / "patches/chromium157-verification/verified.json").read_bytes())
    assert report["status"] == "verified" and len(report["cases"]) == 6
    review = json.loads((ROOT / "patches/chromium157-verification/review.json").read_bytes())
    pin = review["pvt_factory_evidence"]["source_files"][SOURCE]
    for case in report["cases"]:
        assert case["status"] == case["roundtrip"]["status"] == "verified"
        if case["platform"] == "linux":
            assert case["roundtrip"]["patch_count"] == 242
            entry = case["patches"][-1]
            assert entry["path"] == PATCH
            assert entry["preimages"] == {SOURCE: pin["pre_sha256"]}
            assert entry["postimages"] == {SOURCE: pin["post_sha256"]}
            assert [Path(entry["path"]).name[:4] for entry in case["patches"][229:232]] == ["0239", "0240", "0241"]
    platform = (ROOT / "build/linux/157.0.8103.78/patches/chromium-156-remove-private-verification-tokens.patch").read_text()
    assert '-    "//chrome/browser/private_verification_tokens",' in platform
    assert '-void StorageHandler::GetPrivateVerificationTokens(' in platform
    assert '-  PrivateVerificationTokensServiceFactory::GetInstance();' in platform


def test_old_storage_profiles_reject233_without_helper_changes():
    from tools import linux157_storage as arm
    from tools import linux157_x64_storage as x64

    selected, raw = selection.select(ROOT, "linux")
    for profile in (arm.load(ROOT), x64.load(ROOT)):
        with pytest.raises(ValueError, match="unchanged full old229"):
            arm.validate_additions(selected, raw, profile, ROOT)
    for name in ("tools/linux157_storage.py", "tools/linux157_x64_storage.py",
                 "build/linux/157.0.8103.78/arm64-storage-checkpoint.json",
                 "build/linux/157.0.8103.78/x64-storage-checkpoint.json"):
        assert (ROOT / name).read_bytes() == subprocess.check_output([
            "git", "-C", str(ROOT), "show", BASELINE + ":" + name])


def test_native_object_symbol_audit_and_honest_final_link_status():
    supplied = os.environ.get("CHROMIX_PVT_FACTORY_EVIDENCE")
    if not supplied:
        pytest.skip("set CHROMIX_PVT_FACTORY_EVIDENCE to the actual latest x64 donor audit")
    data = json.loads((Path(supplied) / "native.json").read_bytes())
    assert data["donor_run"] == 44712280809
    assert data["object_returncode"] == 0
    assert data["before_undefined_pvt"]
    assert data["after_undefined_pvt"] == []
    audit = json.loads((Path(supplied).parent / "symbol-classification.json").read_bytes())
    assert audit["total_link_inputs"] == 23608
    assert audit["settings_input_count"] == 47
    assert audit["settings_pvt_symbols"] == audit["service_factory_symbols"] == []
    assert audit["external_references_to_policy_member"] == []
    assert audit["whole_archive_used"] is False
    assert len(audit["unresolved_archive_member_only"]) == 2
    assert all("PrivateVerificationTokensIssuerConfig::" in entry["symbol"]
               for entry in audit["unresolved_archive_member_only"])
    assert data["link_attempted"] is True
    assert data["link_status"] in ("passed", "resource-blocked", "failed")
    if data["link_status"] == "passed":
        assert data["link_returncode"] == 0 and data["chrome_bytes"] > 0
    else:
        assert data["link_returncode"] != 0
