"""GPU regressions using pinned Chromium 152 sources and standalone C++ stubs.

0030 executes the entire patched GPUAdapterInfo source against a pinned upstream
preimage; 0091/0092 are applied and included unchanged, including BuildPersona and
CurrentPersona. Only dependency types/config/bindings/GL calls are stubs. Platform
macros simulate Windows x86_64/x86/ARM64, Linux and Mac on the host compiler; these
are not native browser builds or GPU-backend tests. Existing features/limits/WebGL
harnesses execute patched functions with stubbed dependencies. Set
CHROMIX_GPU_BASELINE_ROOT to recovered preimages to verify full-source provenance.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATCHES = ("0030", "0041", "0042", "0110")
PERSONA_PATCHES = ("0091", "0092")
PATCHES = {number: next((ROOT / "patches").glob(f"{number}-*.patch"))
           for number in ("0030",) + SOURCE_PATCHES + PERSONA_PATCHES}
PATCH_BIN = shutil.which("gpatch") or shutil.which("patch")
CXX = os.environ.get("CXX") or shutil.which("clang++") or shutil.which("g++")


def target_path(number):
    return re.search(r"^\+\+\+ b/(.*)$", PATCHES[number].read_text(), re.M)[1]


def source_fixture(number):
    lines = []
    for first, text in SOURCE_SECTIONS[number]:
        assert len(lines) < first
        lines.extend("// unrelated source line\n" for _ in range(first - 1 - len(lines)))
        lines.extend(text.splitlines(keepends=True))
    lines.extend(["// not EOF\n", "// trailing source stays intact\n"])
    return "".join(lines)


def apply_patch(directory, number, *, reverse=False, dry_run=False):
    if PATCH_BIN is None:
        pytest.skip("GNU patch is required")
    command = [PATCH_BIN, "-p1", "--fuzz=0", "--batch", "--forward", "--get=0",
               "--no-backup-if-mismatch", "--reject-file=-", "-i", str(PATCHES[number])]
    if reverse:
        command.append("--reverse")
    if dry_run:
        command.append("--dry-run")
    result = subprocess.run(command, cwd=directory, text=True, capture_output=True,
                            timeout=15, env={**os.environ, "LC_ALL": "C", "PATCH_GET": "0"})
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "offset" not in output and "fuzz" not in output, output


@pytest.fixture(scope="module")
def patched_sources(tmp_path_factory):
    directory = tmp_path_factory.mktemp("gpu-patch-sources")
    sources = {}
    for number in SOURCE_PATCHES:
        path = directory / target_path(number)
        path.parent.mkdir(parents=True, exist_ok=True)
        original = source_fixture(number)
        path.write_text(original)
        apply_patch(directory, number, dry_run=True)
        assert path.read_text() == original
        apply_patch(directory, number)
        sources[number] = path.read_text()
        apply_patch(directory, number, reverse=True)
        assert path.read_text() == original
    return sources


@pytest.fixture(scope="module")
def patched_0030(tmp_path_factory):
    directory = tmp_path_factory.mktemp("gpu-0030-source")
    path = directory / target_path("0030")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PINNED_0030_UPSTREAM)
    original = path.read_text()
    apply_patch(directory, "0030", dry_run=True)
    assert path.read_text() == original
    apply_patch(directory, "0030")
    patched = path.read_text()
    apply_patch(directory, "0030", reverse=True)
    assert path.read_text() == original
    return patched


def test_0030_pinned_patch_is_strict_and_reversible(patched_0030):
    assert patched_0030 != PINNED_0030_UPSTREAM


@pytest.mark.parametrize("number", SOURCE_PATCHES)
def test_patch_applies_without_fuzz_or_offset_and_reverses(patched_sources, number):
    assert patched_sources[number] != source_fixture(number)


@pytest.mark.parametrize("number", SOURCE_PATCHES)
def test_fixture_matches_recovered_local_chromium(tmp_path, number):
    baseline = os.environ.get("CHROMIX_GPU_BASELINE_ROOT")
    if not baseline:
        pytest.skip("set CHROMIX_GPU_BASELINE_ROOT to verify full-source provenance")
    original_path = Path(baseline) / target_path(number)
    original, timestamp = original_path.read_bytes(), original_path.stat().st_mtime_ns
    source = original.decode()
    if number == "0110":
        if PATCH_BIN is None:
            pytest.skip("GNU patch is required for the WebGL predecessor chain")
        target = tmp_path / target_path(number)
        target.parent.mkdir(parents=True)
        target.write_bytes(original)
        for name in (ROOT / "patches/series").read_text().splitlines():
            if not name or name.startswith("#"):
                continue
            if Path(name).name.startswith("0110-"):
                break
            predecessor = ROOT / name
            if re.search(r"^\+\+\+ b/(.*)$", predecessor.read_text(), re.M)[1] != target_path(number):
                continue
            result = subprocess.run(
                [PATCH_BIN, "-p1", "--fuzz=0", "--batch", "--forward", "--get=0",
                 "--no-backup-if-mismatch", "--reject-file=-", "-i", str(predecessor)],
                cwd=tmp_path, text=True, capture_output=True, timeout=15,
                env={**os.environ, "LC_ALL": "C", "PATCH_GET": "0"})
            assert result.returncode == 0, result.stdout + result.stderr
            assert "fuzz" not in result.stdout
        source = target.read_text()
        for _, text in SOURCE_SECTIONS[number]:
            assert source.count(text) == 1
    else:
        lines = source.splitlines(keepends=True)
        for first, text in SOURCE_SECTIONS[number]:
            assert "".join(lines[first - 1:first - 1 + len(text.splitlines())]) == text
    assert (original_path.read_bytes(), original_path.stat().st_mtime_ns) == (original, timestamp)


def excerpt(source, start, end):
    begin = source.index(start)
    return source[begin:source.index(end, begin)]


def test_webgl_identity_queries_are_only_after_persona_return(patched_sources):
    source = patched_sources["0110"]
    for member, enum in (("renderer", "RENDERER"), ("vendor", "VENDOR")):
        case = excerpt(source, f"    case WebGLDebugRendererInfo::kUnmasked{member.title()}Webgl:",
                       "      SynthesizeGLError(")
        early = excerpt(case, "        if (!ungoogled::CurrentPersona().webgl_real)",
                        "        if (ungoogled::CurrentPersona().webgl_real)")
        assert "ContextGL" not in early
        assert f"CurrentPersona().webgl_{member}" in early
        assert case.index("ExtensionEnabled(") < case.index(early)
        assert f"ContextGL()->GetString(GL_{enum})" in case
    assert "if (isContextLost())\n    return ScriptValue::CreateNull" in source


def test_features_and_request_device_validation_are_preserved(patched_sources):
    source = patched_sources["0041"]
    assert 'config.Has("uxr-webgpu-features")' in source
    assert source.index("if (ph_allowed.empty())") < source.index("adapter.GetFeatures(")
    assert "const bool ph_filter = !ph_allowed.empty()" not in source
    assert "if (!features_->Has(f.AsEnum()))" in source
    assert "resolver->RejectWithTypeError(" in source
    assert "required_features_set.insert(AsDawnEnum(f));" in source
    assert "GPUSupportedLimits::Populate(&required_limits," in source
    assert "dawn_desc.requiredLimits = required_limits.GetLinked();" in source


def test_limits_constructor_is_native_for_adapter_and_device(patched_sources):
    source = patched_sources["0042"]
    constructor = excerpt(source, "GPUSupportedLimits::GPUSupportedLimits(", "// static")
    assert "limits.UnlinkedCopyTo(&limits_);" in constructor
    assert "UxrConfig" not in source
    assert "uxr-webgpu-limit-" not in source
    assert "static_cast<decltype(limits_.name)>" not in source
    assert "base::CheckedNumeric<T> value{limitRawIntegerValue}" in source
    assert "value.ValueOrDie() == UndefinedLimitValue<T>()" in source
    assert source.index("limitRawValue->IsUndefined()") < source.index(
        "std::has_single_bit(static_cast<T>(value.ValueOrDie()))")


@pytest.fixture(scope="module")
def runtime_binary(tmp_path_factory, patched_sources):
    if CXX is None:
        pytest.skip("a local C++20 compiler is required")
    features = excerpt(patched_sources["0041"], "GPUSupportedFeatures* MakeFeatureNameSet(",
                       "}  // anonymous namespace")
    requested_features = excerpt(patched_sources["0041"],
                                 "  if (descriptor->hasRequiredFeatures())", "  Vector<wgpu::FeatureName>")
    limits = "\n".join(line for line in patched_sources["0042"].splitlines()
                       if not line.startswith("#include"))
    names = re.findall(r"^  X\((\w+)\)", limits, re.M)
    u64 = {"maxUniformBufferBindingSize", "maxStorageBufferBindingSize", "maxBufferSize"}
    compat = {"maxStorageBuffersInFragmentStage", "maxStorageTexturesInFragmentStage",
              "maxStorageBuffersInVertexStage", "maxStorageTexturesInVertexStage"}
    fields = {name: "uint64_t" if name in u64 else "uint32_t" for name in names}
    native_structs = "namespace wgpu {\n"
    for struct, is_compat in (("Limits", False), ("CompatibilityModeLimits", True)):
        native_structs += f"struct {struct} {{ void* nextInChain = nullptr;\n"
        native_structs += "\n".join(f"  {type_} {name} = std::numeric_limits<{type_}>::max();"
                                     for name, type_ in fields.items() if (name in compat) == is_compat)
        native_structs += "\n};\n"
    native_structs += "}\n"
    declarations = "\n".join(f"  {type_} {name}() const;" for name, type_ in fields.items())
    limit_class = CPP_LIMIT_CLASS.replace("GETTER_DECLARATIONS", declarations)
    webgl_source = patched_sources["0110"]
    webgl_cases = excerpt(webgl_source, "    case WebGLDebugRendererInfo::kUnmaskedRendererWebgl:",
                          "    case GL_VERTEX_ARRAY_BINDING_OES:")
    webgl_guard = excerpt(webgl_source, "ScriptValue WebGLRenderingContextBase::getParameter(",
                          "  const int kIntZero = 0;")
    program = (CPP_SUPPORT + native_structs + limit_class + limits + "\n" + features +
               "\nbool ValidateFeatures(GPUSupportedFeatures* features_, Descriptor* descriptor, "
               "ScriptPromiseResolverBase* resolver) {\n"
               "  bool promise = false; std::set<wgpu::FeatureName> required_features_set;\n" +
               requested_features + "  return true;\n}\n" +
               CPP_WEBGL + webgl_guard + "  switch (pname) {\n" + webgl_cases +
               "    default: return ScriptValue::CreateNull(nullptr);\n  }\n}\n" +
               CPP_DAWN_PREFIX + DAWN_LIMIT_CHECKS + "}\n" + CPP_TESTS)
    directory = tmp_path_factory.mktemp("gpu-runtime")
    source = directory / "gpu.cc"
    source.write_text(program)
    binary = directory / "gpu"
    result = subprocess.run([CXX, "-std=c++20", "-O0", "-Wall", "-Wextra", "-Werror",
                             str(source), "-o", str(binary)], text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return binary


@pytest.mark.parametrize("case", ["features-unset", "features-empty", "features-whitespace",
                                  "features-intersection", "features-unknown", "features-no-adapter-features",
                                  "features-embedded-nul", "features-token-suffix",
                                  "request-features", "request-empty-features", "limits-native", "limits-u32", "limits-u64",
                                  "limits-alignment", "limits-undefined", "limits-direction",
                                  "webgl-persona", "webgl-real", "webgl-context-lost", "webgl-extension-disabled"])
def test_executable_gpu_contract(runtime_binary, case):
    result = subprocess.run([str(runtime_binary), case], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture(scope="module")
def persona_sources(tmp_path_factory):
    directory = tmp_path_factory.mktemp("gpu-persona-sources")
    sources = {}
    for number in PERSONA_PATCHES:
        path = directory / target_path(number)
        path.parent.mkdir(parents=True, exist_ok=True)
        apply_patch(directory, number, dry_run=True)
        assert not path.exists()
        apply_patch(directory, number)
        sources[number] = path.read_text()
        apply_patch(directory, number, reverse=True)
        assert not path.exists()
    return sources


@pytest.mark.parametrize("number", PERSONA_PATCHES)
def test_persona_new_file_patch_applies_and_reverses(persona_sources, number):
    assert persona_sources[number].startswith("// Copyright 2026.\n")


def test_config_stub_typed_numeric_parsers(tmp_path):
    if CXX is None:
        pytest.skip("a local C++20 compiler is required")
    support = CPP_SUPPORT[:CPP_SUPPORT.index("namespace wgpu {")]
    source = tmp_path / "config.cc"
    source.write_text(support + r'''
int main() {
  base::UxrConfig config;
  int integer = 7;
  uint64_t seed = 7;
  double real = 7;
  assert(!config.GetInt("missing", &integer) && integer == 7);
  assert(!config.GetUint64("missing", &seed) && seed == 7);
  assert(!config.GetDouble("missing", &real) && real == 7);
  for (const char* raw : {"", "+", "-", "+-1", "++1", "--1", " 1", "1 ",
                          "1x", "abc", "0x10", "9999999999999999999999999999999999999999"}) {
    config.values["value"] = raw;
    assert(!config.GetInt("value", &integer));
    assert(!config.GetUint64("value", &seed));
  }
  for (const char* raw : {"", "+", "-", "+-1", "++1", "--1", " 1", "1 ",
                          "1x", "abc", "0x10", "nan", "inf", "-inf", "1e309"}) {
    config.values["value"] = raw;
    assert(!config.GetDouble("value", &real));
  }
  for (int value : {std::numeric_limits<int>::min(), -1, 0, 1,
                    std::numeric_limits<int>::max()}) {
    config.values["value"] = std::to_string(value);
    assert(config.GetInt("value", &integer) && integer == value);
  }
  for (const char* raw : {"2147483648", "-2147483649", "1.5", "1e2"}) {
    config.values["value"] = raw;
    assert(!config.GetInt("value", &integer));
  }
  config.values["value"] = "18446744073709551615";
  assert(config.GetUint64("value", &seed) && seed == std::numeric_limits<uint64_t>::max());
  for (const char* raw : {"-0", "-1", "18446744073709551616", "1.5", "1e2"}) {
    config.values["value"] = raw;
    assert(!config.GetUint64("value", &seed));
  }
  config.values["value"] = "+42";
  assert(config.GetInt("value", &integer) && integer == 42);
  assert(config.GetUint64("value", &seed) && seed == 42);
  assert(config.GetDouble("value", &real) && real == 42);
  config.values["value"] = "+1.5e2";
  assert(config.GetDouble("value", &real) && real == 150);
  config.values["value"] = "-0.25";
  assert(config.GetDouble("value", &real) && real == -0.25);
  config.values["value"] = "1.7976931348623157e308";
  assert(config.GetDouble("value", &real) && real == std::numeric_limits<double>::max());
  config.values["value"] = std::string("1\0x", 3);
  assert(!config.GetInt("value", &integer));
  assert(!config.GetUint64("value", &seed));
  assert(!config.GetDouble("value", &real));
}
''')
    binary = tmp_path / "config"
    result = subprocess.run([CXX, "-std=c++20", "-O0", "-Wall", "-Wextra", "-Werror",
                             str(source), "-o", str(binary)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


SIMULATED_PLATFORMS = {
    "windows-x86_64": ("IS_WIN", "ARCH_CPU_X86_64"),
    "windows-x86": ("IS_WIN", "ARCH_CPU_X86"),
    "windows-arm64": ("IS_WIN", "ARCH_CPU_ARM64"),
    "linux-x86_64": ("IS_LINUX", "ARCH_CPU_X86_64"),
    "mac-arm64": ("IS_MAC", "ARCH_CPU_ARM64"),
}


@pytest.fixture(scope="module", params=SIMULATED_PLATFORMS)
def identity_binary(request, tmp_path_factory, patched_sources, patched_0030, persona_sources):
    if CXX is None:
        pytest.skip("a local C++20 compiler is required")
    directory = tmp_path_factory.mktemp(f"gpu-identity-{request.param}")
    os_flag, arch_flag = SIMULATED_PLATFORMS[request.param]
    build_config = "#define BUILDFLAG(flag) flag\n"
    for flag in ("IS_WIN", "IS_LINUX", "IS_MAC"):
        build_config += f"#define {flag} {int(flag == os_flag)}\n"
    build_config += f"#define {arch_flag} 1\n"
    support = CPP_SUPPORT[:CPP_SUPPORT.index("namespace wgpu {")]
    files = {
        target_path("0091"): persona_sources["0091"],
        target_path("0092"): persona_sources["0092"],
        target_path("0030"): patched_0030,
        "base/component_export.h": "#define COMPONENT_EXPORT(component)\n",
        "base/uxr_config.h": "#pragma once\n" + support,
        "base/strings/string_util.h": '#include "base/uxr_config.h"\n',
        "base/strings/string_number_conversions.h": CPP_NUMBER_CONVERSIONS_STUB,
        "build/build_config.h": build_config,
        "third_party/blink/renderer/modules/webgpu/gpu_adapter_info.h": CPP_ADAPTER_INFO_STUB,
        "third_party/blink/renderer/modules/webgpu/gpu_memory_heap_info.h": "#pragma once\n",
        "third_party/blink/renderer/modules/webgpu/gpu_subgroup_matrix_config.h": "#pragma once\n",
        "identity.cc": CPP_IDENTITY_MAIN,
    }
    for name, text in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    binary = directory / "identity"
    result = subprocess.run([CXX, "-std=c++20", "-O0", "-Wall", "-Wextra", "-Werror",
                             "-I", str(directory), str(directory / "identity.cc"),
                             "-o", str(binary)], text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return binary, request.param


NATIVE_CAPABILITIES = {
    "subgroup_min": "8", "subgroup_max": "64", "d3d_shader_model": "66",
    "vk_driver_version": "16909060", "power_preference": "high-performance",
    "memory_heap": "1048576", "matrix_config": "16",
}


def assert_native_adapter(result, prefix="hardware"):
    expected = {
        "vendor": "native-vendor", "architecture": "native-architecture",
        "device": "0x1234", "description": "native-description", "driver": "native-driver",
        "fallback": "1" if prefix == "software" else "0",
        "backend": "Vulkan", "type": "CPU" if prefix == "software" else "DiscreteGPU",
        **NATIVE_CAPABILITIES,
    }
    assert {key: result[f"{prefix}.{key}"] for key in expected} == expected


def run_identity(identity_binary, config, synthetic=True):
    if synthetic:
        config = {"uxr-synthetic-device-tests":"true", **config}
    binary, _ = identity_binary
    result = subprocess.run([str(binary), *(f"{key}={value}" for key, value in config.items())],
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    values = dict(line.split("\t", 1) for line in result.stdout.splitlines())
    assert values["persona.cached"] == "1"
    # Every identity configuration must leave software identity and capabilities native.
    assert_native_adapter(values, "software")
    for key, value in NATIVE_CAPABILITIES.items():
        assert values[f"hardware.{key}"] == value
    assert values["hardware.fallback"] == "0"
    assert values["hardware.backend"] == "Vulkan"
    assert values["hardware.type"] == "DiscreteGPU"
    return values


def has_windows_pool(identity_binary):
    return identity_binary[1] in ("windows-x86_64", "windows-x86")


def test_seed_alone_does_not_select_synthetic_gpu(identity_binary):
    result = run_identity(identity_binary, {"uxr-fingerprint-seed":"42"}, synthetic=False)
    assert_native_adapter(result, "hardware")


def test_explicit_gpu_identity_works_without_synthetic_opt_in(identity_binary):
    result = run_identity(identity_binary, {
        'uxr-webgl-vendor':'fake', 'uxr-webgl-renderer':'fake renderer',
        'uxr-webgpu-vendor':'fake', 'uxr-webgpu-architecture':'fake arch',
        'uxr-webgl-max-texture-size':'1', 'uxr-webgl-fingerprint':'true',
    }, synthetic=False)
    assert_synthetic_adapter(result, 'fake', 'fake arch')
    assert result['persona.webgl_identity_explicit'] == '1'


GPU_TUPLES = {
    "intel": ("gen-12-lp", "Google Inc. (Intel)",
              "ANGLE (Intel, Intel(R) UHD Graphics 770 (0x0000A780) Direct3D11 "
              "vs_5_0 ps_5_0, D3D11)"),
    "nvidia": ("ampere", "Google Inc. (NVIDIA)",
               "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    "amd": ("gcn-5", "Google Inc. (AMD)",
            "ANGLE (AMD, AMD Radeon(TM) Graphics (0x000015D8) Direct3D11 vs_5_0 "
            "ps_5_0, D3D11)"),
}

# Standard MT19937-64 first outputs, independently calculated from its recurrence.
# The last four seeds pin both sides of the integer bucket boundaries 50 and 85.
GOLDEN_SEEDS = [
    (1, 2469588189546311528, "intel"),
    (2, 16668552215174154828, "intel"),
    (3, 10307413207671831467, "nvidia"),
    (4, 14490808261858112199, "amd"),
    (17, 12858804418306843259, "nvidia"),
    (5489, 14514284786278117030, "intel"),
    (18446744073709551615, 478026398904862820, "intel"),
    (138, 4756714960551627549, "intel"),
    (249, 13298449028599932850, "nvidia"),
    (172, 1985853089155243584, "nvidia"),
    (175, 10464333263917161985, "amd"),
]


def assert_synthetic_adapter(result, vendor, architecture, description=""):
    assert result["hardware.vendor"] == vendor
    assert result["hardware.architecture"] == architecture
    assert result["hardware.description"] == description
    assert result["hardware.device"] == ""
    assert result["hardware.driver"] == ""


def assert_template(result, vendor):
    architecture, webgl_vendor, renderer = GPU_TUPLES[vendor]
    assert result["persona.webgl_real"] == "0"
    assert result["persona.webgl_identity_explicit"] == "0"
    assert result["persona.webgpu_vendor"] == vendor
    assert result["persona.webgpu_architecture"] == architecture
    assert result["persona.webgl_vendor"] == webgl_vendor
    assert result["persona.webgl_renderer"] == renderer
    assert_synthetic_adapter(result, vendor, architecture)



def assert_platform_template(result, identity_binary, platform=None, architecture=None):
    """Independent expected platform families; seed recurrence is pinned above."""
    machine = identity_binary[1]
    platform = (platform or ('windows' if machine.startswith('windows') else
                            'macos' if machine.startswith('mac') else 'linux')).lower()
    bucket = int(result['engine_first']) % 100
    if platform in ('windows', 'win32'):
        vendor = 'intel' if bucket < 50 else 'nvidia' if bucket < 85 else 'amd'
        assert_template(result, vendor)
        return
    if platform == 'linux':
        vendor = 'intel' if bucket < 50 else 'nvidia' if bucket < 85 else 'amd'
        architecture_expected = {'intel':'gen-12-lp', 'nvidia':'ampere', 'amd':'rdna-2'}[vendor]
        model = {'intel':'UHD Graphics 770', 'nvidia':'RTX 3060', 'amd':'RX 6600'}[vendor]
        backend = 'OpenGL'
    else:
        arm = architecture == 'arm' or (architecture is None and machine.endswith('arm64'))
        if arm:
            generation = 1 if bucket < 35 else 2 if bucket < 70 else 3
            vendor, architecture_expected, model = 'apple', f'apple-{generation+6}', f'Apple M{generation}'
        else:
            vendor = 'intel' if bucket < 65 else 'amd'
            architecture_expected = 'gen-9' if vendor == 'intel' else 'rdna-1'
            model = 'Iris(TM) Plus Graphics 655' if vendor == 'intel' else 'Radeon Pro 5500M'
        backend = 'Metal'
    assert result['persona.webgl_real'] == '0'
    assert result['persona.webgl_identity_explicit'] == '0'
    assert result['persona.webgpu_vendor'] == vendor
    assert result['persona.webgpu_architecture'] == architecture_expected
    assert model in result['persona.webgl_renderer'] and backend in result['persona.webgl_renderer']
    assert vendor in result['persona.webgl_vendor'].lower()
    assert_synthetic_adapter(result, vendor, architecture_expected)


def assert_real_native(result):
    assert result["persona.webgl_real"] == "1"
    assert result["persona.webgpu_vendor"] == ""
    assert result["persona.webgpu_architecture"] == ""
    assert_native_adapter(result)


@pytest.mark.parametrize("seed,engine_output,vendor", GOLDEN_SEEDS,
                         ids=[str(row[0]) for row in GOLDEN_SEEDS])
def test_identity_golden_seed_and_noise_independence(identity_binary, seed, engine_output, vendor):
    config = {"uxr-fingerprint-seed": str(seed)}
    baseline = run_identity(identity_binary, config)
    assert baseline["persona.seed"] == str(seed)
    assert baseline["engine_first"] == str(engine_output)
    assert_platform_template(baseline, identity_binary)
    for noise in ({"uxr-canvas-seed": str(seed)}, {"uxr-canvas-seed": "0"},
                  {"uxr-canvas-seed": str(seed ^ 0x1234)},
                  {"uxr-disable-fingerprint-noise": "", "uxr-webgl-fingerprint": "false"}):
        assert run_identity(identity_binary, {**config, **noise}) == baseline
    assert run_identity(identity_binary, config) == baseline


@pytest.mark.parametrize("raw", [None, "", "0", "invalid", "-1", "18446744073709551616"])
def test_identity_missing_zero_invalid_seed_stays_native(identity_binary, raw):
    config = {} if raw is None else {"uxr-fingerprint-seed": raw}
    result = run_identity(identity_binary, config)
    assert result["persona.seed"] == "0"
    assert_real_native(result)


@pytest.mark.parametrize("raw,seed", [(None, 17), ("", 17), ("0", 0), ("invalid", 0),
                                     ("-1", 0), ("18446744073709551616", 0)])
def test_identity_canvas_seed_fallback_and_fingerprint_precedence(identity_binary, raw, seed):
    config = {"uxr-canvas-seed": "17"}
    if raw is not None:
        config["uxr-fingerprint-seed"] = raw
    result = run_identity(identity_binary, config)
    assert result["persona.seed"] == str(seed)
    if seed:
        assert_platform_template(result, identity_binary)
    else:
        assert_real_native(result)


@pytest.mark.parametrize("platform,architecture,eligible", [
    (None, None, True), ("windows", "x86", True), ("Windows", "x86", True),
    ("win32", "x86", True), ("linux", "x86", True), ("macOS", "x86", True),
    ("android", "arm", False), ("windows", "arm", True), ("windows", "arm64", True),
])
def test_identity_platform_and_architecture_gate(identity_binary, platform, architecture, eligible):
    config = {"uxr-fingerprint-seed": "4"}
    if platform is not None:
        config["uxr-platform"] = platform
    if architecture is not None:
        config["uxr-ua-arch"] = architecture
    result = run_identity(identity_binary, config)
    if eligible:
        assert_platform_template(result, identity_binary, platform, architecture)
    else:
        assert_real_native(result)


@pytest.mark.parametrize("key", ["uxr-webgl-real", "uxr-disable-gpu-fingerprint"])
@pytest.mark.parametrize("value,enabled", [(None, False), ("false", False), ("0", False),
                                         ("", True), ("true", True), ("1", True)])
def test_identity_real_switch_truth_table(identity_binary, key, value, enabled):
    config = {"uxr-fingerprint-seed": "3"}
    if value is not None:
        config[key] = value
    result = run_identity(identity_binary, config)
    if not enabled:
        assert_platform_template(result, identity_binary)
    else:
        assert_real_native(result)
    # A complete explicit WebGL identity makes false observable on every platform.
    config.update({"uxr-webgl-vendor": "explicit-vendor", "uxr-webgl-renderer": "explicit-renderer"})
    result = run_identity(identity_binary, config)
    assert result["persona.webgl_real"] == str(int(enabled))
    assert result["persona.webgl_identity_explicit"] == str(int(not enabled))
    if enabled:
        assert_real_native(result)
    else:
        assert result["persona.webgl_vendor"] == "explicit-vendor"
        assert result["persona.webgl_renderer"] == "explicit-renderer"
        assert_synthetic_adapter(result, "", "")


@pytest.mark.parametrize("real,disabled", [("false", "true"), ("true", "false"), ("0", "1")])
def test_identity_real_switches_are_combined(identity_binary, real, disabled):
    result = run_identity(identity_binary, {"uxr-fingerprint-seed": "4", "uxr-webgl-real": real,
                                         "uxr-disable-gpu-fingerprint": disabled})
    assert_real_native(result)


@pytest.mark.parametrize("seed", [None, "0", "invalid", "3"])
@pytest.mark.parametrize("vendor", ["Google Inc. (Intel)", "Google Inc. (NVIDIA)",
                                  "Google Inc. (AMD)", "Apple", "custom-vendor"])
def test_complete_webgl_identity_does_not_guess_webgpu_architecture(identity_binary, seed, vendor):
    config = {"uxr-webgl-vendor": vendor, "uxr-webgl-renderer": "custom-renderer"}
    if seed is not None:
        config["uxr-fingerprint-seed"] = seed
    result = run_identity(identity_binary, config)
    assert result["persona.webgl_real"] == "0"
    assert result["persona.webgl_identity_explicit"] == "1"
    assert result["persona.webgl_vendor"] == vendor
    assert result["persona.webgl_renderer"] == "custom-renderer"
    assert result["persona.webgpu_vendor"] == ""
    assert result["persona.webgpu_architecture"] == ""
    assert_synthetic_adapter(result, "", "")


@pytest.mark.parametrize("vendor,renderer", [("explicit-vendor", None), (None, "explicit-renderer"),
                                           ("explicit-vendor", ""), ("", "explicit-renderer"),
                                           ("", ""), ("", None), (None, "")])
@pytest.mark.parametrize("seed", [None, "4"])
def test_partial_webgl_identity_does_not_invent_unknown_counterparts(identity_binary, vendor, renderer, seed):
    config = {}
    for key, value in (("uxr-webgl-vendor", vendor), ("uxr-webgl-renderer", renderer),
                       ("uxr-fingerprint-seed", seed)):
        if value is not None:
            config[key] = value
    result = run_identity(identity_binary, config)
    if vendor or renderer:
        assert result["persona.webgl_identity_explicit"] == "1"
        assert result["persona.webgl_vendor"] == (vendor or "")
        assert result["persona.webgl_renderer"] == (renderer or "")
        assert_synthetic_adapter(result, "", "")
    elif seed:
        assert_platform_template(result, identity_binary)
    else:
        assert_real_native(result)


@pytest.mark.parametrize("overrides", [
    {}, {"uxr-webgl-real": "true"}, {"uxr-disable-gpu-fingerprint": "1"},
    {"uxr-platform": "android", "uxr-ua-arch": "arm"},
    {"uxr-webgl-vendor": "Google Inc. (AMD)", "uxr-webgl-renderer": "custom-renderer"},
    {"uxr-webgl-vendor": "partial-vendor"},
])
@pytest.mark.parametrize("seed", ["0", "4"])
@pytest.mark.parametrize("description", [None, "", "explicit-description"])
def test_complete_webgpu_identity_overrides_hardware_only(identity_binary, overrides, seed, description):
    config = {"uxr-fingerprint-seed": seed, "uxr-webgpu-vendor": "explicit-vendor",
              "uxr-webgpu-architecture": "explicit-architecture", **overrides}
    if description is not None:
        config["uxr-webgpu-description"] = description
    result = run_identity(identity_binary, config)
    assert_synthetic_adapter(result, "explicit-vendor", "explicit-architecture", description or "")


@pytest.mark.parametrize("vendor,architecture", [("intel", None), ("nvidia", None), ("amd", None),
                                               (None, "custom-architecture"), ("intel", ""),
                                               ("", "custom-architecture"), ("", "")])
@pytest.mark.parametrize("context", ["no-seed", "seeded", "real", "explicit-webgl"])
def test_partial_webgpu_identity_never_mixes_native_and_synthetic(identity_binary, vendor, architecture, context):
    config = {"uxr-webgpu-description": "must-not-leak"}
    if context != "no-seed":
        config["uxr-fingerprint-seed"] = "4"
    if context == "real":
        config["uxr-webgl-real"] = "true"
    if context == "explicit-webgl":
        config.update({"uxr-webgl-vendor": "Google Inc. (NVIDIA)", "uxr-webgl-renderer": "custom"})
    if vendor is not None:
        config["uxr-webgpu-vendor"] = vendor
    if architecture is not None:
        config["uxr-webgpu-architecture"] = architecture
    result = run_identity(identity_binary, config)
    if context == "seeded":
        assert_platform_template(result, identity_binary)
    elif context == "explicit-webgl":
        assert_synthetic_adapter(result, "", "")
    else:
        assert_native_adapter(result)


@pytest.mark.parametrize("seed", ["0", "4"])
def test_description_alone_cannot_replace_native_or_template_identity(identity_binary, seed):
    result = run_identity(identity_binary, {"uxr-fingerprint-seed": seed,
                                         "uxr-webgpu-description": "must-not-leak"})
    if seed == "4":
        assert_platform_template(result, identity_binary)
    else:
        assert_native_adapter(result)


CPP_NUMBER_CONVERSIONS_STUB = r'''
#pragma once
#include <charconv>
#include <string>
namespace base {
bool StringToUint64(const std::string& raw, uint64_t* out) {
  auto result = std::from_chars(raw.data(), raw.data() + raw.size(), *out);
  return result.ec == std::errc() && result.ptr == raw.data() + raw.size();
}
bool StringToDouble(const std::string& raw, double* out) {
  auto result = std::from_chars(raw.data(), raw.data() + raw.size(), *out);
  return result.ec == std::errc() && result.ptr == raw.data() + raw.size();
}
}
'''


CPP_ADAPTER_INFO_STUB = r'''
#pragma once
#include "base/uxr_config.h"
namespace blink {
template <typename T> using Member = T*;
template <typename T> using HeapVector = std::vector<T>;
struct GPUMemoryHeapInfo { uint64_t size; };
struct GPUSubgroupMatrixConfig { uint32_t component_count; };
struct Visitor { template <typename T> void Trace(const T&) {} };
struct ScriptWrappable { void Trace(Visitor*) const {} };
class GPUAdapterInfo : public ScriptWrappable {
 public:
  GPUAdapterInfo(const String&, const String&, uint32_t, uint32_t, bool,
                 const String&, const String&, const String&, const String&,
                 const String&, std::optional<uint32_t>, std::optional<uint32_t>, const String&);
  void AppendMemoryHeapInfo(GPUMemoryHeapInfo*);
  void AppendSubgroupMatrixConfig(GPUSubgroupMatrixConfig*);
  const String& vendor() const;
  const String& architecture() const;
  const String& device() const;
  const String& description() const;
  uint32_t subgroupMinSize() const;
  uint32_t subgroupMaxSize() const;
  bool isFallbackAdapter() const;
  const String& driver() const;
  const String& backend() const;
  const String& type() const;
  const HeapVector<Member<GPUMemoryHeapInfo>>& memoryHeaps() const;
  const HeapVector<Member<GPUSubgroupMatrixConfig>>& subgroupMatrixConfigs() const;
  const std::optional<uint32_t>& d3dShaderModel() const;
  const std::optional<uint32_t>& vkDriverVersion() const;
  const String& powerPreference() const;
  void Trace(Visitor*) const;
 private:
  String vendor_, architecture_;
  uint32_t subgroup_min_size_, subgroup_max_size_;
  bool is_fallback_adapter_;
  String device_, description_, driver_, backend_, type_;
  HeapVector<Member<GPUMemoryHeapInfo>> memory_heaps_;
  HeapVector<Member<GPUSubgroupMatrixConfig>> subgroup_matrix_configs_;
  std::optional<uint32_t> d3d_shader_model_, vk_driver_version_;
  String power_preference_;
};
}
'''


CPP_IDENTITY_MAIN = r'''
#include <iostream>
#include "base/uxr_config.h"
#include "components/ungoogled/persona_profile.cc"
#include "third_party/blink/renderer/modules/webgpu/gpu_adapter_info.cc"

template <typename T> void Emit(const std::string& key, const T& value) {
  std::cout << key << '\t' << value << '\n';
}
void EmitAdapter(const std::string& prefix, bool fallback) {
  blink::GPUAdapterInfo adapter("native-vendor", "native-architecture", 8, 64, fallback,
      "0x1234", "native-description", "native-driver", "Vulkan", fallback ? "CPU" : "DiscreteGPU",
      uint32_t{66}, uint32_t{0x01020304}, "high-performance");
  blink::GPUMemoryHeapInfo heap{1048576};
  blink::GPUSubgroupMatrixConfig matrix{16};
  assert(adapter.memoryHeaps().empty() && adapter.subgroupMatrixConfigs().empty());
  adapter.AppendMemoryHeapInfo(&heap);
  adapter.AppendSubgroupMatrixConfig(&matrix);
  assert(adapter.memoryHeaps().size() == 1 && adapter.memoryHeaps()[0] == &heap);
  assert(adapter.subgroupMatrixConfigs().size() == 1 && adapter.subgroupMatrixConfigs()[0] == &matrix);
  Emit(prefix + ".vendor", adapter.vendor().value);
  Emit(prefix + ".architecture", adapter.architecture().value);
  Emit(prefix + ".device", adapter.device().value);
  Emit(prefix + ".description", adapter.description().value);
  Emit(prefix + ".driver", adapter.driver().value);
  Emit(prefix + ".fallback", adapter.isFallbackAdapter());
  Emit(prefix + ".backend", adapter.backend().value);
  Emit(prefix + ".type", adapter.type().value);
  Emit(prefix + ".subgroup_min", adapter.subgroupMinSize());
  Emit(prefix + ".subgroup_max", adapter.subgroupMaxSize());
  Emit(prefix + ".d3d_shader_model", adapter.d3dShaderModel().value());
  Emit(prefix + ".vk_driver_version", adapter.vkDriverVersion().value());
  Emit(prefix + ".power_preference", adapter.powerPreference().value);
  Emit(prefix + ".memory_heap", adapter.memoryHeaps()[0]->size);
  Emit(prefix + ".matrix_config", adapter.subgroupMatrixConfigs()[0]->component_count);
}
int main(int argc, char** argv) {
  auto& config = base::UxrConfig::GetInstance();
  for (int i = 1; i < argc; ++i) {
    std::string arg = argv[i];
    size_t equals = arg.find('=');
    assert(equals != std::string::npos);
    config.values[arg.substr(0, equals)] = arg.substr(equals + 1);
  }
  const auto& persona = ungoogled::CurrentPersona();
  Emit("persona.webgl_real", persona.webgl_real);
  Emit("persona.webgl_identity_explicit", persona.webgl_identity_explicit);
  Emit("persona.webgl_fingerprint", persona.webgl_fingerprint);
  Emit("persona.webgl_vendor", persona.webgl_vendor);
  Emit("persona.webgl_renderer", persona.webgl_renderer);
  Emit("persona.webgpu_vendor", persona.webgpu_vendor);
  Emit("persona.webgpu_architecture", persona.webgpu_architecture);
  const uint64_t seed = ungoogled::SeedFromConfig(config);
  Emit("persona.seed", seed);
  Emit("engine_first", std::mt19937_64(seed)());
  EmitAdapter("hardware", false);
  EmitAdapter("software", true);
  const std::string vendor = persona.webgl_vendor;
  const std::string renderer = persona.webgl_renderer;
  config.values = {{"uxr-webgl-vendor", "changed-vendor"}, {"uxr-webgl-renderer", "changed-renderer"}};
  const auto& cached = ungoogled::CurrentPersona();
  Emit("persona.cached", &cached == &persona && cached.webgl_vendor == vendor && cached.webgl_renderer == renderer);
}
'''


CPP_SUPPORT = r'''
#include <algorithm>
#include <bit>
#include <cassert>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <initializer_list>
#include <limits>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <span>
#include <string>
#include <utility>
#include <vector>
#define DCHECK(x) assert(x)
#define UNSAFE_BUFFERS(x) x
#define UNSAFE_TODO(x) x
class String {
 public:
  std::string value;
  String() = default;
  String(const char* v) : value(v) {}
  String(std::string v) : value(std::move(v)) {}
  String ToAsciiLower() const {
    std::string out = value;
    for (char& c : out) if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
    return out;
  }
  std::string Utf8() const { return value; }
  bool operator==(const char* other) const { return value == other; }
  static String Number(uint64_t v) { return std::to_string(v); }
  static String Format(const char* format, const char*) { return format; }
};
String StrCat(std::initializer_list<String> parts) {
  std::string out; for (const auto& part : parts) out += part.value; return out;
}
namespace base {
std::string ToLowerASCII(std::string value) {
  for (char& c : value) if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
  return value;
}
template <typename T> using span = std::span<T>;
struct UxrConfig {
  std::map<std::string, std::string> values;
  static UxrConfig& GetInstance() { static UxrConfig config; return config; }
  bool Has(const std::string& key) const { return values.contains(key); }
  template <typename T> bool GetNumber(const std::string& key, T* out) const {
    auto it = values.find(key);
    if (it == values.end()) return false;
    const std::string& raw = it->second;
    const char* begin = raw.data();
    const char* end = begin + raw.size();
    if (begin != end && *begin == '+') {
      ++begin;
      if (begin != end && (*begin == '+' || *begin == '-')) return false;
    }
    auto result = std::from_chars(begin, end, *out);
    return result.ec == std::errc() && result.ptr == end;
  }
  bool GetInt(const std::string& key, int* out) const {
    return GetNumber(key, out);
  }
  bool GetDouble(const std::string& key, double* out) const {
    return GetNumber(key, out) && std::isfinite(*out);
  }
  bool GetUint64(const std::string& key, uint64_t* out) const {
    return GetNumber(key, out);
  }
  std::string Get(const std::string& key) const {
    auto it = values.find(key); return it == values.end() ? "" : it->second;
  }
};
enum { TRIM_WHITESPACE, SPLIT_WANT_NONEMPTY };
std::vector<std::string> SplitString(const std::string& raw, const char*, int, int) {
  std::vector<std::string> out;
  size_t begin = 0;
  while (begin <= raw.size()) {
    size_t end = raw.find(',', begin);
    if (end == std::string::npos) end = raw.size();
    std::string token = raw.substr(begin, end - begin);
    size_t first = token.find_first_not_of(" \t\r\n\v\f");
    if (first != std::string::npos)
      out.push_back(token.substr(first, token.find_last_not_of(" \t\r\n\v\f") - first + 1));
    begin = end + 1;
  }
  return out;
}
namespace base::numerics_internal {
template <typename T> class StrictNumeric {
 public:
  constexpr StrictNumeric(T value) : value_(value) {}
  constexpr operator T() const { return value_; }

 private:
  T value_;
};
}
template <typename T> struct CheckedNumeric {
  uint64_t value;
  bool IsValid() const { return value <= std::numeric_limits<T>::max(); }
  base::numerics_internal::StrictNumeric<T> ValueOrDie() const {
    assert(IsValid());
    return static_cast<T>(value);
  }
};
}
template <typename T, typename... Args> T* MakeGarbageCollected(Args&&... args) {
  return new T(std::forward<Args>(args)...);
}
namespace wgpu {
constexpr uint32_t kLimitU32Undefined = UINT32_MAX;
constexpr uint64_t kLimitU64Undefined = UINT64_MAX;
enum class FeatureName { DepthClipControl, TextureCompressionBC, Internal };
struct SupportedFeatures { const FeatureName* features = nullptr; size_t featureCount = 0; };
struct Adapter {
  std::vector<FeatureName> native;
  int* calls;
  void GetFeatures(SupportedFeatures* out) {
    ++*calls; out->features = native.data(); out->featureCount = native.size();
  }
};
}
class V8GPUFeatureName {
 public:
  using Enum = wgpu::FeatureName;
  explicit V8GPUFeatureName(Enum value) : value_(value) {}
  Enum AsEnum() const { return value_; }
  String AsString() const { return AsCStr(); }
  const char* AsCStr() const {
    return value_ == Enum::DepthClipControl ? "depth-clip-control" : "texture-compression-bc";
  }
 private:
  Enum value_;
};
wgpu::FeatureName AsDawnEnum(const V8GPUFeatureName& f) { return f.AsEnum(); }
struct GPUSupportedFeatures {
  std::set<wgpu::FeatureName> names;
  const auto& FeatureNameSet() const { return names; }
  void AddFeatureName(V8GPUFeatureName name) { names.insert(name.AsEnum()); }
  bool Has(wgpu::FeatureName name) const { return names.contains(name); }
  static std::optional<wgpu::FeatureName> ToV8FeatureNameEnum(wgpu::FeatureName f) {
    return f == wgpu::FeatureName::Internal ? std::nullopt : std::optional(f);
  }
};
struct Descriptor {
  std::vector<V8GPUFeatureName> features;
  bool hasRequiredFeatures() const { return !features.empty(); }
  const auto& requiredFeatures() const { return features; }
};
namespace mojom::blink {
enum class ConsoleMessageSource { kRendering };
enum class ConsoleMessageLevel { kWarning };
}
struct ConsoleMessage {
  ConsoleMessage(mojom::blink::ConsoleMessageSource, mojom::blink::ConsoleMessageLevel, String) {}
};
struct ExecutionContext {
  int warnings = 0;
  void AddConsoleMessage(ConsoleMessage* message) { ++warnings; delete message; }
};
enum class DOMExceptionCode { kOperationError };
struct ScriptPromiseResolverBase {
  bool rejected = false;
  bool type_error = false;
  ExecutionContext context;
  ExecutionContext* GetExecutionContext() { return &context; }
  void RejectWithDOMException(DOMExceptionCode, String) { rejected = true; }
  void RejectWithTypeError(String) { rejected = true; type_error = true; }
};
template <typename T> using Member = T*;
template <typename T> using HeapVector = std::vector<T>;
namespace blink {
struct V8UnionUndefinedOrUnsignedLongLongEnforceRange {
  std::optional<uint64_t> value;
  bool IsUndefined() const { return !value; }
  uint64_t GetAsUnsignedLongLongEnforceRange() const { return value.value(); }
};
}
'''

CPP_LIMIT_CLASS = r'''
namespace blink {
struct GPUSupportedLimits {
  struct ComboLimits : wgpu::Limits, wgpu::CompatibilityModeLimits {
    ComboLimits();
    void UnlinkedCopyTo(ComboLimits*) const;
    wgpu::Limits* GetLinked();
  };
  explicit GPUSupportedLimits(const ComboLimits&);
  static bool Populate(ComboLimits*, const HeapVector<std::pair<String,
      Member<V8UnionUndefinedOrUnsignedLongLongEnforceRange>>>&, ScriptPromiseResolverBase*);
GETTER_DECLARATIONS
 private:
  ComboLimits limits_;
};
}
'''

CPP_WEBGL = r'''
using GLenum = unsigned;
constexpr GLenum GL_RENDERER = 1, GL_VENDOR = 2, GL_INVALID_ENUM = 3;
constexpr int kWebGLDebugRendererInfoName = 1;
namespace WebGLDebugRendererInfo {
constexpr GLenum kUnmaskedRendererWebgl = 10, kUnmaskedVendorWebgl = 11;
}
namespace ungoogled {
struct Persona {
  bool webgl_real = false, webgl_fingerprint = false, webgl_identity_explicit = false;
  std::string webgl_renderer = "persona renderer", webgl_vendor = "persona vendor";
  std::string webgpu_vendor, webgpu_architecture;
};
Persona persona;
const Persona& CurrentPersona() { return persona; }
uint64_t GetFarbleSeed64(const std::string&) { assert(false); return 0; }
}
std::string GetGLRendererStringForFingerprint(uint64_t) { assert(false); return ""; }
std::string GetGLVendorStringForFingerprint() { assert(false); return ""; }
struct SecurityOrigin { String RegistrableDomain() const { return "example.test"; } };
struct WebGLExecutionContext { const SecurityOrigin* GetSecurityOrigin() { return nullptr; } };
struct ScriptState { void* GetIsolate() const { return nullptr; } };
struct ScriptValue {
  std::optional<std::string> value;
  static ScriptValue CreateNull(void*) { return {}; }
};
ScriptValue WebGLAny(ScriptState*, String value) { return {value.value}; }
struct GL {
  int calls = 0;
  const char* GetString(GLenum name) {
    ++calls; return name == GL_RENDERER ? "native renderer" : "native vendor";
  }
};
struct WebGLRenderingContextBase {
  bool lost = false, extension = true;
  int errors = 0;
  GL gl;
  bool isContextLost() const { return lost; }
  bool ExtensionEnabled(int) const { return extension; }
  GL* ContextGL() { assert(!lost && extension); return &gl; }
  WebGLExecutionContext* GetExecutionContext() { assert(false); return nullptr; }
  void SynthesizeGLError(GLenum, const char*, const char*) { ++errors; }
  ScriptValue getParameter(ScriptState*, GLenum);
};
'''

CPP_DAWN_PREFIX = r'''
namespace dawn_detail {
struct MaybeError { bool failed = false; };
bool IsPowerOfTwo(uint32_t value) { return std::has_single_bit(value); }
#define DAWN_INVALID_IF(condition, ...) if (condition) return MaybeError{true}
'''

CPP_TESTS = r'''
using Limits = blink::GPUSupportedLimits;
using Raw = blink::V8UnionUndefinedOrUnsignedLongLongEnforceRange;
using Feature = wgpu::FeatureName;
bool Populate(Limits::ComboLimits* out, const char* name, std::optional<uint64_t> value,
              ScriptPromiseResolverBase* resolver) {
  Raw raw{value}; return Limits::Populate(out, {{String(name), &raw}}, resolver);
}
int main(int argc, char** argv) {
  assert(argc == 2);
  std::string mode = argv[1];
  auto& config = base::UxrConfig::GetInstance();
  if (mode.starts_with("features-") || mode == "request-features" || mode == "request-empty-features") {
    std::vector<Feature> native{Feature::DepthClipControl, Feature::TextureCompressionBC, Feature::Internal};
    std::set<Feature> expected{Feature::DepthClipControl, Feature::TextureCompressionBC};
    int expected_calls = 1;
    if (mode == "features-empty" || mode == "features-whitespace" || mode == "request-empty-features") {
      config.values["uxr-webgpu-features"] = mode == "features-whitespace" ? " \t, ,\r\n,,\v\f" : "";
      expected.clear(); expected_calls = 0;
    } else if (mode == "features-intersection" || mode == "request-features") {
      config.values["uxr-webgpu-features"] = " TEXTURE-COMPRESSION-BC,texture-compression-bc,unknown";
      expected = {Feature::TextureCompressionBC};
    } else if (mode == "features-unknown") {
      config.values["uxr-webgpu-features"] = "internal,unknown,depth-clip";
      expected.clear();
    } else if (mode == "features-embedded-nul") {
      config.values["uxr-webgpu-features"] = std::string("depth-clip-control\0suffix", 25);
      expected.clear();
    } else if (mode == "features-token-suffix") {
      config.values["uxr-webgpu-features"] = "depth-clip-control-suffix";
      expected.clear();
    } else if (mode == "features-no-adapter-features") {
      native.clear(); expected.clear();
      config.values["uxr-webgpu-features"] = "texture-compression-bc";
    }
    int calls = 0;
    std::unique_ptr<GPUSupportedFeatures> features(MakeFeatureNameSet({native, &calls}));
    assert(features->names == expected && calls == expected_calls);
    {
      for (Feature f : {Feature::DepthClipControl, Feature::TextureCompressionBC, Feature::Internal}) {
        Descriptor descriptor{{V8GPUFeatureName(f)}};
        ScriptPromiseResolverBase resolver;
        bool accepted = ValidateFeatures(features.get(), &descriptor, &resolver);
        assert(accepted == expected.contains(f));
        assert(resolver.type_error == !accepted);
      }
      Descriptor descriptor; ScriptPromiseResolverBase resolver;
      assert(ValidateFeatures(features.get(), &descriptor, &resolver));
    }
  } else if (mode == "limits-native") {
    for (const char* configured : {"", "0", "1", "128", "384", "8193", "4294967295", "4294967296",
                                   "18446744073709551615", "18446744073709551616", "-1", "abc"}) {
      for (const char* key : {"maxTextureDimension2D", "maxBufferSize", "minUniformBufferOffsetAlignment",
                              "minStorageBufferOffsetAlignment", "maxtexturedimension2d"})
        config.values[std::string("uxr-webgpu-limit-") + key] = configured;
      Limits::ComboLimits adapter;
      adapter.maxTextureDimension2D = 16384; adapter.maxBufferSize = uint64_t{1} << 33;
      adapter.minUniformBufferOffsetAlignment = 256; adapter.minStorageBufferOffsetAlignment = 256;
      Limits::ComboLimits device;
      device.maxTextureDimension2D = 8192; device.maxBufferSize = uint64_t{1} << 28;
      device.minUniformBufferOffsetAlignment = 256; device.minStorageBufferOffsetAlignment = 256;
      Limits a(adapter), d(device);
      assert(a.maxTextureDimension2D() == 16384 && d.maxTextureDimension2D() == 8192);
      assert(a.maxBufferSize() == (uint64_t{1} << 33) && d.maxBufferSize() == (uint64_t{1} << 28));
      assert(a.minUniformBufferOffsetAlignment() == 256 && d.minUniformBufferOffsetAlignment() == 256);
      assert(a.minStorageBufferOffsetAlignment() == 256 && d.minStorageBufferOffsetAlignment() == 256);
      assert(a.maxStorageBuffersInVertexStage() == UINT32_MAX && d.maxStorageBuffersInVertexStage() == UINT32_MAX);
    }
  } else if (mode == "limits-u32" || mode == "limits-u64") {
    const bool wide = mode == "limits-u64";
    for (uint64_t value : {uint64_t{0}, uint64_t{1}, uint64_t{UINT32_MAX - 1}, uint64_t{UINT32_MAX},
                           uint64_t{UINT32_MAX} + 1, UINT64_MAX - 1, UINT64_MAX}) {
      Limits::ComboLimits out; ScriptPromiseResolverBase resolver;
      bool accepted = Populate(&out, wide ? "maxBufferSize" : "maxTextureDimension2D", value, &resolver);
      assert(accepted == (value < (wide ? UINT64_MAX : UINT32_MAX)));
      assert(resolver.rejected == !accepted);
      if (accepted) assert((wide ? out.maxBufferSize : out.maxTextureDimension2D) == value);
      else assert((wide ? out.maxBufferSize : out.maxTextureDimension2D) == (wide ? UINT64_MAX : UINT32_MAX));
    }
  } else if (mode == "limits-alignment") {
    for (const char* name : {"minUniformBufferOffsetAlignment", "minStorageBufferOffsetAlignment"}) {
      for (uint64_t value : {uint64_t{0}, uint64_t{1}, uint64_t{128}, uint64_t{256}, uint64_t{384},
                             uint64_t{512}, uint64_t{1} << 31, uint64_t{UINT32_MAX}, uint64_t{1} << 32}) {
        Limits::ComboLimits out; ScriptPromiseResolverBase resolver;
        bool accepted = Populate(&out, name, value, &resolver);
        assert(accepted == (value < UINT32_MAX && std::has_single_bit(value)));
        assert(resolver.rejected == !accepted);
      }
    }
  } else if (mode == "limits-undefined") {
    for (const char* name : {"maxBufferSize", "maxTextureDimension2D", "minUniformBufferOffsetAlignment",
                             "minStorageBufferOffsetAlignment", "unknownLimit"}) {
      Limits::ComboLimits out; ScriptPromiseResolverBase resolver;
      assert(Populate(&out, name, std::nullopt, &resolver));
      assert(!resolver.rejected);
      assert(resolver.context.warnings == (std::string(name) == "unknownLimit" ? 1 : 0));
      assert(out.maxBufferSize == UINT64_MAX && out.minUniformBufferOffsetAlignment == UINT32_MAX);
    }
    Limits::ComboLimits out; ScriptPromiseResolverBase resolver;
    assert(!Populate(&out, "unknownLimit", 1, &resolver)); assert(resolver.rejected);
  } else if (mode == "limits-direction") {
    using namespace dawn_detail;
    using Maximum = CheckLimit<LimitClass::Maximum>;
    using Alignment = CheckLimit<LimitClass::Alignment>;
    assert(!Maximum::Validate(8192u, 0u).failed);
    assert(!Maximum::Validate(8192u, 8192u).failed);
    assert(Maximum::Validate(8192u, 8193u).failed);
    assert(!Alignment::Validate(256u, 256u).failed);
    assert(!Alignment::Validate(256u, 512u).failed);
    assert(Alignment::Validate(256u, 128u).failed);
    assert(Alignment::Validate(256u, 0u).failed);
    assert(Alignment::Validate(256u, 384u).failed);
  } else if (mode.starts_with("webgl-")) {
    ScriptState script;
    WebGLRenderingContextBase context;
    context.lost = mode == "webgl-context-lost";
    context.extension = mode != "webgl-extension-disabled";
    for (bool real : {false, true}) {
      ungoogled::persona.webgl_real = mode == "webgl-real" || (mode != "webgl-persona" && real);
      for (GLenum name : {WebGLDebugRendererInfo::kUnmaskedRendererWebgl, WebGLDebugRendererInfo::kUnmaskedVendorWebgl}) {
        const bool renderer = name == WebGLDebugRendererInfo::kUnmaskedRendererWebgl;
        context.gl.calls = 0; context.errors = 0;
        auto result = context.getParameter(&script, name);
        if (context.lost || !context.extension) {
          assert(!result.value && context.gl.calls == 0);
          assert(context.errors == (context.lost ? 0 : 1));
        } else {
          assert(result.value == (ungoogled::persona.webgl_real ?
              (renderer ? "native renderer" : "native vendor") : (renderer ? "persona renderer" : "persona vendor")));
          assert(context.gl.calls == (ungoogled::persona.webgl_real ? 1 : 0));
          assert(context.errors == 0);
        }
      }
    }
  } else { assert(false); }
}
'''


# Chromium 152 upstream gpu_adapter_info.cc, pinned independently of patch hunks.
# Unlike the sparse excerpts below, this is the complete unmodified source file.
PINNED_0030_UPSTREAM = r'''// Copyright 2022 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "third_party/blink/renderer/modules/webgpu/gpu_adapter_info.h"

#include "third_party/blink/renderer/modules/webgpu/gpu_memory_heap_info.h"
#include "third_party/blink/renderer/modules/webgpu/gpu_subgroup_matrix_config.h"

namespace blink {

GPUAdapterInfo::GPUAdapterInfo(const String& vendor,
                               const String& architecture,
                               uint32_t subgroup_min_size,
                               uint32_t subgroup_max_size,
                               bool is_fallback_adapter,
                               const String& device,
                               const String& description,
                               const String& driver,
                               const String& backend,
                               const String& type,
                               const std::optional<uint32_t> d3d_shader_model,
                               const std::optional<uint32_t> vk_driver_version,
                               const String& power_preference)
    : vendor_(vendor),
      architecture_(architecture),
      subgroup_min_size_(subgroup_min_size),
      subgroup_max_size_(subgroup_max_size),
      is_fallback_adapter_(is_fallback_adapter),
      device_(device),
      description_(description),
      driver_(driver),
      backend_(backend),
      type_(type),
      d3d_shader_model_(d3d_shader_model),
      vk_driver_version_(vk_driver_version),
      power_preference_(power_preference) {}

void GPUAdapterInfo::AppendMemoryHeapInfo(GPUMemoryHeapInfo* info) {
  memory_heaps_.push_back(info);
}

void GPUAdapterInfo::AppendSubgroupMatrixConfig(
    GPUSubgroupMatrixConfig* config) {
  subgroup_matrix_configs_.push_back(config);
}

const String& GPUAdapterInfo::vendor() const {
  return vendor_;
}

const String& GPUAdapterInfo::architecture() const {
  return architecture_;
}

const String& GPUAdapterInfo::device() const {
  return device_;
}

const String& GPUAdapterInfo::description() const {
  return description_;
}

uint32_t GPUAdapterInfo::subgroupMinSize() const {
  return subgroup_min_size_;
}

uint32_t GPUAdapterInfo::subgroupMaxSize() const {
  return subgroup_max_size_;
}

bool GPUAdapterInfo::isFallbackAdapter() const {
  return is_fallback_adapter_;
}

const String& GPUAdapterInfo::driver() const {
  return driver_;
}

const String& GPUAdapterInfo::backend() const {
  return backend_;
}

const String& GPUAdapterInfo::type() const {
  return type_;
}

const HeapVector<Member<GPUMemoryHeapInfo>>& GPUAdapterInfo::memoryHeaps()
    const {
  return memory_heaps_;
}

const HeapVector<Member<GPUSubgroupMatrixConfig>>&
GPUAdapterInfo::subgroupMatrixConfigs() const {
  return subgroup_matrix_configs_;
}

const std::optional<uint32_t>& GPUAdapterInfo::d3dShaderModel() const {
  return d3d_shader_model_;
}

const std::optional<uint32_t>& GPUAdapterInfo::vkDriverVersion() const {
  return vk_driver_version_;
}

const String& GPUAdapterInfo::powerPreference() const {
  return power_preference_;
}

void GPUAdapterInfo::Trace(Visitor* visitor) const {
  visitor->Trace(memory_heaps_);
  visitor->Trace(subgroup_matrix_configs_);
  ScriptWrappable::Trace(visitor);
}

}  // namespace blink
'''


def test_pinned_0030_matches_local_upstream():
    paths = list((ROOT / ".chromix-build-verify").glob(
        "sparse-real*/context-repair/*/upstream/" + target_path("0030")))
    if not paths:
        pytest.skip("local recovered Chromium upstream is unavailable")
    for path in paths:
        assert path.read_text() == PINNED_0030_UPSTREAM


SOURCE_SECTIONS = {
    '0030': [(1, PINNED_0030_UPSTREAM)],
    '0041': [
        (4, r'''
#include "third_party/blink/renderer/modules/webgpu/gpu_adapter.h"

#include "services/metrics/public/cpp/ukm_builders.h"
#include "third_party/blink/renderer/bindings/core/v8/script_promise_resolver.h"
#include "third_party/blink/renderer/bindings/core/v8/v8_object_builder.h"
#include "third_party/blink/renderer/bindings/modules/v8/v8_gpu_device_descriptor.h"
#include "third_party/blink/renderer/bindings/modules/v8/v8_gpu_queue_descriptor.h"
#include "third_party/blink/renderer/bindings/modules/v8/v8_gpu_request_adapter_options.h"
#include "third_party/blink/renderer/core/dom/dom_exception.h"
#include "third_party/blink/renderer/core/inspector/console_message.h"
#include "third_party/blink/renderer/modules/webgpu/dawn_enum_conversions.h"
#include "third_party/blink/renderer/modules/webgpu/gpu.h"
#include "third_party/blink/renderer/modules/webgpu/gpu_adapter_info.h"
#include "third_party/blink/renderer/modules/webgpu/gpu_device.h"
#include "third_party/blink/renderer/modules/webgpu/gpu_device_lost_info.h"
#include "third_party/blink/renderer/modules/webgpu/gpu_memory_heap_info.h"
#include "third_party/blink/renderer/modules/webgpu/gpu_subgroup_matrix_config.h"
#include "third_party/blink/renderer/modules/webgpu/gpu_supported_features.h"
#include "third_party/blink/renderer/modules/webgpu/gpu_supported_limits.h"
#include "third_party/blink/renderer/modules/webgpu/string_utils.h"
#include "third_party/blink/renderer/platform/graphics/gpu/webgpu_callback.h"
#include "third_party/blink/renderer/platform/heap/garbage_collected.h"
#include "third_party/blink/renderer/platform/runtime_enabled_features.h"

namespace blink {

namespace {

GPUSupportedFeatures* MakeFeatureNameSet(wgpu::Adapter adapter) {
  GPUSupportedFeatures* features = MakeGarbageCollected<GPUSupportedFeatures>();
  DCHECK(features->FeatureNameSet().empty());

  wgpu::SupportedFeatures supported_features;
  adapter.GetFeatures(&supported_features);
  // SAFETY: Required from caller
  const auto features_span = UNSAFE_BUFFERS(base::span<const wgpu::FeatureName>(
      supported_features.features, supported_features.featureCount));
  for (const auto& f : features_span) {
    auto feature_name_enum_optional =
        GPUSupportedFeatures::ToV8FeatureNameEnum(f);
    if (feature_name_enum_optional) {
      features->AddFeatureName(
          V8GPUFeatureName(feature_name_enum_optional.value()));
      }
  }
  return features;
}

}  // anonymous namespace
'''),
        (96, r'''
  vendor_ = String::FromUtf8(info.vendor);
  architecture_ = String::FromUtf8(info.architecture);
  if (info.deviceID <= 0xffff) {
    device_ = String::Format("0x%04x", info.deviceID);
  } else {
    device_ = String::Format("0x%08x", info.deviceID);
  }
'''),
        (254, r'''
  GPUSupportedLimits::ComboLimits required_limits;
  if (descriptor->hasRequiredLimits()) {
    dawn_desc.requiredLimits = required_limits.GetLinked();
    if (!GPUSupportedLimits::Populate(&required_limits,
                                      descriptor->requiredLimits(), resolver)) {
      return promise;
    }
  }

  // Use a set to prevent duplicate features.
  HashSet<wgpu::FeatureName> required_features_set;
  // The ShaderModuleCompilationOptions feature is required only if the adapter
  // has the ShaderModuleCompilationOptions feature and the user has enabled the
  // WebGPUDeveloperFeatures flag. It is needed to control
  // strict math during shader module compilation.
  if (RuntimeEnabledFeatures::WebGPUDeveloperFeaturesEnabled() &&
      GetHandle().HasFeature(
          wgpu::FeatureName::ShaderModuleCompilationOptions)) {
    required_features_set.insert(
        wgpu::FeatureName::ShaderModuleCompilationOptions);
  }
  if (descriptor->hasRequiredFeatures()) {
    for (const V8GPUFeatureName& f : descriptor->requiredFeatures()) {
      // If the feature is not a valid feature reject with a type error.
      if (!features_->Has(f.AsEnum())) {
        resolver->RejectWithTypeError(
            UNSAFE_TODO(String::Format("Unsupported feature: %s", f.AsCStr())));
        return promise;
      }
      required_features_set.insert(AsDawnEnum(f));
    }
  }

  Vector<wgpu::FeatureName> required_features(required_features_set);
  dawn_desc.requiredFeatures = required_features.data();
  dawn_desc.requiredFeatureCount = required_features.size();
'''),
    ],
    '0042': [
        (1, r'''// Copyright 2021 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "third_party/blink/renderer/modules/webgpu/gpu_supported_limits.h"

#include <algorithm>

#include "base/numerics/checked_math.h"
#include "third_party/blink/renderer/bindings/core/v8/v8_union_undefined_unsignedlonglongenforcerange.h"
#include "third_party/blink/renderer/bindings/modules/v8/v8_gpu_extent_3d_dict.h"
#include "third_party/blink/renderer/core/dom/dom_exception.h"
#include "third_party/blink/renderer/core/execution_context/execution_context.h"
#include "third_party/blink/renderer/core/inspector/console_message.h"

#define SUPPORTED_LIMITS(X)                    \
  X(maxTextureDimension1D)                     \
  X(maxTextureDimension2D)                     \
  X(maxTextureDimension3D)                     \
  X(maxTextureArrayLayers)                     \
  X(maxBindGroups)                             \
  X(maxBindGroupsPlusVertexBuffers)            \
  X(maxBindingsPerBindGroup)                   \
  X(maxDynamicUniformBuffersPerPipelineLayout) \
  X(maxDynamicStorageBuffersPerPipelineLayout) \
  X(maxSampledTexturesPerShaderStage)          \
  X(maxSamplersPerShaderStage)                 \
  X(maxStorageBuffersPerShaderStage)           \
  X(maxStorageTexturesPerShaderStage)          \
  X(maxUniformBuffersPerShaderStage)           \
  X(maxUniformBufferBindingSize)               \
  X(maxStorageBufferBindingSize)               \
  X(minUniformBufferOffsetAlignment)           \
  X(minStorageBufferOffsetAlignment)           \
  X(maxVertexBuffers)                          \
  X(maxBufferSize)                             \
  X(maxVertexAttributes)                       \
  X(maxVertexBufferArrayStride)                \
  X(maxInterStageShaderVariables)              \
  X(maxColorAttachments)                       \
  X(maxColorAttachmentBytesPerSample)          \
  X(maxComputeWorkgroupStorageSize)            \
  X(maxComputeInvocationsPerWorkgroup)         \
  X(maxComputeWorkgroupSizeX)                  \
  X(maxComputeWorkgroupSizeY)                  \
  X(maxComputeWorkgroupSizeZ)                  \
  X(maxComputeWorkgroupsPerDimension)          \
  X(maxStorageBuffersInFragmentStage)          \
  X(maxStorageTexturesInFragmentStage)         \
  X(maxStorageBuffersInVertexStage)            \
  X(maxStorageTexturesInVertexStage)           \
  X(maxImmediateSize)

namespace blink {

namespace {
template <typename T>
constexpr T UndefinedLimitValue();

template <>
constexpr uint32_t UndefinedLimitValue<uint32_t>() {
  return wgpu::kLimitU32Undefined;
}

template <>
constexpr uint64_t UndefinedLimitValue<uint64_t>() {
  return wgpu::kLimitU64Undefined;
}
}  // namespace

// GPUSupportedLimits

GPUSupportedLimits::GPUSupportedLimits(const ComboLimits& limits) {
  limits.UnlinkedCopyTo(&limits_);
}

// static
bool GPUSupportedLimits::Populate(
    ComboLimits* out,
    const HeapVector<
        std::pair<String,
                  Member<V8UnionUndefinedOrUnsignedLongLongEnforceRange>>>& in,
    ScriptPromiseResolverBase* resolver) {
  auto* context = resolver->GetExecutionContext();
  // TODO(crbug.com/dawn/685): This loop is O(n^2) if the developer
  // passes all of the limits. It could be O(n) with a mapping of
  // String -> wgpu::Limits::*member.
  for (const auto& [limitName, limitRawValue] : in) {
#define X(name)                                                               \
  if (limitName == #name) {                                                   \
    using T = decltype(GPUSupportedLimits::ComboLimits::name);                \
    if (limitRawValue->IsUndefined()) {                                       \
      continue;                                                               \
    }                                                                         \
    uint64_t limitRawIntegerValue =                                           \
        limitRawValue->GetAsUnsignedLongLongEnforceRange();                   \
    base::CheckedNumeric<T> value{limitRawIntegerValue};                      \
    if (!value.IsValid() || value.ValueOrDie() == UndefinedLimitValue<T>()) { \
      resolver->RejectWithDOMException(                                       \
          DOMExceptionCode::kOperationError,                                  \
          StrCat(                                                             \
              {"Required " #name " limit (",                                  \
               String::Number(limitRawIntegerValue),                          \
               ") exceeds the maximum representable value for its type."}));  \
      return false;                                                           \
    }                                                                         \
    out->name = value.ValueOrDie();                                           \
    continue;                                                                 \
  }
    SUPPORTED_LIMITS(X)
#undef X
    if (limitRawValue->IsUndefined()) {
      auto* console_message = MakeGarbageCollected<ConsoleMessage>(
          mojom::blink::ConsoleMessageSource::kRendering,
          mojom::blink::ConsoleMessageLevel::kWarning,
          StrCat({"The limit \"", limitName, "\" is not recognized."}));
      context->AddConsoleMessage(console_message);
    } else {
      resolver->RejectWithDOMException(
          DOMExceptionCode::kOperationError,
          StrCat({"The limit \"", limitName,
                  "\" with a non-undefined value is not recognized."}));
      return false;
    }
  }
  return true;
}

#define X(name)                                                              \
  decltype(GPUSupportedLimits::ComboLimits::name) GPUSupportedLimits::name() \
      const {                                                                \
    return limits_.name;                                                     \
  }
SUPPORTED_LIMITS(X)
#undef X

// GPUSupportedLimits::ComboLimits

GPUSupportedLimits::ComboLimits::ComboLimits() = default;

void GPUSupportedLimits::ComboLimits::UnlinkedCopyTo(
    GPUSupportedLimits::ComboLimits* o) const {
  *static_cast<wgpu::Limits*>(o) = *this;
  o->wgpu::Limits::nextInChain = nullptr;
  *static_cast<wgpu::CompatibilityModeLimits*>(o) = *this;
  o->wgpu::CompatibilityModeLimits::nextInChain = nullptr;
}

wgpu::Limits* GPUSupportedLimits::ComboLimits::GetLinked() {
  this->wgpu::Limits::nextInChain =
      static_cast<wgpu::CompatibilityModeLimits*>(this);
  this->wgpu::CompatibilityModeLimits::nextInChain = nullptr;
  return this;
}

}  // namespace blink
'''),
    ],
    '0110': [
        (4295, r'''ScriptValue WebGLRenderingContextBase::getParameter(ScriptState* script_state,
                                                    GLenum pname) {
  if (isContextLost())
    return ScriptValue::CreateNull(script_state->GetIsolate());
  const int kIntZero = 0;
'''),
        (4566, r'''      return ScriptValue::CreateNull(script_state->GetIsolate());
    case WebGLDebugRendererInfo::kUnmaskedRendererWebgl:
      if (ExtensionEnabled(kWebGLDebugRendererInfoName)) {
        if (ungoogled::CurrentPersona().webgl_real)
          return WebGLAny(script_state,
                          String(ContextGL()->GetString(GL_RENDERER)));
        const std::string configured_renderer =
            ungoogled::CurrentPersona().webgl_renderer;
        if (!ungoogled::CurrentPersona().webgl_fingerprint &&
            ungoogled::CurrentPersona().webgl_identity_explicit &&
            !configured_renderer.empty())
          return WebGLAny(script_state, String(configured_renderer));
        const SecurityOrigin* origin =
            GetExecutionContext() ? GetExecutionContext()->GetSecurityOrigin()
                                  : nullptr;
        if (ungoogled::CurrentPersona().webgl_fingerprint) {
          const std::string fingerprint_renderer =
              GetGLRendererStringForFingerprint(
                  ungoogled::GetFarbleSeed64(
                      origin ? origin->RegistrableDomain().Utf8()
                             : std::string()));
          if (!fingerprint_renderer.empty())
            return WebGLAny(script_state, String(fingerprint_renderer));
        }
        return WebGLAny(script_state, String(configured_renderer));
      }
      SynthesizeGLError(
          GL_INVALID_ENUM, "getParameter",
          "invalid parameter name, WEBGL_debug_renderer_info not enabled");
      return ScriptValue::CreateNull(script_state->GetIsolate());
    case WebGLDebugRendererInfo::kUnmaskedVendorWebgl:
      if (ExtensionEnabled(kWebGLDebugRendererInfoName)) {
        if (ungoogled::CurrentPersona().webgl_real)
          return WebGLAny(script_state,
                          String(ContextGL()->GetString(GL_VENDOR)));
        const std::string configured_vendor =
            ungoogled::CurrentPersona().webgl_vendor;
        if (!ungoogled::CurrentPersona().webgl_fingerprint &&
            ungoogled::CurrentPersona().webgl_identity_explicit &&
            !configured_vendor.empty())
          return WebGLAny(script_state, String(configured_vendor));
        if (ungoogled::CurrentPersona().webgl_fingerprint) {
          const std::string fingerprint_vendor =
              GetGLVendorStringForFingerprint();
          if (!fingerprint_vendor.empty())
            return WebGLAny(script_state, String(fingerprint_vendor));
        }
        return WebGLAny(script_state, String(configured_vendor));
      }
      SynthesizeGLError(
          GL_INVALID_ENUM, "getParameter",
          "invalid parameter name, WEBGL_debug_renderer_info not enabled");
      return ScriptValue::CreateNull(script_state->GetIsolate());
    case GL_VERTEX_ARRAY_BINDING_OES:  // OES_vertex_array_object
'''),
    ],
}


DAWN_LIMIT_CHECKS = r'''enum class LimitClass {
    Alignment,
    Maximum,
};

template <LimitClass C>
struct CheckLimit;

template <>
struct CheckLimit<LimitClass::Alignment> {
    template <typename T>
    static bool IsBetter(T lhs, T rhs) {
        return lhs < rhs;
    }

    template <typename T>
    static MaybeError Validate(T supported, T required) {
        DAWN_INVALID_IF(IsBetter(required, supported),
                        "Required limit (%u) is lower than the supported limit (%u).", required,
                        supported);
        DAWN_INVALID_IF(!IsPowerOfTwo(required), "Required limit (%u) is not a power of two.",
                        required);
        return {};
    }
};

template <>
struct CheckLimit<LimitClass::Maximum> {
    template <typename T>
    static bool IsBetter(T lhs, T rhs) {
        return lhs > rhs;
    }

    template <typename T>
    static MaybeError Validate(T supported, T required) {
        DAWN_INVALID_IF(IsBetter(required, supported),
                        "Required limit (%u) is greater than the supported limit (%u).", required,
                        supported);
        return {};
    }
};

'''


def test_dawn_limit_fixture_matches_local_source():
    path = ROOT / ".chromix-build-verify/src/third_party/dawn/src/dawn/native/Limits.h"
    if not path.exists():
        pytest.skip("the local Dawn source is unavailable")
    assert DAWN_LIMIT_CHECKS in path.read_text()
