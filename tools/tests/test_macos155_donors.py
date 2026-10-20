"""Exact macOS155 donors and source compatibility; not native browser acceptance."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from unittest import mock

import pytest

from tools import apply_restored_patches as arp
from tools import fetch_upstream_cache as fetch
from tools import macos_restored_generators as generators
from tools import macos_runtime as runtime
from tools import patch_selection as selection
from tools import restore_upstream_cache as restore
from tools.tests.test_macos154_patch_selection import predecessor_sections
from tools.tests.test_patch_selection import clone_repo, set_platform_pins

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((Path(__file__).parent / "fixtures/macos155_generators.json").read_text())
VERSION, CORE, MAC = generators.PINS155


@pytest.mark.parametrize("arch,artifact,size", [
    ("x64", 11992595839, 12608343117),
    ("arm64", 11971638847, 10999897218),
])
def test_exact_successful_dual_donors(arch, artifact, size):
    pin, identity = fetch.load_manifest("macos", arch, root=ROOT)
    assert pin["run_id"] == 38682673288
    assert pin["chromium_version"] == VERSION
    assert pin["ungoogled_commit"] == CORE
    assert pin["head_sha"] == MAC
    assert pin["artifact"]["id"] == artifact
    assert pin["artifact"]["size_in_bytes"] == size
    assert pin["source_roots"] == ["src"]
    assert pin["artifact"]["inner_archive"] == "build_src.tar.zst"
    assert identity["artifact_digest"] == FIXTURE["donor"][arch]["digest"]
    restored_identity, _, _ = restore.identities(ROOT, "macos", arch)
    for name in (restore.MAC154_ESBUILD_LINK, restore.MAC154_ESBUILD_ALIAS):
        assert restore.is_known_external_link(name, "macos", restored_identity)
        for key in restored_identity:
            changed = dict(restored_identity, **{key: "unverified"})
            assert not restore.is_known_external_link(name, "macos", changed)
        assert not restore.is_known_external_link(name + "/other", "macos", restored_identity)
    assert FIXTURE["gitlink"] == {"path": "ungoogled-chromium", "mode": "160000",
                                  "type": "commit", "sha": CORE}
    now = datetime(2026, 10, 18, tzinfo=timezone.utc)
    fetch.validate_metadata(pin, FIXTURE["run"], FIXTURE["donor"][arch], now=now)
    for field, value in (("id", 1), ("size_in_bytes", size + 1),
                         ("digest", "sha256:" + "0" * 64), ("expired", True)):
        changed = dict(FIXTURE["donor"][arch], **{field: value})
        with pytest.raises(fetch.CacheMiss):
            fetch.validate_metadata(pin, FIXTURE["run"], changed, now=now)
    for field, value in (("conclusion", "failure"), ("head_sha", "0" * 40),
                         ("path", ".github/workflows/other.yml")):
        with pytest.raises(fetch.CacheMiss):
            fetch.validate_metadata(pin, dict(FIXTURE["run"], **{field: value}),
                                    FIXTURE["donor"][arch], now=now)


def test_new_stack_never_appends_mac154_quit_manifest(tmp_path):
    repo = clone_repo(tmp_path)
    set_platform_pins(repo, "macos", VERSION, CORE, MAC)
    identity, patches = selection.select(repo, "macos")
    assert len(patches) == 225
    assert identity["selection"]["platform_commit"] == MAC
    assert identity["selection"]["additions"]["manifest"]["path"] == selection.MACOS155_ADDITIONS_MANIFEST
    assert sum(name.startswith("patches/chromium155/") for name, _ in patches) == 10
    assert not any("chromium154" in name or "0226" in name for name, _ in patches)
    # A retained historical manifest is not part of the 155 recipe.
    (repo / selection.MACOS154_ADDITIONS_MANIFEST).write_text("not a 155 input")
    assert selection.select(repo, "macos") == (identity, patches)
    set_platform_pins(repo, "macos", VERSION, CORE, selection.PLATFORM_COMMITS["macos"])
    with pytest.raises(selection.SelectionError, match="core/platform pins"):
        selection.select(repo, "macos")


@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_exact_generator_source_repair_resume_and_native_probe_gate(tmp_path, arch):
    src = tmp_path.resolve() / "src"
    for name, text in FIXTURE["sources"].items():
        path = src / generators.DEVTOOLS / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    pins = dict(zip(("ChromiumVersion", "UngoogledCommit", "UngoogledMacOSCommit"),
                    generators.PINS155))
    with mock.patch("tools.platform_pins.load_pins", return_value=pins), \
            mock.patch.object(generators, "package_identity", return_value={"fixture": True}), \
            mock.patch.object(generators, "smoke", side_effect=ValueError("native failed")) as probe:
        before = generators.prepare(src, host_arch=arch, repo=ROOT)
        assert before["pins"] == list(generators.PINS155)
        assert before["manifest_sha256"] != generators.manifest_digest()
        with pytest.raises(ValueError, match="native failed"):
            generators.prepare(src, host_arch=arch, repo=ROOT, repair=True)
        assert generators.prepare(src, host_arch=arch, repo=ROOT) == before
        probe.side_effect = None
        probe.return_value = {"fixture": "not a real native probe"}
        after = generators.prepare(src, host_arch=arch, repo=ROOT, repair=True)
        assert not after["repair_needed"]
        assert generators.prepare(src, host_arch=arch, repo=ROOT, repair=True) == after
        for name, expected in FIXTURE["repaired_sha256"].items():
            assert generators.sha256((src / generators.DEVTOOLS / name).read_bytes()) == expected
        unchanged = "scripts/build/typescript/typescript.gni"
        assert (src / generators.DEVTOOLS / unchanged).read_text() == FIXTURE["sources"][unchanged]
        path = src / generators.DEVTOOLS / "scripts/build/esbuild.js"
        path.write_bytes(path.read_bytes() + b"unknown")
        with pytest.raises(ValueError, match="unknown macOS generator source"):
            generators.prepare(src, host_arch=arch, repo=ROOT)


def test_155_upstream_quit_and_bindgen_bytes(tmp_path):
    for name, entry in FIXTURE["upstream_sources"].items():
        assert selection.digest(entry["text"].encode()) == entry["sha256"]
    name = "chrome/browser/ui/startup/startup_browser_creator_impl.cc"
    source = FIXTURE["upstream_sources"][name]["text"]
    callback = source.split("headless::ProcessHeadlessCommands(", 1)[1].split("      continue;", 1)[0]
    assert "chrome::CloseAllBrowsersAndQuit();" in callback
    assert "app_controller_mac::AllowApplicationToTerminate();" in callback
    assert "std::move(profile_keepalive)" in callback
    assert "browser->GetWindow()" not in callback and "WeakPtr" not in callback
    assert '#include "chrome/browser/lifetime/application_lifetime_desktop.h"' in source
    bindgen = FIXTURE["upstream_sources"][runtime.BINDGEN_WRAPPER]
    assert bindgen["sha256"] == runtime.BINDGEN_154_ORIGINAL_SHA256
    src = tmp_path.resolve()
    path = src / runtime.BINDGEN_WRAPPER
    path.parent.mkdir(parents=True)
    path.write_text(bindgen["text"])
    runtime.repair_bindgen_wrapper(src, "arm64")
    assert selection.digest(path.read_bytes()) == runtime.BINDGEN_154_PATCHED_SHA256
    runtime.repair_bindgen_wrapper(src, "arm64")


@pytest.mark.parametrize("substituted", [False, True])
def test_independent_full_225_stack(tmp_path, substituted):
    location = os.environ.get("CHROMIX_MACOS155_SOURCES")
    if not location:
        pytest.skip("set CHROMIX_MACOS155_SOURCES to independent pristine/core/mac inputs")
    donor = Path(location).resolve()
    core, mac = donor / "core", donor / "mac"
    src = tmp_path / "src"
    shutil.copytree(donor / "pristine", src)
    _, patches = selection.select(ROOT, "macos", src=src, core=core)
    assert len(patches) == 225
    targets = {name for _, raw in patches for name, _, _ in arp.transform_patch(raw, set(), [])[1]}
    log = []
    for tooling in (core, mac):
        for name, target, section in predecessor_sections(tooling, targets):
            patch = tmp_path / "upstream.patch"
            patch.write_text(section)
            result = subprocess.run([shutil.which("gpatch") or "patch", *arp.PATCH_OPTIONS,
                                     "--input", str(patch)], cwd=src, capture_output=True, text=True)
            log.append(name + ":" + target + "\n" + result.stdout + result.stderr)
            assert result.returncode == 0 and not re.search(r"fuzz|FAILED", log[-1], re.I), log[-1]
    if substituted:
        rules = arp._rules((core / "domain_regex.list").read_bytes())
        for name in targets & set((core / "domain_substitution.list").read_text().splitlines()):
            path = src / name
            if path.is_file():
                text, encoding = arp._decode(path.read_bytes())
                path.write_bytes(arp._substitute(text, rules).encode(encoding))
        report = arp.run_apply(src, ROOT, core, mac, "macos", shutil.which("gpatch") or "patch")
        assert report["status"] == "applied" and report["patch_count"] == 225
        (tmp_path / "applied.json").write_text(json.dumps(report, indent=2))
        assert arp.run_apply(src, ROOT, core, mac, "macos", shutil.which("gpatch") or "patch",
                             check=True)["status"] == "checked"
        (src / ".chromix-domain-substituted").write_text(CORE)
    else:
        shutil.copytree(ROOT / arp.LITE, src, dirs_exist_ok=True)
        for name, _ in patches:
            result = subprocess.run([shutil.which("gpatch") or "patch", *arp.PATCH_OPTIONS,
                                     "--input", str(ROOT / name)], cwd=src, capture_output=True, text=True)
            log.append(name + "\n" + result.stdout + result.stderr)
            assert result.returncode == 0 and not re.search(r"fuzz|FAILED", log[-1], re.I), log[-1]
    (tmp_path / "apply.log").write_text("\n".join(log))
    output = tmp_path / "verified.json"
    result = subprocess.run([sys.executable, str(ROOT / "tools/verify_patch_stack.py"),
        "--src", str(src), "--repo", str(ROOT), "--core", str(core),
        "--platform-tooling", str(mac), "--platform", "macos", "--output", str(output)],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert report["status"] == "verified" and report["patch_count"] == 225
    assert report["domain_substituted"] == substituted
