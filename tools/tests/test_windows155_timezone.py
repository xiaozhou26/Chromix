"""Exact-155 replay and source-derived native ICU tests; scheduler/V8 are shims."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess

import pytest

from test_fingerprint_features import added, block

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).with_name("fixtures")
PATCHES = tuple("patches/chromium155-windows/" + name for name in (
    "0228-timezone-default-declaration.patch",
    "0229-timezone-default-implementation.patch",
    "0230-blink-default-timezone-api.patch",
    "0231-blink-default-timezone-bridge.patch",
    "0232-renderer-launch-timezone-install.patch",
    "0233-renderer-timezone-webstring-api.patch",
))
CONTROLLER = "third_party/blink/renderer/core/timezone/timezone_controller"
RENDERER = "content/renderer/render_thread_impl.cc"
BRIDGE = "third_party/blink/renderer/controller/blink_initializer.cc"
PUBLIC = "third_party/blink/public/web/blink.h"
WEB_STRING = "third_party/blink/public/platform/web_string.h"
EVIDENCE_PINS = {
    "155.0.8059.39": ("windows155_timezone_sources.json.gz",
                      "04cbc6ded84ceb31a054c370d300af29efdcc42b6c89b1ea6d7d1e689c11fab6"),
    "155.0.8059.79": ("windows15579_timezone_sources.json.gz",
                      "72affb69c886fdf048dbf8a2fab8b57f58915f5e349a4e742d94da7009a159a5"),
}


@pytest.fixture(scope="module", params=tuple(EVIDENCE_PINS))
def evidence(request):
    name, sha256 = EVIDENCE_PINS[request.param]
    raw = (FIXTURES / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == sha256
    data = json.loads(gzip.decompress(raw))
    assert data["version"] == request.param
    if request.param == "155.0.8059.79":
        revision = "a49e2e3d4addfd84e765b52afe230bedb81096db"
        assert data["chromium_revision"] == revision
        assert data["url_prefix"] == f"https://raw.githubusercontent.com/chromium/chromium/{revision}/"
        assert data["sources"]["chrome/VERSION"]["text"] == (
            "MAJOR=155\nMINOR=0\nBUILD=8059\nPATCH=79\n")
    else:
        assert data["url_prefix"] == "https://chromium.googlesource.com/chromium/src/+/155.0.8059.39/"
    return data


def run_patch(directory, path, reverse=False):
    program = shutil.which("gpatch") or shutil.which("patch")
    assert program, "GNU patch is required for exact-source replay"
    result = subprocess.run(
        [program, "--batch", "--fuzz=0", "-p1", *(["-R"] if reverse else []),
         "-i", str(path)], cwd=directory, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "fuzz" not in result.stdout and "FAILED" not in result.stdout


@pytest.fixture(scope="module")
def sources(tmp_path_factory, evidence):
    directory = tmp_path_factory.mktemp("windows155-timezone-" + evidence["version"])
    for name, entry in evidence["sources"].items():
        raw = entry["text"].encode()
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    for number in (5, 7, 19):
        run_patch(directory, next((ROOT / "patches").glob(f"{number:04d}-*.patch")))
    before = {name: (directory / name).read_bytes() for name in evidence["sources"]}
    for patch in PATCHES:
        run_patch(directory, ROOT / patch)
    result = {name: (directory / name).read_text() for name in before}
    for patch in reversed(PATCHES):
        run_patch(directory, ROOT / patch, reverse=True)
    assert {name: (directory / name).read_bytes() for name in before} == before
    return result


def test_exact_source_install_boundary_and_layering(sources):
    renderer = sources[RENDERER]
    setter = block(renderer, "void RenderThreadImpl::SetUxrConfig(")
    assert setter.index("CHECK(snapshot.SetAll(") < setter.index("snapshot.Get(")
    assert setter.index("snapshot.Get(") < setter.index("CHECK(blink::SetRendererDefaultTimeZone(")
    assert "PostTask" not in setter and "ForTesting" not in setter
    assert 'third_party/blink/renderer/core/' not in renderer
    assert "BLINK_EXPORT bool SetRendererDefaultTimeZone(const WebString& timezone_id);" in sources[PUBLIC]
    assert "TimeZoneController::SetDefaultTimeZone(timezone_id)" in block(
        sources[BRIDGE], "bool SetRendererDefaultTimeZone(")
    assert '"+third_party/blink/renderer/core"' in sources[
        "third_party/blink/renderer/controller/DEPS"]
    assert '"//third_party/blink/renderer/core"' in sources[
        "third_party/blink/renderer/controller/BUILD.gn"]
    assert '"blink_initializer.cc"' in sources[
        "third_party/blink/renderer/controller/BUILD.gn"]
    assert '"+third_party/blink/public"' in sources["content/DEPS"]
    assert renderer.index("InitializeWebKit(&binders)") < renderer.index(
        "AddInterface<mojom::Renderer>")
    host = sources["content/browser/renderer_host/render_process_host_impl.cc"]
    assert host.index("GetRendererInterface()->SetUxrConfig(") < host.index(
        "GetRendererInterface()->InitializeRenderer(")


def test_native_notification_and_devtools_paths_are_preserved(sources, evidence):
    original = evidence["sources"][CONTROLLER + ".cc"]["text"]
    patched = sources[CONTROLLER + ".cc"]
    for signature in (
        "bool TimeZoneController::SetIcuTimeZoneAndNotifyV8(",
        "void NotifyTimezoneChangeToV8(",
        "void NotifyTimezoneChangeOnWorkerThread(",
        "void NotifyTimezoneChangeOnMainThread(",
        "void DispatchTimeZoneChangeEventToFrames(",
        "TimeZoneController::SetTimeZoneOverride(",
        "void TimeZoneController::ChangeTimeZoneOverride(",
        "void TimeZoneController::ClearTimeZoneOverride(",
    ):
        assert block(patched, signature) == block(original, signature)
    apply = block(patched, "bool TimeZoneController::SetDefaultTimeZone(")
    assert "HasTimeZoneOverride()" in apply
    assert apply.index("SetIcuTimeZoneAndNotifyV8(") < apply.index("host_timezone_id_ =")
    monitor = block(patched, "void TimeZoneController::OnTimeZoneChange(")
    assert 'Get("uxr-timezone")' in monitor
    assert "SetDefaultTimeZone(effective_id);" in monitor


def test_manifest_appends_231_without_changing_donor230():
    data = json.loads((ROOT / "build/patch-additions-windows155.json").read_text())
    series = [line for line in (ROOT / "patches/series").read_text().splitlines()
              if line.strip() and not line.startswith("#")]
    assert len(series) == 224
    assert len(series) + len(data["patches"]) == 231
    assert data["patches"][0] == {
        "path": "patches/chromium155-windows/0227-opaque-png-export-consistency.patch",
        "sha256": "4c048a50930c3541a3bb9e4442ad7f18e94c04ebccf86c0cfbfc286b15a70751",
    }
    assert data["patches"][1:] == [
        {"path": patch, "sha256": hashlib.sha256((ROOT / patch).read_bytes()).hexdigest()}
        for patch in PATCHES
    ]


def test_selector_authenticates_target231_and_donor230_manifest():
    from tools import patch_selection

    manifest = ROOT / patch_selection.WINDOWS155_ADDITIONS_MANIFEST
    data = json.loads(manifest.read_bytes())
    assert data["chromium_version"] == (ROOT / "CHROMIUM_WINDOWS_VERSION").read_text().strip() == "155.0.8059.79"
    assert data["core_commit"] == "9f145d8815e6e50cc4a2537fcac40966d911e027"
    assert data["platform_commit"] == "a1e3123c8d543b5c9a1eecfaf0f06cf242c0db37"
    historical = {**data, "chromium_version": "155.0.8059.39",
                  "core_commit": "ffeb45a2bb88bc56a1dfed31410aeb10e9efcd5a",
                  "platform_commit": "989b4382364baaddb8b3180e1eb1030ca96a7273"}
    assert hashlib.sha256((json.dumps(historical, indent=2) + "\n").encode()).hexdigest() == (
        patch_selection.WINDOWS155_ADDITIONS_SHA256)
    donor = {**historical, "patches": historical["patches"][:-1]}
    assert hashlib.sha256((json.dumps(donor, indent=2) + "\n").encode()).hexdigest() == (
        "5e7983a05e07f8f3ec10806fa06f180fb5ef20490568d8d83ce1b1e3db5fa104")
    identity, patches = patch_selection.select(ROOT, "windows")
    assert len(patches) == 231
    assert identity["selection"]["additions"]["patches"] == data["patches"]
    assert patches[-1][0] == PATCHES[-1]
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == (
        patch_selection.WINDOWS15579_ADDITIONS_SHA256)
    correction = (ROOT / PATCHES[-1]).read_text().splitlines()
    removed = [line[1:] for line in correction if line.startswith("-") and not line.startswith("---")]
    added_lines = [line[1:] for line in correction if line.startswith("+") and not line.startswith("+++")]
    assert len(removed) == len(added_lines) == 1
    assert removed[0].replace("FromUTF8", "FromUtf8") == added_lines[0]
    assert "FromUTF8" in removed[0]


def test_new_timezone_calls_match_exact_declarations(sources, evidence):
    web_string = sources[WEB_STRING]
    assert "static WebString FromUtf8(std::string_view s);" in web_string
    assert "FromUTF8" not in web_string
    assert "operator String() const;" in web_string
    assert "WebString::FromUtf8(timezone)" in sources[RENDERER]
    assert "WebString::FromUTF8(timezone)" not in sources[RENDERER]
    wtf = sources["third_party/blink/renderer/platform/wtf/text/wtf_string.h"]
    assert "bool empty() const" in wtf
    assert "[[nodiscard]] std::string Ascii() const;" in wtf
    assert "using AutoLock = internal::BasicAutoLock<Lock>;" in sources[
        "base/synchronization/lock.h"]
    assert "std::string Get(const std::string& key) const;" in added(3)
    original = evidence["sources"][CONTROLLER + ".cc"]["text"]
    install = block(sources[CONTROLLER + ".cc"], "bool TimeZoneController::SetDefaultTimeZone(")
    native = block(original, "bool TimeZoneController::SetIcuTimeZoneAndNotifyV8(")
    for call in (
        "icu::TimeZone::createTimeZone(",
        "icu::UnicodeString(timezone_id.Ascii().data(), -1, US_INV)",
        "*timezone == icu::TimeZone::getUnknown()",
    ):
        assert call in install and call in native
    for signature in (
        "static bool HasTimeZoneOverride();",
        "static bool SetIcuTimeZoneAndNotifyV8(const String& timezone_id);",
    ):
        assert signature in sources[CONTROLLER + ".h"]


def test_single_file_split_preserves_original_diff_bytes_and_order(evidence):
    split = evidence["patch_split"]
    assert [entry["path"] for entry in split["patches"]] == list(PATCHES[:-1])
    assert [entry["target"] for entry in split["patches"]] == [
        CONTROLLER + ".h", CONTROLLER + ".cc", PUBLIC, BRIDGE, RENDERER,
    ]
    sections = {}
    for entry in split["patches"]:
        raw = (ROOT / entry["path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        assert raw.count(b"diff --git ") == 1
        target = entry["target"]
        assert raw.startswith(f"diff --git a/{target} b/{target}\n".encode())
        sections[target] = raw
    original = b"".join(sections[target] for target in split["original_target_order"])
    assert split["original_sha256"] == "354c8c86228f6f937e8f61a0a7305d24a78a17c29be09c4c603b56cf787009e4"
    assert hashlib.sha256(original).hexdigest() == split["original_sha256"]


def test_timezone_split_passes_unmodified_patch_body_linter(tmp_path):
    from tools import check_patches

    for patch in PATCHES:
        shutil.copy2(ROOT / patch, tmp_path / Path(patch).name)
    report = check_patches.Report()
    check_patches.check_bodies(report, tmp_path, verbose=True)
    assert not report.failures


def icu_flags():
    prefix = os.environ.get("CHROMIX_TIMEZONE_ICU_ROOT")
    if not prefix and Path("/opt/homebrew/opt/icu4c/include/unicode/timezone.h").exists():
        prefix = "/opt/homebrew/opt/icu4c"
    if prefix:
        return ["-I" + prefix + "/include", "-L" + prefix + "/lib",
                "-Wl,-rpath," + prefix + "/lib", "-licui18n", "-licuuc", "-licudata"]
    program = shutil.which("pkg-config")
    if program:
        result = subprocess.run([program, "--cflags", "--libs", "icu-i18n"],
                                capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            import shlex
            return shlex.split(result.stdout)
    pytest.skip("ICU development libraries required; set CHROMIX_TIMEZONE_ICU_ROOT")


@pytest.fixture(scope="module")
def native_binary(tmp_path_factory, sources):
    compiler = os.environ.get("CXX") or shutil.which("clang++") or shutil.which("c++")
    if not compiler:
        pytest.skip("C++20 compiler required")
    controller = sources[CONTROLLER + ".cc"]
    header = "\n".join(line for line in sources[CONTROLLER + ".h"].splitlines()
                       if not line.startswith("#include"))
    definitions = "\n".join(block(controller, signature) for signature in (
        "TimeZoneController::TimeZoneController()",
        "TimeZoneController& TimeZoneController::instance()",
        "bool CanonicalEquals(",
        "TimeZoneController::TimeZoneOverrideResult\nTimeZoneController::SetTimeZoneOverride(",
        "bool TimeZoneController::HasTimeZoneOverride()",
        "const String& TimeZoneController::TimeZoneIdOverride()",
        "void TimeZoneController::ClearTimeZoneOverride()",
        "void TimeZoneController::ChangeTimeZoneOverride(",
        "bool TimeZoneController::SetIcuTimeZoneAndNotifyV8(",
        "bool TimeZoneController::SetDefaultTimeZone(",
        "void TimeZoneController::OnTimeZoneChange(",
        "const String& TimeZoneController::GetHostTimezoneId()",
        "void TimeZoneController::ChangeTimeZoneForTesting(",
    ))
    directory = tmp_path_factory.mktemp("windows155-timezone-native")
    dependencies = {
        WEB_STRING: sources[WEB_STRING],
        "base/memory/scoped_refptr.h": (
            "#include <memory>\n"
            "template <class T> using scoped_refptr = std::shared_ptr<T>;\n"),
        "third_party/blink/public/platform/web_common.h": "#define BLINK_PLATFORM_EXPORT\n",
        "third_party/blink/renderer/platform/wtf/forward.h": (
            "namespace blink { class String; class StringView; class AtomicString; }\n"),
    }
    for name, text in dependencies.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    unit = '#define INSIDE_BLINK 1\n'
    unit += (FIXTURES / "windows155_timezone_shim.h").read_text() + header
    declaration = re.search(r"BLINK_EXPORT bool SetRendererDefaultTimeZone\([^;]+;",
                            sources[PUBLIC]).group()
    unit += "\n#define BLINK_EXPORT\nnamespace blink {\n" + declaration + "\n}\n"
    unit += """
