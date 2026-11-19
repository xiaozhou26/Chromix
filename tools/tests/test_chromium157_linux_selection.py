"""Production Linux157 composition, replay authentication, and historical fail-closed tests."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from tools import patch_selection as selection
from tools.tests.test_patch_selection import clone_repo, set_platform_pins

ROOT = Path(__file__).resolve().parents[2]
LINUX_IMPLEMENTATION = "501dd12cc295285ef6206cb3dd3786168a3820d9"
MANIFEST = json.loads((ROOT / selection.MANIFEST157).read_bytes())
DOWNSTREAM = MANIFEST["platforms"]["linux"]["downstream"]


def blob(name, ref=LINUX_IMPLEMENTATION):
    return subprocess.check_output(["git", "-C", str(ROOT), "show", ref + ":" + name])


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
    set_platform_pins(repo, "linux", selection.VERSION157, selection.CORE157,
                      selection.PLATFORM157_COMMITS["linux"])
    path = repo / "build/ungoogled-revisions.psd1"
    path.write_text(path.read_text().replace(
        'UngoogledLinuxVersion = "157.0.8103.78-1"',
        'UngoogledLinuxVersion = "156.0.8078.79-1"'))
    return repo


def activate(repo, monkeypatch):
    assert selection.LINUX157_VALIDATED_REVIEW_SHA256 == selection.REVIEW157_SHA256
    assert selection.validate_manifest157(json.loads((repo / selection.MANIFEST157).read_bytes()))


def test_real_fresh_recipe_does_not_activate_pending_review(linux_repo, monkeypatch):
    pins = selection.load_pins(linux_repo, "linux")
    assert pins["ChromiumVersion"] == selection.VERSION157
    assert pins["UngoogledLinuxVersion"] == selection.VERSION156 + "-1"
    old_review = subprocess.check_output(["git", "-C", str(ROOT), "show",
        "63dd0fc5934b7c27d4a1e3439a024a96ad96bba0:" + selection.REVIEW157])
    store(linux_repo, selection.REVIEW157, old_review)
    monkeypatch.setattr(selection, "REVIEW157_SHA256", selection.digest(old_review))
    monkeypatch.setattr(selection, "LINUX157_VALIDATED_REVIEW_SHA256", None)
    with pytest.raises(selection.SelectionError, match="Linux blocked"):
        selection.select(linux_repo, "linux")


def test_integrated_linux_selection_matches_real_recipe_and_parser():
    from tools import linux157_source
    pins = selection.load_pins(ROOT, "linux")
    identity, patches = selection.select(ROOT, "linux")
    assert pins["ChromiumVersion"] == selection.VERSION157
    assert pins["UngoogledLinuxVersion"] == selection.VERSION156 + "-1"
    assert len(patches) == 228
    assert identity["selection"]["downstream"]["recipe"] == linux157_source.recipe(ROOT)
    assert identity["selection"]["core_gitlink"] == selection.CORE156
    assert identity["selection"]["effective_core_commit"] == selection.CORE157


def test_exact_recipe_source_and_nine_override_group_match_actual_implementation(linux_repo):
    assert selection.validate_linux157_downstream(DOWNSTREAM) == DOWNSTREAM
    source = json.loads((linux_repo / DOWNSTREAM["source_manifest"]["path"]).read_bytes())
    assert DOWNSTREAM["platform_overrides"] == [
        {key: entry[key] for key in ("upstream_path", "upstream_sha256", "path", "sha256")}
        for entry in source["patches"]]
    assert len(DOWNSTREAM["platform_overrides"]) == 9
    assert DOWNSTREAM["platform_override_group_sha256"] == selection.canonical_digest(DOWNSTREAM["platform_overrides"])
    assert DOWNSTREAM["recipe"]["sha256"] == "131f3d59a27e1db157e1d0a8bfdbfcdc5b70a7c10bc7f0c4fa5936a6841cc692"


def test_precise_host_go_helper_and_previous_unbound_helper_rejected(linux_repo):
    helper = "tools/linux157_build.py"
    assert DOWNSTREAM["recipe"]["inputs"][helper] == "f5c30730663ccb70b5b04505053296c88d39df683f010b06aa792d3d8ab53315"
    delta = DOWNSTREAM["validation"]["recipe_integration"]["changed_inputs"][helper]
    assert delta == {
        "before_sha256": "048e644354c26812c5b0db71e272c53066eb6f1b7d19d68821bb4329cfdb6503",
        "sha256": DOWNSTREAM["recipe"]["inputs"][helper],
    }
    old = blob(helper, "98f97fb23a97b41140c8a2d50e72ad7ad403a9b8")
    assert selection.digest(old) == delta["before_sha256"]
    store(linux_repo, helper, old)
    with pytest.raises(selection.SelectionError, match="changed Linux157 recipe input"):
        selection.select(linux_repo, "linux")


@pytest.mark.parametrize("helper", ["tools/linux157_source.py", "tools/linux157_toolchain.py",
                                    "tools/linux157_build.py"])
def test_previous_pruning_recipe_helper_is_rejected(linux_repo, helper):
    delta = DOWNSTREAM["validation"]["recipe_integration"]["pruning_recovery"]
    assert delta["recipe_sha256"] == DOWNSTREAM["validation"]["recipe_integration"]["gn_recovery"]["previous_recipe_sha256"]
    old = blob(helper, delta["baseline_commit"])
    assert selection.digest(old) == delta["changed_inputs"][helper]["before_sha256"]
    assert selection.digest(blob(helper, delta["implementation_commit"])) == delta["changed_inputs"][helper]["sha256"]
    store(linux_repo, helper, old)
    with pytest.raises(selection.SelectionError, match="changed Linux157 recipe input"):
        selection.select(linux_repo, "linux")


def test_gn_recovery_binds_exact_new_inputs_and_preserves_donor_patch_identity(linux_repo):
    from tools import linux157_checkpoint as checkpoint
    from tools import linux157_gn as gn
    delta = DOWNSTREAM["validation"]["recipe_integration"]["gn_recovery"]
    assert delta["recipe_sha256"] == DOWNSTREAM["validation"]["recipe_integration"]["libclang_recovery"]["previous_recipe_sha256"]
    assert set(delta["changed_inputs"]) == checkpoint.REVIEWED_CHANGED | checkpoint.REVIEWED_ADDED
    assert delta["source_manifest_unchanged"] == DOWNSTREAM["source_manifest"]
    assert delta["host_gn"]["compiler_binaries"] == gn.BINARIES
    assert delta["host_gn"]["bootstrap_inputs"] == gn.INPUTS
    assert delta["gn_args"]["removed_lines"] == [
        "rust_prebuilt_stdlib = false\n", "use_chromium_rust_toolchain = true\n",
        "v8_use_metagen_instance_types=false\n"]
    selected, _ = selection.select(linux_repo, "linux")
    for arch, donor in checkpoint.load(linux_repo)["donors"].items():
        from tools import linux157_postgn as postgn
        historical = postgn.load(linux_repo)["donors"][arch]["source_receipt"]["recipe"]
        checkpoint.validate_recipe_delta(donor["source_receipt"]["recipe"], historical)
        with pytest.raises(ValueError, match="delta"):
            checkpoint.validate_recipe_delta(donor["source_receipt"]["recipe"], DOWNSTREAM["recipe"])
        original = donor["source_receipt"]["chromix_selection"]
        assert original["patches"] == selected["patches"][:-2]
        with pytest.raises(ValueError, match="cannot change the 226 patches"):
            checkpoint.compatible_selection(original, selected)
        assert delta["gn_args"]["identities"][arch]["original_sha256"] == donor["files"]["src/out/Chromix/args.gn"]
        assert delta["donors"][arch]["artifact"] == donor["artifact"]


@pytest.mark.parametrize("helper", ["tools/linux157_source.py", "tools/linux157_build.py"])
def test_pre_gn_helper_rollback_is_rejected(linux_repo, helper):
    delta = DOWNSTREAM["validation"]["recipe_integration"]["gn_recovery"]
    old = blob(helper, delta["baseline_commit"])
    assert selection.digest(old) == delta["changed_inputs"][helper]["before_sha256"]
    store(linux_repo, helper, old)
    with pytest.raises(selection.SelectionError, match="changed Linux157 recipe input"):
        selection.select(linux_repo, "linux")


@pytest.mark.parametrize("mutation", ["args-line", "args-hash", "donor", "compiler", "new-input"])
def test_unknown_gn_recovery_scope_is_rejected(mutation):
    manifest = copy.deepcopy(MANIFEST)
    downstream = manifest["platforms"]["linux"]["downstream"]
    delta = downstream["validation"]["recipe_integration"]["gn_recovery"]
    if mutation == "args-line":
        delta["gn_args"]["removed_lines"].append("chrome_pgo_phase=0\n")
    elif mutation == "args-hash":
        delta["gn_args"]["identities"]["x64"]["original_sha256"] = "0" * 64
    elif mutation == "donor":
        delta["donors"]["arm64"]["run_id"] += 1
    elif mutation == "compiler":
        delta["host_gn"]["compiler_binaries"]["clang"] = "0" * 64
    else:
        downstream["recipe"]["inputs"]["tools/unreviewed_gn.py"] = "0" * 64
    with pytest.raises(selection.SelectionError, match="downstream recipe"):
        selection.validate_manifest157(manifest)


def test_libclang_scope_and_latest_postgn_donors_are_exact(linux_repo):
    from tools import linux157_checkpoint as prior
    from tools import linux157_libclang as libclang
    from tools import linux157_postgn as postgn
    delta = DOWNSTREAM["validation"]["recipe_integration"]["libclang_recovery"]
    assert delta["recipe_sha256"] == DOWNSTREAM["recipe"]["sha256"]
    assert set(delta["changed_inputs"]) == postgn.CHANGED | postgn.ADDED
    assert delta["post_gn_manifest"] == {"path": postgn.MANIFEST, "sha256": postgn.MANIFEST_SHA256}
    assert delta["libclang_manifest"] == {"path": libclang.MANIFEST, "sha256": libclang.MANIFEST_SHA256}
    package = libclang.load(linux_repo)
    assert delta["package"]["download"] == package["download"]
    assert delta["package"]["files_sha256"] == selection.canonical_digest(package["files"])
    assert len(package["files"]) == 16
    for name, expected in delta["preserved_inputs"].items():
        assert selection.digest(blob(name, delta["baseline_commit"])) == expected
        assert DOWNSTREAM["recipe"]["inputs"][name] == expected
    selected, _ = selection.select(linux_repo, "linux")
    for arch, donor in postgn.load(linux_repo)["donors"].items():
        postgn.delta(donor["source_receipt"]["recipe"], DOWNSTREAM["recipe"])
        original = donor["source_receipt"]["chromix_selection"]
        assert original["patches"] == selected["patches"][:-2]
        with pytest.raises(ValueError, match="cannot change the 226 patches"):
            prior.compatible_selection(original, selected)
        assert delta["donors"][arch]["artifact"] == donor["artifact"]
        assert delta["donors"][arch]["workflow_path"] == donor["workflow_path"]
        assert donor["files"]["src/out/Chromix/build.ninja"]


@pytest.mark.parametrize("helper", ["tools/linux157_source.py", "tools/linux157_build.py"])
def test_pre_libclang_helper_rollback_is_rejected(linux_repo, helper):
    delta = DOWNSTREAM["validation"]["recipe_integration"]["libclang_recovery"]
    old = blob(helper, delta["baseline_commit"])
    assert selection.digest(old) == delta["changed_inputs"][helper]["before_sha256"]
    store(linux_repo, helper, old)
    with pytest.raises(selection.SelectionError, match="changed Linux157 recipe input"):
        selection.select(linux_repo, "linux")


@pytest.mark.parametrize("mutation", ["package", "postgn", "old-pregn", "donor", "bindings-only", "extra-helper"])
def test_unknown_libclang_input_scope_is_rejected(mutation):
    manifest = copy.deepcopy(MANIFEST)
    downstream = manifest["platforms"]["linux"]["downstream"]
    delta = downstream["validation"]["recipe_integration"]["libclang_recovery"]
    if mutation == "package":
        delta["package"]["download"]["sha256"] = "0" * 64
    elif mutation == "postgn":
        delta["post_gn_manifest"]["sha256"] = "0" * 64
    elif mutation == "old-pregn":
        downstream["recipe"]["inputs"]["build/linux/157.0.8103.78/pre-gn-checkpoints.json"] = "0" * 64
    elif mutation == "donor":
        delta["donors"]["arm64"]["run_id"] += 1
    elif mutation == "bindings-only":
        delta["package"]["file_count"] = 2
    else:
        downstream["recipe"]["inputs"]["tools/unreviewed_libclang.py"] = "0" * 64
    with pytest.raises(selection.SelectionError, match="downstream recipe"):
        selection.validate_manifest157(manifest)


def test_reviewed_activation_preserves_real_origin_and_effective_core(linux_repo, monkeypatch):
    activate(linux_repo, monkeypatch)
    identity, patches = selection.select(linux_repo, "linux")
    chosen = identity["selection"]
    assert len(patches) == 228
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


@pytest.mark.parametrize("mutation", ["none", "wrong-hash", "old-review", "recipe", "source"])
def test_review_activation_cannot_be_partial_or_claim_unverified_stack(linux_repo, monkeypatch, mutation):
    if mutation in ("none", "wrong-hash"):
        monkeypatch.setattr(selection, "LINUX157_VALIDATED_REVIEW_SHA256", None if mutation == "none" else "0" * 64)
    else:
        review = json.loads((linux_repo / selection.REVIEW157).read_bytes())
        if mutation == "old-review":
            del review["linux_downstream"]
        elif mutation == "recipe":
            review["linux_downstream"]["recipe_sha256"] = "0" * 64
        else:
            review["linux_downstream"]["source_manifest"]["sha256"] = "0" * 64
        store(linux_repo, selection.REVIEW157, json.dumps(review).encode())
    with pytest.raises(selection.SelectionError):
        selection.select(linux_repo, "linux")


@pytest.mark.parametrize("mutation", ["tamper", "missing", "symlink"])
def test_final_replay_report_is_required_and_hash_bound(linux_repo, mutation):
    path = linux_repo / DOWNSTREAM["validation"]["report"]["path"]
    if mutation == "tamper":
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "missing":
        path.unlink()
    else:
        path.unlink()
        path.symlink_to(ROOT / DOWNSTREAM["validation"]["report"]["path"])
    with pytest.raises(selection.SelectionError):
        selection.select(linux_repo, "linux")


@pytest.mark.parametrize("mutation", ["failed", "arch", "count", "order", "platform-order", "raw-only", "runtime"])
def test_even_reauthenticated_report_must_cover_exact_stack(linux_repo, monkeypatch, mutation):
    manifest = copy.deepcopy(MANIFEST)
    downstream = manifest["platforms"]["linux"]["downstream"]
    path = linux_repo / downstream["validation"]["report"]["path"]
    report = json.loads(path.read_bytes())
    case = report["cases"][0]
    if mutation == "failed":
        case["status"] = "failed"
    elif mutation == "arch":
        case["architectures"] = ["x64"]
    elif mutation == "count":
        case["roundtrip"]["patch_count"] = 234
    elif mutation == "order":
        case["patches"].reverse()
    elif mutation == "platform-order":
        case["predecessors"].reverse()
    elif mutation == "raw-only":
        report["cases"].pop(1)
    else:
        case["runtime_verified"] = True
    raw = json.dumps(report).encode()
    path.write_bytes(raw)
    downstream["validation"]["report"]["sha256"] = selection.digest(raw)
    raw = json.dumps(manifest).encode()
    store(linux_repo, selection.MANIFEST157, raw)
    monkeypatch.setattr(selection, "MANIFEST157_SHA256", selection.digest(raw))
    monkeypatch.setattr(selection, "LINUX157_DOWNSTREAM_SHA256", selection.canonical_digest(downstream))
    with pytest.raises(selection.SelectionError):
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
