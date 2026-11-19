"""Exact Linux157 recipe authentication; final patch-review activation is test-only."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from tools import patch_selection as selection
from tools.tests.test_patch_selection import clone_repo, set_platform_pins

ROOT = Path(__file__).resolve().parents[2]
LINUX_IMPLEMENTATION = "d1edf19bd5074f9473d89dc91a6c6d833d15ecc3"
MANIFEST = json.loads((ROOT / selection.MANIFEST157).read_bytes())
DOWNSTREAM = MANIFEST["platforms"]["linux"]["downstream"]


def blob(name):
    return subprocess.check_output(["git", "-C", str(ROOT), "show", LINUX_IMPLEMENTATION + ":" + name])


def store(repo, name, raw):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


@pytest.fixture
def linux_repo(tmp_path, monkeypatch):
    repo = clone_repo(tmp_path)
    shutil.copy2(ROOT / selection.MANIFEST157, repo / selection.MANIFEST157)
    for name, expected in DOWNSTREAM["recipe"]["inputs"].items():
        raw = blob(name)
        assert selection.digest(raw) == expected
        store(repo, name, raw)
    # Exercise the Linux agent's actual pin parser without editing production platform tools.
    store(repo, "tools/platform_pins.py", blob("tools/platform_pins.py"))
    modules = {}
    for name in ("linux157_source", "platform_pins"):
        spec = importlib.util.spec_from_file_location(name, repo / ("tools/" + name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules[name] = module
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(selection, "load_pins", modules["platform_pins"].load_pins)
    set_platform_pins(repo, "linux", selection.VERSION157, selection.CORE157,
                      selection.PLATFORM157_COMMITS["linux"])
    path = repo / "build/ungoogled-revisions.psd1"
    path.write_text(path.read_text().replace(
        'UngoogledLinuxVersion = "157.0.8103.78-1"',
        'UngoogledLinuxVersion = "156.0.8078.79-1"'))
    return repo


def activate(repo, monkeypatch):
    """Synthetic review tests admission logic, not evidence of a real full-stack replay."""
    review = json.loads((repo / selection.REVIEW157).read_bytes())
    review["linux_downstream_validation"] = {
        "status": "validated", "scope": "source-replay",
        "recipe_sha256": DOWNSTREAM["recipe"]["sha256"],
        "source_manifest": DOWNSTREAM["source_manifest"],
        "platform_override_group_sha256": DOWNSTREAM["platform_override_group_sha256"],
        "patch_count": 226, "architectures": ["x64", "arm64"],
    }
    raw = (json.dumps(review, indent=2) + "\n").encode()
    store(repo, selection.REVIEW157, raw)
    monkeypatch.setattr(selection, "REVIEW157_SHA256", selection.digest(raw))
    monkeypatch.setattr(selection, "LINUX157_VALIDATED_REVIEW_SHA256", selection.digest(raw))
    monkeypatch.setitem(selection.PLATFORM157_STATUS, "linux", "reviewed-downstream-patches")
    manifest = copy.deepcopy(MANIFEST)
    manifest["platforms"]["linux"]["selection_status"] = "reviewed-downstream-patches"
    raw = (json.dumps(manifest, indent=2) + "\n").encode()
    store(repo, selection.MANIFEST157, raw)
    monkeypatch.setattr(selection, "MANIFEST157_SHA256", selection.digest(raw))


def test_real_fresh_recipe_does_not_activate_pending_review(linux_repo):
    pins = selection.load_pins(linux_repo, "linux")
    assert pins["ChromiumVersion"] == selection.VERSION157
    assert pins["UngoogledLinuxVersion"] == selection.VERSION156 + "-1"
    with pytest.raises(selection.SelectionError, match="Linux blocked"):
        selection.select(linux_repo, "linux")
    assert selection.LINUX157_VALIDATED_REVIEW_SHA256 is None


def test_exact_recipe_source_and_nine_override_group_match_actual_implementation(linux_repo):
    assert selection.validate_linux157_downstream(DOWNSTREAM) == DOWNSTREAM
    source = json.loads((linux_repo / DOWNSTREAM["source_manifest"]["path"]).read_bytes())
    assert DOWNSTREAM["platform_overrides"] == [
        {key: entry[key] for key in ("upstream_path", "upstream_sha256", "path", "sha256")}
        for entry in source["patches"]]
    assert len(DOWNSTREAM["platform_overrides"]) == 9
    assert DOWNSTREAM["platform_override_group_sha256"] == selection.canonical_digest(DOWNSTREAM["platform_overrides"])
    assert DOWNSTREAM["recipe"]["sha256"] == "42d788d91c0ec2345c253044665af7f623501a73c8bdb627a94d1739790aeb11"


def test_reviewed_activation_preserves_real_origin_and_effective_core(linux_repo, monkeypatch):
    activate(linux_repo, monkeypatch)
    identity, patches = selection.select(linux_repo, "linux")
    chosen = identity["selection"]
    assert len(patches) == 226
    assert chosen["core_commit"] == chosen["effective_core_commit"] == selection.CORE157
    assert chosen["core_gitlink"] == selection.CORE156 != selection.CORE157
    assert chosen["platform_version"] == "156.0.8078.79-1"
    assert chosen["platform_commit"] == selection.PLATFORM156_COMMITS["linux"]
    assert chosen["downstream"] == DOWNSTREAM
    assert chosen["downstream"]["recipe"]["official_platform_tag"] is None
    assert chosen["selection_status"] == "reviewed-downstream-patches"
    assert all(not path.startswith("build/linux/") for path, _ in patches)
    for style in ("posix", "windows"):
        assert len(selection.preparation_key(linux_repo, "linux", style)) == 64


@pytest.mark.parametrize("mutation", ["fake-gitlink", "fake-ref", "fake-tag", "wrong-core", "recipe-hash",
                                      "source-hash", "group-hash", "order", "missing", "extra", "helper-hash"])
def test_schema_never_accepts_other_downstream_composition(mutation):
    manifest = copy.deepcopy(MANIFEST)
    value = manifest["platforms"]["linux"]["downstream"]
    recipe = value["recipe"]
    if mutation == "fake-gitlink":
        recipe["platform_core_gitlink"] = selection.CORE157
    elif mutation == "fake-ref":
        recipe["platform_ref"] = selection.VERSION157 + "-1"
    elif mutation == "fake-tag":
        recipe["official_platform_tag"] = selection.VERSION157 + "-1"
    elif mutation == "wrong-core":
        recipe["core_override"] = selection.CORE156
    elif mutation == "recipe-hash":
        recipe["sha256"] = "0" * 64
    elif mutation == "source-hash":
        value["source_manifest"]["sha256"] = "0" * 64
    elif mutation == "group-hash":
        value["platform_override_group_sha256"] = "0" * 64
    elif mutation == "order":
        value["platform_overrides"].reverse()
    elif mutation == "missing":
        value["platform_overrides"].pop()
    elif mutation == "extra":
        value["platform_overrides"].append(value["platform_overrides"][0])
    else:
        recipe["inputs"]["tools/linux157_build.py"] = "0" * 64
    with pytest.raises(selection.SelectionError, match="downstream recipe"):
        selection.validate_manifest157(manifest)


@pytest.mark.parametrize("target", list(DOWNSTREAM["recipe"]["inputs"]))
@pytest.mark.parametrize("mutation", ["tamper", "missing", "symlink"])
def test_activated_linux_rejects_any_changed_recipe_input(linux_repo, monkeypatch, target, mutation):
    activate(linux_repo, monkeypatch)
    path = linux_repo / target
    if mutation == "tamper":
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "missing":
        path.unlink()
    else:
        external = linux_repo / "linked-input"
        path.rename(external)
        path.symlink_to(external)
    with pytest.raises(selection.SelectionError):
        selection.select(linux_repo, "linux")


@pytest.mark.parametrize("mutation", ["extra", "linked-directory"])
def test_activated_linux_requires_exact_platform_inventory(linux_repo, monkeypatch, mutation):
    activate(linux_repo, monkeypatch)
    directory = linux_repo / "build/linux/157.0.8103.78/patches"
    if mutation == "extra":
        (directory / "unreviewed.patch").write_bytes(b"extra")
    else:
        moved = directory.with_name("linked-patches")
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
    with pytest.raises(selection.SelectionError):
        selection.select(linux_repo, "linux")


@pytest.mark.parametrize("mutation", ["none", "wrong-hash", "old-review", "status", "scope", "recipe", "group", "arch", "count"])
def test_review_activation_cannot_be_partial_or_claim_unverified_stack(linux_repo, monkeypatch, mutation):
    activate(linux_repo, monkeypatch)
    if mutation in ("none", "wrong-hash"):
        monkeypatch.setattr(selection, "LINUX157_VALIDATED_REVIEW_SHA256", None if mutation == "none" else "0" * 64)
    else:
        review = json.loads((linux_repo / selection.REVIEW157).read_bytes())
        evidence = review["linux_downstream_validation"]
        if mutation == "old-review":
            del review["linux_downstream_validation"]
        elif mutation == "status":
            evidence["status"] = "pending"
        elif mutation == "scope":
            evidence["scope"] = "platform-only"
        elif mutation == "recipe":
            evidence["recipe_sha256"] = "0" * 64
        elif mutation == "group":
            evidence["platform_override_group_sha256"] = "0" * 64
        elif mutation == "arch":
            evidence["architectures"] = ["x64"]
        else:
            evidence["patch_count"] = 225
        raw = json.dumps(review).encode()
        store(linux_repo, selection.REVIEW157, raw)
        monkeypatch.setattr(selection, "REVIEW157_SHA256", selection.digest(raw))
        monkeypatch.setattr(selection, "LINUX157_VALIDATED_REVIEW_SHA256", selection.digest(raw))
    with pytest.raises(selection.SelectionError, match="Linux blocked"):
        selection.select(linux_repo, "linux")


def test_pending_review_cannot_be_enabled_by_manifest_status_alone(linux_repo, monkeypatch):
    activate(linux_repo, monkeypatch)
    monkeypatch.setattr(selection, "LINUX157_VALIDATED_REVIEW_SHA256", None)
    with pytest.raises(selection.SelectionError, match="Linux blocked"):
        selection.select(linux_repo, "linux")


@pytest.mark.parametrize("field,value", [
    ("LinuxUngoogledCommit", selection.CORE156),
    ("UngoogledLinuxCommit", "0" * 40),
    ("UngoogledLinuxVersion", selection.VERSION157 + "-1"),
])
def test_activated_linux_rejects_wrong_origin_or_effective_pin(linux_repo, monkeypatch, field, value):
    activate(linux_repo, monkeypatch)
    path = linux_repo / "build/ungoogled-revisions.psd1"
    path.write_text(re.sub(rf'(?m)^(  {field} = )"[^"]+"', rf'\g<1>"{value}"', path.read_text()))
    with pytest.raises(selection.SelectionError):
        selection.select(linux_repo, "linux")


def test_mac_and_windows_do_not_require_unselected_linux_recipe_inputs(linux_repo):
    for name in DOWNSTREAM["recipe"]["inputs"]:
        (linux_repo / name).unlink()
    for platform in ("windows", "macos"):
        set_platform_pins(linux_repo, platform, selection.VERSION157, selection.CORE157,
                          selection.PLATFORM157_COMMITS[platform])
        assert len(selection.select(linux_repo, platform)[1]) == selection.PATCH_COUNTS157[platform]