#include <type_traits>
static_assert(std::is_same_v<decltype(&blink::WebString::FromUtf8),
                            blink::WebString (*)(std::string_view)>);
static_assert(std::is_convertible_v<blink::WebString, blink::String>);
static_assert(std::is_same_v<decltype(&blink::SetRendererDefaultTimeZone),
                            bool (*)(const blink::WebString&)>);
static_assert(std::is_same_v<decltype(&blink::TimeZoneController::SetDefaultTimeZone),
                            bool (*)(const blink::String&)>);
"""
    unit += "\nnamespace blink {\n" + definitions
    unit += "\nTimeZoneController::~TimeZoneController() = default;\n"
    unit += block(sources[BRIDGE], "bool SetRendererDefaultTimeZone(") + "\n}\n"
    unit += block(sources[RENDERER], "void RenderThreadImpl::SetUxrConfig(")
    unit += (FIXTURES / "windows155_timezone_cases.cc").read_text()
    cpp, binary = directory / "timezone.cc", directory / "timezone"
    cpp.write_text(unit)
    result = subprocess.run([compiler, "-std=c++20", "-Wall", "-Wextra", "-Werror",
                             "-I" + str(directory), str(cpp), *icu_flags(), "-o", str(binary)],
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    return binary


@pytest.mark.parametrize("before,after,diagnostic", [
    ("blink::WebString::FromUtf8(timezone)",
     "blink::WebString::FromUTF8(timezone)", "FromUTF8"),
    ("WebString::FromUtf8(std::string_view value)",
     "WebString::FromUtf8(const std::string& value)", "FromUtf8"),
])
def test_real_webstring_header_rejects_wrong_api(native_binary, tmp_path,
                                               before, after, diagnostic):
    source = (native_binary.parent / "timezone.cc").read_text()
    assert source.count(before) == 1
    cpp = tmp_path / "wrong-api.cc"
    cpp.write_text(source.replace(before, after))
    compiler = os.environ.get("CXX") or shutil.which("clang++") or shutil.which("c++")
    result = subprocess.run(
        [compiler, "-std=c++20", "-fsyntax-only", "-I" + str(native_binary.parent),
         str(cpp), *icu_flags()], capture_output=True, text=True, timeout=90)
    assert result.returncode != 0
    assert diagnostic in result.stderr


@pytest.mark.parametrize("case", [
    "monitor-early", "monitor-late", "no-timezone", "empty-timezone",
    "override-before", "override-after", "override-same", "invalid-default",
    "invalid-under-override", "invalid-launch", "immutable-snapshot", "replay",
])
@pytest.mark.parametrize("lazy", [False, True])
def test_native_icu_install_order_and_override(native_binary, case, lazy):
    result = subprocess.run([str(native_binary), case, str(int(lazy))],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("mutation,case", [
    ("missing-install", "monitor-early"),
    ("missing-install", "monitor-late"),
    ("missing-main-notification", "monitor-late"),
    ("missing-worker-notification", "monitor-late"),
])
def test_harness_detects_install_and_notification_regressions(native_binary, tmp_path,
                                                             mutation, case):
    source = (native_binary.parent / "timezone.cc").read_text()
    before, after = {
        "missing-install": (
            "return TimeZoneController::SetDefaultTimeZone(timezone_id);",
            "(void)timezone_id; return true;"),
        "missing-main-notification": (
            "    NotifyTimezoneChangeOnMainThread();", "    // Mutation: no main-isolate notification."),
        "missing-worker-notification": (
            "  WorkerThread::CallOnAllWorkerThreads(&NotifyTimezoneChangeOnWorkerThread,\n"
            "                                       TaskType::kInternalDefault);",
            "  // Mutation: no worker notification."),
    }[mutation]
    assert source.count(before) == 1
    cpp, binary = tmp_path / "mutated.cc", tmp_path / "mutated"
    cpp.write_text(source.replace(before, after))
    compiler = os.environ.get("CXX") or shutil.which("clang++") or shutil.which("c++")
    compiled = subprocess.run([compiler, "-std=c++20", "-I" + str(native_binary.parent),
                               str(cpp), *icu_flags(), "-o", str(binary)],
                              capture_output=True, text=True, timeout=90)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    result = subprocess.run([str(binary), case, "0"], capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert "assert" in result.stderr.lower()


def test_optional_independent_exact_sources(evidence):
    variable = ("CHROMIX_WINDOWS15579_TIMEZONE_SOURCES" if evidence["version"] == "155.0.8059.79"
                else "CHROMIX_WINDOWS155_TIMEZONE_SOURCES")
    directory = os.environ.get(variable)
    if not directory:
        pytest.skip(f"set {variable} to independently fetched exact tag")
    for name, entry in evidence["sources"].items():
        assert hashlib.sha256((Path(directory) / name).read_bytes()).hexdigest() == entry["sha256"]
