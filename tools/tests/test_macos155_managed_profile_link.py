"""Pinned macOS155 real-controller inclusion; GN probes are not full browser links."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import types

import pytest

from tools import apply_restored_patches as arp
from tools import patch_selection as selection
from tools.tests.test_linux155_managed_profile_link import (
    BUILD, CONSUMER, CONTROLLER, FACTORY, SOURCE_LINE, UTILS, source_data,
)
from tools.tests.test_macos152_patch_selection import run_patch
from tools.tests.test_patch_selection import clone_repo, set_platform_pins

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "1a7d4b9c8f4e7c3530534682b4c233275f1f99a2"
PATCH = selection.MACOS155_ADDITIONS_ROOT + "/0225-managed-profile-controller-link.patch"
OVERLAY = ROOT / "tools/tests/fixtures/macos155_managed_profile_overlay.json"
OVERLAY_SHA256 = "a647e4b016d42f4caf83178ae21062b4ac0304d9fa0768c68e438ff7c549d433"


@pytest.fixture(scope="module")
def baseline_selector():
    raw = subprocess.run(["git", "-C", str(ROOT), "show", BASELINE + ":tools/patch_selection.py"],
                         check=True, capture_output=True).stdout
    module = types.ModuleType("tools._baseline_patch_selection")
    module.__package__ = "tools"
    exec(compile(raw, "baseline_patch_selection.py", "exec"), module.__dict__)
    return module


def test_exact_append_and_historical_identity(tmp_path, baseline_selector):
    repo = clone_repo(tmp_path)
    historical, old = baseline_selector.select(repo, "macos")
    current, patches = selection.select(repo, "macos")
    manifest = json.loads((repo / selection.MACOS155_ADDITIONS_MANIFEST).read_bytes())
    assert len(old) == 224 and len(patches) == 225
    assert manifest == {
        "schema_version": 1, "kind": "macos155-managed-profile-controller-link",
        "chromium_version": selection.VERSION155, "core_commit": selection.CORE155,
        "platform_commit": selection.PLATFORM155_COMMITS["macos"],
        "patches": [{"path": PATCH, "sha256": selection.digest((ROOT / PATCH).read_bytes())}],
    }
    assert selection.digest((repo / selection.MACOS155_ADDITIONS_MANIFEST).read_bytes()) == selection.MACOS155_ADDITIONS_SHA256
    assert patches == old + [(PATCH, (ROOT / PATCH).read_bytes())]
    assert current["patches"] == historical["patches"] + manifest["patches"]
    assert {k: v for k, v in current["selection"].items() if k != "additions"} == historical["selection"]
    assert current["selection"]["additions"]["manifest"] == {
        "path": selection.MACOS155_ADDITIONS_MANIFEST, "sha256": selection.MACOS155_ADDITIONS_SHA256}
    assert current["selection"]["additions"]["patches"] == manifest["patches"]
    assert (ROOT / PATCH).read_bytes() == (ROOT / selection.LINUX155_ADDITIONS_ROOT / Path(PATCH).name).read_bytes()
    assert arp.transform_patch((ROOT / PATCH).read_bytes(), set(), [])[1] == [(BUILD, "modify", None)]
    for style in ("posix", "windows"):
        assert selection.preparation_key(repo, "macos", style) != baseline_selector.preparation_key(repo, "macos", style)
    (repo / selection.MACOS155_ADDITIONS_MANIFEST).unlink()
    shutil.rmtree(repo / selection.MACOS155_ADDITIONS_ROOT)
    assert selection.select(repo, "macos") == (historical, old)
    for style in ("posix", "windows"):
        assert selection.preparation_key(repo, "macos", style) == baseline_selector.preparation_key(repo, "macos", style)


def test_apply_receipt_binds_only_the_gn_addition(tmp_path, source_data):
    repo = clone_repo(tmp_path)
    core = tmp_path / "core"
    core.mkdir()
    (core / "chromium_version.txt").write_text(selection.VERSION155 + "\n")
    for name in ("domain_regex.list", "domain_substitution.list"):
        (core / name).write_text(source_data["tooling"]["core/" + name]["text"])
    identity, patches, _ = arp._load(repo, core, tmp_path / "mac", "macos")
    selected, _ = selection.select(repo, "macos", core=core)
    assert len(patches) == 225 and patches[-1][0] == PATCH
    assert patches[-1][2] == [(BUILD, "modify", None)]
    assert identity["selection"] == selected["selection"]
    assert identity["series"]["sha256"] == selected["series_sha256"]
    assert identity["series"]["patches"] == selected["patches"]


@pytest.mark.parametrize("platform,version,core,commit,count", [
    ("macos", selection.VERSION, selection.MACOS154_CORE, selection.PLATFORM_COMMITS["macos"], 225),
    ("macos", selection.MACOS152_VERSION, selection.MACOS152_CORE, selection.MACOS152_PLATFORM, 224),
    ("windows", selection.VERSION155, selection.CORE155, selection.PLATFORM155_COMMITS["windows"], 231),
    ("linux", selection.VERSION155, selection.CORE155, selection.PLATFORM155_COMMITS["linux"], 226),
    ("windows", selection.VERSION, selection.CORE, selection.PLATFORM_COMMITS["windows"], 224),
    ("linux", selection.VERSION, selection.CORE, selection.PLATFORM_COMMITS["linux"], 224),
    ("linux", "153.0.8010.36", selection.CORE, selection.PLATFORM_COMMITS["linux"], 224),
])
def test_other_manifests_and_keys_unchanged(tmp_path, baseline_selector, platform, version, core, commit, count):
    repo = clone_repo(tmp_path)
    set_platform_pins(repo, platform, version, core, commit)
    expected = baseline_selector.select(repo, platform)
    assert len(expected[1]) == count
    assert selection.select(repo, platform) == expected
    keys = {style: baseline_selector.preparation_key(repo, platform, style) for style in ("posix", "windows")}
    (repo / selection.MACOS155_ADDITIONS_MANIFEST).write_text("not an input to this platform/version")
    shutil.rmtree(repo / selection.MACOS155_ADDITIONS_ROOT)
    assert selection.select(repo, platform) == expected
    assert {style: selection.preparation_key(repo, platform, style) for style in keys} == keys


@pytest.mark.parametrize("mutation", ["patch-missing", "patch-changed", "extra", "subdirectory",
                                      "manifest-missing", "manifest-changed", "directory-missing",
                                      "patch-link", "manifest-link", "directory-link"])
def test_macos155_additions_fail_closed(tmp_path, mutation):
    repo = clone_repo(tmp_path)
    patch, manifest, directory = (repo / PATCH, repo / selection.MACOS155_ADDITIONS_MANIFEST,
                                  repo / selection.MACOS155_ADDITIONS_ROOT)
    if mutation == "patch-missing":
        patch.unlink()
    elif mutation == "patch-changed":
        patch.write_bytes(patch.read_bytes() + b"\n")
    elif mutation == "extra":
        (directory / "unexpected.patch").write_text("unexpected")
    elif mutation == "subdirectory":
        (directory / "extra").mkdir()
    elif mutation == "manifest-missing":
        manifest.unlink()
    elif mutation == "manifest-changed":
        manifest.write_bytes(manifest.read_bytes() + b"\n")
    elif mutation == "directory-missing":
        shutil.rmtree(directory)
    else:
        path = {"patch-link": patch, "manifest-link": manifest, "directory-link": directory}[mutation]
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        path.symlink_to(ROOT / path.relative_to(repo), target_is_directory=mutation == "directory-link")
    with pytest.raises(selection.SelectionError):
        selection.select(repo, "macos")
    with pytest.raises(selection.SelectionError):
        selection.preparation_key(repo, "macos", "posix")


@pytest.mark.parametrize("field", ["MacOSUngoogledCommit", "UngoogledMacOSCommit"])
def test_macos155_addition_cannot_bypass_pins(tmp_path, field):
    repo = clone_repo(tmp_path)
    path = repo / "build/ungoogled-revisions.psd1"
    path.write_text(re.sub(rf'(?m)^(  {field} = )"[^"]+"', rf'\g<1>"{"0" * 40}"', path.read_text()))
    with pytest.raises(selection.SelectionError, match="core/platform pins"):
        selection.select(repo, "macos")


@pytest.mark.parametrize("which", ["src", "core"])
def test_macos155_addition_requires_exact_source_version(tmp_path, which):
    repo = clone_repo(tmp_path)
    directory = tmp_path / which
    path = directory / ("chrome/VERSION" if which == "src" else "chromium_version.txt")
    path.parent.mkdir(parents=True)
    path.write_text("MAJOR=154\nMINOR=0\nBUILD=8037\nPATCH=97\n" if which == "src" else selection.VERSION + "\n")
    with pytest.raises(selection.SelectionError, match="version"):
        selection.select(repo, "macos", **{which: directory})


def test_independent_mac_overlay_inventory():
    location = os.environ.get("CHROMIX_MACOS155_SOURCES")
    if not location:
        pytest.skip("set CHROMIX_MACOS155_SOURCES to authenticate downloaded mac overlay bytes")
    overlay = json.loads(OVERLAY.read_bytes())
    mac = Path(location) / "mac"
    assert (mac / "patches/series").read_text() == overlay["series"]
    for name, entry in overlay["patches"].items():
        raw = (mac / "patches" / name).read_bytes()
        assert selection.digest(raw) == entry["sha256"]
        assert sorted(set(re.findall(r"^\+\+\+ b/(.+)$", raw.decode(), re.M))) == entry["targets"]


@pytest.fixture
def mac_sources(tmp_path, source_data):
    assert selection.digest(OVERLAY.read_bytes()) == OVERLAY_SHA256
    overlay = json.loads(OVERLAY.read_bytes())
    assert overlay["platform_commit"] == selection.PLATFORM155_COMMITS["macos"]
    assert overlay["core_gitlink"] == selection.CORE155
    assert selection.digest(overlay["series"].encode()) == overlay["series_sha256"]
    assert set(overlay["series"].splitlines()) == set(overlay["patches"])
    assert not set(source_data["sources"]) & {
        target for entry in overlay["patches"].values() for target in entry["targets"]}
    src = tmp_path / "src"
    for name, entry in source_data["sources"].items():
        path = src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(entry["text"])
    applied = []
    for line in source_data["tooling"]["core/patches/series"]["text"].splitlines():
        name = line.split("#", 1)[0].strip()
        if not name:
            continue
        entry = source_data["tooling"]["core/patches/" + name]
        targets = set(entry["targets"]) & source_data["sources"].keys()
        if not targets:
            continue
        sections = [section for section in re.split(r"(?m)(?=^--- a/)", entry["text"])
                    if (match := re.match(r"--- a/([^\n]+)", section)) and match[1] in targets]
        patch = tmp_path / "predecessor.patch"
        patch.write_text("".join(sections))
        result = run_patch(src, patch)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not re.search(r"fuzz|offset|FAILED", result.stdout + result.stderr, re.I)
        applied.append(name)
    assert applied == ["core/ungoogled-chromium/fix-building-without-safebrowsing.patch",
                       "core/ungoogled-chromium/remove-unused-preferences-fields.patch"]
    return src


@pytest.mark.parametrize("substituted", [False, True])
def test_actual_15539_source_replay_and_preserved_gates(mac_sources, source_data, tmp_path, substituted):
    rules = arp._rules(source_data["tooling"]["core/domain_regex.list"]["text"].encode())
    listed = set(source_data["tooling"]["core/domain_substitution.list"]["text"].splitlines())
    assert BUILD not in listed and CONTROLLER + ".cc" not in listed
    if substituted:
        for name in source_data["sources"].keys() & listed:
            path = mac_sources / name
            path.write_text(arp._substitute(path.read_text(), rules))
    before = {name: (mac_sources / name).read_bytes() for name in source_data["sources"]}
    assert SOURCE_LINE.encode() not in before[BUILD]
    for name in (CONSUMER, CONTROLLER + ".cc"):
        assert FACTORY.encode() in before[name] and b"CHECK_IS_TEST();" in before[name]
    assert b"  ShowManagementDisclaimer();" not in before[CONTROLLER + ".cc"]
    assert b"return SigninUIError::SigninDisallowed(email);" in before[UTILS]
    assert not re.search(rb"^#if.*(?:IS_MAC|IS_LINUX|ENABLE_BOUND_SESSION|TEST_ONLY)",
                         before[CONTROLLER + ".cc"] + before[CONSUMER], re.M)
    patch, entries = arp.transform_patch((ROOT / PATCH).read_bytes(), listed, rules)
    assert patch == (ROOT / PATCH).read_bytes() and entries == [(BUILD, "modify", None)]
    result = run_patch(mac_sources, ROOT / PATCH)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not re.search(r"fuzz|offset|FAILED", result.stdout + result.stderr, re.I)
    after = (mac_sources / BUILD).read_bytes()
    assert after.count(SOURCE_LINE.encode()) == 1
    assert after.replace(SOURCE_LINE.encode(), b"") == before[BUILD]
    assert after.decode() == source_data["sources"][BUILD]["text"].replace(
        '    "data_protection/view_source_navigation_throttle.cc",\n', "")
    for name in source_data["sources"].keys() - {BUILD}:
        assert (mac_sources / name).read_bytes() == before[name]
    result = run_patch(mac_sources, ROOT / PATCH, reverse=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (mac_sources / BUILD).read_bytes() == before[BUILD]


def impl_body(build):
    start = build.index('source_set("impl") {') + len('source_set("impl") {')
    depth = 1
    for end in range(start, len(build)):
        depth += (build[end] == "{") - (build[end] == "}")
        if depth == 0:
            return build[start:end]
    raise AssertionError("unclosed enterprise impl")


@pytest.mark.parametrize("platform,arch,included", [
    ("mac", "x64", True), ("mac", "arm64", True),
    ("linux", "x64", True), ("win", "x64", True),
    ("android", "arm64", False), ("chromeos", "x64", False),
])
@pytest.mark.parametrize("features", [False, True])
def test_real_gn_desktop_source_gate(mac_sources, tmp_path, platform, arch, included, features):
    gn = os.environ.get("CHROMIX_GN") or shutil.which("gn")
    if not gn:
        pytest.skip("set CHROMIX_GN to run real GN evaluation of the unchanged impl body")
    result = run_patch(mac_sources, ROOT / PATCH)
    assert result.returncode == 0, result.stdout + result.stderr
    body = impl_body((mac_sources / BUILD).read_text())
    harness = tmp_path / "gn"
    harness.mkdir()
    (harness / ".gn").write_text('buildconfig = "//BUILDCONFIG.gn"\n')
    (harness / "BUILDCONFIG.gn").write_text('set_default_toolchain("//:toolchain")\n')
    flags = {name: name == "is_" + platform for name in ("is_mac", "is_win", "is_linux", "is_android", "is_chromeos")}
    for name in ("enterprise_watermark", "enterprise_data_controls", "enterprise_client_certificates",
                 "enterprise_cloud_content_analysis", "enterprise_local_content_analysis",
                 "enterprise_content_analysis", "enable_print_preview", "enable_extensions_core"):
        flags[name] = features
    config = "".join(f"{key} = {str(value).lower()}\n" for key, value in flags.items())
    config += f"safe_browsing_mode = {int(features)}\n"
    # The real impl body runs unchanged; only its resulting lists are captured, not compiled.
    (harness / "BUILD.gn").write_text(config + f'current_cpu = "{arch}"\n' + '''
    template("capture_impl") {
      write_file("$root_build_dir/sources.json", invoker.sources, "json")
      write_file("$root_build_dir/deps.json", invoker.deps, "json")
      write_file("$root_build_dir/public_deps.json", invoker.public_deps, "json")
      group(target_name) {}
    }
    toolchain("toolchain") {
      tool("stamp") { command = "touch {{output}}" }
    }
    capture_impl("impl") {''' + body + "\n}\n")
    result = subprocess.run([gn, "gen", "out"], cwd=harness, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    sources = json.loads((harness / "out/sources.json").read_text())
    deps = json.loads((harness / "out/deps.json").read_text())
    assert sources.count("signin/managed_profile_creation_controller.cc") == int(included)
    assert sources.count("signin/profile_management_disclaimer_service.cc") == int(included)
    if included:
        assert "//chrome/browser/ui/browser_window:create_browser_window" in deps
        for dep in ("//chrome/browser/signin", "//chrome/browser/themes", "//chrome/browser/search_engine_choice",
                    "//chrome/browser/ui/signin", "//chrome/browser/ui/webui/signin:signin_utils",
                    "//chrome/browser/ui/window_feature_controller", "//chrome/browser/profiles:profile_util"):
            assert dep in deps
