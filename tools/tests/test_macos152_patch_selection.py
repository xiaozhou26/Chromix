"""macOS 152 selection and strict application to independent upstream sources."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from tools import apply_restored_patches as arp
from tools import patch_selection as selection
from tools.tests.test_patch_selection import clone_repo, set_linux_version, set_macos_pins

ROOT = Path(__file__).resolve().parents[2]
NAME = "0209-display-native-screen-regressions.patch"
TARGET = "third_party/blink/renderer/core/frame/web_frame_widget_test.cc"
PREIMAGE_SHA256 = "9d1c641238c8d927d1ce64f6bb112b78a6b3ad91be16db59ac205fff8b7d24a5"
# Full-file source: chromium/src/+/152.0.7977.82; no core/mac predecessors touch it.
SECTIONS = {
    17: '''#include "cc/test/property_tree_test_utils.h"
#include "cc/trees/scroll_source_type.h"
#include "components/viz/common/surfaces/parent_local_surface_id_allocator.h"
#include "testing/gmock/include/gmock/gmock.h"
#include "third_party/blink/public/common/features.h"
#include "third_party/blink/public/common/input/synthetic_web_input_event_builders.h"
#include "third_party/blink/public/mojom/page/widget.mojom-shared.h"
#include "third_party/blink/public/web/web_plugin_params.h"
#include "third_party/blink/public/web/web_script_source.h"
#include "third_party/blink/renderer/core/css/properties/css_property_ref.h"
''',
    91: '''  bool invoked_ = false;
};

}  // namespace

class WebFrameWidgetSimTest : public SimTest {};
''',
    452: '''  visual_properties.is_pinch_gesture_active = false;
  LocalFrameRootWidget()->ApplyVisualProperties(visual_properties);
  EXPECT_FALSE(layer_tree_host->is_external_pinch_gesture_active_for_testing());
}

const char EVENT_LISTENER_RESULT_HISTOGRAM[] = "Event.PassiveListeners";

// Keep in sync with enum defined in
// RenderWidgetInputHandler::LogPassiveEventListenersUma.
enum {
  PASSIVE_LISTENER_UMA_ENUM_PASSIVE,
  PASSIVE_LISTENER_UMA_ENUM_UNCANCELABLE,
  PASSIVE_LISTENER_UMA_ENUM_SUPPRESSED,
  PASSIVE_LISTENER_UMA_ENUM_CANCELABLE,
  PASSIVE_LISTENER_UMA_ENUM_CANCELABLE_AND_CANCELED,
  PASSIVE_LISTENER_UMA_ENUM_FORCED_NON_BLOCKING_DUE_TO_FLING,
  PASSIVE_LISTENER_UMA_ENUM_FORCED_NON_BLOCKING_DUE_TO_MAIN_THREAD_RESPONSIVENESS_DEPRECATED,
  PASSIVE_LISTENER_UMA_ENUM_COUNT
};

// Since std::unique_ptr<InputHandlerProxy::DidOverscrollParams> isn't copyable
''',
    1829: '''  EXPECT_FALSE(listener->GetInvokedStateAndReset());
}

// Tests that page scale is propagated to all remote frames controlled
// by a widget.
TEST_F(WebFrameWidgetSimTest, PropagateScaleToRemoteFrames) {
''',
}


@pytest.fixture
def macos152_repo(tmp_path):
    repo = clone_repo(tmp_path)
    set_macos_pins(repo, historical=True)
    return repo


def run_patch(src, patch, reverse=False):
    program = shutil.which("gpatch") or shutil.which("patch")
    assert program, "GNU patch is required"
    return subprocess.run(
        [program, *arp.PATCH_OPTIONS, *(["--reverse"] if reverse else []), "-i", str(patch)],
        cwd=src, capture_output=True, text=True, timeout=30,
        env={**os.environ, "LC_ALL": "C", "PATCH_GET": "0"})


def test_exact_macos152_selects_only_authenticated_0209(macos152_repo):
    identity, patches = selection.select(macos152_repo, "macos")
    assert len(patches) == 224
    assert identity["selection"] == {
        "schema_version": 1, "version": selection.MACOS152_VERSION, "platform": "macos",
        "core_commit": selection.MACOS152_CORE,
        "platform_commit": selection.MACOS152_PLATFORM,
        "base_series_sha256": selection.digest((ROOT / "patches/series").read_bytes()),
    }
    overrides = [path for path, _ in patches if path.count("/") > 1]
    assert overrides == [selection.MACOS152_OVERRIDE_ROOT + "/" + NAME]
    series = (ROOT / "patches/series").read_bytes().replace(
        ("patches/" + NAME).encode(), overrides[0].encode())
    assert identity["series_sha256"] == selection.digest(series)
    assert identity["patches"] == [
        {"path": path, "sha256": selection.digest(raw)} for path, raw in patches]
    for path, raw in patches:
        assert raw == (ROOT / path).read_bytes()


@pytest.mark.parametrize("mutation", ["missing", "changed", "extra", "predecessor",
                                      "symlink", "directory-symlink", "series", "pins"])
def test_macos152_inputs_fail_closed(macos152_repo, mutation):
    repo = macos152_repo
    directory = repo / selection.MACOS152_OVERRIDE_ROOT
    patch = directory / NAME
    if mutation == "missing":
        patch.unlink()
    elif mutation == "changed":
        patch.write_bytes(patch.read_bytes() + b"\n")
    elif mutation == "extra":
        (directory / "unexpected.patch").write_bytes(b"unexpected")
    elif mutation == "predecessor":
        path = repo / "patches" / NAME
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "symlink":
        patch.unlink()
        patch.symlink_to(ROOT / selection.MACOS152_OVERRIDE_ROOT / NAME)
    elif mutation == "directory-symlink":
        shutil.rmtree(directory)
        directory.symlink_to(ROOT / selection.MACOS152_OVERRIDE_ROOT, target_is_directory=True)
    elif mutation == "series":
        path = repo / "patches/series"
        path.write_text(path.read_text().replace("patches/" + NAME + "\n", ""))
    else:
        (repo / "build/ungoogled-revisions.psd1").unlink()
    with pytest.raises(selection.SelectionError):
        selection.select(repo, "macos")


@pytest.mark.parametrize("field,value", [
    ("MacOSUngoogledCommit", "0" * 40),
    ("UngoogledMacOSCommit", "0" * 40),
    ("MacOSUngoogledVersion", "152.0.7977.82-2"),
    ("UngoogledMacOSVersion", "152.0.7977.82-1.2"),
])
def test_macos152_requires_exact_core_and_platform_pins(macos152_repo, field, value):
    repo = macos152_repo
    path = repo / "build/ungoogled-revisions.psd1"
    path.write_text(re.sub(rf'(?m)^(  {field} = )"[^"]+"',
                           rf'\g<1>"{value}"', path.read_text()))
    with pytest.raises(selection.SelectionError, match="pins|Ungoogled"):
        selection.select(repo, "macos")


@pytest.mark.parametrize("which", ["source", "core"])
@pytest.mark.parametrize("value", [None, "154.0.8037.97", "152.0.7977.82"])
def test_macos152_checks_source_and_tooling_version(tmp_path, macos152_repo, which, value):
    directory = tmp_path / which
    path = directory / ("chrome/VERSION" if which == "source" else "chromium_version.txt")
    path.parent.mkdir(parents=True)
    if value:
        text = ("".join(f"{k}={v}\n" for k, v in
                        zip(("MAJOR", "MINOR", "BUILD", "PATCH"), value.split(".")))
                if which == "source" else value + "\n")
        path.write_text(text)
    kwargs = {"src" if which == "source" else "core": directory}
    if value == selection.MACOS152_VERSION:
        assert "selection" in selection.select(macos152_repo, "macos", **kwargs)[0]
    else:
        with pytest.raises(selection.SelectionError, match="version|missing patch input"):
            selection.select(macos152_repo, "macos", **kwargs)


def clone_bf3221b_repo(tmp_path, *, additions=True):
    repo = clone_repo(tmp_path, additions=additions)
    revision = "bf3221bcfa08b652ff62a44f52ccce393ef1d351"
    paths = ["build/ungoogled-revisions.psd1", "CHROMIUM_VERSION",
             "CHROMIUM_LINUX_VERSION", "CHROMIUM_WINDOWS_VERSION", "CHROMIUM_MACOS_VERSION"]
    if additions:
        paths += [selection.LINUX155_ADDITIONS_MANIFEST, selection.WINDOWS155_ADDITIONS_MANIFEST,
                  selection.MACOS154_ADDITIONS_MANIFEST]
    for name in paths:
        raw = subprocess.run(["git", "-C", str(ROOT), "show", revision + ":" + name],
                             check=True, capture_output=True, timeout=30).stdout
        (repo / name).write_bytes(raw)
    return repo


def test_macos152_does_not_change_other_platform_or_legacy_selection(tmp_path):
    repo = clone_bf3221b_repo(tmp_path)
    before = {platform: selection.select(repo, platform) for platform in ("linux", "windows", None)}
    assert before["linux"][0]["selection"]["version"] == selection.VERSION155
    assert before["windows"][0]["selection"]["version"] == selection.VERSION155
    assert "selection" not in before[None][0]
    shutil.rmtree(repo / selection.MACOS152_OVERRIDE_ROOT)
    for platform, expected in before.items():
        assert selection.select(repo, platform) == expected
    set_linux_version(repo, "152.0.7977.82")
    identity, patches = selection.select(repo, "linux")
    assert "selection" not in identity
    assert all(path.count("/") == 1 for path, _ in patches)


@pytest.mark.parametrize("style", ["posix", "windows"])
def test_macos152_preparation_key_binds_selected_identity(macos152_repo, monkeypatch, style):
    actual = selection.preparation_key(macos152_repo, "macos", style)
    identity, patches = selection.select(macos152_repo, "macos")
    monkeypatch.setattr(selection, "select", lambda *_: (
        {key: value for key, value in identity.items() if key != "selection"}, patches))
    assert selection.preparation_key(macos152_repo, "macos", style) != actual
    legacy = [("patches/" + Path(path).name,
               (ROOT / "patches" / Path(path).name).read_bytes()) for path, _ in patches]
    monkeypatch.setattr(selection, "select", lambda *_: ({}, legacy))
    assert selection.preparation_key(macos152_repo, "macos", style) != actual


def test_0209_preserves_every_test_and_only_changes_152_context():
    base = (ROOT / "patches" / NAME).read_text()
    override = (ROOT / selection.MACOS152_OVERRIDE_ROOT / NAME).read_text()
    assert override == base.replace(
        " // Since std::unique_ptr<InputHandlerProxy::DidOverscrollParams> isn't copyable\n",
        ' const char EVENT_LISTENER_RESULT_HISTOGRAM[] = "Event.PassiveListeners";\n').replace(
        "@@ -1744,6 +2260,49 @@", "@@ -1829,6 +2345,49 @@")
    assert [line for line in base.splitlines() if line.startswith("+")] == [
        line for line in override.splitlines() if line.startswith("+")]


def test_independent_152_context_reproduces_failure_and_0209_0216_roundtrip(tmp_path):
    lines = []
    for start, text in SECTIONS.items():
        lines.extend("// unrelated pinned Chromium source\n" for _ in range(start - 1 - len(lines)))
        lines.extend(text.splitlines(keepends=True))
    original = ("".join(lines) + "// not EOF\n").encode()
    path = tmp_path / TARGET
    path.parent.mkdir(parents=True)
    path.write_bytes(original)
    result = run_patch(tmp_path, ROOT / "patches" / NAME)
    assert result.returncode != 0
    assert "Hunk #3 FAILED at 477" in result.stdout
    assert "Hunk #4 succeeded at 1854 (offset 85 lines)" in result.stdout
    path.write_bytes(original)
    chain = [ROOT / selection.MACOS152_OVERRIDE_ROOT / NAME,
             ROOT / "patches/0216-display-native-emulated-regressions.patch"]
    for reverse in (False, True):
        for patch in reversed(chain) if reverse else chain:
            result = run_patch(tmp_path, patch, reverse)
            assert result.returncode == 0, result.stdout + result.stderr
            assert not re.search(r"fuzz|offset|FAILED", result.stdout + result.stderr, re.I)
    assert path.read_bytes() == original


@pytest.mark.parametrize("substituted", [False, True])
def test_optional_independent_152_full_stack(tmp_path, macos152_repo, substituted):
    root = os.environ.get("CHROMIX_MACOS152_SOURCES")
    if not root:
        pytest.skip("set CHROMIX_MACOS152_SOURCES to independent pristine/core/mac directories")
    donor = Path(root)
    pristine, core, mac = (donor / name for name in ("pristine", "core", "mac"))
    assert selection.source_version(pristine) == selection.MACOS152_VERSION
    original = (pristine / TARGET).read_bytes()
    assert hashlib.sha256(original).hexdigest() == PREIMAGE_SHA256
    for start, text in SECTIONS.items():
        assert "".join(original.decode().splitlines(keepends=True)[
            start - 1:start - 1 + len(text.splitlines())]) == text
    src = tmp_path / "src"
    shutil.copytree(pristine, src)
    _, selected = selection.select(macos152_repo, "macos", src=src, core=core)
    targets = {target for _, raw in selected
               for target, _, _ in arp.transform_patch(raw, set(), [])[1]}
    # Apply every predecessor section affecting a selected target, in series order.
    for tooling in (core, mac):
        for name in (tooling / "patches/series").read_text().splitlines():
            name = name.split("#", 1)[0].strip()
            if not name:
                continue
            text = (tooling / "patches" / name).read_text()
            for section in re.split(r"(?m)(?=^--- (?:a/|/dev/null))", text):
                match = re.match(r"--- (\S+)[^\n]*\n\+\+\+ (\S+)", section)
                if not match:
                    continue
                target = (match[2] if match[2] != "/dev/null" else match[1])[2:]
                if target not in targets:
                    continue
                patch = tmp_path / "predecessor.patch"
                patch.write_text(section)
                result = run_patch(src, patch)
                assert result.returncode == 0, name + result.stdout + result.stderr
    if substituted:
        rules = arp._rules((core / "domain_regex.list").read_bytes())
        listed = set((core / "domain_substitution.list").read_text().splitlines())
        for name in targets & listed:
            path = src / name
            if path.is_file():
                text, encoding = arp._decode(path.read_bytes())
                path.write_bytes(arp._substitute(text, rules).encode(encoding))
        program = shutil.which("gpatch") or "patch"
        report = arp.run_apply(src, macos152_repo, core, mac, "macos", program)
        assert report["status"] == "applied"
        assert report["patch_count"] == 224
        assert arp.run_apply(src, macos152_repo, core, mac, "macos", program, check=True)["status"] == "checked"
        (src / ".chromix-domain-substituted").write_text(selection.MACOS152_CORE)
    else:
        shutil.copytree(ROOT / arp.LITE, src, dirs_exist_ok=True)
        for name, _ in selected:
            result = run_patch(src, ROOT / name)
            assert result.returncode == 0, name + result.stdout + result.stderr
    output = tmp_path / "verified.json"
    result = subprocess.run([
        os.sys.executable, str(ROOT / "tools/verify_patch_stack.py"),
        "--src", str(src), "--repo", str(macos152_repo), "--core", str(core),
        "--platform-tooling", str(mac), "--platform", "macos", "--output", str(output)],
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(output.read_text())["patch_count"] == 224
